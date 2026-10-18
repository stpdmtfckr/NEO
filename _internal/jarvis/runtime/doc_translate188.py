"""NEO 188 NEO-DOC-003 / TZ-DOCUMENT-TRANSLATION-ALIGNED-REVIEW-01.

Translate ONE chosen plain ``.docx`` into a separate, editable copy and review
each translated fragment next to its source.

* the container is inspected safely before anything else: size/entry/ratio
  limits, no macros (``vbaProject``/macro content types refuse), no DOCTYPE,
  external links are listed and never fetched;
* fragments are paragraphs and simple-table cell paragraphs in body order, each
  with a stable ID (``p7``, ``t2.r1.c3.p0``) bound to the source version (a
  hash of the original bytes); long paragraphs are translated in parts that
  stay linked to their paragraph;
* anything not supported (text boxes, fields, drawings, equations, tracked
  changes, footnote/comment references, merged or nested tables, headers,
  footers, footnotes, comments, embedded objects, SmartArt) is detected before
  processing, listed in the preview and left as it is — nothing disappears;
* translation runs only through the installed LOCAL engine; the address is
  checked to be local right before every fragment; there is no fallback to an
  external service and nothing is downloaded;
* the new file is written only on request, next to the original under a new
  unique name, through a temporary file and an exclusive link (never
  overwriting); the original bytes are verified unchanged; every other part
  of the package is copied byte for byte;
* fragments, source text and translations live in memory of this session only.
"""
from __future__ import annotations

import hashlib
import html
import io
import ntpath
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
_Q = lambda tag: f'{{{W}}}{tag}'

MAX_CHARS = 100_000
MAX_FRAGMENTS = 1000
MAX_ENTRIES = 2000
MAX_UNPACKED = 64 * 1024 * 1024
MAX_RATIO = 200
PART_CHARS = 1500
BATCH_FRAGMENTS = 60
BATCH_SECONDS = 90.0

_UNSUPPORTED_IN_PARAGRAPH = {
    'txbxContent': 'textbox', 'fldSimple': 'field', 'instrText': 'field', 'fldChar': 'field', 'drawing': 'drawing',
    'pict': 'drawing', 'object': 'object', 'ins': 'tracked', 'del': 'tracked', 'footnoteReference': 'footnote',
    'endnoteReference': 'footnote', 'commentReference': 'comment', 'sdt': 'control', 'smartTag': 'smarttag',
}


@dataclass
class Fragment:
    ident: str
    kind: str                     # paragraph | cell | heading
    text: str
    position: str
    t_indices: list[int]          # global indices of this paragraph's <w:t> nodes in document order
    translation: str = ''
    status: str = 'new'           # new | done | skipped | error | edited | unsupported
    flag: str = ''                # numbers/urls to check


@dataclass
class Inventory:
    path: str
    version: str
    fragments: list[Fragment]
    unsupported: list[tuple[str, str]]     # (kind, where)
    external_links: int
    total_t: int
    chars: int


@dataclass
class Job:
    inventory: Inventory
    source: str
    target: str
    state: str = 'translating'    # translating | paused | review | saved | cancelled
    saved_path: str = ''
    started_at: float = field(default_factory=time.monotonic)

    @property
    def label(self) -> str:
        return ntpath.basename(self.inventory.path.replace('/', '\\'))

    def pending(self) -> list[Fragment]:
        return [f for f in self.inventory.fragments if f.status == 'new']


class DocError(Exception):
    def __init__(self, key: str, **values: Any):
        super().__init__(key)
        self.key, self.values = key, values


