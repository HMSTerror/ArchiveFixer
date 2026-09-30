"""Expand nested archives with password presets and commit each result safely."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Sequence
from uuid import uuid4

from archive_extractors import EXTRACTOR_LABELS, extraction_command, output_encoding
from archive_logic import volume_part


ARCHIVE_HINT = re.compile(r"(?i)\.(?:7z|shan7z|7shanz|7zshan|zip|rar|tar|gz|bz2|xz|cab|iso|wim)(?=\.|$)")
TERMINAL_CONTAINER_SUFFIXES = frozenset({
    ".apk", ".aab", ".ipa", ".jar", ".war", ".ear",
    ".docx", ".xlsx", ".pptx", ".epub", ".odt", ".ods", ".odp",
    ".save", ".sav",
})
SAVE_PATH_RE = re.compile(r"(?i)(?:save|backup|autosave|存档|备份)")
MAX_LAYERS = 32


class ExtractionError(Exception):
    """Extraction did not finish; no archive cleanup has started."""


class CleanupError(Exception):
    """All layers extracted, but archive cleanup was incomplete."""


@dataclass(frozen=True)
class ArchiveSnapshot:
    path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class Layer:
    source: Path
    destination: Path
    number: int
    archive_files: tuple[ArchiveSnapshot, ...]


@dataclass(frozen=True)
class BatchResult:
    outer_archives: int
    layers: int
    final_files: int
    deleted_archives: int
    successful_jobs: int = 0
    failed_jobs: int = 0
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractionJob:
    source: Path
    base: str
    folder_mode: bool = False
    archives: tuple[Path, ...] = ()


@dataclass(frozen=True)
class BranchResult:
    outer_destination: Path
    layers: tuple[Layer, ...]


@dataclass(frozen=True)
class JobResult:
    source: Path
    base: str
    parent: Path
    workdir: Path
    folder_mode: bool
    branches: tuple[BranchResult, ...]

    @property
    def layers(self) -> tuple[Layer, ...]:
        return tuple(layer for branch in self.branches for layer in branch.layers)


def _has_archive_signature(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            header = stream.read(512)
    except OSError as exc:
        if not path.exists():
            raise ExtractionError(
                f"文件在扫描期间消失或变得不可访问：{path}。原包已保留；请检查安全软件隔离记录。"
            ) from exc
        raise ExtractionError(f"无法读取文件：{path}：{exc}") from exc
    return (
        header.startswith(b"7z\xbc\xaf\x27\x1c")
        or header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
        or header.startswith(b"Rar!\x1a\x07")
        or header.startswith(b"\x1f\x8b")
        or header.startswith(b"BZh")
        or header.startswith(b"\xfd7zXZ\x00")
        or header.startswith(b"MSCF")
        or header.startswith(b"MSWIM\x00\x00\x00")
        or header[257:262] == b"ustar"
    )


def _walk_error(error: OSError) -> None:
    raise ExtractionError(f"无法完整扫描解压目录：{error}") from error


def is_linked_path(path: Path) -> bool:
    """Treat Windows junctions like symbolic links when scanning user data."""
    return path.is_symlink() or path.is_junction()


def _is_terminal_container(path: Path) -> bool:
    return path.suffix.casefold() in TERMINAL_CONTAINER_SUFFIXES and not ARCHIVE_HINT.search(path.name)


def _is_save_archive_path(path: Path, scan_root: Path) -> bool:
    relative = path.relative_to(scan_root)
    directory_marker = any(SAVE_PATH_RE.search(part) for part in relative.parts[:-1])
    file_marker = bool(SAVE_PATH_RE.search(path.stem))
    return directory_marker or file_marker


def _has_direct_launcher(folder: Path) -> bool:
    try:
        return any(
            item.is_file() and item.suffix.casefold() in {".exe", ".apk", ".bat", ".cmd", ".sh", ".html"}
            for item in folder.iterdir()
        )
    except OSError as exc:
        raise ExtractionError(f"无法检查最终文件夹：{folder}：{exc}") from exc


def discover_archive_inputs(folder: Path, recursive: bool = True) -> list[Path]:
    """Find likely archives in a folder without changing any names or files.

    """
    if is_linked_path(folder):
        raise ExtractionError(f"拒绝扫描链接目录：{folder}")
    if recursive:
        try:
            direct_files = [path for path in folder.iterdir() if path.is_file() and not is_linked_path(path)]
        except OSError as exc:
            raise ExtractionError(f"无法扫描文件夹：{folder}：{exc}") from exc
        has_launcher = any(path.suffix.casefold() == ".exe" for path in direct_files)
        has_direct_archive = any(
            path.suffix.casefold() in {".7z", ".rar", ".shan7z", ".7shanz", ".7zshan"}
            or ((part := volume_part(path.name)) is not None and part[1] == 1)
            for path in direct_files
        )
        if has_launcher and not has_direct_archive:
            return []
        files = []
        for root, dirs, names in os.walk(folder, onerror=_walk_error):
            dirs[:] = [name for name in dirs if not is_linked_path(Path(root) / name)]
            files.extend(Path(root) / name for name in names)
    else:
        try:
            files = list(folder.iterdir())
        except OSError as exc:
            raise ExtractionError(f"无法扫描文件夹：{folder}：{exc}") from exc
    files = [path for path in files if not is_linked_path(path) and path.is_file()]
    found: set[Path] = set()
    for path in files:
        if _is_terminal_container(path) or _is_save_archive_path(path, folder):
            continue
        if _has_archive_signature(path) or ARCHIVE_HINT.search(path.name):
            found.add(path)
    for path in tuple(found):
        part = volume_part(path.name)
        if part and part[1] == 1:
            found.update(_volume_group(path))
    return sorted(found, key=lambda path: str(path).casefold())


def expand_dropped_folder(folder: Path, recursive: bool = True) -> list[Path]:
    """Treat a mixed parent as separate archive files and folder jobs.

    """
    direct_archives = discover_archive_inputs(folder, recursive=False)
    child_folders = []
    try:
        children = list(folder.iterdir())
    except OSError as exc:
        raise ExtractionError(f"无法读取文件夹：{folder}：{exc}") from exc
    for child in children:
        if child.is_dir() and not is_linked_path(child) and discover_archive_inputs(child, recursive=recursive):
            child_folders.append(child)
    if direct_archives and child_folders:
        return sorted(direct_archives + child_folders, key=lambda path: str(path).casefold())
    return [folder] if discover_archive_inputs(folder, recursive=recursive) else []


def flatten_existing_result(root: Path) -> tuple[Path, int]:
    """Collapse a previous run's sole archive-wrapper chain into its result folder.

    """
    if is_linked_path(root) or not root.is_dir():
        raise ExtractionError("请选择已有的解压结果文件夹。")
    chain = [root]
    current = root
    while True:
        try:
            entries = list(current.iterdir())
        except OSError as exc:
            raise ExtractionError(f"无法读取目录：{current}：{exc}") from exc
        if len(entries) != 1 or is_linked_path(entries[0]) or not entries[0].is_dir():
            break
        child = entries[0]
        archive_base = current.name[:-3] if current.name.endswith("_解压") else ""
        if not (child.name.endswith("_解压") or child.name.casefold() == archive_base.casefold() or _has_direct_launcher(child)):
            break
        chain.append(child)
        current = child
    if len(chain) == 1:
        raise ExtractionError("这个结果目录没有可去除的压缩层外壳。")

    try:
        resolved_root = root.resolve(strict=True)
        resolved_parent = root.parent.resolve(strict=True)
        resolved_leaf = current.resolve(strict=True)
    except OSError as exc:
        raise ExtractionError(f"无法核对现有目录路径：{exc}") from exc
    if resolved_root.parent != resolved_parent or not resolved_leaf.is_relative_to(resolved_root):
        raise ExtractionError("目录不在预期范围内，已停止整理。")
    stage = resolved_parent / f".archive_flat_stage_{uuid4().hex}"
    backup = resolved_parent / f".archive_flat_backup_{uuid4().hex}"
    if stage.exists() or backup.exists():
        raise ExtractionError("整理暂存路径已存在，请重试。")
    try:
        current.rename(stage)
    except OSError as exc:
        raise ExtractionError(f"无法暂存最终内容：{current}：{exc}") from exc
    try:
        root.rename(backup)
    except OSError as exc:
        try:
            stage.rename(current)
        except OSError:
            pass
        raise ExtractionError(f"无法暂存旧目录：{root}：{exc}") from exc
    try:
        stage.rename(root)
    except OSError as exc:
        try:
            backup.rename(root)
        except OSError:
            pass
        raise ExtractionError(f"无法生成单层结果目录；暂存位置为 {stage}：{exc}") from exc

    try:
        resolved_backup = backup.resolve(strict=True)
    except OSError as exc:
        raise CleanupError(f"结果已整理，但无法核对旧目录：{backup}：{exc}") from exc
    if is_linked_path(backup) or resolved_backup.parent != resolved_parent or not backup.name.startswith(".archive_flat_backup_"):
        raise CleanupError(f"结果已整理，但拒绝清理非预期旧目录：{backup}")
    for folder, dirs, files in os.walk(backup, onerror=_walk_error):
        if files or any(is_linked_path(Path(folder) / name) for name in dirs):
            raise CleanupError(f"结果已整理，但旧目录中出现文件，已保留：{backup}")
    try:
        shutil.rmtree(backup)
    except OSError as exc:
        raise CleanupError(f"结果已整理，但清理空目录失败：{backup}：{exc}") from exc
    return root, len(chain) - 1


def _volume_prefix(path: Path) -> str | None:
    part = volume_part(path.name)
    return path.name[: path.name.rfind(".", 0, part[0])] if part else None


def _volume_width(path: Path) -> int | None:
    part = volume_part(path.name)
    return part[0] - path.name.rfind(".", 0, part[0]) - 1 if part else None


def _volume_group(first: Path) -> list[Path]:
    prefix = _volume_prefix(first)
    if prefix is None:
        return [first]
    width = _volume_width(first)
    group = []
    for sibling in first.parent.iterdir():
        sibling_prefix = _volume_prefix(sibling)
        if (
            is_linked_path(sibling)
            or not sibling.is_file()
            or sibling_prefix is None
            or sibling_prefix.casefold() != prefix.casefold()
            or _volume_width(sibling) != width
        ):
            continue
        group.append(sibling)
    return sorted(group, key=lambda path: volume_part(path.name)[1])


def _normalize_volume_group(first: Path) -> Path:
    group = _volume_group(first)
    targets = [path.with_name(path.name[: volume_part(path.name)[0]]) for path in group]
    folded = [str(path).casefold() for path in targets]
    if len(folded) != len(set(folded)):
        raise ExtractionError(f"分卷改名后出现重名：{first.parent}")
    for source, target in zip(group, targets):
        if source != target and target.exists():
            raise ExtractionError(f"分卷目标文件已存在：{target}")
    renamed: list[tuple[Path, Path]] = []
    for source, target in zip(group, targets):
        if source == target:
            continue
        try:
            source.rename(target)
            renamed.append((source, target))
        except OSError as exc:
            rollback_errors = []
            for original, changed in reversed(renamed):
                try:
                    changed.rename(original)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{changed}：{rollback_exc}")
            if rollback_errors:
                raise ExtractionError(
                    f"分卷改名失败：{source}：{exc}；回滚未全部成功：{'；'.join(rollback_errors)}"
                ) from exc
            raise ExtractionError(f"分卷改名失败，已回滚：{source}：{exc}") from exc
    return first.with_name(first.name[: volume_part(first.name)[0]])


def _archive_base(path: Path) -> str:
    name = path.name
    part = volume_part(name)
    if part and part[0] == len(name):
        name = name[: name.rfind(".", 0, part[0])]
    suffix = Path(name).suffix
    if suffix and ARCHIVE_HINT.fullmatch(suffix):
        name = name[: -len(suffix)]
    elif suffix:
        name = name[: -len(suffix)]
    return name or "archive"


def _archive_files(path: Path) -> tuple[ArchiveSnapshot, ...]:
    part = volume_part(path.name)
    paths = _volume_group(path) if part and part[0] == len(path.name) and part[1] == 1 else [path]
    snapshots = []
    for item in paths:
        try:
            stat = item.stat()
        except OSError as exc:
            raise ExtractionError(f"分卷文件不可用：{item}：{exc}") from exc
        snapshots.append(ArchiveSnapshot(item, stat.st_size, stat.st_mtime_ns))
    return tuple(snapshots)


def _nested_candidates(destination: Path, log: Callable[[str], None] | None = None) -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(destination, onerror=_walk_error):
        dirs[:] = [name for name in dirs if not is_linked_path(Path(root) / name)]
        files.extend(Path(root) / name for name in names if not is_linked_path(Path(root) / name))
    files.sort(key=lambda path: str(path).casefold())
    if any(path.suffix.casefold() == ".exe" for path in files):
        if log:
            log("本层已包含可执行程序，保留其中的内嵌压缩文件。")
        return []
    save_count = sum(1 for path in files if _is_save_archive_path(path, destination))
    if save_count and log:
        log(f"识别并保留 {save_count} 个存档/备份压缩文件。")

    preliminary = []
    for path in files:
        if _is_terminal_container(path) or _is_save_archive_path(path, destination):
            continue
        part = volume_part(path.name)
        if part and part[1] != 1:
            continue
        if _has_archive_signature(path) or ARCHIVE_HINT.search(path.name):
            preliminary.append(path)
    archive_set = set(preliminary)
    for path in preliminary:
        part = volume_part(path.name)
        if part and part[1] == 1:
            archive_set.update(_volume_group(path))
    other_files = [path for path in files if path not in archive_set]
    if preliminary and other_files:
        note_files = [
            path for path in other_files
            if not _is_terminal_container(path) and not _is_save_archive_path(path, destination)
        ]
        safe_sidecars = all(
            path.suffix.casefold() in {".txt", ".nfo", ".url"} and path.stat().st_size <= 1_000_000
            for path in note_files
        )
        near_root = all(len(path.relative_to(destination).parts) <= 2 for path in preliminary)
        if not safe_sidecars or len(note_files) > 2 or not near_root:
            if log:
                log(f"本层已有其他最终内容，保留 {len(preliminary)} 个内嵌压缩文件。")
            return []

    candidates = []
    for path in files:
        if _is_terminal_container(path) or _is_save_archive_path(path, destination):
            continue
        part = volume_part(path.name)
        if part and part[1] != 1:
            prefix = _volume_prefix(path)
            if ARCHIVE_HINT.search(prefix or ""):
                siblings = _volume_group(path)
                if not any(volume_part(sibling.name)[1] == 1 for sibling in siblings):
                    first_label = ".0001" if _volume_width(path) == 4 else ".001"
                    raise ExtractionError(f"发现缺少 {first_label} 首卷的分卷：{path}")
            continue
        signature = _has_archive_signature(path)
        hinted = bool(ARCHIVE_HINT.search(path.name))
        if not signature and not hinted:
            continue
        if part and part[1] == 1:
            path = _normalize_volume_group(path)
        candidates.append(path)
    return candidates


class ExtractionEngine:
    # Note: commit/cleanup and vendor errors — .agents/notes/implemented/bug-fix/2026-09-30-result-commit-and-vendor-errors.md
    def __init__(
        self,
        executable: Path,
        passwords: str | Sequence[str],
        log: Callable[[str], None],
        backend: str = "7zip",
    ) -> None:
        if backend not in EXTRACTOR_LABELS:
            raise ValueError(f"不支持的解压程序：{backend}")
        self.executable = executable
        self.backend = backend
        supplied = [passwords] if isinstance(passwords, str) else list(passwords)
        self.passwords = tuple(supplied) if any(supplied) else ("",)
        self.log = log
        self.layers: list[Layer] = []

    def run(
        self,
        jobs: list[tuple[Path, str] | ExtractionJob],
        output_root: Path | None,
        delete_archives: bool,
        max_concurrent: int = 2,
    ) -> BatchResult:
        if not jobs:
            raise ExtractionError("没有可解压的文件。")
        if not 1 <= max_concurrent <= 8:
            raise ExtractionError("并行任务数应为 1 到 8。")
        specs = [job if isinstance(job, ExtractionJob) else ExtractionJob(job[0], job[1]) for job in jobs]
        final_paths = [
            spec.source if spec.folder_mode else (output_root or spec.source.parent) / f"{spec.base}_解压"
            for spec in specs
        ]
        if len({str(path).casefold() for path in final_paths}) != len(final_paths):
            raise ExtractionError("多个压缩包会输出到同一个文件夹；请调整选择或输出目录。")
        for spec, path in zip(specs, final_paths):
            if is_linked_path(spec.source):
                raise ExtractionError(f"拒绝处理链接路径：{spec.source}")
            if spec.folder_mode and not path.is_dir():
                raise ExtractionError(f"待处理文件夹不存在：{path}")
            if spec.folder_mode:
                root = spec.source.resolve(strict=True)
                for archive in spec.archives:
                    if not archive.resolve().is_relative_to(root):
                        raise ExtractionError(f"压缩包不在所选文件夹内：{archive}")
                    if is_linked_path(archive) or any(
                        is_linked_path(parent) for parent in archive.parents
                        if parent.is_relative_to(spec.source)
                    ):
                        raise ExtractionError(f"拒绝处理链接中的压缩包：{archive}")
            if not spec.folder_mode and (path.exists() or is_linked_path(path)):
                raise ExtractionError(f"最终输出目录已存在，避免覆盖：{path}")
        for folder_spec in (spec for spec in specs if spec.folder_mode):
            for file_spec in (spec for spec in specs if not spec.folder_mode):
                if file_spec.source.is_relative_to(folder_spec.source):
                    raise ExtractionError(f"同一压缩包同时被单独选择和文件夹任务包含：{file_spec.source}")
        folder_specs = [spec for spec in specs if spec.folder_mode]
        for index, folder_spec in enumerate(folder_specs):
            for other in folder_specs[index + 1:]:
                if folder_spec.source.is_relative_to(other.source) or other.source.is_relative_to(folder_spec.source):
                    raise ExtractionError(f"嵌套文件夹被重复选择：{folder_spec.source}；{other.source}")
        self.layers = []
        workdirs: dict[Path, Path] = {}

        def process_one(spec: ExtractionJob) -> JobResult:
            worker = ExtractionEngine(self.executable, self.passwords, self.log, self.backend)
            parent = spec.source.parent if spec.folder_mode else (output_root or spec.source.parent)
            try:
                parent.mkdir(parents=True, exist_ok=True)
                workdir = Path(tempfile.mkdtemp(prefix=".archive_work_", dir=parent))
                workdirs[spec.source] = workdir
            except OSError as exc:
                raise ExtractionError(f"无法创建解压工作区：{parent}：{exc}") from exc
            branches: list[BranchResult] = []
            if spec.folder_mode:
                for archive in spec.archives:
                    part = volume_part(archive.name)
                    if part and part[1] != 1:
                        prefix = _volume_prefix(archive)
                        if ARCHIVE_HINT.search(prefix or "") and not any(
                            volume_part(sibling.name)[1] == 1 for sibling in _volume_group(archive)
                        ):
                            first_label = ".0001" if _volume_width(archive) == 4 else ".001"
                            raise ExtractionError(f"文件夹中缺少 {first_label} 首卷：{archive}")
                        continue
                    if part and part[1] == 1:
                        archive = _normalize_volume_group(archive)
                    branch_parent = workdir / f"branch_{len(branches) + 1:03d}"
                    branch_parent.mkdir()
                    before = len(worker.layers)
                    destination = worker._expand(archive, _archive_base(archive), branch_parent, 1)
                    branches.append(BranchResult(destination, tuple(worker.layers[before:])))
                if not branches:
                    raise ExtractionError(f"文件夹内没有可解压的首卷：{spec.source}")
            else:
                destination = worker._expand(spec.source, spec.base, workdir, 1)
                branches.append(BranchResult(destination, tuple(worker.layers)))
            return JobResult(spec.source, spec.base, parent, workdir, spec.folder_mode, tuple(branches))

        errors = []
        results: list[JobResult] = []
        with ThreadPoolExecutor(max_workers=min(max_concurrent, len(jobs))) as pool:
            futures = {pool.submit(process_one, spec): spec.source for spec in specs}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    errors.append(f"{source.name}：{exc}")
                    workdir = workdirs.get(source)
                    if workdir is not None and workdir.exists():
                        self.log(f"失败任务的工作目录已保留，可检查后手动清理：{workdir}")
                else:
                    results.append(result)

        completed: list[JobResult] = []
        warnings: list[str] = []
        final_files = 0
        deleted = 0
        for result in results:
            stage: Path | None = None
            try:
                stage = Path(tempfile.mkdtemp(prefix=".archive_stage_", dir=result.parent))
                staged_files = self._stage_final_content(result, stage)
                if delete_archives:
                    self._verify_archive_snapshots(result.layers)
                if result.folder_mode:
                    self._check_folder_stage_merge(stage, result.source)
                final_path = result.source if result.folder_mode else result.parent / f"{result.base}_解压"
                if result.folder_mode:
                    self._merge_stage_into_folder(stage, final_path, result.parent)
                else:
                    self._rename_owned_directory(stage, final_path, result.parent)
                self.log(f"最终内容已整理到：{final_path}")
            except Exception as exc:
                errors.append(f"{result.source.name}：{exc}")
                for temporary in (result.workdir, stage):
                    if temporary is not None and temporary.exists():
                        self.log(f"任务未完整完成，暂存目录已保留供检查：{temporary}")
            else:
                completed.append(result)
                final_files += staged_files
                # Once the result is committed, a cleanup failure cannot undo it.
                if result.folder_mode:
                    try:
                        stage.rmdir()
                    except OSError as exc:
                        warning = f"{result.source.name}：最终内容已交付，但空暂存目录未清理：{stage}：{exc}"
                        warnings.append(warning)
                        self.log(warning)
                try:
                    deleted += self._cleanup_workdirs([result], delete_archives)
                except Exception as exc:
                    warning = f"{result.source.name}：最终内容已交付，但压缩层清理未完成：{exc}"
                    warnings.append(warning)
                    self.log(warning)

        self.layers = [layer for result in completed for layer in result.layers]
        return BatchResult(
            len(jobs), len(self.layers), final_files, deleted,
            len(completed), len(errors), tuple(errors), tuple(warnings),
        )

    @staticmethod
    def _mapped_relative(path: Path, root: Path, wrappers: set[Path]) -> Path:
        current = root
        retained = []
        for part in path.relative_to(root).parts:
            current = current / part
            if current not in wrappers:
                retained.append(part)
        return Path(*retained)

    def _stage_final_content(self, result: JobResult, stage: Path) -> int:
        payload: list[tuple[Path, Path, int]] = []
        empty_dirs: list[tuple[Path, int]] = []
        for branch_index, branch in enumerate(result.branches):
            root = branch.outer_destination
            archive_files = {snapshot.path for layer in branch.layers for snapshot in layer.archive_files}
            wrappers = {layer.destination for layer in branch.layers if layer.destination != root}
            for layer in branch.layers:
                child = layer.destination / _archive_base(layer.source)
                try:
                    entries = list(layer.destination.iterdir())
                except OSError as exc:
                    raise ExtractionError(f"无法检查解压目录：{layer.destination}：{exc}") from exc
                if len(entries) == 1 and entries[0].is_dir() and not is_linked_path(entries[0]):
                    sole = entries[0]
                    if sole == child:
                        wrappers.add(sole)
                    elif _has_direct_launcher(sole):
                        wrappers.add(sole)
            for folder, dirs, files in os.walk(root, onerror=_walk_error):
                folder_path = Path(folder)
                linked_dirs = [name for name in dirs if is_linked_path(folder_path / name)]
                dirs[:] = [name for name in dirs if name not in linked_dirs]
                for name in files + linked_dirs:
                    source = folder_path / name
                    if source in archive_files:
                        continue
                    relative = self._mapped_relative(source, root, wrappers)
                    payload.append((source, relative, branch_index))
                if not dirs and not files and not linked_dirs and folder_path not in wrappers and folder_path != root:
                    empty_dirs.append((self._mapped_relative(folder_path, root, wrappers), branch_index))
        if not payload:
            raise ExtractionError(f"未找到最终文件，原始压缩包已保留：{result.source}")

        folded = [str(relative).casefold() for _, relative, _ in payload]
        if len(folded) != len(set(folded)):
            if not result.folder_mode or len(result.branches) < 2:
                raise ExtractionError(f"整理后的文件名发生冲突，原始压缩包已保留：{result.source}")
            labels = []
            seen_labels = set()
            for branch in result.branches:
                base = _archive_base(branch.layers[0].source)
                label = base
                number = 2
                while label.casefold() in seen_labels:
                    label = f"{base}_{number}"
                    number += 1
                seen_labels.add(label.casefold())
                labels.append(label)
            payload = [(source, Path(labels[index]) / relative, index) for source, relative, index in payload]
            empty_dirs = [(Path(labels[index]) / relative, index) for relative, index in empty_dirs]
            folded = [str(relative).casefold() for _, relative, _ in payload]
            self.log(f"多个压缩包的最终文件同名，分别放入子文件夹：{', '.join(labels)}")
            if len(folded) != len(set(folded)):
                raise ExtractionError(f"同一压缩包内部仍有同名文件：{result.source}")
        file_keys = set(folded)
        for _, relative, _ in payload:
            if any(str(parent).casefold() in file_keys for parent in relative.parents if parent != Path(".")):
                raise ExtractionError(f"整理后的文件与目录发生冲突：{relative}")

        for relative, _ in empty_dirs:
            if relative != Path(".") and str(relative).casefold() not in file_keys:
                (stage / relative).mkdir(parents=True, exist_ok=True)
        for source, relative, _ in payload:
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or is_linked_path(destination):
                raise ExtractionError(f"整理后的目标已存在：{destination}")
            try:
                source.replace(destination)
            except OSError as exc:
                raise ExtractionError(f"整理最终文件失败：{source}：{exc}") from exc
        self.log(f"整理完成：{result.source.name}，最终文件 {len(payload)} 个。")
        return len(payload)

    @staticmethod
    def _verify_archive_snapshots(layers: list[Layer] | tuple[Layer, ...]) -> None:
        snapshots = {snapshot.path: snapshot for layer in layers for snapshot in layer.archive_files}
        for snapshot in snapshots.values():
            try:
                stat = snapshot.path.stat()
            except OSError as exc:
                raise ExtractionError(f"压缩包已变化，停止整理：{snapshot.path}：{exc}") from exc
            if is_linked_path(snapshot.path) or (stat.st_size, stat.st_mtime_ns) != (snapshot.size, snapshot.mtime_ns):
                raise ExtractionError(f"压缩包在处理期间发生变化，停止整理：{snapshot.path}")

    @staticmethod
    def _rename_owned_directory(source: Path, destination: Path, parent: Path) -> None:
        try:
            resolved_parent = parent.resolve(strict=True)
            resolved_source = source.resolve(strict=True)
            resolved_target_parent = destination.parent.resolve(strict=True)
        except OSError as exc:
            raise ExtractionError(f"无法核对整理路径：{exc}") from exc
        if is_linked_path(source) or resolved_source.parent != resolved_parent or resolved_target_parent != resolved_parent:
            raise ExtractionError(f"整理路径不在预期位置：{source}")
        if destination.exists():
            raise ExtractionError(f"最终输出目录已存在，避免覆盖：{destination}")
        try:
            source.rename(destination)
        except OSError as exc:
            raise ExtractionError(f"无法生成最终文件夹：{destination}：{exc}") from exc

    @staticmethod
    def _check_folder_stage_merge(stage: Path, folder: Path) -> None:
        try:
            resolved_parent = folder.parent.resolve(strict=True)
            resolved_stage = stage.resolve(strict=True)
            resolved_folder = folder.resolve(strict=True)
        except OSError as exc:
            raise ExtractionError(f"无法核对文件夹整理路径：{exc}") from exc
        if (
            is_linked_path(stage)
            or is_linked_path(folder)
            or resolved_stage.parent != resolved_parent
            or resolved_folder.parent != resolved_parent
            or not stage.name.startswith(".archive_stage_")
        ):
            raise ExtractionError(f"文件夹整理路径不在预期位置：{folder}")
        for entry in stage.iterdir():
            target = folder / entry.name
            if target.exists() or is_linked_path(target):
                raise ExtractionError(f"文件夹中已有同名内容，避免覆盖：{target}")

    @classmethod
    def _merge_stage_into_folder(cls, stage: Path, folder: Path, parent: Path) -> None:
        if folder.parent.resolve(strict=True) != parent.resolve(strict=True):
            raise ExtractionError(f"文件夹不在预期输出位置：{folder}")
        cls._check_folder_stage_merge(stage, folder)
        moved: list[tuple[Path, Path]] = []
        for entry in list(stage.iterdir()):
            target = folder / entry.name
            try:
                entry.rename(target)
                moved.append((entry, target))
            except OSError as exc:
                rollback_errors = []
                for original, changed in reversed(moved):
                    try:
                        changed.rename(original)
                    except OSError as rollback_exc:
                        rollback_errors.append(f"{changed}：{rollback_exc}")
                detail = f"回滚未全部成功：{'；'.join(rollback_errors)}" if rollback_errors else "已回滚"
                raise ExtractionError(f"无法将最终内容放入原文件夹，{detail}：{target}：{exc}") from exc

    @staticmethod
    def _remove_owned_workdir(workdir: Path, parent: Path) -> None:
        try:
            resolved_parent = parent.resolve(strict=True)
            resolved_workdir = workdir.resolve(strict=True)
        except OSError as exc:
            raise CleanupError(f"无法核对工作区路径：{workdir}：{exc}") from exc
        if (
            is_linked_path(workdir)
            or resolved_workdir.parent != resolved_parent
            or not workdir.name.startswith(".archive_work_")
        ):
            raise CleanupError(f"拒绝清理非本次创建的工作区：{workdir}")

        def clear_readonly(function: Callable[[str], None], failed_path: str, error: BaseException) -> None:
            target = Path(failed_path)
            if not isinstance(error, PermissionError):
                raise error
            resolved_target = target.resolve(strict=True)
            if is_linked_path(target) or not resolved_target.is_relative_to(resolved_workdir):
                raise error
            os.chmod(target, stat.S_IWRITE)
            function(failed_path)

        try:
            shutil.rmtree(workdir, onexc=clear_readonly)
        except OSError as exc:
            raise CleanupError(f"无法清理临时压缩层：{workdir}：{exc}") from exc

    def _cleanup_workdirs(self, results: list[JobResult], delete_archives: bool) -> int:
        if not delete_archives:
            for result in results:
                backup = result.parent / f"{result.base}_压缩层备份"
                counter = 2
                while backup.exists():
                    backup = result.parent / f"{result.base}_压缩层备份_{counter}"
                    counter += 1
                self._rename_owned_directory(result.workdir, backup, result.parent)
                self.log(f"压缩层已保留在：{backup}")
            return 0

        deleted = len({
            snapshot.path
            for result in results
            for layer in result.layers
            for snapshot in layer.archive_files
        })
        for result in results:
            self._remove_owned_workdir(result.workdir, result.parent)
        root_archives = {
            snapshot.path: snapshot
            for result in results
            for branch in result.branches
            for snapshot in branch.layers[0].archive_files
        }
        for snapshot in root_archives.values():
            try:
                stat = snapshot.path.stat()
            except OSError as exc:
                raise CleanupError(f"原始压缩包不可用：{snapshot.path}：{exc}") from exc
            if (stat.st_size, stat.st_mtime_ns) != (snapshot.size, snapshot.mtime_ns):
                raise CleanupError(f"原始压缩包在整理期间发生变化：{snapshot.path}")
            try:
                snapshot.path.unlink()
            except OSError as exc:
                raise CleanupError(f"最终内容已保留，但删除原始压缩包失败：{snapshot.path}：{exc}") from exc
            self.log(f"已删除原始压缩包：{snapshot.path}")
        for result in results:
            if not result.folder_mode:
                continue
            for branch in result.branches:
                for snapshot in branch.layers[0].archive_files:
                    parent = snapshot.path.parent
                    while parent != result.source and parent.is_relative_to(result.source):
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
        return deleted

    def _expand(self, source: Path, base: str, parent: Path, number: int) -> Path:
        if number > MAX_LAYERS:
            raise ExtractionError(f"超过 {MAX_LAYERS} 层压缩包：{source}")
        if is_linked_path(source) or not source.is_file():
            raise ExtractionError(f"压缩包文件不可用：{source}")
        destination = parent / f"{base}_解压"
        if destination.exists():
            raise ExtractionError(f"解压目录已存在，避免覆盖：{destination}")

        snapshots = _archive_files(source)
        self.log(f"第 {number} 层：{source.name} → {destination}")
        last_error = ""
        for index, password in enumerate(self.passwords, start=1):
            if not password and any(self.passwords):
                continue
            try:
                attempt = Path(tempfile.mkdtemp(prefix=".archive_attempt_", dir=parent))
            except OSError as exc:
                raise ExtractionError(f"无法创建密码尝试目录：{parent}：{exc}") from exc
            error_log = parent / f".archive_diagnostic_{uuid4().hex}.log" if self.backend == "winrar" else None
            args = extraction_command(self.backend, self.executable, source, attempt, password, error_log)
            self.log(f"第 {number} 层尝试预设密码 {index}/{len(self.passwords)}：{source.name}")
            try:
                command = subprocess.run(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding=output_encoding(self.backend),
                    errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            except OSError as exc:
                self._remove_attempt(attempt, parent)
                raise ExtractionError(f"无法启动 {EXTRACTOR_LABELS[self.backend]}：{exc}") from exc
            diagnostic = ""
            if error_log is not None and error_log.exists():
                try:
                    diagnostic = error_log.read_bytes().decode("utf-16", errors="replace").strip()
                    error_log.unlink()
                except OSError as exc:
                    raise ExtractionError(f"无法核对 WinRAR 错误记录，原包已保留：{error_log}：{exc}") from exc
            # Some WinRAR errors return zero with -inul, including wrong 7z passwords.
            # A nonempty error log is a failed attempt even on the trial edition.
            has_output = any(attempt.iterdir())
            if command.returncode == 0 and not diagnostic and has_output:
                try:
                    attempt.rename(destination)
                except OSError as exc:
                    raise ExtractionError(f"无法保存成功的解压结果：{destination}：{exc}") from exc
                self.log(f"第 {number} 层使用预设密码 {index} 解压成功：{source.name}")
                break
            lines = (diagnostic or command.stderr or command.stdout).strip().splitlines()
            last_error = " | ".join(lines[-4:]) if lines else (
                f"退出码 {command.returncode}" if command.returncode else "解压程序未产生内容；请检查密码或压缩包"
            )
            for secret in self.passwords:
                if secret:
                    last_error = last_error.replace(secret, "[已隐藏]")
            self._remove_attempt(attempt, parent)
        else:
            raise ExtractionError(f"第 {number} 层所有预设密码均未成功：{source.name}：{last_error}")

        self.layers.append(Layer(source, destination, number, snapshots))
        candidates = _nested_candidates(destination, self.log)
        if not candidates:
            self.log(f"第 {number} 层完成；未发现更深层压缩包。")
        for nested in candidates:
            self._expand(nested, _archive_base(nested), nested.parent, number + 1)
        return destination

    @staticmethod
    def _remove_attempt(attempt: Path, parent: Path) -> None:
        try:
            resolved_parent = parent.resolve(strict=True)
            resolved_attempt = attempt.resolve(strict=True)
        except OSError as exc:
            raise ExtractionError(f"无法核对密码尝试目录：{attempt}：{exc}") from exc
        if is_linked_path(attempt) or resolved_attempt.parent != resolved_parent or not attempt.name.startswith(".archive_attempt_"):
            raise ExtractionError(f"拒绝清理非本次创建的密码尝试目录：{attempt}")

        def clear_readonly(function: Callable[[str], None], failed_path: str, error: BaseException) -> None:
            target = Path(failed_path)
            if not isinstance(error, PermissionError):
                raise error
            if is_linked_path(target) or not target.resolve(strict=True).is_relative_to(resolved_attempt):
                raise error
            os.chmod(target, stat.S_IWRITE)
            function(failed_path)

        try:
            shutil.rmtree(attempt, onexc=clear_readonly)
        except OSError as exc:
            raise ExtractionError(f"无法清理失败的密码尝试目录：{attempt}：{exc}") from exc

