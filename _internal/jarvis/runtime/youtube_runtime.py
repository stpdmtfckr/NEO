# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""JavaScript runtime support for NEO JARVIS YouTube audio.

HOTFIX77+ keeps the runtime self-healing and independent from a specific hotfix
virtual-environment path.  A private Deno is stored under LOCALAPPDATA, and an
already installed system Deno/Node/QuickJS may be used as a fallback.  YouTube
commands can repair the runtime on demand instead of failing merely because the
launcher-side preparation was skipped or a previous runtime path was malformed.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from jarvis.runtime.hidden_subprocess import hidden_process_kwargs

DENO_VERSION = "2.8.1"
DENO_ASSET = "deno-x86_64-pc-windows-msvc.zip"
# Pinned digest of the official Deno v2.8.1 Windows x86_64 ZIP. GitHub release
# metadata exposes this digest directly; do not depend on downloading/parsing the
# auxiliary .sha256sum asset, which can be rewritten by proxies/CDNs as HTML.
DENO_ARCHIVE_SHA256 = "5fb5bac71f609fb91ec8960fb290885aadc27eeb22f07a8eca0c3db6be38b11a"
_GITHUB_BASE = f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}"
_DENO_CDN_BASE = f"https://dl.deno.land/release/v{DENO_VERSION}"
_SOURCEFORGE_BASE = f"https://sourceforge.net/projects/deno.mirror/files/v{DENO_VERSION}"
DENO_ARCHIVE_URLS = (
    f"{_GITHUB_BASE}/{DENO_ASSET}",
    f"{_DENO_CDN_BASE}/{DENO_ASSET}",
    f"{_SOURCEFORGE_BASE}/{DENO_ASSET}/download",
)


def managed_runtime_root() -> Path:
    import sys
    from jarvis.runtime.distribution164 import native_host
    if native_host():
        return Path(os.environ['NEO_RUNTIME_ROOT'])
    exe = Path(sys.executable).resolve()
    if exe.parent.name.casefold() == "scripts":
        return exe.parent.parent
    return exe.parent


def persistent_tools_root() -> Path:
    local = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    if local:
        return Path(local) / "NeoJarvis" / "tools"
    return managed_runtime_root() / "tools"


def local_deno_path() -> Path:
    return persistent_tools_root() / "deno" / "deno.exe"


def _legacy_deno_paths() -> tuple[Path, ...]:
    # HOTFIX74/75 kept Deno inside whichever venv was active. If such a runtime
    # already exists, adopt it rather than downloading the same binary again.
    candidates: list[Path] = [managed_runtime_root() / "tools" / "deno" / "deno.exe"]
    local = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    if local:
        runtime_parent = Path(local) / "NeoJarvis" / "runtime"
        try:
            for item in runtime_parent.glob("*/tools/deno/deno.exe"):
                candidates.append(item)
        except Exception:
            pass
    unique: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return tuple(unique)


def _run_version(path: str | os.PathLike[str], *, kind: str) -> tuple[int, int] | None:
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            **hidden_process_kwargs(),
        )
        if completed.returncode != 0:
            return None
        lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        if not lines:
            return None
        text = lines[0].casefold()
        if kind == "deno":
            if not text.startswith("deno "):
                return None
            raw = text.split(maxsplit=1)[1].lstrip("v")
        elif kind == "node":
            # node --version normally returns just v22.x.x
            raw = text.lstrip("v")
        elif kind == "quickjs":
            # qjs version formats differ; availability is enough because yt-dlp
            # performs its own exact compatibility check.
            return (999, 0)
        else:
            return None
        parts = raw.split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return None


def _deno_ok(path: str | os.PathLike[str]) -> bool:
    version = _run_version(path, kind="deno")
    return bool(version and version >= (2, 3))


def _node_ok(path: str | os.PathLike[str]) -> bool:
    version = _run_version(path, kind="node")
    return bool(version and version >= (22, 0))


def _quickjs_ok(path: str | os.PathLike[str]) -> bool:
    return _run_version(path, kind="quickjs") is not None


def _adopt_legacy_deno() -> str:
    target = local_deno_path()
    if target.is_file() and _deno_ok(target):
        return str(target)
    for candidate in _legacy_deno_paths():
        if candidate == target or not candidate.is_file() or not _deno_ok(candidate):
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, target)
            if _deno_ok(target):
                return str(target)
        except Exception:
            continue
    return ""


