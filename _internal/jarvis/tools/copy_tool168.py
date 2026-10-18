"""NEO 168 tool handler ``copy``: copy and paste texts, pictures, screenshots, files and folders by voice.

After copying NEO says what is on the clipboard («Скопировал файл отчёт.docx»);
«вставь» pastes into the active window, a named window, a named field, a line
number, the end of a document, a text file or a folder. When the NEO window
itself is active, NEO asks where to paste instead of guessing.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import time
from typing import Any
import zipfile

from jarvis.runtime import clipboard168 as clip
from jarvis.runtime import input168
from jarvis.tools.registry import ToolExecution
from jarvis.tools.ui_messages168 import msg

logger = logging.getLogger(__name__)
_TEXT_SUFFIXES = {'.txt', '.md', '.csv', '.json', '.log', '.ini', '.cfg', '.xml', '.html', '.htm', '.py', '.js', '.ts',
                  '.css', '.yaml', '.yml', '.bat', '.cmd', '.ps1', '.sql', '.srt', '.tsv', '.rtf', '.toml'}


def _code(config: Any) -> str:
    return str(config.get('app.language', 'ru') or 'ru')


def _foreground() -> tuple[int, int, str]:
    """(hwnd, pid, title) of the active window."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    hwnd = user32.GetForegroundWindow() or 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buffer, 256)
    return int(hwnd or 0), int(pid.value), buffer.value


