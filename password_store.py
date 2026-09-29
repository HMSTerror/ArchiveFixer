"""User-local storage for the five optional 7-Zip password presets."""

from __future__ import annotations

import json
import os
from pathlib import Path


def default_store_path() -> Path:
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return root / "ArchiveFixer" / "password-presets.json"


def load_password_presets(path: Path, default_password: str) -> list[str]:
    presets = [default_password, "", "", "", ""]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return presets
    values = data.get("passwords") if isinstance(data, dict) else None
    if isinstance(values, list) and len(values) == 5 and all(isinstance(value, str) for value in values):
        return values
    return presets


def save_password_presets(path: Path, passwords: list[str]) -> None:
    if len(passwords) != 5 or any(not isinstance(value, str) for value in passwords):
        raise ValueError("必须提供 5 个密码预设。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps({"passwords": passwords}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
