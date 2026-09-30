"""Read-only batch planning, shared by preview and extraction preflight."""

from dataclasses import dataclass
from pathlib import Path

from archive_engine import ExtractionError, ExtractionJob, _volume_group, discover_archive_inputs, is_linked_path, validate_volume_group
from archive_logic import RenameDecision, extraction_base, plan_renames, volume_part


@dataclass(frozen=True)
class PlanItem:
    source: Path
    destination: Path | None
    files: tuple[Path, ...]
    renames: tuple[RenameDecision, ...]
    input_bytes: int
    issues: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class BatchPlan:
    items: tuple[PlanItem, ...]
    jobs: tuple[ExtractionJob, ...]
    issues: tuple[str, ...]
    input_bytes: int


def plan_batch(
    inputs: list[Path], output_root: Path | None = None, *, rename: bool = False,
    target_suffix: str = ".7z", recursive: bool = True,
) -> BatchPlan:
    """Report known conflicts and volume gaps without running an extractor."""
    # Note: .agents/notes/implemented/feature/2026-09-30-batch-control-and-preview.md
    items, jobs, issues = [], [], []
    folders = [path for path in inputs if path.is_dir()]
    unique_files: dict[Path, int] = {}
    if output_root is not None and output_root.exists() and not output_root.is_dir():
        issues.append(f"目标目录不是文件夹：{output_root}")
    for index, folder in enumerate(folders):
        for other in folders[index + 1:]:
            left, right = folder.resolve(), other.resolve()
            if left.is_relative_to(right) or right.is_relative_to(left):
                issues.append(f"父子文件夹重复选择：{folder}；{other}")

    for source in inputs:
        problems, note, files, decisions, destination, size = [], "", (), (), None, 0
        try:
            if is_linked_path(source) or not source.exists():
                raise ExtractionError(f"来源不存在或是链接路径：{source}")
            folder_mode = source.is_dir()
            if not folder_mode and any(source.resolve().is_relative_to(folder.resolve()) for folder in folders):
                note = "已由所选文件夹任务包含"
            else:
                paths = discover_archive_inputs(source, recursive) if folder_mode else _volume_group(source)
                files = tuple(paths)
                for path in files:
                    file_size = path.stat().st_size
                    unique_files[path] = file_size
                    size += file_size
                    validate_volume_group(path)
                decisions = tuple(plan_renames(paths, target_suffix)) if rename else ()
                problems.extend(f"{decision.source.name}：{decision.detail}" for decision in decisions if decision.status == "conflict")
                targets = {decision.source: decision.target for decision in decisions}
                candidate = targets.get(source, source)
                if folder_mode:
                    if not files:
                        problems.append("文件夹内没有可处理的压缩包")
                    destination = source
                    jobs.append(ExtractionJob(source, source.name, True, tuple(targets.get(path, path) for path in files)))
                else:
                    base = extraction_base(candidate)
                    if base is None:
                        if volume_part(candidate.name):
                            note = "后续分卷由首卷一起处理"
                        else:
                            problems.append("后缀不可识别；请先改名或使用“改名并逐层解压”")
                    else:
                        destination = (output_root or source.parent) / f"{base}_解压"
                        jobs.append(ExtractionJob(candidate, base))
                        if destination.exists() or is_linked_path(destination):
                            problems.append(f"结果目录已存在，避免覆盖：{destination}")
        except (ExtractionError, OSError) as exc:
            problems.append(str(exc))
        items.append(PlanItem(source, destination, files, decisions, size, tuple(problems), note))
        issues.extend(f"{source.name}：{problem}" for problem in problems)

    destinations: dict[str, Path] = {}
    for item in items:
        if item.destination is None:
            continue
        key = str(item.destination.resolve()).casefold()
        if key in destinations:
            issues.append(f"多个任务会写入同一结果目录：{item.destination}")
        destinations[key] = item.source
        for folder in folders:
            if item.source != folder and item.destination.resolve().is_relative_to(folder.resolve()):
                issues.append(f"结果目录会写入另一个文件夹任务：{item.destination}")
    if not jobs and not issues:
        issues.append("没有可解压的首卷或压缩包")
    return BatchPlan(tuple(items), tuple(jobs), tuple(dict.fromkeys(issues)), sum(unique_files.values()))
