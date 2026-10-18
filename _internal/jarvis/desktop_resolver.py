# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations
from typing import Any

import ctypes
import difflib
from collections import deque
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import quote_plus

from jarvis.runtime.text_normalization import normalize_spoken_command


@dataclass(frozen=True)
class ResolvedTarget:
    kind: str  # app | path | url | shell | shortcut
    value: str
    label: str
    score: float = 1.0


def normalize(value: str) -> str:
    text = normalize_spoken_command(value).casefold().replace("ё", "е")
    text = re.sub(r"[’'`]+", "", text)
    return re.sub(r"[^a-zа-я0-9+#.]+", " ", text).strip()


_RU_ENDINGS = (
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими", "ую", "юю",
    "ия", "ию", "ии", "ием", "иях", "ей", "ой", "ый", "ий", "ого", "ему",
    "ов", "ев", "ом", "ем", "ам", "ям", "ах", "ях", "ы", "и", "а", "я", "у", "ю", "е",
)


def _stem_token(token: str) -> str:
    token = normalize(token)
    if not token or not re.search(r"[а-я]", token):
        return token
    if len(token) <= 4:
        return token
    for ending in _RU_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[: -len(ending)]
    return token


def strip_target_words(value: str) -> str:
    """Remove natural-language object words and harmless request fillers."""

    text = normalize(value)
    patterns = (
        r"^(?:пожалуйста\s+)?(?:эту\s+|этот\s+|мою\s+|мой\s+|мне\s+)?(?:папк\w*|директор\w*)\s+",
        r"^(?:пожалуйста\s+)?(?:эту\s+|этот\s+|мой\s+|мне\s+)?(?:файл\w*)\s+",
        r"^(?:пожалуйста\s+)?(?:это\s+|эту\s+|этот\s+)?(?:приложен\w*|программ\w*)\s+",
        r"^(?:пожалуйста\s+)?(?:эту\s+|этот\s+)?(?:страниц\w*|сайт\w*|ярлык\w*)\s+",
        r"^(?:пожалуйста\s+)?(?:эту\s+|этот\s+|мне\s+)?(?:игр\w*)\s+",
        r"^(?:the\s+)?(?:app|application|program|game|folder|directory|file|page|website|site|shortcut)\s+",
    )
    changed = True
    while changed:
        changed = False
        for pattern in patterns:
            new = re.sub(pattern, "", text, count=1, flags=re.I).strip()
            if new != text:
                text = new
                changed = True
    return text.strip()


WEBSITE_ALIASES: dict[str, str] = {
    "википедия": "https://ru.wikipedia.org",
    "wikipedia": "https://www.wikipedia.org",
    "wiki": "https://www.wikipedia.org",
    "вк": "https://vk.com",
    "vk": "https://vk.com",
    "вконтакте": "https://vk.com",
    "контакт": "https://vk.com",
    "яндекс музыка": "https://music.yandex.ru",
    "яндекс музыки": "https://music.yandex.ru",
    "яндекс музыку": "https://music.yandex.ru",
    "яндекс музуку": "https://music.yandex.ru",
    "яндекс мызуку": "https://music.yandex.ru",
    "яндекс мюзик": "https://music.yandex.ru",
    "яндекс мьюзик": "https://music.yandex.ru",
    "yandex music": "https://music.yandex.ru",
    "ютуб": "https://www.youtube.com",
    "youtube": "https://www.youtube.com",
    "гугл": "https://www.google.com",
    "google": "https://www.google.com",
    "яндекс": "https://yandex.ru",
    "yandex": "https://yandex.ru",
    "гитхаб": "https://github.com",
    "github": "https://github.com",
    "реддит": "https://www.reddit.com",
    "reddit": "https://www.reddit.com",
    "твич": "https://www.twitch.tv",
    "twitch": "https://www.twitch.tv",
    "дискорд веб": "https://discord.com/app",
    "discord web": "https://discord.com/app",
    "телеграм веб": "https://web.telegram.org",
    "telegram web": "https://web.telegram.org",
}

APP_ALIASES: dict[str, str] = {
    "проводник": "explorer.exe",
    "explorer": "explorer.exe",
    "блокнот": "notepad.exe",
    "notepad": "notepad.exe",
    "диспетчер задач": "taskmgr.exe",
    "task manager": "taskmgr.exe",
    "калькулятор": "calc.exe",
    "calculator": "calc.exe",
    "командная строка": "cmd.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "пауэршелл": "powershell.exe",
    "терминал": "wt.exe",
    "windows terminal": "wt.exe",
    "paint": "mspaint.exe",
    "паинт": "mspaint.exe",
    "настройки": "ms-settings:",
    "settings": "ms-settings:",
}

def _known_folder(guid_text: str, fallback_name: str) -> Path:
    """Resolve a redirected Windows Known Folder, with a portable fallback."""
    if os.name != "nt":
        return (Path.home() / fallback_name).resolve(strict=False)
    try:
        u = uuid.UUID(guid_text.strip("{}"))
        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]
        guid = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        ole32 = ctypes.windll.ole32
        init_hr = int(ole32.CoInitializeEx(None, 0x2))  # COINIT_APARTMENTTHREADED
        did_init = init_hr in (0, 1)
        try:
            hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
            if hr == 0 and out.value:
                try:
                    return Path(out.value).resolve(strict=False)
                finally:
                    ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))
        finally:
            if did_init:
                ole32.CoUninitialize()
    except Exception:
        pass
    return (Path.home() / fallback_name).resolve(strict=False)