def _window_behind_neo() -> str:
    """Title of the first normal window under NEO in z-order (for the paste question)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    hwnd = user32.GetForegroundWindow()
    for _ in range(200):
        hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT
        if not hwnd:
            break
        if not user32.IsWindowVisible(hwnd):
            continue
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) == os.getpid():
            continue
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buffer, 256)
        title = buffer.value.strip()
        if title and title not in {'Program Manager', 'Windows Input Experience'}:
            return title
    return ''


def _resolve(config: Any, name: str, kind: str) -> Path | None:
    value = str(name or '').strip().strip('«»"“”')
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_absolute() and path.exists():
        return path
    from jarvis.desktop_resolver import find_user_path
    found = find_user_path(value, kind=kind, max_seconds=3.0)
    return found if found is not None and found.exists() else None


def _explorer_selection() -> list[str]:
    """Files/folders selected in the active File Explorer window (via its own Ctrl+C)."""
    before = clip.sequence()
    input168.press(input168.KeySpec(('ctrl', 'c')))
    if not clip.wait_change(before):
        return []
    content = clip.read()
    return content.files if content.kind == 'files' else []


def _file_text(path: Path, limit: int = 2_000_000) -> str | None:
    """Text of a text-like file or a .docx document; None for other files."""
    suffix = path.suffix.casefold()
    if suffix == '.docx':
        with zipfile.ZipFile(path) as archive:
            xml = archive.read('word/document.xml').decode('utf-8', 'replace')
        xml = re.sub(r'</w:p>', '\n', xml)
        xml = re.sub(r'<w:tab/>', '\t', xml)
        text = re.sub(r'<[^>]+>', '', xml)
        import html
        return html.unescape(text).strip()[:limit]
    if suffix not in _TEXT_SUFFIXES or path.stat().st_size > limit:
        return None
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'utf-16', 'cp1251', 'latin-1'):
        try:
            text = raw.decode(encoding)
            if encoding == 'utf-16' and not raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
                continue
            return text
        except UnicodeDecodeError:
            continue
    return None


def _copied(code: str, content: clip.Content, **data: Any) -> ToolExecution:
    return ToolExecution(True, msg('copied', code, what=clip.describe(content, code)),
                         {'kind': content.kind, 'characters': len(content.text), 'files': content.files, **data})


def copy_reply(config: Any, back: int = 1) -> ToolExecution:
    from jarvis.runtime.conversation_journal import current_scope
    from jarvis.runtime.reply_memory168 import last
    code = _code(config)
    text = last(current_scope(), back=back)
    if not text:
        return ToolExecution(False, msg('no_reply', code), {})
    clip.set_text(text)
    return _copied(code, clip.Content('text', text=text), source='assistant_reply')


def copy_selection(config: Any) -> ToolExecution:
    code = _code(config)
    before = clip.sequence()
    input168.press(input168.KeySpec(('ctrl', 'c')))
    if not clip.wait_change(before):
        return ToolExecution(False, msg('nothing_selected', code), {'requires_clarification': True})
    return _copied(code, clip.read(), source='selection')


def copy_page(config: Any) -> ToolExecution:
    """All text of the page/document; Windows OCR of the window when the app has no text to copy."""
    code = _code(config)
    before = clip.sequence()
    input168.press(input168.KeySpec(('ctrl', 'a')))
    time.sleep(0.08)
    input168.press(input168.KeySpec(('ctrl', 'c')))
    if clip.wait_change(before):
        content = clip.read()
        if content.kind == 'text' and content.text.strip():
            return _copied(code, content, source='select_all')
    from jarvis.runtime import screen_ocr168, ui_elements168
    _hwnd, _process, window = ui_elements168.foreground()
    rect = ui_elements168._rect(window)
    lines = screen_ocr168.text_lines(screen_ocr168.grab(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]), limit=400) if rect else []
    if not lines:
        return ToolExecution(False, msg('nothing_selected', code), {})
    text = '\n'.join(lines)
    clip.set_text(text)
    return _copied(code, clip.Content('text', text=text), source='ocr')


def copy_file_text(config: Any, name: str = '') -> ToolExecution:
    code = _code(config)
    path = _resolve(config, name, 'file') if name else None
    if path is None and not name:
        selected = _explorer_selection()
        path = Path(selected[0]) if selected else None
    if path is None:
        key = 'no_file' if name else 'no_selection_file'
        return ToolExecution(False, msg(key, code, name=name), {'requires_clarification': not name})
    text = _file_text(path) if path.is_file() else None
    if text is None:
        clip.set_files([path])
        return ToolExecution(True, msg('not_text', code, name=path.name), {'kind': 'files', 'files': [str(path)]})
    clip.set_text(text)
    return _copied(code, clip.Content('text', text=text), path=str(path))


def copy_files(config: Any, name: str = '', kind: str = 'file') -> ToolExecution:
    """The file/folder itself, like Ctrl+C in File Explorer (paste into a folder, Telegram, Discord…)."""
    code = _code(config)
    if name:
        path = _resolve(config, name, kind)
        if path is None:
            return ToolExecution(False, msg('no_file', code, name=name), {})
        paths = [str(path)]
    else:
        paths = _explorer_selection()
        if not paths:
            return ToolExecution(False, msg('no_selection_file', code), {'requires_clarification': True})
    clip.set_files(paths)
    return _copied(code, clip.Content('files', files=paths), source='files')


def copy_url(config: Any, registry: Any) -> ToolExecution:
    result = registry.execute('clipboard', {'action': 'copy_page_url'})
    if not result.success:
        return ToolExecution(False, msg('no_url', _code(config)), {'detail': result.text})
    return _copied(_code(config), clip.read(), source='page_url')


def copy_screenshot(config: Any, registry: Any, scope: str) -> ToolExecution:
    code = _code(config)
    if scope == 'last':
        from jarvis.runtime.screenshot_storage import latest_screenshot
        newest = latest_screenshot(Path(config.paths.screenshots))       # HIST-066: grouped folders too
        shots = [newest] if newest is not None else []
        if not shots:
            return ToolExecution(False, msg('no_screenshot', code), {})
        size = clip.set_image(shots[0])
        return _copied(code, clip.Content('image', image_size=size), path=str(shots[0]))
    action = 'capture_window' if scope == 'window' else 'capture'
    result = registry.execute('screen', {'action': action, 'clipboard': True})
    if not result.success:
        return result
    return _copied(code, clip.read(), path=(result.data or {}).get('path', ''))


def copy_link(config: Any, index: Any) -> ToolExecution:
    """Address of the N-th page link (UI Automation value of the link, not a click)."""
    from jarvis.runtime import ui_elements168 as elements
    from jarvis.tools.ui_messages168 import msg as m
    code = _code(config)
    value = input168.parse_ordinal(str(index), code)
    if value is None or value == 0:
        return ToolExecution(False, m('bad_index', code, value=index), {'requires_clarification': True})
    _hwnd, _process, found = elements.collect('link', limit=200)
    element = elements.pick(found, value)
    if element is None:
        return ToolExecution(False, m('no_link', code, index=value, count=len(found)), {})
    url = ''
    for getter in (lambda: element.wrapper.legacy_properties().get('Value', ''),
                   lambda: element.wrapper.iface_value.CurrentValue):
        try:
            url = str(getter() or '').strip()
        except Exception:
            url = ''
        if url:
            break
    text = url or element.name
    clip.set_text(text)
    return _copied(code, clip.Content('text', text=text), source='link', name=element.name)


# ------------------------------------------------------------------ paste
def _paste_now(code: str, content: clip.Content, where: str) -> ToolExecution:
    input168.press(input168.KeySpec(('ctrl', 'v')))
    return ToolExecution(True, msg('pasted', code, what=clip.describe(content, code), where=where),
                         {'kind': content.kind, 'verified_input_sent': True})


def _content_or_error(code: str) -> tuple[clip.Content | None, ToolExecution | None]:
    content = clip.read()
    if content.kind == 'empty':
        return None, ToolExecution(False, msg('clip_empty', code), {})
    return content, None


def paste(config: Any) -> ToolExecution:
    code = _code(config)
    content, error = _content_or_error(code)
    if error:
        return error
    _hwnd, pid, title = _foreground()
    if pid == os.getpid():
        behind = _window_behind_neo() or 'Notepad'
        return ToolExecution(False, msg('where_paste', code, behind=behind[:40]),
                             {'requires_clarification': True, 'candidate_window': behind})
    return _paste_now(code, content, msg('where_window', code, name=title[:60]) if title else '')


def paste_window(config: Any, registry: Any, name: str) -> ToolExecution:
    code = _code(config)
    content, error = _content_or_error(code)
    if error:
        return error
    focus = registry.execute('window', {'action': 'focus', 'title': str(name or '').strip()})
    if not focus.success:
        return ToolExecution(False, msg('no_window', code, name=name), {'detail': focus.text})
    time.sleep(0.2)
    _hwnd, _pid, title = _foreground()
    return _paste_now(code, content, msg('where_window', code, name=(title or name)[:60]))


def paste_field(config: Any, registry: Any, name: str) -> ToolExecution:
    code = _code(config)
    content, error = _content_or_error(code)
    if error:
        return error
    ordinal = input168.parse_ordinal(str(name), code)
    if ordinal is not None:
        # «вставь во второе поле» — the N-th input field of the window.
        from jarvis.runtime import ui_elements168 as elements
        _h, _p, fields = elements.collect('field', limit=100)
        target = elements.pick(fields, ordinal)
        if target is None:
            return ToolExecution(False, msg('no_field', code, name=name), {'count': len(fields)})
        try:
            target.wrapper.set_focus()
        except Exception:
            target.wrapper.click_input()
        label = target.name or str(ordinal)
    else:
        focus = registry.execute('input_control', {'action': 'focus_control', 'name': str(name or '').strip(), 'control_type': 'Edit'})
        if not focus.success:
            return ToolExecution(False, msg('no_field', code, name=name), {'detail': focus.text})
        label = name
    time.sleep(0.08)
    return _paste_now(code, content, msg('where_field', code, name=label))


def paste_line(config: Any, line: Any) -> ToolExecution:
    code = _code(config)
    content, error = _content_or_error(code)
    if error:
        return error
    number = input168.parse_ordinal(str(line), code)
    if number is None or number < 1:
        return ToolExecution(False, msg('bad_index', code, value=line), {'requires_clarification': True})
    input168.press(input168.KeySpec(('ctrl', 'home')))
    if number > 1:
        input168.press(input168.KeySpec(('down',), min(number - 1, 1000)), gap=0.0)
    input168.press(input168.KeySpec(('home',)))
    result = _paste_now(code, content, msg('where_line', code, line=number))
    if content.kind == 'text' and not content.text.endswith('\n'):
        input168.press(input168.KeySpec(('enter',)))  # the pasted text becomes its own line
    return result


def paste_end(config: Any) -> ToolExecution:
    code = _code(config)
    content, error = _content_or_error(code)
    if error:
        return error
    input168.press(input168.KeySpec(('ctrl', 'end')))
    if content.kind == 'text':
        input168.press(input168.KeySpec(('enter',)))
    return _paste_now(code, content, msg('where_end', code))


def _text_encoding182(data: bytes) -> tuple[str | None, bytes]:
    """(codec without BOM, BOM bytes) of an existing text file; (None, b'') when unknown."""
    for bom, codec in ((b'\xff\xfe\x00\x00', 'utf-32-le'), (b'\x00\x00\xfe\xff', 'utf-32-be'),
                       (b'\xef\xbb\xbf', 'utf-8'), (b'\xff\xfe', 'utf-16-le'), (b'\xfe\xff', 'utf-16-be')):
        if data.startswith(bom):
            try:
                data[len(bom):].decode(codec)
            except UnicodeDecodeError:
                return None, b''
            return codec, bom
    try:
        data.decode('utf-8')
    except UnicodeDecodeError:
        return None, b''
    return 'utf-8', b''


def paste_file(config: Any, registry: Any, name: str) -> ToolExecution:
    """Append the clipboard text to a text file (no window needed)."""
    code = _code(config)
    content, error = _content_or_error(code)
    if error:
        return error
    if content.kind != 'text':
        return ToolExecution(False, msg('need_text', code), {})
    path = _resolve(config, name, 'file')
    if path is None or not path.is_file():
        return ToolExecution(False, msg('no_file', code, name=name), {})
    path = registry.security.assert_mutation_path_allowed(path)
    # NEO 183: the same strict detector as the file tool — the file keeps its encoding, BOM and
    # line breaks; binary or undecidable files are refused; the change joins the undo journal.
    from jarvis.runtime import text_encoding183 as te
    data = path.read_bytes()
    enc = te.detect(data)
    if not enc.ok:
        outcome = 'binary_file' if enc.reason == 'binary' else 'unknown_encoding'
        return ToolExecution(False, te.message(enc.reason or 'unknown', code), {'path': str(path), 'outcome': outcome})
    old = te.decode(data, enc)
    addition = te.match_newlines(content.text, enc.newline)
    separator = '' if not old or old.endswith(('\n', '\r')) else enc.newline
    new = old + separator + addition + ('' if addition.endswith(('\n', '\r')) else enc.newline)
    try:
        payload = te.encode(new, enc)
    except UnicodeEncodeError:
        return ToolExecution(False, te.message('invalid_encoding', code, encoding=str(enc.codec)), {'path': str(path), 'outcome': 'invalid_encoding'})
    from jarvis.tools.implementations import _atomic_write_bytes183, _backup_file, _record_file_mutation
    backups = getattr(getattr(config, 'paths', None), 'backups', None)
    backup = _backup_file(path, backups) if backups is not None else None
    _atomic_write_bytes183(path, payload)
    if backups is not None:
        _record_file_mutation(backups, path, backup=backup, existed_before=True, operation='paste_file')
    return ToolExecution(True, msg('appended', code, what=clip.describe(content, code), name=path.name), {'path': str(path)})


def paste_folder(config: Any, registry: Any, name: str) -> ToolExecution:
    """Files are copied into the folder, a picture is saved as PNG, text as a .txt file."""
    code = _code(config)
    content, error = _content_or_error(code)
    if error:
        return error
    folder = _resolve(config, name, 'folder')
    if folder is None or not folder.is_dir():
        return ToolExecution(False, msg('no_file', code, name=name), {})
    folder = registry.security.assert_mutation_path_allowed(folder)
    saved = clip.save_to_folder(content, folder)
    names = ', '.join(p.name for p in saved[:3]) + (' …' if len(saved) > 3 else '')
    return ToolExecution(True, msg('saved', code, what=clip.describe(content, code), folder=folder.name, files=names),
                         {'saved': [str(p) for p in saved]})


def paste_reply(config: Any) -> ToolExecution:
    """«вставь мой последний ответ в это поле»: NEO's last answer into the focused field of the working window."""
    from jarvis.runtime.conversation_journal import current_scope
    from jarvis.runtime.reply_memory168 import last
    from jarvis.runtime import ui_elements168
    code = _code(config)
    text = last(current_scope())
    if not text:
        return ToolExecution(False, msg('no_reply', code), {})
    if not ui_elements168.ensure_target() and ui_elements168.neo_is_foreground():
        return ToolExecution(False, msg('no_target', code), {'safe_failure': True})
    clip.set_text(text)
    _hwnd, _pid, title = _foreground()
    return _paste_now(code, clip.Content('text', text=text), msg('where_window', code, name=title[:60]) if title else '')


