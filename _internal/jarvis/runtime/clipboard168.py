"""NEO 168: the Windows clipboard as NEO sees it — text, files and pictures.

Everything NEO copies is also described back to the user («Скопировал текст,
243 символа: «Добрый день…»», «Скопировал файл отчёт.docx», «Скопировал
картинку 1920×1080»), so it is always clear what will be pasted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path, PureWindowsPath
import shutil
import time

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
CF_UNICODETEXT, CF_DIB, CF_HDROP = 13, 8, 15


@dataclass
class Content:
    kind: str                      # text | files | image | empty
    text: str = ''
    files: list[str] = field(default_factory=list)
    image_size: tuple[int, int] = (0, 0)


_WORDS = {
    'text': ('текст', 'text', 'Text', 'texte', 'texto', 'testo', 'texto'),
    'chars': ('символов', 'characters', 'Zeichen', 'caractères', 'caracteres', 'caratteri', 'caracteres'),
    'file': ('файл', 'file', 'Datei', 'fichier', 'archivo', 'file', 'ficheiro'),
    'files': ('файлы', 'files', 'Dateien', 'fichiers', 'archivos', 'file', 'ficheiros'),
    'image': ('картинку', 'a picture', 'ein Bild', 'une image', 'una imagen', 'un’immagine', 'uma imagem'),
    'empty': ('ничего', 'nothing', 'nichts', 'rien', 'nada', 'niente', 'nada'),
    'more': ('и ещё', 'and', 'und noch', 'et encore', 'y', 'e altri', 'e mais'),
}


def _w(key: str, code: str) -> str:
    return _WORDS[key][_CODES.index(code) if code in _CODES else 1]


def preview(text: str, limit: int = 60) -> str:
    flat = ' '.join(str(text or '').split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + '…'


def describe(content: Content, code: str) -> str:
    """Short human description of clipboard content (used after copy and paste)."""
    if content.kind == 'text' and content.text:
        return f"{_w('text', code)} ({len(content.text)} {_w('chars', code)}): «{preview(content.text)}»"
    if content.kind == 'files' and content.files:
        names = [PureWindowsPath(p).name for p in content.files]
        if len(names) == 1:
            return f"{_w('file', code)} {names[0]}"
        shown = ', '.join(names[:3])
        rest = f" {_w('more', code)} {len(names) - 3}" if len(names) > 3 else ''
        return f"{_w('files', code)}: {shown}{rest}"
    if content.kind == 'image':
        width, height = content.image_size
        return f"{_w('image', code)} {width}×{height}" if width else _w('image', code)
    return _w('empty', code)


# ------------------------------------------------------------ Windows side
def _require_windows() -> None:
    if os.name != 'nt':
        raise OSError('The clipboard helpers are available on Windows only')


def sequence() -> int:
    """Clipboard change counter (increases on every copy)."""
    if os.name != 'nt':
        return 0
    import ctypes
    return int(ctypes.windll.user32.GetClipboardSequenceNumber())


def wait_change(before: int, timeout: float = 1.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sequence() != before:
            return True
        time.sleep(0.03)
    return False


def _open():
    import win32clipboard
    for _ in range(20):
        try:
            win32clipboard.OpenClipboard()
            return win32clipboard
        except Exception:
            time.sleep(0.03)
    raise RuntimeError('The clipboard is busy in another program')


def read() -> Content:
    _require_windows()
    clip = _open()
    try:
        if clip.IsClipboardFormatAvailable(CF_HDROP):
            files = list(clip.GetClipboardData(CF_HDROP) or ())
            if files:
                return Content('files', files=[str(f) for f in files])
        if clip.IsClipboardFormatAvailable(CF_UNICODETEXT):
            text = str(clip.GetClipboardData(CF_UNICODETEXT) or '')
            if text:
                return Content('text', text=text)
        if clip.IsClipboardFormatAvailable(CF_DIB):
            data = clip.GetClipboardData(CF_DIB)
            width = int.from_bytes(data[4:8], 'little', signed=True) if len(data) >= 12 else 0
            height = abs(int.from_bytes(data[8:12], 'little', signed=True)) if len(data) >= 12 else 0
            return Content('image', image_size=(width, height))
    finally:
        clip.CloseClipboard()
    return Content('empty')


def set_text(text: str) -> None:
    _require_windows()
    clip = _open()
    try:
        clip.EmptyClipboard()
        clip.SetClipboardData(CF_UNICODETEXT, str(text))
    finally:
        clip.CloseClipboard()


def set_files(paths: list[str | Path]) -> None:
    """Put files on the clipboard like Ctrl+C in File Explorer (CF_HDROP)."""
    _require_windows()
    import struct
    names = [str(Path(p).resolve()) for p in paths]
    # DROPFILES header (20 bytes): offset to file list, pt(x, y), fNC, fWide=1
    header = struct.pack('<IiiII', 20, 0, 0, 0, 1)
    body = ('\0'.join(names) + '\0\0').encode('utf-16-le')
    clip = _open()
    try:
        clip.EmptyClipboard()
        clip.SetClipboardData(CF_HDROP, header + body)
    finally:
        clip.CloseClipboard()


def set_image(path: str | Path) -> tuple[int, int]:
    _require_windows()
    import io
    from PIL import Image
    with Image.open(path) as image:
        rgb = image.convert('RGB')
        size = rgb.size
        out = io.BytesIO()
        rgb.save(out, 'BMP')
    clip = _open()
    try:
        clip.EmptyClipboard()
        clip.SetClipboardData(CF_DIB, out.getvalue()[14:])   # DIB = BMP without the file header
    finally:
        clip.CloseClipboard()
    return size


def image_from_clipboard():
    from PIL import ImageGrab
    image = ImageGrab.grabclipboard()
    return image if hasattr(image, 'save') else None


def _new_file(folder: Path, stem: str, suffix: str) -> Path:
    """NEO 182: reserve a name that does not exist yet (exclusive create), so two pastes in the
    same second never overwrite each other."""
    counter = 1
    while True:
        name = f'{stem}{suffix}' if counter == 1 else f'{stem} ({counter}){suffix}'
        target = folder / name
        try:
            with target.open('xb'):
                pass
            return target
        except FileExistsError:
            counter += 1


def save_to_folder(content: Content, folder: Path) -> list[Path]:
    """Files are copied, a picture becomes a PNG, text becomes a .txt file."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    saved: list[Path] = []
    if content.kind == 'files':
        for source in content.files:
            src = Path(source)
            target = folder / src.name
            counter = 2
            while target.exists():
                target = folder / f'{src.stem} ({counter}){src.suffix}'
                counter += 1
            if src.is_dir():
                shutil.copytree(src, target)
            else:
                shutil.copy2(src, target)
            saved.append(target)
    elif content.kind == 'image':
        image = image_from_clipboard()
        if image is None:
            raise RuntimeError('The picture on the clipboard could not be read')
        target = _new_file(folder, f'NEO_clipboard_{stamp}', '.png')
        try:
            image.save(target, 'PNG')
        except BaseException:
            target.unlink(missing_ok=True)       # NEO 183: never leave an empty half-made file
            raise
        saved.append(target)
    elif content.kind == 'text':
        target = _new_file(folder, f'NEO_clipboard_{stamp}', '.txt')
        try:
            # newline='' keeps the clipboard's own line breaks (CRLF stays CRLF)
            with target.open('w', encoding='utf-8', newline='') as handle:
                handle.write(content.text)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        saved.append(target)
    return saved
