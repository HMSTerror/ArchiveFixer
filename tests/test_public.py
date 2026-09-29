"""Tests for public release naming, discovery, and safe failure behavior."""

from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from archive_engine import ExtractionEngine, ExtractionJob, discover_archive_inputs, expand_dropped_folder
from archive_logic import extraction_base, plan_renames, target_name


CASES = (
    "测试.shan7z",
    "测试.7shanz",
    "hello.shan7z",
    "hello.7shanz",
    "hello.world.shan7z",
    "hello.world.7shanz",
    "中文 文件名.shan7z",
    "中文 文件名.7shanz",
)


class PublicReleaseTests(unittest.TestCase):
    def test_eight_requested_names(self) -> None:
        for name in CASES:
            with self.subTest(name=name):
                expected = name.rsplit(".", 1)[0] + ".7z"
                self.assertEqual(target_name(name), expected)
                self.assertEqual(extraction_base(Path(name)), name.rsplit(".", 1)[0])
                with TemporaryDirectory() as temporary:
                    source = Path(temporary) / name
                    source.write_bytes(b"archive sample")
                    decision = plan_renames([source])[0]
                    self.assertEqual(decision.status, "rename")
                    decision.source.rename(decision.target)
                    self.assertFalse(source.exists())
                    self.assertEqual((Path(temporary) / expected).read_bytes(), b"archive sample")

    def test_digits_case_and_middle_text(self) -> None:
        cases = {
            "2026 demo.7ShAnZ": "2026 demo.7z",
            "alpha.shan7z.beta.shan7z": "alpha.shan7z.beta.7z",
            "alpha.7shanz.beta": "alpha.7shanz.7z",
            "pack.7z.001删": "pack.7z.001",
            "pack.7z.002.fake": "pack.7z.002",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(target_name(source), expected)

    def test_discovery_recognizes_special_suffixes_without_magic_header(self) -> None:
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            files = [folder / name for name in CASES]
            for path in files:
                path.write_bytes(b"dummy")
            self.assertEqual(set(discover_archive_inputs(folder)), set(files))

    def test_discovery_with_launcher_and_mixed_folder(self) -> None:
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            archive = folder / "demo.shan7z"
            archive.write_bytes(b"dummy")
            (folder / "launch.exe").write_bytes(b"dummy")
            child = folder / "child"
            child.mkdir()
            (child / "nested.7shanz").write_bytes(b"dummy")
            self.assertIn(archive, discover_archive_inputs(folder))
            self.assertEqual(set(expand_dropped_folder(folder)), {archive, child})

    def test_existing_target_is_conflict_and_not_overwritten(self) -> None:
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "demo.shan7z"
            target = folder / "demo.7z"
            source.write_bytes(b"source")
            target.write_bytes(b"existing")
            decision = plan_renames([source])[0]
            self.assertEqual(decision.status, "conflict")
            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(target.read_bytes(), b"existing")

    def test_missing_source_is_skipped_by_gui(self) -> None:
        from archive_gui import ArchiveApp

        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "missing.7shanz"
            app = ArchiveApp.__new__(ArchiveApp)
            app.rows = {"row": source}
            app.folder_archives = {}
            app.status = type("Status", (), {"set": lambda self, value: None})()
            messages = []
            app._log = messages.append
            app._refresh_preview = lambda: None
            self.assertEqual(app._rename(["row"]), ([], 1))
            self.assertFalse(source.exists())
            self.assertTrue(any("源文件不存在" in message for message in messages))

    def test_empty_folder_has_no_archives(self) -> None:
        with TemporaryDirectory() as temporary:
            self.assertEqual(discover_archive_inputs(Path(temporary)), [])
            self.assertEqual(expand_dropped_folder(Path(temporary)), [])

    def test_gui_reports_permission_error_and_preserves_source(self) -> None:
        from archive_gui import ArchiveApp

        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "demo.7shanz"
            source.write_bytes(b"source")
            app = ArchiveApp.__new__(ArchiveApp)
            app.rows = {"row": source}
            app.folder_archives = {}
            app.status = type("Status", (), {"set": lambda self, value: None})()
            messages = []
            app._log = messages.append
            app._refresh_preview = lambda: None
            with patch.object(Path, "rename", side_effect=PermissionError("access denied")):
                ready, skipped = app._rename(["row"])
            self.assertEqual((ready, skipped), ([], 1))
            self.assertTrue(source.exists())
            self.assertTrue(any("改名失败" in message for message in messages))

    def test_nested_password_archive_with_installed_7zip(self) -> None:
        from archive_gui import find_7zip

        seven_zip = find_7zip()
        if seven_zip is None:
            self.skipTest("7-Zip is not installed")
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            staging = folder / "staging"
            staging.mkdir()
            (staging / "payload.txt").write_text("finished", encoding="utf-8")
            for archive, member in (("inner.7z", "payload.txt"), ("outer.7z", "inner.7z")):
                subprocess.run(
                    [str(seven_zip), "a", "-y", "-pfixture-pass", "-mhe=on", archive, member],
                    cwd=staging,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    check=True,
                )
            disguised = folder / "demo.shan7z"
            (staging / "outer.7z").rename(disguised)
            engine = ExtractionEngine(seven_zip, ["wrong", "fixture-pass"], lambda message: None)
            result = engine.run([ExtractionJob(disguised, extraction_base(disguised))], None, True, max_concurrent=1)
            self.assertEqual(result.layers, 2)
            self.assertEqual((folder / "demo_解压" / "payload.txt").read_text(encoding="utf-8"), "finished")
            self.assertFalse(disguised.exists())


if __name__ == "__main__":
    unittest.main()