def find_js_runtime() -> tuple[str, str]:
    adopted = _adopt_legacy_deno()
    if adopted:
        return "deno", adopted

    found_deno = shutil.which("deno")
    if found_deno and _deno_ok(found_deno):
        return "deno", str(Path(found_deno).resolve())

    # yt-dlp currently supports Node >=22 and QuickJS as additional EJS runtimes.
    # These are fallbacks only; the private Deno remains the preferred contract.
    found_node = shutil.which("node")
    if found_node and _node_ok(found_node):
        return "node", str(Path(found_node).resolve())

    for name in ("qjs", "qjs.exe"):
        found_qjs = shutil.which(name)
        if found_qjs and _quickjs_ok(found_qjs):
            return "quickjs", str(Path(found_qjs).resolve())
    return "", ""


def find_deno() -> str:
    kind, path = find_js_runtime()
    return path if kind == "deno" else ""


def _download_bytes(url: str, *, timeout: float = 90.0) -> bytes:
    import requests
    response = requests.get(
        url,
        timeout=timeout,
        allow_redirects=True,
        headers={"User-Agent": f"NEO-JARVIS/{DENO_VERSION} Windows runtime bootstrap"},
    )
    response.raise_for_status()
    return bytes(response.content)


def _download_first(urls: tuple[str, ...], *, timeout: float) -> tuple[bytes, str]:
    errors: list[str] = []
    for url in urls:
        try:
            data = _download_bytes(url, timeout=timeout)
            if data:
                return data, url
            errors.append(f"{url}: empty response")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors[-3:]) or "download failed")


def ensure_deno(*, progress: bool = False) -> str:
    kind, existing = find_js_runtime()
    if kind == "deno" and existing:
        return existing
    if os.name != "nt":
        raise RuntimeError("Автоматическая установка Deno в этой сборке реализована только для Windows")

    target = local_deno_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        print(f"Preparing private Deno {DENO_VERSION} runtime for YouTube audio...")

    # HOTFIX77: use the immutable digest published in the official GitHub release
    # metadata.  HOTFIX76 downloaded the separate .sha256sum file first; on the
    # affected Windows connection that request returned a non-checksum payload, so
    # the installer rejected Deno before it ever downloaded the actual ZIP.
    expected = DENO_ARCHIVE_SHA256

    # Try every source until one yields the *exact* signed-release payload.  Do
    # not stop at the first HTTP 200: captive portals, filtering proxies and
    # anti-bot pages can return HTML with a successful status code.
    archive = b""
    archive_source = ""
    source_errors: list[str] = []
    for candidate_url in DENO_ARCHIVE_URLS:
        try:
            candidate = _download_bytes(candidate_url, timeout=220.0)
            if not candidate.startswith(b"PK"):
                preview = candidate[:120].decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")
                source_errors.append(f"{candidate_url}: not a ZIP ({preview[:80]})")
                continue
            actual = hashlib.sha256(candidate).hexdigest().casefold()
            if actual != expected:
                source_errors.append(f"{candidate_url}: SHA-256 {actual}")
                continue
            archive = candidate
            archive_source = candidate_url
            break
        except Exception as exc:
            source_errors.append(f"{candidate_url}: {exc}")

    if not archive:
        details = "; ".join(source_errors[-3:])
        raise RuntimeError(
            "Не удалось получить проверенный Deno 2.8.1. "
            f"Ожидался SHA-256 {expected}. {details}"
        )


    with tempfile.TemporaryDirectory(prefix="neo_jarvis_deno_") as td:
        zip_path = Path(td) / DENO_ASSET
        zip_path.write_bytes(archive)
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = {Path(name).name.casefold(): name for name in zf.namelist()}
            member = names.get("deno.exe")
            if not member:
                raise RuntimeError("В официальном архиве Deno не найден deno.exe")
            payload = zf.read(member)
        temp_target = target.with_suffix(".new.exe")
        temp_target.write_bytes(payload)
        os.replace(temp_target, target)

    if not _deno_ok(target):
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError("Установленный Deno не прошёл проверку запуска")
    if progress:
        print(f"Deno ready: {target}")
    return str(target)


def ensure_js_runtime(*, progress: bool = False) -> tuple[str, str]:
    kind, path = find_js_runtime()
    if kind and path:
        return kind, path
    # Prefer a private Deno. If its download is impossible but a supported system
    # runtime appears during the attempt, fall back to it before surfacing failure.
    try:
        deno = ensure_deno(progress=progress)
        return "deno", deno
    except Exception as exc:
        kind, path = find_js_runtime()
        if kind and path:
            return kind, path
        raise RuntimeError(str(exc)) from exc
