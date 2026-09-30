"""Windows GUI entry point and packaged-build smoke test."""

from __future__ import annotations

from pathlib import Path
import sys
import time
import zipfile
from tempfile import TemporaryDirectory

from archive_gui import ArchiveApp, TkinterDnD, find_7zip, main


def smoke_test() -> None:
    """Check GUI, naming and a real ZIP workflow when 7-Zip is available."""
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
            executable = find_7zip()
            if executable is not None:
                archive = Path(temporary) / "extract.zip"
                with zipfile.ZipFile(archive, "w") as stream:
                    stream.writestr("payload.txt", "complete")
                app.extractor_choice.set("7-Zip")
                app.extractor_path.set(str(executable))
                app.delete_archives.set(True)
                app._add_paths([archive])
                app._extract_selected()
                deadline = time.monotonic() + 20
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.02)
                if app.busy:
                    app.cancel_event.set()
                    raise RuntimeError("Extraction smoke test timed out")
                result = Path(temporary) / "extract_解压" / "payload.txt"
                if not result.is_file() or result.read_text() != "complete" or archive.exists():
                    raise RuntimeError("Extraction smoke test failed")
        finally:
            root.destroy()


if __name__ == "__main__":
    if sys.argv[1:] == ["--smoke-test"]:
        smoke_test()
    else:
        main()
