"""Regression tests for committed results, rollback, and selection boundaries."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import os
import stat
import subprocess

from archive_engine import CleanupError, ExtractionEngine, ExtractionError, ExtractionJob, Layer, _archive_files, discover_archive_inputs
from archive_gui import ArchiveApp


def fake_expand(engine, source, base, parent, number):
    destination = parent / f"{base}_解压"
    destination.mkdir()
    (destination / "payload.txt").write_text("complete", encoding="utf-8")
    engine.layers.append(Layer(source, destination, number, _archive_files(source)))
    return destination


def fake_app(rows, folders=None):
    app = ArchiveApp.__new__(ArchiveApp)
    app.rows = rows
    app.folder_archives = folders or {}
    app.rename_suffix = Mock(get=Mock(return_value=".7z"))
    app.concurrent = Mock(get=Mock(return_value=2))
    app.output_dir = Mock(get=Mock(return_value=""))
    app.status = Mock()
    app._log = Mock()
    app._refresh_preview = Mock()
    app._resolve_extractor = Mock(return_value=(Path("7z.exe"), "7zip"))
    return app


class ReliabilityTests(unittest.TestCase):
    def test_readonly_partial_attempt_does_not_block_next_password(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.7z"
            source.write_bytes(b"archive")
            attempts = []

            def run(args, **kwargs):
                destination = Path(next(arg[2:] for arg in args if arg.startswith("-o")))
                attempts.append(destination)
                payload = destination / "payload.txt"
                payload.write_text("complete")
                if len(attempts) == 1:
                    os.chmod(payload, stat.S_IREAD)
                    return subprocess.CompletedProcess(args, 2, "", "wrong password")
                return subprocess.CompletedProcess(args, 0, "", "")

            engine = ExtractionEngine(Path("7z.exe"), ["wrong", "correct"], lambda message: None)
            with patch("archive_engine.subprocess.run", run):
                result = engine.run([ExtractionJob(source, "sample")], None, True)
            self.assertEqual((result.successful_jobs, result.failed_jobs), (1, 0))
            self.assertEqual(len(attempts), 2)
    def test_disguised_save_archive_is_preserved(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "存档.fake").write_bytes(b"PK\x03\x04data")
            (root / "backup.fake").write_bytes(b"7z\xbc\xaf\x27\x1cdata")
            (root / "SAVE.fake").write_bytes(b"PK\x03\x04data")
            self.assertEqual(discover_archive_inputs(root), [])

    def test_cleanup_error_keeps_committed_job_successful(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "sample.7z"
            archive.write_bytes(b"source")
            engine = ExtractionEngine(Path("7z.exe"), "", lambda message: None)
            with patch.object(ExtractionEngine, "_expand", fake_expand), patch.object(
                ExtractionEngine, "_cleanup_workdirs", side_effect=CleanupError("simulated lock")
            ):
                result = engine.run([ExtractionJob(archive, "sample")], None, True)
            self.assertEqual((root / "sample_解压" / "payload.txt").read_text(), "complete")
            self.assertEqual((result.successful_jobs, result.failed_jobs, result.final_files), (1, 0, 1))
            self.assertEqual(len(result.warnings), 1)
            self.assertTrue(archive.exists())

    def test_folder_merge_move_failure_rolls_back(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "input"
            stage = root / ".archive_stage_test"
            folder.mkdir()
            stage.mkdir()
            for name in ("a.txt", "b.txt"):
                (stage / name).write_text(name)
            rename = Path.rename

            def fail_second(source, target):
                if source == stage / "b.txt":
                    raise PermissionError("simulated lock")
                return rename(source, target)

            with patch.object(Path, "rename", fail_second), self.assertRaises(ExtractionError):
                ExtractionEngine._merge_stage_into_folder(stage, folder, root)
            self.assertEqual(list(folder.iterdir()), [])
            self.assertEqual({p.name for p in stage.iterdir()}, {"a.txt", "b.txt"})

    def test_empty_stage_cleanup_failure_is_a_warning(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "input"
            folder.mkdir()
            archive = folder / "sample.7z"
            archive.write_bytes(b"source")
            rmdir = Path.rmdir

            def fail_stage(path):
                if path.name.startswith(".archive_stage_"):
                    raise PermissionError("simulated lock")
                return rmdir(path)

            engine = ExtractionEngine(Path("7z.exe"), "", lambda message: None)
            with patch.object(ExtractionEngine, "_expand", fake_expand), patch.object(Path, "rmdir", fail_stage):
                result = engine.run([ExtractionJob(folder, "input", True, (archive,))], None, False)
            self.assertEqual((result.successful_jobs, result.failed_jobs), (1, 0))
            self.assertEqual(len(result.warnings), 1)
            self.assertEqual((folder / "payload.txt").read_text(), "complete")

    def test_unselected_alias_does_not_block_selected_rename(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, other = root / "sample.shan7z", root / "sample.7shanz"
            first.write_bytes(b"selected")
            other.write_bytes(b"unselected")
            app = fake_app({"first": first, "other": other})
            self.assertEqual(app._rename(["first"]), (["first"], 0))
            self.assertEqual((root / "sample.7z").read_bytes(), b"selected")
            self.assertEqual(other.read_bytes(), b"unselected")

    def test_nested_folders_rejected_before_combined_rename(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inner = root / "outer" / "inner"
            inner.mkdir(parents=True)
            source = inner / "sample.shan7z"
            source.write_bytes(b"source")
            app = fake_app({"outer": inner.parent, "inner": inner}, {"outer": (source,), "inner": (source,)})
            with patch("archive_gui.messagebox.showerror"):
                self.assertFalse(app._preflight_combined(["outer", "inner"]))
            self.assertTrue(source.exists())

    def test_folder_job_rejects_external_archive_before_workdir(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "input"
            folder.mkdir()
            external = root / "external.7z"
            external.write_bytes(b"external")
            engine = ExtractionEngine(Path("7z.exe"), "", lambda message: None)
            with self.assertRaises(ExtractionError):
                engine.run([ExtractionJob(folder, "input", True, (external,))], None, True)
            self.assertEqual(external.read_bytes(), b"external")
            self.assertEqual(list(folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