# --------------------------------------------------------------- inspection
def _read_zip(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocError('damaged') from exc
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise DocError('too_large')
    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            raise DocError('damaged')                     # encrypted entry
        total += info.file_size
        if total > MAX_UNPACKED or (info.compress_size and info.file_size / max(1, info.compress_size) > MAX_RATIO):
            raise DocError('too_large')
    names = {i.filename for i in infos}
    if 'word/document.xml' not in names or '[Content_Types].xml' not in names:
        raise DocError('damaged')
    types = archive.read('[Content_Types].xml').decode('utf-8', 'replace')
    if 'macroEnabled' in types or any(n.casefold().endswith('vbaproject.bin') for n in names):
        raise DocError('macros')
    return archive


def _text_of(element: ET.Element) -> str:
    return ''.join(t.text or '' for t in element.iter(_Q('t')))


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def inspect(path: str, data: bytes) -> Inventory:
    if not str(path).casefold().endswith('.docx'):
        raise DocError('not_docx')
    archive = _read_zip(data)
    raw = archive.read('word/document.xml')
    if b'<!DOCTYPE' in raw[:2048].upper():
        raise DocError('damaged')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise DocError('damaged') from exc
    body = root.find(_Q('body'))
    if body is None:
        raise DocError('damaged')
    order = {id(node): n for n, node in enumerate(root.iter(_Q('t')))}
    fragments: list[Fragment] = []
    unsupported: list[tuple[str, str]] = []
    chars = 0

    def paragraph(p: ET.Element, ident: str, kind: str, where: str) -> None:
        nonlocal chars
        text = _text_of(p)
        if not text.strip():
            return
        found = {kind_ for node in p.iter() for name, kind_ in _UNSUPPORTED_IN_PARAGRAPH.items() if _local(node.tag) == name}
        if any(node.tag == f'{{{M}}}oMath' or node.tag == f'{{{M}}}oMathPara' for node in p.iter()):
            found.add('equation')
        style = p.find(f'{_Q("pPr")}/{_Q("pStyle")}')
        if style is not None and str(style.get(_Q('val'), '')).casefold().startswith(('heading', 'title', 'заголовок')):
            kind = 'heading'
        indices = [order[id(t)] for t in p.iter(_Q('t'))]
        fragment = Fragment(ident, kind, text, where, indices)
        if found:
            fragment.status = 'unsupported'
            unsupported.extend((k, where) for k in sorted(found))
        fragments.append(fragment)
        chars += len(text)

    p_number = t_number = 0
    for child in list(body):
        name = _local(child.tag)
        if name == 'p':
            p_number += 1
            paragraph(child, f'p{p_number}', 'paragraph', f'¶{p_number}')
        elif name == 'tbl':
            t_number += 1
            complex_table = any(_local(n.tag) in {'gridSpan', 'vMerge', 'hMerge'} for n in child.iter()) or \
                any(_local(n.tag) == 'tbl' for n in list(child.iter())[1:])
            if complex_table:
                unsupported.append(('table', f'T{t_number}'))
                continue
            for r, row in enumerate(child.findall(_Q('tr')), 1):
                for c, cell in enumerate(row.findall(_Q('tc')), 1):
                    for k, p in enumerate(cell.findall(_Q('p'))):
                        paragraph(p, f't{t_number}.r{r}.c{c}.p{k}', 'cell', f'T{t_number} {r}:{c}')
        elif name == 'sdt':
            unsupported.append(('control', 'body'))
    names = archive.namelist()
    for name in names:
        low = name.casefold()
        if re.match(r'word/(header|footer)\d*\.xml$', low) and _has_text(archive.read(name)):
            unsupported.append(('header' if 'header' in low else 'footer', ntpath.basename(name)))
        elif low in {'word/footnotes.xml', 'word/endnotes.xml'} and _has_text(archive.read(name), skip_separators=True):
            unsupported.append(('footnotes', ntpath.basename(name)))
        elif low == 'word/comments.xml' and _has_text(archive.read(name)):
            unsupported.append(('comments', 'comments.xml'))
        elif low.startswith('word/embeddings/'):
            unsupported.append(('embedded', ntpath.basename(name)))
        elif low.startswith('word/diagrams/') and low.endswith('.xml') and 'data' in low:
            unsupported.append(('smartart', ntpath.basename(name)))
    external = 0
    if 'word/_rels/document.xml.rels' in names:
        external = archive.read('word/_rels/document.xml.rels').decode('utf-8', 'replace').count('TargetMode="External"')
    if len(fragments) > MAX_FRAGMENTS or chars > MAX_CHARS:
        raise DocError('limit', fragments=len(fragments), chars=chars)
    return Inventory(str(path), hashlib.sha256(data).hexdigest(), fragments, unsupported, external, len(order), chars)


def _has_text(raw: bytes, *, skip_separators: bool = False) -> bool:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return True
    if skip_separators:
        notes = [n for n in root if _local(n.tag) in {'footnote', 'endnote'} and n.get(_Q('type')) not in {'separator', 'continuationSeparator'}]
        return any(_text_of(n).strip() for n in notes)
    return bool(_text_of(root).strip())


# --------------------------------------------------------------- translation
_NUM = re.compile(r'\d[\d.,:/-]*\d|\d')
_URL = re.compile(r'https?://\S+|www\.\S+', re.I)


def parts(text: str, size: int = PART_CHARS) -> list[str]:
    """A long paragraph is translated in sentence-bounded parts that stay linked to it."""
    out: list[str] = []
    rest = text
    while len(rest) > size:
        cut = max(rest.rfind('. ', 0, size), rest.rfind('! ', 0, size), rest.rfind('? ', 0, size), rest.rfind('; ', 0, size))
        cut = cut + 2 if cut > size // 3 else size
        out.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        out.append(rest)
    return out


def check_flags(source: str, translation: str) -> str:
    """Numbers and links must survive literally; otherwise the fragment is marked for review."""
    missing = [n for n in _NUM.findall(source) if n not in translation]
    links = [u for u in _URL.findall(source) if u.rstrip('.,;)') not in translation]
    return 'numbers' if missing else 'links' if links else ''


def translate_fragment(fragment: Fragment, translate: Callable[[str], str]) -> None:
    try:
        pieces = [str(translate(piece) or '').strip() for piece in parts(fragment.text)]
    except Exception:
        fragment.status = 'error'
        return
    if not all(pieces):
        fragment.status = 'error'                      # an empty model answer is never «done»
        return
    fragment.translation = ' '.join(pieces)
    fragment.status = 'done'
    fragment.flag = check_flags(fragment.text, fragment.translation)


def run_batch(job: Job, translate: Callable[[str], str], *, limit: int | None = None, seconds: float | None = None,
              cancelled: Callable[[], bool] = lambda: False) -> int:
    limit = BATCH_FRAGMENTS if limit is None else limit
    seconds = BATCH_SECONDS if seconds is None else seconds
    started, done = time.monotonic(), 0
    for fragment in job.pending():
        if cancelled() or job.state != 'translating' or done >= limit or time.monotonic() - started > seconds:
            break
        translate_fragment(fragment, translate)
        done += 1
    if not job.pending() and job.state == 'translating':
        job.state = 'review'
    return done


def local_translator(config: Any, source: str, target: str) -> Callable[[str], str]:
    """The installed local engine only; the address is re-checked before every fragment."""
    from jarvis.ollama_runtime import is_local_ollama_url
    from jarvis.runtime import translation184

    def call(text: str) -> str:
        if not is_local_ollama_url(translation184._base_url(config)):
            raise DocError('not_local')
        return translation184._generate(config, translation184.model_name(config), text, source, target)
    return call


# --------------------------------------------------------------- writing
_T_RE = re.compile(rb'<w:t(?P<attrs>(?:\s[^>]*?)?)(?:/>|>(?P<body>.*?)</w:t>)', re.S)


def build_copy(original: bytes, inventory: Inventory) -> bytes:
    """A new package: only the text of translated paragraphs changes; every other byte is copied."""
    source = zipfile.ZipFile(io.BytesIO(original))
    raw = source.read('word/document.xml')
    spans = list(_T_RE.finditer(raw))
    if len(spans) != inventory.total_t:
        raise DocError('write_unsafe')                 # prefixes/structure we cannot edit in place safely
    replace: dict[int, bytes] = {}
    for fragment in inventory.fragments:
        if fragment.status not in {'done', 'edited'} or not fragment.t_indices:
            continue
        first, *rest = fragment.t_indices
        replace[first] = html.escape(fragment.translation, quote=False).encode('utf-8')
        for index in rest:
            replace[index] = b''
    out = bytearray()
    last = 0
    for index, match in enumerate(spans):
        if index not in replace:
            continue
        out += raw[last:match.start()]
        attrs = match.group('attrs') or b''
        if b'xml:space' not in attrs:
            attrs += b' xml:space="preserve"'
        out += b'<w:t' + attrs + b'>' + replace[index] + b'</w:t>'
        last = match.end()
    out += raw[last:]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as target:
        for info in source.infolist():
            data = bytes(out) if info.filename == 'word/document.xml' else source.read(info.filename)
            target.writestr(info, data, compress_type=info.compress_type)
    return buffer.getvalue()


def copy_name(path: Path, target: str) -> Path:
    stem, suffix = path.stem, path.suffix
    candidate = path.with_name(f'{stem} ({target.upper()}){suffix}')
    number = 2
    while candidate.exists():
        candidate = path.with_name(f'{stem} ({target.upper()} {number}){suffix}')
        number += 1
    return candidate


def save(job: Job) -> Path:
    path = Path(job.inventory.path)
    try:
        original = path.read_bytes()
    except OSError as exc:
        raise DocError('missing') from exc
    if hashlib.sha256(original).hexdigest() != job.inventory.version:
        raise DocError('changed')
    data = build_copy(original, job.inventory)
    check = zipfile.ZipFile(io.BytesIO(data))
    ET.fromstring(check.read('word/document.xml'))       # the new document still parses
    for _ in range(20):
        final = copy_name(path, job.target)
        temp = final.with_name('.' + final.name + '.neo188.tmp')
        try:
            with open(temp, 'xb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, final)                     # exclusive: never replaces an existing file
            except FileExistsError:
                continue
            except OSError:
                # A file system without hard links: exclusive create of the final name.
                with open(final, 'xb') as handle:
                    handle.write(data)
            break
        finally:
            try:
                temp.unlink()
            except OSError:
                pass
    else:
        raise DocError('write_failed')
    if hashlib.sha256(path.read_bytes()).hexdigest() != job.inventory.version:
        raise DocError('changed')
    job.saved_path, job.state = str(final), 'saved'
    return final
