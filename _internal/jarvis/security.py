# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import Any

from jarvis.config import ConfigManager


class Risk(IntEnum):
    SAFE = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return self.name.lower()


class SecurityPolicy:
    def __init__(self, config: ConfigManager):
        self.config = config

    def needs_confirmation(self, risk: Risk) -> bool:
        if risk >= Risk.HIGH:
            return True
        if risk == Risk.MEDIUM:
            return bool(self.config.get("security.confirm_medium_risk", True))
        return False

    def assert_mutation_path_allowed(self, path_value: str | Path) -> Path:
        path = Path(path_value).expanduser().resolve(strict=False)
        # Voice workflows should work in ordinary user folders on every drive
        # (D:\Important, game/mod folders, Downloads, etc.) without a permission
        # dialog. We still hard-block Windows/program/system locations so a speech
        # recognition mistake cannot rewrite the OS.
        if bool(self.config.get("security.full_user_file_access", True)):
            if self._is_protected_windows_path(path):
                raise PermissionError(f"Системный путь защищён от изменения: {path}")
            return path
        if not self.config.get("security.restrict_file_mutations", True):
            return path
        roots = [Path(item).resolve(strict=False) for item in self.config.get("security.allowed_roots", [])]
        for root in roots:
            try:
                path.relative_to(root)
                return path
            except ValueError:
                continue
        raise PermissionError(
            f"Изменение пути запрещено политикой безопасности: {path}. "
            "Добавьте папку в разрешённые корни в настройках."
        )

    @staticmethod
    def _is_protected_windows_path(path: Path) -> bool:
        import os
        if os.name != "nt":
            return False
        value = str(path).casefold().rstrip("\\/")
        protected = []
        for key in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            raw = os.environ.get(key, "")
            if raw:
                protected.append(str(Path(raw).resolve(strict=False)).casefold().rstrip("\\/"))
        # Windows creates these folders on arbitrary drive letters.
        tail = path.name.casefold()
        if tail in {"system volume information", "$recycle.bin"}:
            return True
        return any(value == base or value.startswith(base + "\\") for base in protected)

    @staticmethod
    def sanitize_tool_arguments(arguments: Any) -> dict[str, Any]:
        if arguments is None:
            return {}
        if not isinstance(arguments, dict):
            raise TypeError("Tool arguments must be an object")
        return arguments
