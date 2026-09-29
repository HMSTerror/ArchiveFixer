"""Tests for public release naming, discovery, and safe failure behavior."""

from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from archive_extractors import extraction_command, find_extractor, output_encoding
from archive_engine import (
    ExtractionEngine,
    ExtractionError,
    ExtractionJob,
    _normalize_volume_group,
    discover_archive_inputs,
    expand_dropped_folder,
)
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

    def test_zip_rename_choice_preserves_volume_numbers(self) -> None:
        self.assertEqual(target_name("中文 文件.shan7z", ".zip"), "中文 文件.zip")
        self.assertEqual(target_name("demo.7z.fake", ".zip"), "demo.zip")
        self.assertEqual(target_name("demo.zip.fake", ".7z"), "demo.7z")
        self.assertEqual(target_name("demo.7z.001删", ".zip"), "demo.7z.001")
        self.assertEqual(target_name("name.zip.part.7shanz", ".zip"), "name.zip.part.zip")
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "demo.shan7z"
            existing = Path(temporary) / "demo.zip"
            source.write_bytes(b"source")
            existing.write_bytes(b"existing")
            self.assertEqual(plan_renames([source], ".zip")[0].status, "conflict")

    def test_extractor_command_adapters(self) -> None:
        source = Path("C:/input/中文 文件.zip")
        destination = Path("C:/output folder")
        seven_zip = extraction_command("7zip", Path("7z.exe"), source, destination, "secret")
        self.assertIn("-psecret", seven_zip)
        self.assertIn(f"-o{destination}", seven_zip)
        self.assertEqual(seven_zip[-1], str(source))

        winrar = extraction_command("winrar", Path("Rar.exe"), source, destination, "secret")
        self.assertEqual(winrar[:4], ["Rar.exe", "x", "-y", "-o-"])
        self.assertIn("-psecret", winrar)
        self.assertEqual(winrar[-1], str(destination) + "\\")
        self.assertIn("-p-", extraction_command("winrar", Path("Rar.exe"), source, destination, ""))

        bandizip = extraction_command("bandizip", Path("bz.exe"), source, destination, "secret")
        self.assertEqual(bandizip[:3], ["bz.exe", "x", "-y"])
        self.assertIn("-consolemode:utf8", bandizip)
        self.assertIn("-p:secret", bandizip)
        self.assertIn(f"-o:{destination}", bandizip)
        self.assertNotIn("-p:", extraction_command("bandizip", Path("bz.exe"), source, destination, ""))
        self.assertEqual(output_encoding("bandizip"), "utf-8")

    def test_find_winrar_and_bandizip_console_tools(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            winrar = root / "WinRAR" / "Rar.exe"
            bandizip = root / "Bandizip" / "bz.exe"
            winrar.parent.mkdir()
            bandizip.parent.mkdir()
            winrar.write_bytes(b"sample")
            bandizip.write_bytes(b"sample")
            with patch("archive_extractors.shutil.which", return_value=None), patch.dict(
                "archive_extractors.os.environ", {"ProgramFiles": str(root)}, clear=True,
            ):
                self.assertEqual(find_extractor("winrar"), winrar)
                self.assertEqual(find_extractor("bandizip"), bandizip)

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

    def test_seven_z_shan_alias(self) -> None:
        with TemporaryDirectory() as temporary:
            archive = Path(temporary) / "demo.7zshan"
            archive.write_bytes(b"dummy")
            self.assertEqual(target_name(archive.name), "demo.7z")
            self.assertEqual(extraction_base(archive), "demo")
            self.assertEqual(discover_archive_inputs(archive.parent), [archive])

    def test_archive_deletion_is_opt_in(self) -> None:
        from archive_gui import ArchiveApp, TkinterDnD

        with TemporaryDirectory() as temporary:
            root = TkinterDnD.Tk()
            root.withdraw()
            try:
                app = ArchiveApp(root, preset_path=Path(temporary) / "presets.json")
                self.assertFalse(app.delete_archives.get())
            finally:
                root.destroy()

    def test_gui_preview_uses_zip_rename_choice(self) -> None:
        from archive_gui import ArchiveApp, TkinterDnD

        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "中文 文件.shan7z"
            source.write_bytes(b"dummy")
            root = TkinterDnD.Tk()
            root.withdraw()
            try:
                app = ArchiveApp(root, preset_path=Path(temporary) / "presets.json")
                app._add_paths([source])
                app.rename_suffix.set(".zip")
                app._refresh_preview()
                iid = next(iter(app.rows))
                self.assertEqual(app.table.item(iid, "values")[1], "中文 文件.zip")
                app.extractor_path.set("C:/custom/7z.exe")
                app.extractor_choice.set("WinRAR")
                app._on_extractor_change()
                self.assertEqual(app.active_backend, "winrar")
                app.extractor_choice.set("7-Zip")
                app._on_extractor_change()
                self.assertEqual(app.extractor_path.get(), "C:/custom/7z.exe")
            finally:
                root.destroy()

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
            app.rename_suffix = type("Value", (), {"get": lambda self: ".7z"})()
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
            app.rename_suffix = type("Value", (), {"get": lambda self: ".7z"})()
            app.status = type("Status", (), {"set": lambda self, value: None})()
            messages = []
            app._log = messages.append
            app._refresh_preview = lambda: None
            with patch.object(Path, "rename", side_effect=PermissionError("access denied")):
                ready, skipped = app._rename(["row"])
            self.assertEqual((ready, skipped), ([], 1))
            self.assertTrue(source.exists())
            self.assertTrue(any("改名失败" in message for message in messages))

    def test_combined_action_checks_7zip_before_renaming(self) -> None:
        from archive_gui import ArchiveApp

        app = ArchiveApp.__new__(ArchiveApp)
        app.busy = False
        app.extractor_choice = type("Value", (), {"get": lambda self: "7-Zip"})()
        app.extractor_path = type("Value", (), {"get": lambda self: "missing-7z.exe"})()
        app._selected = lambda: ["row"]
        app._rename = Mock(return_value=(["row"], 0))
        app._start_extract = Mock()
        with patch("archive_gui.messagebox.showerror"):
            app._rename_and_extract()
        app._rename.assert_not_called()
        app._start_extract.assert_not_called()

    def test_combined_action_checks_existing_output_before_renaming(self) -> None:
        from archive_gui import ArchiveApp, find_7zip

        seven_zip = find_7zip()
        if seven_zip is None:
            self.skipTest("7-Zip is not installed")
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "demo.shan7z"
            source.write_bytes(b"dummy")
            (Path(temporary) / "demo_解压").mkdir()
            app = ArchiveApp.__new__(ArchiveApp)
            app.busy = False
            app.extractor_choice = type("Value", (), {"get": lambda self: "7-Zip"})()
            app.extractor_path = type("Value", (), {"get": lambda self: str(seven_zip)})()
            app.rename_suffix = type("Value", (), {"get": lambda self: ".7z"})()
            app.concurrent = type("Value", (), {"get": lambda self: 2})()
            app.output_dir = type("Value", (), {"get": lambda self: ""})()
            app.rows = {"row": source}
            app.folder_archives = {}
            app._selected = lambda: ["row"]
            app._rename = Mock(return_value=(["row"], 0))
            app._start_extract = Mock()
            with patch("archive_gui.messagebox.showerror"):
                app._rename_and_extract()
            app._rename.assert_not_called()
            app._start_extract.assert_not_called()

    def test_volume_rename_failure_restores_earlier_parts(self) -> None:
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            first = folder / "demo.7z.001删"
            second = folder / "demo.7z.002删"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            original_rename = Path.rename

            def fail_second(source: Path, target: Path) -> Path:
                if source == second:
                    raise PermissionError("simulated lock")
                return original_rename(source, target)

            with patch.object(Path, "rename", fail_second), self.assertRaises(ExtractionError):
                _normalize_volume_group(first)
            self.assertEqual(first.read_bytes(), b"first")
            self.assertEqual(second.read_bytes(), b"second")
            self.assertFalse((folder / "demo.7z.001").exists())

    def test_one_bad_archive_does_not_block_valid_job(self) -> None:
        from archive_gui import find_7zip

        seven_zip = find_7zip()
        if seven_zip is None:
            self.skipTest("7-Zip is not installed")
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "payload.txt").write_text("ok", encoding="utf-8")
            subprocess.run(
                [str(seven_zip), "a", "-y", "good.7z", "payload.txt"],
                cwd=folder,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
            )
            bad = folder / "bad.7z"
            bad.write_bytes(b"not an archive")
            messages: list[str] = []
            result = ExtractionEngine(seven_zip, "", messages.append).run(
                [ExtractionJob(folder / "good.7z", "good"), ExtractionJob(bad, "bad")],
                None,
                True,
                max_concurrent=2,
            )
            self.assertEqual(result.successful_jobs, 1)
            self.assertEqual(result.failed_jobs, 1)
            self.assertTrue((folder / "good_解压" / "payload.txt").is_file())
            self.assertFalse((folder / "good.7z").exists())
            self.assertTrue(bad.exists())
            self.assertTrue(any("bad.7z" in error for error in result.errors))

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

    def test_zip_choice_extracts_real_zip_archive(self) -> None:
        from archive_gui import find_7zip

        seven_zip = find_7zip()
        if seven_zip is None:
            self.skipTest("7-Zip is not installed")
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "中文 文件.txt").write_text("zip payload", encoding="utf-8")
            subprocess.run(
                [str(seven_zip), "a", "-y", "-tzip", "demo.zip", "中文 文件.txt"],
                cwd=folder,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
            )
            disguised = folder / "demo.7shanz"
            (folder / "demo.zip").rename(disguised)
            decision = plan_renames([disguised], ".zip")[0]
            self.assertEqual(decision.status, "rename")
            decision.source.rename(decision.target)
            result = ExtractionEngine(seven_zip, "", lambda message: None).run(
                [ExtractionJob(decision.target, extraction_base(decision.target))], None, False,
                max_concurrent=1,
            )
            self.assertEqual(result.successful_jobs, 1)
            self.assertEqual((folder / "demo_解压" / "中文 文件.txt").read_text(encoding="utf-8"), "zip payload")
            self.assertTrue(decision.target.exists())


if __name__ == "__main__":
    unittest.main()
