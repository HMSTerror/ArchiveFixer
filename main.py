"""Windows GUI entry point and packaged-build smoke test."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from archive_gui import ArchiveApp, TkinterDnD, main


def smoke_test() -> None:
    """Check that the packaged GUI loads and can rename a sample file."""
    with TemporaryDirectory() as temporary:
        root = TkinterDnD.Tk()
        root.withdraw()
        try:
            app = ArchiveApp(root, preset_path=Path(temporary) / "presets.json")
            if app.delete_archives.get():
                raise RuntimeError("Archive deletion should be opt in")
            source = Path(temporary) / "demo.shan7z"
            source.write_bytes(b"sample")
            app.rename_suffix.set(".zip")
            app._add_paths([source])
            app._rename(list(app.rows))
            if not (Path(temporary) / "demo.zip").is_file() or source.exists():
                raise RuntimeError("Rename smoke test failed")
        finally:
            root.destroy()


if __name__ == "__main__":
    if sys.argv[1:] == ["--smoke-test"]:
        smoke_test()
    else:
        main()
