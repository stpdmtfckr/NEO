"""One release label shared by diagnostic filenames and protocol summaries."""
from pathlib import Path
import re


def release_number(internal: Path | None = None) -> str:
    root = Path(internal) if internal is not None else Path(__file__).resolve().parents[2]
    try:
        first_line = (root / 'VERSION.txt').read_text(encoding='utf-8-sig').splitlines()[0]
    except (OSError, IndexError):
        return 'unknown'
    match = re.fullmatch(r'NEO v\d+\.\d+\.\d+\.(\d+)(?:-[A-Z0-9-]+)?', first_line)
    return match.group(1) if match else 'unknown'