_DESKTOP = _known_folder("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "Desktop")
_DOCUMENTS = _known_folder("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "Documents")
_DOWNLOADS = _known_folder("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads")
_PICTURES = _known_folder("{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures")
_VIDEOS = _known_folder("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "Videos")
_MUSIC = _known_folder("{4BD8D571-6D19-48D3-BE97-422220080E43}", "Music")

# Use Windows Known Folder IDs instead of assuming C:\ paths or English
# profile layout.  This also respects redirection and 32/64-bit Program Files
# semantics documented by Microsoft.
_PROGRAM_FILES = _known_folder("{905E63B6-C1BF-494E-B29C-65B732D3D21A}", "Program Files")
_PROGRAM_FILES_X86 = _known_folder("{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}", "Program Files (x86)")
_WINDOWS_DIR = _known_folder("{F38BF404-1D43-42F2-9305-67DE0B28FC23}", "Windows")
_SYSTEM32 = _known_folder("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}", "System32")
_PROGRAM_DATA = _known_folder("{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}", "ProgramData")
_APPDATA = _known_folder("{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}", "AppData/Roaming")
_LOCALAPPDATA = _known_folder("{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}", "AppData/Local")

FOLDER_ALIASES: dict[str, Path] = {
    "рабочий стол": _DESKTOP, "desktop": _DESKTOP,
    "загрузки": _DOWNLOADS, "загрузка": _DOWNLOADS, "downloads": _DOWNLOADS, "download": _DOWNLOADS,
    "документы": _DOCUMENTS, "documents": _DOCUMENTS,
    "картинки": _PICTURES, "изображения": _PICTURES, "pictures": _PICTURES,
    "видео": _VIDEOS, "videos": _VIDEOS,
    "музыка": _MUSIC, "music": _MUSIC,
    "домашняя папка": Path.home(), "home": Path.home(), "профиль": Path.home(), "profile": Path.home(),
    "program files": _PROGRAM_FILES, "программные файлы": _PROGRAM_FILES,
    "program files x86": _PROGRAM_FILES_X86, "program files (x86)": _PROGRAM_FILES_X86,
    "program files 86": _PROGRAM_FILES_X86, "программные файлы x86": _PROGRAM_FILES_X86,
    "windows": _WINDOWS_DIR, "виндовс": _WINDOWS_DIR,
    "system32": _SYSTEM32, "system 32": _SYSTEM32, "систем 32": _SYSTEM32,
    "programdata": _PROGRAM_DATA, "program data": _PROGRAM_DATA,
    "appdata": _APPDATA, "app data": _APPDATA, "roaming": _APPDATA,
    "localappdata": _LOCALAPPDATA, "local appdata": _LOCALAPPDATA, "local": _LOCALAPPDATA,
}

_SKIP_DIRS = {
    "appdata", "node_modules", ".git", ".venv", "venv", "__pycache__", "$recycle.bin",
    "windows", "program files", "program files (x86)", "programdata", "system volume information",
    ".cache", ".npm", ".cargo", ".rustup",
}

_START_APPS_CACHE: tuple[float, list[tuple[str, str]]] = (0.0, [])
_REGISTRY_APPS_CACHE: tuple[float, list[tuple[str, str]]] = (0.0, [])
_APPLICATION_QUERY_CACHE: dict[str, tuple[float, list[ResolvedTarget]]] = {}

_RU_TO_LAT = str.maketrans({
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y",
    "к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f",
    "х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
})

def _latin_key(value: str) -> str:
    """Cross-script comparison key for spoken Russian brand/app names.

    Whisper often returns ``Сириус Лаунчер`` while an executable/shortcut is named
    ``Sirius Launcher``.  Comparing only Unicode strings makes those names look
    unrelated; transliterating Russian tokens before fuzzy scoring fixes that class
    of errors without hard-coding every application name.
    """
    text = normalize(value)
    return re.sub(r"[^a-z0-9+#.]+", " ", text.translate(_RU_TO_LAT)).strip()

# Spoken Russian names often have to match an English Start-menu display name.
# Keep this deliberately small and brand-oriented; fuzzy matching handles the rest.
_TOKEN_EQUIVALENTS: dict[str, set[str]] = {
    "яндекс": {"yandex"}, "yandex": {"яндекс"},
    "музык": {"music"}, "music": {"музык", "музыка"},
    "дискорд": {"discord"}, "discord": {"дискорд"},
    "телеграм": {"telegram"}, "telegram": {"телеграм"},
    "стим": {"steam"}, "steam": {"стим"},
    "спотифай": {"spotify"}, "spotify": {"спотифай"},
    "опера": {"opera"}, "opera": {"опера"},
    "хром": {"chrome"}, "chrome": {"хром"},
    "браузер": {"browser"}, "browser": {"браузер"},
    "ворд": {"word"}, "word": {"ворд"},
    "эксел": {"excel"}, "excel": {"эксел", "эксель"},
    "лаунчер": {"launcher"}, "launcher": {"лаунчер"},
    "сириус": {"sirius"}, "sirius": {"сириус"},
    "майнкрафт": {"minecraft"}, "minecraft": {"майнкрафт"},
    "дарктайд": {"darktide"}, "darktide": {"дарктайд"},
    "элден": {"elden"}, "elden": {"элден"},
    "ринг": {"ring"}, "ring": {"ринг"},
    "варкрафт": {"warcraft"}, "warcraft": {"варкрафт"},
    "вау": {"wow"}, "wow": {"вау"},
    "ворлд": {"world"}, "world": {"ворлд"},
    "оф": {"of"}, "of": {"оф"},
    "слей": {"slay"}, "slay": {"слей"},
    "зе": {"the"}, "the": {"зе"},
    "спайр": {"spire"}, "spire": {"спайр"},
    "фортнайт": {"fortnite"}, "fortnite": {"фортнайт"},
    "батлнет": {"battle.net", "battlenet"}, "battlenet": {"батлнет"},
}


def _token_similarity(a: str, b: str) -> float:
    a = normalize(a)
    b = normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sa, sb = _stem_token(a), _stem_token(b)
    if sa and sb and sa == sb:
        return 0.97
    if sb in _TOKEN_EQUIVALENTS.get(sa, set()) or sa in _TOKEN_EQUIVALENTS.get(sb, set()):
        return 0.98
    # Prefix similarity is useful for ASR/morphology, but one-character tokens
    # such as Russian «и» transliterate to ``i`` and used to score 0.94 against
    # every English folder beginning with i (e.g. Important).  Require a real
    # lexical prefix before awarding this shortcut.
    if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):
        return 0.94
    if sa and sb and min(len(sa), len(sb)) >= 3 and (sa.startswith(sb) or sb.startswith(sa)):
        return 0.92
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return 0.90
    direct = difflib.SequenceMatcher(None, a, b).ratio()
    # Cross-script fuzzy score: ``сириус`` <-> ``Sirius``,
    # ``майнкрафт`` <-> ``Minecraft`` etc.  Keep it below exact token equality.
    la, lb = _latin_key(a), _latin_key(b)
    latin = difflib.SequenceMatcher(None, la, lb).ratio() if la and lb else 0.0
    if la and lb and min(len(la), len(lb)) >= 3 and (la.startswith(lb) or lb.startswith(la)):
        latin = max(latin, 0.94)
    return max(direct, min(0.98, latin))


def _score(needle: str, candidate: str) -> float:
    """Human-friendly fuzzy score with a penalty for missing important words.

    RC5 could resolve "яндекс музыка" to a generic shortcut named just "Music".
    This scorer requires coverage of multi-word requests, while remaining tolerant
    of grammatical endings and ASR typos such as "музуку"/"improtant".
    """

    n = strip_target_words(needle)
    c = strip_target_words(candidate)
    if not n or not c:
        return 0.0
    if n == c:
        return 1.0
    n_tokens = n.split()
    c_tokens = c.split()
    direct = difflib.SequenceMatcher(None, n, c).ratio()
    if not n_tokens or not c_tokens:
        return direct

    token_scores = [max(_token_similarity(nt, ct) for ct in c_tokens) for nt in n_tokens]
    good = [value >= 0.70 for value in token_scores]
    coverage = sum(good) / len(good)
    average = sum(token_scores) / len(token_scores)

    # Missing one of two requested words (e.g. "яндекс" in "Music") must not
    # beat a real two-word app/PWA candidate.
    if len(n_tokens) >= 2 and coverage < 0.99:
        direct *= 0.58 + 0.30 * coverage
    if len(c_tokens) < len(n_tokens):
        direct *= max(0.58, len(c_tokens) / len(n_tokens))

    token_score = average * (0.62 + 0.38 * coverage)
    if coverage == 1.0:
        token_score = max(token_score, 0.78 + average * 0.21)
    return min(1.0, max(direct, token_score))




_APP_AUXILIARY_MARKERS = (
    "service", "webhelper", "helper", "updater", "update", "crashpad",
    "crashhandler", "installer", "uninstall", "bootstrapper", "renderer",
)

def _application_candidate_score(needle: str, label: str, value: str = "", *, base: float | None = None) -> float:
    """Application-specific ranking that suppresses helper/service processes.

    A short brand request such as ``Steam`` must resolve to the user-facing Steam
    client, not ``SteamService`` or ``Steam Client WebHelper``.  The generic fuzzy
    scorer intentionally rewards token coverage and therefore used to give helper
    processes near-perfect scores.  Keep that behaviour for files/folders, but add
    a launcher-only penalty for well-known auxiliary process roles.
    """
    q = normalize(strip_target_words(needle))
    label_n = normalize(label)
    value_stem = ""
    try:
        value_stem = normalize(Path(str(value or "")).stem)
    except Exception:
        value_stem = ""
    score = float(_score(q, label_n) if base is None else base)
    if not q:
        return 0.0

    # Exact semantic names always win, including cross-script brand aliases.
    if label_n == q or (_latin_key(label_n) and _latin_key(label_n) == _latin_key(q)):
        score = max(score, 1.0)

    hay_compact = re.sub(r"[^a-zа-я0-9]+", "", f"{label_n} {value_stem}")
    query_compact = re.sub(r"[^a-zа-я0-9]+", "", q)
    for marker in _APP_AUXILIARY_MARKERS:
        if marker in hay_compact and marker not in query_compact:
            score *= 0.48
            break

    # A process called e.g. ``Steam Client WebHelper`` contains the exact brand
    # token, but should still rank below a plain ``Steam`` StartApps/exe entry.
    if len(label_n.split()) > len(q.split()) and any(m in label_n.replace(" ", "") for m in _APP_AUXILIARY_MARKERS):
        score *= 0.85

    # Guard short brand names against accidental prefix-neighbours.  The generic
    # ASR scorer quite reasonably considers ``Steam`` and ``Stream`` close, but
    # an *application launcher* must not turn "open Steam" into "Stream Dock
    # AJAZZ" just because the requested app is missing.  Extra words are allowed
    # when one label token is an exact brand token (``Opera GX``, ``Discord
    # Canary``); otherwise a one-token query is deliberately pushed below the
    # automatic-launch threshold and will ask instead of opening the wrong EXE.
    q_tokens = [token for token in q.split() if token]
    label_tokens = [token for token in label_n.split() if token]
    if len(q_tokens) == 1 and len(label_tokens) > 1:
        q_token = q_tokens[0]
        q_latin = _latin_key(q_token)
        exact_token = any(
            token == q_token or (q_latin and _latin_key(token) == q_latin)
            for token in label_tokens
        )
        if not exact_token:
            score *= 0.68
    return max(0.0, min(1.0, score))


def _application_identity_rank(needle: str, item: ResolvedTarget) -> tuple[int, int, int]:
    """Secondary launcher ranking for equal/near-equal fuzzy scores.

    Windows StartApps frequently exposes one user-facing application together
    with architecture/help variants.  For example, a query for ``Cheat Engine``
    can produce ``Cheat Engine``, ``Cheat Engine (32-bit)``, ``Cheat Engine
    (64-bit)`` and a help entry, all with a fuzzy score of 1.0 because every
    query token is present.  The assistant should open the plain exact app instead
    of asking which variant the user meant.

    The tuple is deliberately only a *tie breaker*: the main fuzzy score still
    decides unrelated candidates.  Existing EXE targets are preferred over shell
    aliases/shortcuts when semantic identity is otherwise equal.
    """
    q = normalize(strip_target_words(needle))
    label = normalize(item.label)
    value_stem = ""
    try:
        value_stem = normalize(Path(str(item.value or "")).stem)
    except Exception:
        value_stem = ""

    exact_label = int(bool(q and (label == q or (_latin_key(label) and _latin_key(label) == _latin_key(q)))))
    exact_exe = int(bool(q and (value_stem == q or (_latin_key(value_stem) and _latin_key(value_stem) == _latin_key(q)))))
    kind_rank = {"app": 3, "shortcut": 2, "shell": 1}.get(str(item.kind or ""), 0)

    # Prefer the shortest exact display name. A plain "Cheat Engine" therefore
    # beats "Cheat Engine (64-bit SSE4-AVX2)" even if both received 1.0.
    extra_tokens = max(0, len(label.split()) - len(q.split())) if q else 0
    semantic_rank = exact_label * 100 + exact_exe * 80 - min(20, extra_tokens * 4)
    return semantic_rank, kind_rank, -len(label)


def application_candidate_is_exact(name: str, item: ResolvedTarget) -> bool:
    """Return True when a candidate is the plain semantic identity requested."""
    return _application_identity_rank(name, item)[0] >= 100

def target_similarity(needle: str, candidate: str) -> float:
    """Public scorer for launchers/stores/process bridges.

    Keep every executable discovery path on the same ASR-tolerant scoring model,
    including cross-script requests such as ``дарктайд`` -> ``Darktide``.
    """
    return _score(needle, candidate)


def _best_alias(name: str, aliases: Mapping[str, object], *, threshold: float = 0.74) -> tuple[str, object, float] | None:
    cleaned = strip_target_words(name)
    if not cleaned:
        return None
    if cleaned in aliases:
        return cleaned, aliases[cleaned], 1.0
    best: tuple[str, object, float] | None = None
    for alias, value in aliases.items():
        score = _score(cleaned, alias)
        if best is None or score > best[2]:
            best = (alias, value, score)
    return best if best and best[2] >= threshold else None


def common_user_roots() -> list[Path]:
    roots: list[Path] = []
    candidates = [
        Path.home(), _DESKTOP, _DOCUMENTS, _DOWNLOADS, _PICTURES, _VIDEOS, _MUSIC,
    ]
    for env_name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.getenv(env_name)
        if value:
            candidates.append(Path(value))
    for path in candidates:
        try:
            path = path.resolve(strict=False)
        except OSError:
            continue
        if path.exists() and path not in roots:
            roots.append(path)
    return roots



def _hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt": return {}
    kwargs: dict[str, Any] = {}
    try: kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except Exception: pass
    try:
        si = subprocess.STARTUPINFO(); si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        si.wShowWindow = 0; kwargs["startupinfo"] = si
    except Exception: pass
    return kwargs

def _windows_start_apps() -> list[tuple[str, str]]:
    global _START_APPS_CACHE
    if os.name != "nt":
        return []
    cached_at, cached = _START_APPS_CACHE
    if cached and time.monotonic() - cached_at < 180.0:
        return cached
    script = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3.5,
            check=False,
            **_hidden_subprocess_kwargs(),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return cached
        raw = json.loads(proc.stdout)
        if isinstance(raw, dict):
            raw = [raw]
        values: list[tuple[str, str]] = []
        for item in raw if isinstance(raw, list) else []:
            name = str(item.get("Name", "") or "").strip()
            appid = str(item.get("AppID", "") or "").strip()
            if name and appid:
                values.append((name, appid))
        if values:
            _START_APPS_CACHE = (time.monotonic(), values)
            return values
    except Exception:
        pass
    return cached


def find_start_app(name: str, *, threshold: float = 0.69) -> ResolvedTarget | None:
    needle = strip_target_words(name)
    if not needle:
        return None
    best: tuple[float, str, str] | None = None
    for app_name, appid in _windows_start_apps():
        score = _score(needle, app_name)
        if best is None or score > best[0]:
            best = (score, app_name, appid)
    if best and best[0] >= threshold:
        return ResolvedTarget("shell", best[2], best[1], best[0])
    return None


def find_start_menu_link(name: str) -> Path | None:
    if os.name != "nt":
        return None
    roots = [
        Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.getenv("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path.home() / "Desktop",
        Path(os.getenv("PUBLIC", "C:/Users/Public")) / "Desktop",
    ]
    needle = strip_target_words(name)
    best: tuple[float, Path] | None = None
    for root in roots:
        if not root.exists():
            continue
        try:
            for link in root.rglob("*"):
                if link.suffix.casefold() not in {".lnk", ".url", ".appref-ms"}:
                    continue
                score = _score(needle, link.stem)
                if score >= 0.995:
                    return link
                if best is None or score > best[0]:
                    best = (score, link)
        except OSError:
            continue
    return best[1] if best and best[0] >= 0.69 else None


def find_installed_executable(name: str) -> Path | None:
    """Bounded fuzzy scan for apps not registered in StartApps/Start Menu."""
    if os.name != "nt":
        return None
    needle = strip_target_words(name)
    if not needle:
        return None
    roots: list[Path] = []
    for value in (
        os.getenv("LOCALAPPDATA", ""),
        os.getenv("ProgramFiles", ""),
        os.getenv("ProgramFiles(x86)", ""),
    ):
        if not value:
            continue
        base = Path(value)
        if base.name.casefold() == "appdata":
            base = base / "Local" / "Programs"
        elif base == Path(os.getenv("LOCALAPPDATA", "")):
            base = base / "Programs"
        if base.exists() and base not in roots:
            roots.append(base)
    best: tuple[float, Path] | None = None
    started = time.monotonic()
    seen = 0
    for root in roots:
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and seen < 30000 and time.monotonic() - started < 2.2:
            folder, depth = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        seen += 1
                        if seen >= 30000 or time.monotonic() - started >= 2.2:
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False) and depth < 5:
                                if entry.name.casefold() not in _SKIP_DIRS:
                                    stack.append((Path(entry.path), depth + 1))
                                continue
                            if not entry.is_file(follow_symlinks=False) or not entry.name.casefold().endswith(".exe"):
                                continue
                        except OSError:
                            continue
                        stem = Path(entry.name).stem
                        stem_score = _score(needle, stem)
                        parent_score = _score(needle, folder.name)
                        # A matching install directory is useful evidence, but it
                        # must never make every EXE inside that directory look like
                        # the requested application.  This was especially harmful
                        # for Steam: SteamService/steamwebhelper live below a folder
                        # named Steam.  Keep the folder signal bounded, then apply
                        # the same helper/service penalties as the global launcher.
                        base_score = max(stem_score, parent_score * 0.86)
                        score = _application_candidate_score(needle, stem, entry.path, base=base_score)
                        if best is None or score > best[0]:
                            best = (score, Path(entry.path))
                        # Early exit only for an executable whose *own name* is an
                        # exact/near-exact match. Parent-folder equality alone is
                        # not enough to choose an arbitrary helper binary.
                        if stem_score >= 0.985 and score >= 0.90:
                            return Path(entry.path)
            except (OSError, PermissionError):
                continue
    return best[1] if best and best[0] >= 0.72 else None


def _search_path_tree(
    root: Path,
    needle: str,
    *,
    kind: str,
    max_entries: int,
    max_seconds: float,
    max_depth: int,
) -> Path | None:
    started = time.monotonic()
    best: tuple[float, int, Path] | None = None
    seen_entries = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and seen_entries < max_entries and time.monotonic() - started < max_seconds:
        folder, depth = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    seen_entries += 1
                    if seen_entries >= max_entries or time.monotonic() - started >= max_seconds:
                        break
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir and entry.name.casefold() in _SKIP_DIRS:
                        continue
                    eligible = kind == "any" or (kind == "folder" and is_dir) or (kind == "file" and is_file)
                    if eligible:
                        candidate_name = Path(entry.name).stem if is_file else entry.name
                        score = _score(needle, candidate_name)
                        ranking = (score, -depth)
                        if best is None or ranking > (best[0], best[1]):
                            best = (score, -depth, Path(entry.path))
                        if score >= 0.995:
                            return Path(entry.path)
                    if is_dir and depth < max_depth:
                        stack.append((Path(entry.path), depth + 1))
        except (OSError, PermissionError):
            continue
    return best[2] if best and best[0] >= 0.62 else None


def find_user_path_in_root(
    root: Path,
    name: str,
    *,
    kind: str = "any",
    max_entries: int = 70000,
    max_seconds: float = 4.0,
    max_depth: int = 8,
) -> Path | None:
    needle = strip_target_words(name)
    if not needle:
        return None
    try:
        root = root.expanduser().resolve(strict=False)
    except OSError:
        return None
    if not root.exists() or not root.is_dir():
        return None
    return _search_path_tree(root, needle, kind=kind, max_entries=max_entries, max_seconds=max_seconds, max_depth=max_depth)


def _fixed_drive_roots() -> list[Path]:
    """Return usable fixed-drive roots for a bounded second-pass search on Windows."""
    if os.name != "nt":
        return []
    roots: list[Path] = []
    try:
        import psutil
        for part in psutil.disk_partitions(all=False):
            opts = str(part.opts or "").casefold()
            if "cdrom" in opts or "removable" in opts:
                continue
            mount = Path(part.mountpoint)
            if mount.exists() and mount not in roots:
                roots.append(mount)
    except Exception:
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{letter}:\\")
            if root.exists():
                roots.append(root)
    return roots




def _exact_top_level_user_path(name: str, *, kind: str = "any") -> Path | None:
    """Prefer literal drive/user-root names before any fuzzy recursive match.

    A spoken ``Important`` should resolve to ``D:\\Important`` when that exact
    folder exists, not to an unrelated deep folder whose fuzzy score happens to
    be acceptable.  Keep this pass intentionally shallow and cheap.
    """
    needle = strip_target_words(name)
    if not needle:
        return None
    wanted = normalize(needle)
    roots: list[Path] = []
    for item in common_user_roots() + _fixed_drive_roots():
        try:
            item = item.resolve(strict=False)
        except OSError:
            continue
        if item.exists() and item not in roots:
            roots.append(item)
    for root in roots:
        # The root itself may be the requested named folder.
        root_name = root.name or str(root)
        if normalize(root_name) == wanted:
            if kind == "any" or (kind == "folder" and root.is_dir()) or (kind == "file" and root.is_file()):
                return root
        try:
            for child in root.iterdir():
                try:
                    eligible = kind == "any" or (kind == "folder" and child.is_dir()) or (kind == "file" and child.is_file())
                except OSError:
                    continue
                if eligible and normalize(child.stem if child.is_file() else child.name) == wanted:
                    return child
        except (OSError, PermissionError):
            continue
    return None


def _search_exact_path_tree(
    root: Path,
    needle: str,
    *,
    kind: str,
    max_entries: int,
    max_seconds: float,
    max_depth: int,
) -> Path | None:
    """Exact-name recursive search used by explicit file/folder commands."""
    wanted = normalize(needle)
    wanted_name = normalize(Path(needle).name)
    wanted_stem = normalize(Path(needle).stem)
    wants_extension = bool(Path(needle).suffix)
    started = time.monotonic()
    seen = 0
    queue = deque([(root, 0)])
    while queue and seen < max_entries and time.monotonic() - started < max_seconds:
        folder, depth = queue.popleft()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    seen += 1
                    if seen >= max_entries or time.monotonic() - started >= max_seconds:
                        break
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir and entry.name.casefold() in _SKIP_DIRS:
                        continue
                    eligible = kind == "any" or (kind == "folder" and is_dir) or (kind == "file" and is_file)
                    if eligible:
                        if is_file:
                            full = normalize(entry.name)
                            stem = normalize(Path(entry.name).stem)
                            exact = full == wanted_name if wants_extension else stem in {wanted, wanted_stem}
                        else:
                            exact = normalize(entry.name) == wanted
                        if exact:
                            return Path(entry.path)
                    if is_dir and depth < max_depth:
                        queue.append((Path(entry.path), depth + 1))
        except (OSError, PermissionError):
            continue
    return None


def _collect_exact_path_tree(
    root: Path,
    needle: str,
    *,
    kind: str,
    limit: int,
    max_entries: int,
    max_seconds: float,
    max_depth: int,
) -> list[Path]:
    """Collect a few literal-name matches for ambiguity-safe file/folder routing."""
    wanted = normalize(needle)
    wanted_name = normalize(Path(needle).name)
    wanted_stem = normalize(Path(needle).stem)
    wants_extension = bool(Path(needle).suffix)
    started = time.monotonic()
    seen = 0
    found: list[Path] = []
    queue = deque([(root, 0)])
    while queue and seen < max_entries and time.monotonic() - started < max_seconds and len(found) < limit:
        folder, depth = queue.popleft()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    seen += 1
                    if seen >= max_entries or time.monotonic() - started >= max_seconds or len(found) >= limit:
                        break
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_file = entry.is_file(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir and entry.name.casefold() in _SKIP_DIRS:
                        continue
                    eligible = kind == "any" or (kind == "folder" and is_dir) or (kind == "file" and is_file)
                    if eligible:
                        if is_file:
                            full = normalize(entry.name)
                            stem = normalize(Path(entry.name).stem)
                            exact = full == wanted_name if wants_extension else stem in {wanted, wanted_stem}
                        else:
                            exact = normalize(entry.name) == wanted
                        if exact:
                            found.append(Path(entry.path).resolve(strict=False))
                    if is_dir and depth < max_depth:
                        queue.append((Path(entry.path), depth + 1))
        except (OSError, PermissionError):
            continue
    return found


def find_user_path_exact_candidates(
    name: str,
    *,
    kind: str = "any",
    limit: int = 5,
    max_seconds: float = 7.0,
) -> list[Path]:
    """Return literal matches; callers must clarify when more than one exists."""
    raw = str(name or "").strip().strip('"')
    needle = strip_target_words(raw)
    if not needle:
        return []
    # An explicitly dictated path is authoritative and need not be searched.
    try:
        direct = Path(raw).expanduser()
        if direct.exists():
            if kind == "file" and direct.is_file():
                return [direct.resolve(strict=False)]
            if kind in {"folder", "directory"} and direct.is_dir():
                return [direct.resolve(strict=False)]
            if kind == "any":
                return [direct.resolve(strict=False)]
    except OSError:
        pass

    out: list[Path] = []
    seen: set[str] = set()
    roots = common_user_roots()
    if roots:
        per_seconds = max(0.5, max_seconds * 0.65 / len(roots))
        for root in roots:
            for match in _collect_exact_path_tree(
                root, needle, kind=kind, limit=limit,
                max_entries=45000, max_seconds=per_seconds,
                max_depth=8 if root == Path.home().resolve(strict=False) else 6,
            ):
                key = os.path.normcase(str(match))
                if key not in seen:
                    seen.add(key)
                    out.append(match)
                    if len(out) >= limit:
                        return out
    remaining = max(0.5, max_seconds * 0.35)
    drives = _fixed_drive_roots()
    per_drive = max(0.45, remaining / max(1, len(drives)))
    for root in drives:
        for match in _collect_exact_path_tree(
            root, needle, kind=kind, limit=limit,
            max_entries=38000, max_seconds=per_drive, max_depth=6,
        ):
            key = os.path.normcase(str(match))
            if key not in seen:
                seen.add(key)
                out.append(match)
                if len(out) >= limit:
                    return out
    return out


def find_user_path_exact(
    name: str,
    *,
    kind: str = "any",
    max_entries: int = 120000,
    max_seconds: float = 7.0,
) -> Path | None:
    """Find the exact spoken file/folder name without fuzzy neighbouring matches."""
    needle = strip_target_words(name)
    if not needle:
        return None
    exact = _exact_top_level_user_path(needle, kind=kind)
    if exact is not None:
        return exact
    roots = common_user_roots()
    if roots:
        per_entries = max(8000, max_entries // len(roots))
        per_seconds = max(0.6, max_seconds / len(roots))
        for root in roots:
            match = _search_exact_path_tree(root, needle, kind=kind, max_entries=per_entries, max_seconds=per_seconds, max_depth=8)
            if match is not None:
                return match
    # Fixed drives are a second pass for D:\Work, D:\Games, etc. The match itself
    # stays exact even though the search area is broad.
    for root in _fixed_drive_roots():
        match = _search_exact_path_tree(root, needle, kind=kind, max_entries=32000, max_seconds=1.7, max_depth=6)
        if match is not None:
            return match
    return None

def find_user_path(
    name: str,
    *,
    kind: str = "any",
    max_entries: int = 100000,
    max_seconds: float = 5.5,
) -> Path | None:
    """Fuzzy-search normal user locations without requiring an exact full path."""

    raw_name = str(name or "").strip()
    raw_key = normalize(raw_name)
    if kind in {"any", "folder"}:
        for alias, alias_path in FOLDER_ALIASES.items():
            if normalize(alias) == raw_key:
                path = Path(alias_path)
                if path.exists():
                    return path.resolve(strict=False)
    needle = strip_target_words(name)
    if not needle:
        return None
    exact = _exact_top_level_user_path(needle, kind=kind)
    if exact is not None:
        return exact
    folder_alias = _best_alias(needle, FOLDER_ALIASES, threshold=0.76)
    if folder_alias and kind in {"any", "folder"}:
        path = Path(folder_alias[1])
        if path.exists():
            return path

    roots = common_user_roots()
    if not roots:
        return None
    per_root_entries = max(6000, max_entries // len(roots))
    per_root_seconds = max(0.55, max_seconds / len(roots))
    best: tuple[float, Path] | None = None
    for root in roots:
        match = _search_path_tree(
            root,
            needle,
            kind=kind,
            max_entries=per_root_entries,
            max_seconds=per_root_seconds,
            max_depth=8 if root == Path.home().resolve(strict=False) else 6,
        )
        if match is None:
            continue
        score = _score(needle, match.stem if match.is_file() else match.name)
        if best is None or score > best[0]:
            best = (score, match)
        if score >= 0.96:
            break
    if best and best[0] >= 0.62:
        return best[1]

    # If the requested folder/file is not under the standard profile folders, do
    # a short bounded scan of fixed drives. This covers common D:\Games, D:\Work,
    # project folders, etc. without forcing users to dictate full paths.
    for root in _fixed_drive_roots():
        match = _search_path_tree(
            root, needle, kind=kind, max_entries=24000, max_seconds=1.35, max_depth=5
        )
        if match is not None:
            score = _score(needle, match.stem if match.is_file() else match.name)
            if score >= 0.66:
                return match
    return None



def _windows_registry_apps() -> list[tuple[str, str]]:
    """Return (display name, executable path) from standard Windows app registries."""
    global _REGISTRY_APPS_CACHE
    if os.name != "nt":
        return []
    cached_at, cached = _REGISTRY_APPS_CACHE
    if cached and time.monotonic() - cached_at < 300.0:
        return cached
    values: list[tuple[str, str]] = []
    try:
        import winreg
        # App Paths is the strongest source because the value points directly to exe.
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for base in (r"Software\Microsoft\Windows\CurrentVersion\App Paths",):
                try:
                    with winreg.OpenKey(hive, base) as root:
                        count = winreg.QueryInfoKey(root)[0]
                        for i in range(count):
                            try:
                                sub = winreg.EnumKey(root, i)
                                with winreg.OpenKey(root, sub) as key:
                                    exe, _ = winreg.QueryValueEx(key, None)
                                path = os.path.expandvars(str(exe or "").strip('"'))
                                if path and Path(path).suffix.casefold() == ".exe":
                                    values.append((Path(sub).stem, path))
                            except OSError:
                                continue
                except OSError:
                    pass
        # Uninstall entries often contain DisplayIcon or an InstallLocation even
        # when the application is absent from StartApps (portable launchers are a
        # common example).  Read both 32/64-bit registry views.
        uninstall_roots = (
            r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
            r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        )
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for base in uninstall_roots:
                try:
                    with winreg.OpenKey(hive, base) as root:
                        count = winreg.QueryInfoKey(root)[0]
                        for i in range(count):
                            try:
                                sub = winreg.EnumKey(root, i)
                                with winreg.OpenKey(root, sub) as key:
                                    def q(name: str) -> str:
                                        try:
                                            value, _ = winreg.QueryValueEx(key, name)
                                            return str(value or "").strip()
                                        except OSError:
                                            return ""
                                    display = q("DisplayName")
                                    icon = q("DisplayIcon")
                                    location = q("InstallLocation")
                                if not display:
                                    continue
                                icon_path = os.path.expandvars(icon.split(",", 1)[0].strip('" ')) if icon else ""
                                # Do not probe every uninstall entry on disk while building the
                                # application index.  A stale installer entry can point at a
                                # sleeping/disconnected drive and make an ordinary "open Steam"
                                # block for minutes.  DisplayIcon is already a launch candidate;
                                # its existence is validated only if it actually wins ranking.
                                if icon_path and Path(icon_path).suffix.casefold() == ".exe":
                                    values.append((display, icon_path))
                                    continue
                                # InstallLocation-only entries are intentionally not globbed here.
                                # The bounded installed/portable scans below handle those apps
                                # without turning registry enumeration into unbounded filesystem IO.
                            except OSError:
                                continue
                except OSError:
                    pass
    except Exception:
        return cached
    # Stable de-duplication.
    seen: set[str] = set()
    clean: list[tuple[str, str]] = []
    for label, exe in values:
        key = os.path.normcase(os.path.abspath(exe)) if exe else ""
        if not key or key in seen:
            continue
        seen.add(key)
        clean.append((label, exe))
    _REGISTRY_APPS_CACHE = (time.monotonic(), clean)
    return clean


def _running_executable_candidates(name: str) -> list[ResolvedTarget]:
    if os.name != "nt":
        return []
    try:
        import psutil
    except Exception:
        return []
    needle = strip_target_words(name)
    out: list[ResolvedTarget] = []
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            exe = str(proc.info.get("exe") or "").strip()
            pname = str(proc.info.get("name") or "").strip()
            if not exe or not Path(exe).is_file():
                continue
            label = Path(pname or exe).stem
            score = max(_score(needle, label), _score(needle, Path(exe).parent.name))
            if score >= 0.68:
                out.append(ResolvedTarget("app", exe, label, score))
        except Exception:
            continue
    return out


def _registry_application_candidates(name: str) -> list[ResolvedTarget]:
    needle = strip_target_words(name)
    out: list[ResolvedTarget] = []
    generic = {"uninstall", "unins000", "update", "updater", "helper", "crashpad_handler", "setup", "installer"}
    for display, exe in _windows_registry_apps():
        stem = Path(exe).stem
        score = max(_score(needle, display), _score(needle, stem), _score(needle, Path(exe).parent.name))
        if stem.casefold() in generic and normalize(stem) not in normalize(needle):
            score *= 0.35
        if score >= 0.68:
            out.append(ResolvedTarget("app", exe, display or stem, score))
    return out


def _start_application_candidates(name: str, *, limit: int = 8) -> list[ResolvedTarget]:
    needle = strip_target_words(name)
    ranked: list[ResolvedTarget] = []
    for app_name, appid in _windows_start_apps():
        score = _score(needle, app_name)
        if score >= 0.64:
            ranked.append(ResolvedTarget("shell", appid, app_name, score))
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[:limit]


def _portable_executable_search(name: str) -> Path | None:
    """Last-resort bounded search for portable apps/games on user/fixed drives.

    This path is intentionally slower than StartApps/registry lookups and runs only
    after those indexes miss.  It searches executable files, never arbitrary folders,
    so a request for an application cannot accidentally open ``D:\\Games\\Foo``.
    """
    if os.name != "nt":
        return None
    needle = strip_target_words(name)
    if not needle:
        return None
    roots: list[Path] = []
    likely_names = ("Games", "Game", "Apps", "Applications", "Programs", "Launchers", "Portable")
    for root in common_user_roots():
        if root.exists() and root not in roots:
            roots.append(root)
    for drive in _fixed_drive_roots():
        for child_name in likely_names:
            child = drive / child_name
            if child.exists() and child not in roots:
                roots.append(child)
        # A shallow drive-root scan is useful for D:\SiriusLauncher\launcher.exe.
        if drive not in roots:
            roots.append(drive)
    started = time.monotonic()
    best: tuple[float, Path] | None = None
    seen = 0
    banned_dirs = {x for x in _SKIP_DIRS if x not in {"program files", "program files (x86)", "programdata"}}
    for root in roots:
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and seen < 70000 and time.monotonic() - started < 4.5:
            folder, depth = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        seen += 1
                        if seen >= 70000 or time.monotonic() - started >= 4.5:
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if depth < (5 if root.anchor != str(root) else 4) and entry.name.casefold() not in banned_dirs:
                                    # Prefer traversing directories that resemble the app name.
                                    stack.append((Path(entry.path), depth + 1))
                                continue
                            if not entry.is_file(follow_symlinks=False) or not entry.name.casefold().endswith(".exe"):
                                continue
                        except OSError:
                            continue
                        exe = Path(entry.path)
                        stem = exe.stem
                        stem_score = _score(needle, stem)
                        parent_score = _score(needle, exe.parent.name)
                        joined_score = _score(needle, f"{exe.parent.name} {stem}")
                        # Do not allow a perfectly matching parent directory to
                        # promote an unrelated service/helper executable to 1.0.
                        # The EXE name remains the primary launcher identity.
                        base_score = max(stem_score, parent_score * 0.84, joined_score * 0.90)
                        score = _application_candidate_score(needle, stem, str(exe), base=base_score)
                        if best is None or score > best[0]:
                            best = (score, exe)
                        if stem_score >= 0.99 and score >= 0.90:
                            return exe
            except (OSError, PermissionError):
                continue
    return best[1] if best and best[0] >= 0.74 else None


def _rank_application_candidates(cleaned: str, candidates: list[ResolvedTarget]) -> list[ResolvedTarget]:
    adjusted: list[ResolvedTarget] = []
    for item in candidates:
        adjusted.append(ResolvedTarget(
            item.kind, item.value, item.label,
            _application_candidate_score(cleaned, item.label, item.value, base=float(item.score)),
        ))
    dedup: dict[tuple[str, str], ResolvedTarget] = {}
    for item in adjusted:
        key = (item.kind, os.path.normcase(str(item.value)))
        old = dedup.get(key)
        if old is None or item.score > old.score:
            dedup[key] = item
    return sorted(
        dedup.values(),
        key=lambda item: (float(item.score), *_application_identity_rank(cleaned, item)),
        reverse=True,
    )


def _exact_ranked_application(cleaned: str, candidates: list[ResolvedTarget]) -> list[ResolvedTarget]:
    ranked = _rank_application_candidates(cleaned, candidates)
    return [item for item in ranked if application_candidate_is_exact(cleaned, item)]


def application_candidates(name: str, *, limit: int = 5) -> list[ResolvedTarget]:
    """Rank executable/application-only candidates without blocking normal commands.

    v3.7 collected every possible source before ranking.  On Windows that meant a
    request for an already-running Steam could still invoke PowerShell, registry
    enumeration and multiple filesystem scans before *logging the first result*.
    v3.8 is staged: an exact hit from a cheap source returns immediately; expensive
    portable scans run only after indexed sources fail.
    """
    raw = str(name or "").strip().strip('"')
    cleaned = strip_target_words(raw)
    if not cleaned:
        return []

    cache_key = normalize(cleaned)
    cached = _APPLICATION_QUERY_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < 120.0:
        return list(cached[1][:max(1, int(limit))])

    candidates: list[ResolvedTarget] = []

    def finish(items: list[ResolvedTarget]) -> list[ResolvedTarget]:
        ranked = _rank_application_candidates(cleaned, items)
        now = time.monotonic()
        # REC-APPLICATION-FREEZE-SOURCE-01: the per-query cache is bounded and
        # drops expired keys instead of growing for the whole session.
        if len(_APPLICATION_QUERY_CACHE) >= 128:
            for key in [k for k, (stamp, _rows) in list(_APPLICATION_QUERY_CACHE.items()) if now - stamp >= 120.0]:
                _APPLICATION_QUERY_CACHE.pop(key, None)
            while len(_APPLICATION_QUERY_CACHE) >= 128:
                _APPLICATION_QUERY_CACHE.pop(next(iter(_APPLICATION_QUERY_CACHE)), None)
        _APPLICATION_QUERY_CACHE[cache_key] = (now, ranked[:12])
        return ranked[:max(1, int(limit))]

    # 1) Explicit path / built-in alias: no system enumeration required.
    try:
        p = Path(raw).expanduser()
        if p.is_file() and p.suffix.casefold() == ".exe":
            candidates.append(ResolvedTarget("app", str(p.resolve(strict=False)), p.stem, 1.0))
            return finish(candidates)
    except OSError:
        pass

    alias = _best_alias(cleaned, APP_ALIASES, threshold=0.76)
    if alias:
        exe = str(alias[1])
        if not exe.startswith("ms-settings:"):
            resolved = shutil.which(exe) or exe
            candidates.append(ResolvedTarget("app", resolved, alias[0], alias[2]))
            exact = _exact_ranked_application(cleaned, candidates)
            if exact:
                return finish(exact)

    # 2) Running processes are the fastest and most useful source.  This makes
    #    "open Steam" behave like a taskbar click instead of starting an index scan.
    running = _running_executable_candidates(cleaned)
    candidates.extend(running)
    exact = _exact_ranked_application(cleaned, candidates)
    if exact:
        return finish(exact)

    # 3) Windows StartApps. PowerShell itself has a hard timeout and exact matches
    #    return before registry/filesystem fallback work.
    start_apps = _start_application_candidates(cleaned)
    candidates.extend(start_apps)
    exact = _exact_ranked_application(cleaned, candidates)
    if exact:
        return finish(exact)

    # 4) Registry App Paths / DisplayIcon index. This stage is metadata-only in
    #    v3.8; it no longer walks every InstallLocation while answering a command.
    registry = _registry_application_candidates(cleaned)
    candidates.extend(registry)
    exact = _exact_ranked_application(cleaned, candidates)
    if exact:
        return finish(exact)

    # PATH executables are cheap and deterministic.
    which = shutil.which(raw) or shutil.which(cleaned)
    if which:
        candidates.append(ResolvedTarget("app", which, Path(which).stem, 0.97))
        exact = _exact_ranked_application(cleaned, candidates)
        if exact:
            return finish(exact)

    # 5) Start-menu shortcut scan.  Still bounded to the small Start Menu/Desktop
    #    trees and returns immediately for a literal launcher name.
    link = find_start_menu_link(cleaned)
    if link is not None:
        candidates.append(ResolvedTarget("shortcut", str(link), link.stem, max(0.70, _score(cleaned, link.stem))))
        exact = _exact_ranked_application(cleaned, candidates)
        if exact:
            return finish(exact)

    # 6) Installed executable scan (hard 2.2s bound), then portable fixed-drive
    #    search (hard 4.5s bound) only when no indexed candidate is convincing.
    exe = find_installed_executable(cleaned)
    if exe is not None:
        candidates.append(ResolvedTarget("app", str(exe), exe.stem, max(0.72, _score(cleaned, exe.stem))))

    ranked = _rank_application_candidates(cleaned, candidates)
    if not ranked or ranked[0].score < 0.82:
        portable = _portable_executable_search(cleaned)
        if portable is not None:
            raw_score = max(_score(cleaned, portable.stem), _score(cleaned, portable.parent.name) * 0.84)
            candidates.append(ResolvedTarget(
                "app", str(portable), portable.stem,
                max(0.0, _application_candidate_score(cleaned, portable.stem, str(portable), base=raw_score)),
            ))
    return finish(candidates)


def resolve_application_target(name: str) -> ResolvedTarget | None:
    ranked = application_candidates(name, limit=5)
    if not ranked:
        return None
    return ranked[0]

def resolve_basic_target(name: str, *, hint: str = "auto") -> ResolvedTarget | None:
    raw = str(name or "").strip().strip('"')
    cleaned = strip_target_words(raw)
    hint = str(hint or "auto").casefold()
    if hint == "game":
        hint = "app"
    if not cleaned:
        return None

    # Exact folder aliases are checked against the raw spoken target before the
    # generic word stripper.  "Program Files x86" contains the English word
    # "Program", which is normally stripped as an application-type filler; doing
    # that first made this canonical Windows folder impossible to resolve in auto
    # mode.
    raw_folder_key = normalize(raw)
    if hint in {"auto", "folder", "directory"}:
        for alias, alias_path in FOLDER_ALIASES.items():
            if normalize(alias) == raw_folder_key:
                folder = Path(alias_path)
                if folder.exists() and folder.is_dir():
                    return ResolvedTarget("path", str(folder.resolve(strict=False)), folder.name or alias, 1.0)

    candidate = Path(raw).expanduser()
    try:
        if candidate.exists():
            # Explicit type hints are hard constraints, not fuzzy suggestions.
            # ``app/game`` accepts only an EXE path; ``file`` only a file; and
            # ``folder`` only a directory. This prevents a literal existing
            # folder named like a game from stealing an executable request.
            if hint == "app":
                if candidate.is_file() and candidate.suffix.casefold() == ".exe":
                    return ResolvedTarget("app", str(candidate.resolve(strict=False)), candidate.stem, 1.0)
            elif hint == "file":
                if candidate.is_file():
                    return ResolvedTarget("path", str(candidate.resolve(strict=False)), candidate.name or str(candidate), 1.0)
            elif hint in {"folder", "directory"}:
                if candidate.is_dir():
                    return ResolvedTarget("path", str(candidate.resolve(strict=False)), candidate.name or str(candidate), 1.0)
            elif hint in {"auto", "web", "site"}:
                return ResolvedTarget("path", str(candidate.resolve(strict=False)), candidate.name or str(candidate), 1.0)
    except OSError:
        pass

    folder_match = _best_alias(cleaned, FOLDER_ALIASES, threshold=0.76)
    if folder_match and hint in {"auto", "folder", "directory"}:
        folder = Path(folder_match[1])
        if folder.exists():
            return ResolvedTarget("path", str(folder), folder.name or folder_match[0], folder_match[2])

    if hint in {"auto", "web", "site"}:
        if re.match(r"^[a-z][a-z0-9+.-]*://", raw, flags=re.I):
            return ResolvedTarget("url", raw, raw)
        if re.match(r"^[\w.-]+\.[a-z]{2,}(?:/\S*)?$", raw, flags=re.I):
            return ResolvedTarget("url", "https://" + raw, raw)

    site_match = _best_alias(cleaned, WEBSITE_ALIASES, threshold=0.69)
    if site_match and hint in {"web", "site"}:
        return ResolvedTarget("url", str(site_match[1]), site_match[0], site_match[2])
    if hint in {"web", "site"}:
        # An explicit "открой сайт X" must NEVER degrade into a local file/app
        # whose name merely looks similar. For unknown organizations/brands ask
        # Open a normal Google result page for the official site. Never auto-jump to an unverified domain.
        query = cleaned
        if not any(token in query for token in ("официаль", "official")):
            query += " официальный сайт"
        url = "https://www.google.com/search?q=" + quote_plus(query)
        return ResolvedTarget("url", url, cleaned, 0.80)

    # Explicit app/game requests are type-locked: they may resolve only to an
    # executable, a Windows StartApps entry, or a launchable shortcut. They must
    # never fall through into an arbitrary folder/file with a similar name.
    if hint == "app":
        app_target = resolve_application_target(cleaned)
        if app_target is not None:
            return app_target
        # Keep a settings URI only for an explicit known application alias.
        app_match = _best_alias(cleaned, APP_ALIASES, threshold=0.76)
        if app_match and str(app_match[1]).startswith("ms-settings:"):
            return ResolvedTarget("url", str(app_match[1]), app_match[0], app_match[2])
        return None

    if hint == "auto":
        app_target = resolve_application_target(cleaned)
        if app_target is not None and app_target.score >= 0.78:
            return app_target

    # Browser use is opt-in for spoken desktop commands. A service alias such as
    # “Яндекс Музыка” must not silently become a website when the user simply says
    # “открой …”. Only an explicit web/site hint (handled above) or a literal URL
    # may open the browser. Auto mode continues with local app/file resolution.

    path_kind = "any"
    if hint in {"folder", "directory"}:
        path_kind = "folder"
    elif hint == "file":
        path_kind = "file"
    if hint in {"folder", "directory", "file"}:
        path = find_user_path_exact(cleaned, kind=path_kind)
        if path:
            return ResolvedTarget("path", str(path), path.name, 1.0)
        return None
    # Bare ``открой X`` is application-first.  If no app exists, accept only an
    # exact local basename in auto mode; fuzzy filesystem matching is reserved for
    # an explicit ``папку``/``файл`` request.  This prevents commands such as
    # ``открой Steam`` from opening an unrelated README/sample file containing the
    # same token somewhere in its name.
    path = find_user_path_exact(cleaned, kind=path_kind)
    if path:
        return ResolvedTarget("path", str(path), path.name, 1.0)

    # Do not fall back to WEBSITE_ALIASES in auto mode. This is the hard guard
    # that keeps ordinary “открой <service>” commands application-first.
    return None
