"""Build and verify a portable release before replacing previous build outputs."""

from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4
from version import VERSION


def require_local_child(path: Path, parent: Path) -> None:
    if path.is_symlink() or path.is_junction() or path.resolve().parent != parent.resolve():
        raise RuntimeError(f"Refusing a linked or external build path: {path}")


def publish(outputs: list[Path], destination: Path) -> None:
    """Rollback previous outputs if publication is interrupted by a file lock."""
    old, installed = [], []
    for source in outputs:
        require_local_child(destination / source.name, destination)
    try:
        for source in outputs:
            target = destination / source.name
            if target.exists():
                backup = destination / f".release_previous_{uuid4().hex}_{source.name}"
                target.rename(backup)
                old.append((backup, target))
            source.rename(target)
            installed.append((target, source))
    except OSError as exc:
        rollback_errors = []
        for target, source in reversed(installed):
            try:
                target.rename(source)
            except OSError as error:
                rollback_errors.append(str(error))
        for backup, target in reversed(old):
            try:
                backup.rename(target)
            except OSError as error:
                rollback_errors.append(f"{backup}: {error}")
        detail = "; ".join(rollback_errors) or "previous outputs restored"
        raise RuntimeError(f"Could not publish build: {exc}; {detail}") from exc
    for backup, _ in old:
        require_local_child(backup, destination)
        try:
            shutil.rmtree(backup) if backup.is_dir() else backup.unlink()
        except OSError as exc:
            print(f"New release is ready; previous output remains at {backup}: {exc}")


def main() -> None:
    root = Path(__file__).resolve().parent
    build, dist = root / "build", root / "dist"
    for directory in (build, dist):
        require_local_child(directory, root)
        directory.mkdir(exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="release_", dir=build))
    generated = workspace / "dist"
    version_file = workspace / "version-info.txt"
    version_numbers = tuple(int(part) for part in VERSION.split(".")) + (0,)
    version_file.write_text(
        f"VSVersionInfo(ffi=FixedFileInfo(filevers={version_numbers!r}, prodvers={version_numbers!r}, "
        "mask=0x3f, flags=0x2, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)), "
        "kids=[StringFileInfo([StringTable('040904B0', [StringStruct('CompanyName', 'HMSTerror'), "
        "StringStruct('FileDescription', 'ArchiveFixer archive utility'), "
        f"StringStruct('FileVersion', '{VERSION}'), StringStruct('ProductVersion', '{VERSION}'), "
        "StringStruct('ProductName', 'ArchiveFixer'), StringStruct('OriginalFilename', 'ArchiveFixer.exe'), "
        "StringStruct('LegalCopyright', 'Copyright (c) 2026 HMSTerror')])]), "
        "VarFileInfo([VarStruct('Translation', [1033,1200])])])", encoding="utf-8")
    try:
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
            "--onedir", "--windowed", "--noupx", "--name", "ArchiveFixer",
            "--collect-all", "tkinterdnd2", "--add-data", f"{root / 'THIRD_PARTY_NOTICES.txt'};.",
            "--version-file", str(version_file),
            "--distpath", str(generated), "--workpath", str(workspace / "work"),
            "--specpath", str(workspace), str(root / "main.py"),
        ], cwd=root, check=True)
        application = generated / "ArchiveFixer"
        executable = application / "ArchiveFixer.exe"
        for name in ("LICENSE", "README.md", "THIRD_PARTY_NOTICES.txt"):
            shutil.copy2(root / name, application / name)
        subprocess.run([str(executable), "--smoke-test"], check=True, timeout=45,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with executable.open("rb") as stream:
            exe_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        (application / "SHA256SUMS.txt").write_text(f"{exe_hash}  ArchiveFixer.exe\n", encoding="ascii")
        archive = Path(shutil.make_archive(str(generated / "ArchiveFixer-portable"), "zip",
                                          root_dir=generated, base_dir="ArchiveFixer"))
        with archive.open("rb") as stream:
            zip_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        checksums = generated / "SHA256SUMS.txt"
        checksums.write_text(f"{zip_hash}  ArchiveFixer-portable.zip\n{exe_hash}  ArchiveFixer/ArchiveFixer.exe\n",
                             encoding="ascii")
        publish([application, archive, checksums], dist)
    except Exception:
        print(f"Build failed; previous release is preserved. Build workspace: {workspace}")
        raise
    else:
        require_local_child(workspace, build)
        shutil.rmtree(workspace)
        print(f"Verified portable release: {dist / 'ArchiveFixer-portable.zip'}")
        print(f"SHA-256: {zip_hash}")


if __name__ == "__main__":
    main()
