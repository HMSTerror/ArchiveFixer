"""File naming and extraction decisions for disguised 7-Zip archives."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


VOLUME_RE = re.compile(r"\.(0[0-9]{3}|[0-9]{3})(?![0-9])")
ARCHIVE_SUFFIXES = frozenset({".7z", ".shan7z", ".7shanz", ".zip", ".rar", ".tar", ".gz", ".bz2", ".xz", ".cab", ".iso", ".wim"})
DISGUISED_SEVEN_ZIP_SUFFIXES = frozenset({".shan7z", ".7shanz"})


@dataclass(frozen=True)
class RenameDecision:
    source: Path
    target: Path
    status: str  # rename, unchanged, conflict
    detail: str


def volume_part(name: str) -> tuple[int, int] | None:
    """Return (last matching segment's end offset, part number)."""
    matches = [m for m in VOLUME_RE.finditer(name) if m.start() > 0 and int(m.group(1)) > 0]
    if not matches:
        return None
    match = matches[-1]
    return match.end(), int(match.group(1))


def target_name(name: str) -> str:
    """Keep volume numbering or normalize a disguised 7z suffix."""
    part = volume_part(name)
    if part:
        return name[: part[0]]
    suffix = Path(name).suffix
    if suffix.casefold() in DISGUISED_SEVEN_ZIP_SUFFIXES:
        return name[: -len(suffix)] + ".7z"
    existing_7z = re.search(r"(?i)\.7z(?=\.|$)", name)
    if existing_7z:
        return name[: existing_7z.end()]
    if suffix.casefold() == ".7z":
        return name
    if suffix:
        return name[: -len(suffix)] + ".7z"
    return name + ".7z"


def plan_renames(paths: list[Path]) -> list[RenameDecision]:
    targets = [path.with_name(target_name(path.name)) for path in paths]
    counts: dict[str, int] = {}
    for target in targets:
        key = str(target).casefold()
        counts[key] = counts.get(key, 0) + 1

    decisions = []
    for source, target in zip(paths, targets):
        if target == source:
            decisions.append(RenameDecision(source, target, "unchanged", "无需改名"))
        elif counts[str(target).casefold()] > 1:
            decisions.append(RenameDecision(source, target, "conflict", "多个文件会改成同一名称"))
        elif target.exists():
            decisions.append(RenameDecision(source, target, "conflict", "目标文件已存在"))
        else:
            decisions.append(RenameDecision(source, target, "rename", "待改名"))
    return decisions


def extraction_base(path: Path) -> str | None:
    """Return output-folder base for an extractable first volume or single archive."""
    name = path.name
    part = volume_part(name)
    if part and part[0] == len(name):
        if part[1] != 1:
            return None
        base = name[: name.rfind(".", 0, part[0])]
        suffix = Path(base).suffix
        if suffix.casefold() in ARCHIVE_SUFFIXES:
            base = base[: -len(suffix)]
        return base or None
    suffix = path.suffix
    if suffix.casefold() in ARCHIVE_SUFFIXES:
        return name[: -len(suffix)] or None
    return None