def clipboard_what(config: Any) -> ToolExecution:
    code = _code(config)
    content = clip.read()
    return ToolExecution(True, msg('clip_is', code, what=clip.describe(content, code)), {'kind': content.kind})


def run(config: Any, registry: Any, args: dict[str, Any]) -> ToolExecution:
    action = str(args.get('action', '') or '')
    name = str(args.get('name', '') or '').strip()
    if action == 'reply':
        return copy_reply(config)
    if action == 'previous_reply':
        return copy_reply(config, back=2)
    if action == 'selection':
        return copy_selection(config)
    if action == 'page':
        return copy_page(config)
    if action == 'file_text':
        return copy_file_text(config, name)
    if action in {'file', 'folder'}:
        return copy_files(config, name, action)
    if action == 'url':
        return copy_url(config, registry)
    if action == 'link':
        return copy_link(config, args.get('index', ''))
    if action in {'screenshot', 'window_screenshot', 'last_screenshot'}:
        return copy_screenshot(config, registry, {'screenshot': 'screen', 'window_screenshot': 'window', 'last_screenshot': 'last'}[action])
    if action == 'paste':
        return paste(config)
    if action == 'paste_window':
        return paste_window(config, registry, name)
    if action == 'paste_field':
        return paste_field(config, registry, name)
    if action == 'paste_line':
        return paste_line(config, args.get('index', ''))
    if action == 'paste_end':
        return paste_end(config)
    if action == 'paste_file':
        return paste_file(config, registry, name)
    if action == 'paste_folder':
        return paste_folder(config, registry, name)
    if action == 'what':
        return clipboard_what(config)
    if action == 'paste_reply':
        return paste_reply(config)
    raise ValueError(f'unknown copy action: {action}')
