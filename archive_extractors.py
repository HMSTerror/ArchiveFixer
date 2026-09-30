"""Command-line adapters for locally installed archive programs."""

from __future__ import annotations

import os
from pathlib import Path
import shutil


EXTRACTOR_LABELS = {
    "7zip": "7-Zip",
    "winrar": "WinRAR",
    "bandizip": "Bandizip",
}


def find_extractor(kind: str) -> Path | None:
    """Find a console extractor without bundling a third-party executable."""
    if kind not in EXTRACTOR_LABELS:
        raise ValueError(f"不支持的解压程序：{kind}")
    commands = {
        "7zip": ("7z", "7za"),
        "winrar": ("WinRAR", "WinRAR.exe"),
        "bandizip": ("bz", "bz.exe"),
    }
    candidates = [shutil.which(command) for command in commands[kind]]
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env_name)
        if not root:
            continue
        root_path = Path(root)
        if kind == "7zip":
            candidates.append(str(root_path / "7-Zip" / "7z.exe"))
        elif kind == "winrar":
            candidates.append(str(root_path / "WinRAR" / "WinRAR.exe"))
        else:
            candidates.extend((
                str(root_path / "Bandizip" / "bz.exe"),
                str(root_path / "Programs" / "Bandizip" / "bz.exe"),
            ))
    if kind == "7zip":
        try:
            import winreg

            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for key_name in (r"SOFTWARE\7-Zip", r"SOFTWARE\WOW6432Node\7-Zip"):
                    try:
                        with winreg.OpenKey(hive, key_name) as key:
                            install_dir, _ = winreg.QueryValueEx(key, "Path")
                            candidates.append(str(Path(install_dir) / "7z.exe"))
                    except OSError:
                        pass
        except ImportError:
            pass
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def extraction_command(
    kind: str, executable: Path, source: Path, destination: Path, password: str,
    error_log: Path | None = None,
) -> list[str]:
    """Build one unattended extraction attempt into a new destination folder."""
    if kind == "7zip":
        return [
            str(executable), "x", "-y", "-aos", "-bd", "-bb0",
            "-bso0", "-bsp0", "-bse1", "-sccUTF-8",
            f"-p{password if password else '-'}", f"-o{destination}", str(source),
        ]
    if kind == "winrar":
        # Rar.exe handles RAR only; WinRAR.exe also handles ZIP and 7z.
        args = [
            str(executable), "x", "-y", "-o-", "-ibck", "-inul", "-cfg-",
            f"-p{password if password else '-'}", str(source), str(destination) + os.sep,
        ]
        if error_log is not None:
            args.insert(2, f"-ilog{error_log}")
        return args
    if kind == "bandizip":
        args = [str(executable), "x", "-y", "-aos", "-consolemode:utf8"]
        if password:
            args.append(f"-p:{password}")
        return args + [f"-o:{destination}", str(source)]
    raise ValueError(f"不支持的解压程序：{kind}")


def output_encoding(kind: str) -> str:
    if kind not in EXTRACTOR_LABELS:
        raise ValueError(f"不支持的解压程序：{kind}")
    return "mbcs" if kind == "winrar" and os.name == "nt" else "utf-8"
