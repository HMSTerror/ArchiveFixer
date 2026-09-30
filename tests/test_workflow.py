"""Planning, real process cancellation, progress, and GUI recovery controls."""

from pathlib import Path
import subprocess
import sys
import threading
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from archive_engine import ExtractionCancelled, ExtractionEngine, ExtractionJob, JobOutcome, JobProgress, discover_archive_inputs, expand_dropped_folder
from archive_gui import ArchiveApp, TkinterDnD
from archive_plan import plan_batch
from archive_reports import redact_secrets
from archive_extractors import find_extractor
from test_reliability import fake_expand


class WorkflowTests(unittest.TestCase):
    def test_scan_ignores_workdirs_and_compressed_layer_backups(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "actual.7z"
            source.write_bytes(b"source")
            for name in (".archive_work_previous", ".archive_stage_previous", ".archive_attempt_previous",
                         ".archive_flat_backup_previous", "demo_压缩层备份", "demo_压缩层备份_2"):
                folder = root / name
                folder.mkdir()
                (folder / "old.7z").write_bytes(b"old work")
            self.assertEqual(discover_archive_inputs(root), [source])
            self.assertEqual(len(list(root.rglob("old.7z"))), 6)

    def test_dropped_parent_does_not_create_jobs_from_old_workdirs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "actual.7z"
            source.write_bytes(b"source")
            for name in ("regular", ".archive_work_previous", "demo_压缩层备份"):
                folder = root / name
                folder.mkdir()
                (folder / "inside.7z").write_bytes(b"archive")
            self.assertEqual(set(expand_dropped_folder(root)), {source, root / "regular"})

    def test_preview_rejects_a_managed_workdir_as_folder_input(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary) / ".archive_work_previous"
            folder.mkdir()
            source = folder / "old.7z"
            source.write_bytes(b"recoverable")
            self.assertTrue(plan_batch([folder]).issues)
            self.assertFalse(plan_batch([source]).issues)
            self.assertEqual(source.read_bytes(), b"recoverable")

    def test_partial_cleanup_counts_confirmed_deletions(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "input"
            folder.mkdir()
            first, second = folder / "a.7z", folder / "b.7z"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            unlink = Path.unlink

            def fail_second(path, *args, **kwargs):
                if path == second:
                    raise PermissionError("locked")
                return unlink(path, *args, **kwargs)

            with patch.object(ExtractionEngine, "_expand", fake_expand), patch.object(Path, "unlink", fail_second):
                result = ExtractionEngine(Path("7z.exe"), "", lambda message: None).run(
                    [ExtractionJob(folder, "input", True, (first, second))], None, True)
            self.assertEqual((result.successful_jobs, result.deleted_archives), (1, 1))
            self.assertEqual(result.outcomes[0].status, "warning")
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())

    def test_gui_combined_preflight_checks_all_folders_before_rename(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            first, second = folder / "first", folder / "second"
            first.mkdir()
            second.mkdir()
            (first / "a.shan7z").write_bytes(b"source")
            (second / "b.shan7z").write_bytes(b"source")
            (second / "b.7shanz").write_bytes(b"conflicting source")
            root = TkinterDnD.Tk()
            root.withdraw()
            try:
                app = ArchiveApp(root, preset_path=folder / "presets.json")
                app._add_paths([first, second])
                with patch.object(app, "_resolve_extractor", return_value=(Path("7z.exe"), "7zip")), patch("archive_gui.messagebox.showerror"):
                    app._rename_and_extract()
                self.assertFalse(app.busy)
                self.assertTrue((first / "a.shan7z").exists())
                self.assertFalse((first / "a.7z").exists())
            finally:
                root.destroy()

    def test_gui_preview_opens_without_renaming(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "demo.shan7z"
            source.write_bytes(b"source")
            root = TkinterDnD.Tk()
            root.withdraw()
            try:
                app = ArchiveApp(root, preset_path=folder / "presets.json")
                app._add_paths([source])
                app._preview_selected()
                self.assertTrue(any(widget.winfo_class() == "Toplevel" for widget in root.winfo_children()))
                self.assertEqual(source.read_bytes(), b"source")
                self.assertFalse((folder / "demo.7z").exists())
                self.assertFalse(app.busy)
            finally:
                root.destroy()

    def test_cancel_terminates_child_process_tree(self):
        import ctypes
        from ctypes import wintypes
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_marker = root / "child.pid"
            cancel = threading.Event()
            engine = ExtractionEngine(Path("7z.exe"), "", lambda message: None, cancel_event=cancel)
            failures = []
            child_code = "import os,time,sys; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
            parent_code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]],creationflags=subprocess.CREATE_NO_WINDOW); time.sleep(60)"

            def run():
                try:
                    engine._run_command([sys.executable, "-c", parent_code, child_code, str(child_marker)])
                except Exception as exc:
                    failures.append(exc)

            worker = threading.Thread(target=run)
            worker.start()
            deadline = time.monotonic() + 8
            while not child_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            cancel.set()
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
            self.assertTrue(child_marker.exists())
            self.assertIsInstance(failures[0], ExtractionCancelled)
            pid = int(child_marker.read_text())
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    code = wintypes.DWORD()
                    self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                    self.assertNotEqual(code.value, 259, "Child extractor is still running")
                finally:
                    kernel.CloseHandle(handle)

    def test_gui_combined_first_volume_extracts_real_companions(self):
        creator = find_extractor("7zip")
        if creator is None:
            self.skipTest("7-Zip unavailable")
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            payload = folder / "payload.txt"
            content = bytes(range(256)) * 32
            payload.write_bytes(content)
            subprocess.run([str(creator), "a", "-y", "-m0=Copy", "-v2k", "-pExample@abc", "game.7z", payload.name],
                           cwd=folder, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
            volumes = sorted(folder.glob("game.7z.*"))
            for path in volumes:
                path.rename(path.with_name(path.name + "删"))
            root = TkinterDnD.Tk()
            root.withdraw()
            try:
                app = ArchiveApp(root, preset_path=folder / "presets.json")
                app.extractor_path.set(str(creator))
                app.password_vars[0].set("Example@abc")
                app.delete_archives.set(True)
                app._add_paths([folder / "game.7z.001删"])
                app._rename_and_extract()
                deadline = time.monotonic() + 12
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.02)
                self.assertFalse(app.busy)
                self.assertEqual((folder / "game_解压" / "payload.txt").read_bytes(), content)
                self.assertFalse(list(folder.glob("game.7z.*")))
                self.assertEqual(next(iter(app.outcomes.values())).status, "success")
            finally:
                app.cancel_event.set()
                root.destroy()

    def test_gui_volume_rename_rolls_back_companions_on_failure(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            first, second = folder / "a.7z.001删", folder / "a.7z.002删"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            root = TkinterDnD.Tk()
            root.withdraw()
            try:
                app = ArchiveApp(root, preset_path=folder / "presets.json")
                app._add_paths([first])
                rename = Path.rename

                def fail_second(source, target):
                    if source == second:
                        raise PermissionError("locked")
                    return rename(source, target)

                with patch.object(Path, "rename", fail_second):
                    self.assertEqual(app._rename(list(app.rows)), ([], 1))
                self.assertEqual(first.read_bytes(), b"first")
                self.assertEqual(second.read_bytes(), b"second")
            finally:
                root.destroy()

    def test_preview_reports_volume_gap_without_changing_files(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = [root / "demo.7z.001删", root / "demo.7z.003删"]
            for path in files:
                path.write_bytes(b"source")
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            plan = plan_batch([files[0]], rename=True)
            self.assertTrue(any(".002" in issue for issue in plan.issues))
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

    def test_preview_includes_companions_and_deduplicates_sizes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = [root / "demo.7z.001删", root / "demo.7z.002删"]
            for path in files:
                path.write_bytes(b"source")
            plan = plan_batch(files, rename=True)
            self.assertEqual(plan.issues, ())
            self.assertEqual((len(plan.jobs), plan.input_bytes), (1, 12))
            self.assertEqual(len(plan.items[0].renames), 2)
            self.assertTrue(all(path.exists() for path in files))

    def test_preview_detects_duplicate_output_across_directories(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = []
            for name in ("a", "b"):
                folder = root / name
                folder.mkdir()
                source = folder / "same.7z"
                source.write_bytes(b"source")
                inputs.append(source)
            plan = plan_batch(inputs, root / "output")
            self.assertTrue(any("同一结果目录" in issue for issue in plan.issues))
            self.assertFalse((root / "output").exists())

    def test_cancel_stops_running_process_and_queued_job_keeps_sources(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / "ready"
            sources = [root / "a.7z", root / "b.7z"]
            for source in sources:
                source.write_bytes(b"original")
            cancel, results, failures = threading.Event(), [], []
            real_command = ExtractionEngine._run_command

            def slow_command(engine, args):
                attempt = Path(next(arg[2:] for arg in args if arg.startswith("-o")))
                code = "from pathlib import Path; import time,sys; Path(sys.argv[1]).write_text('partial'); Path(sys.argv[2]).touch(); time.sleep(60)"
                return real_command(engine, [sys.executable, "-c", code, str(attempt / "partial.txt"), str(ready)])

            def run():
                try:
                    results.append(ExtractionEngine(Path("7z.exe"), "", lambda message: None, cancel_event=cancel).run(
                        [ExtractionJob(path, path.stem) for path in sources], None, True, max_concurrent=1))
                except Exception as exc:
                    failures.append(exc)

            with patch.object(ExtractionEngine, "_run_command", slow_command):
                worker = threading.Thread(target=run)
                worker.start()
                deadline = time.monotonic() + 8
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                cancel.set()
                worker.join(timeout=8)
            self.assertFalse(worker.is_alive(), "Cancellation failed to stop the real process")
            self.assertTrue(ready.exists(), "Fixture process did not start")
            self.assertEqual(failures, [])
            result = results[0]
            self.assertEqual((result.cancelled_jobs, result.failed_jobs, result.successful_jobs), (2, 0, 0))
            self.assertEqual(len(list(root.glob(".archive_work_*"))), 1)
            self.assertTrue(any(path.name == "partial.txt" for path in root.rglob("partial.txt")))
            self.assertTrue(all(source.read_bytes() == b"original" for source in sources))
            self.assertFalse(list(root.glob("*_解压")))

    def test_cancel_after_commit_keeps_result_and_source(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "a.7z"
            source.write_bytes(b"original")
            cancel = threading.Event()

            def progress(update):
                if update.state == "cleaning":
                    cancel.set()

            engine = ExtractionEngine(Path("7z.exe"), "", lambda message: None, cancel_event=cancel, progress=progress)
            with patch.object(ExtractionEngine, "_expand", fake_expand):
                result = engine.run([ExtractionJob(source, "a")], None, True)
            self.assertEqual((result.successful_jobs, result.cancelled_jobs, result.deleted_archives), (1, 0, 0))
            self.assertEqual(result.outcomes[0].status, "warning")
            self.assertEqual((root / "a_解压" / "payload.txt").read_text(), "complete")
            self.assertEqual(source.read_bytes(), b"original")

    def test_completed_job_is_delivered_before_next_job_finishes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [root / "a.7z", root / "b.7z"]
            for source in sources:
                source.write_bytes(b"original")
            updates = []

            def expand(engine, source, base, parent, number):
                if source == sources[1]:
                    deadline = time.monotonic() + 4
                    while not (root / "a_解压").exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    if not (root / "a_解压").exists():
                        raise RuntimeError("Previous result waited for all extraction jobs")
                return fake_expand(engine, source, base, parent, number)

            with patch.object(ExtractionEngine, "_expand", expand):
                result = ExtractionEngine(Path("7z.exe"), "", lambda message: None, progress=updates.append).run(
                    [ExtractionJob(path, path.stem) for path in sources], None, True, max_concurrent=1)
            self.assertEqual(result.successful_jobs, 2)
            self.assertEqual(sum(update.state == "success" for update in updates), 2)

    def test_password_deduplication_preserves_preset_number(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "a.7z"
            source.write_bytes(b"source")
            calls, updates = [], []

            def run(engine, args):
                calls.append(args)
                attempt = Path(next(arg[2:] for arg in args if arg.startswith("-o")))
                if "-pcorrect" in args:
                    (attempt / "payload.txt").write_text("complete")
                    return subprocess.CompletedProcess(args, 0, "", "")
                return subprocess.CompletedProcess(args, 2, "", "wrong")

            with patch.object(ExtractionEngine, "_run_command", run):
                result = ExtractionEngine(Path("7z.exe"), ["wrong", "wrong", "correct"], lambda message: None,
                                          progress=updates.append).run([ExtractionJob(source, "a")], None, True)
            self.assertEqual(result.successful_jobs, 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual([update.password_index for update in updates if update.state == "extracting"], [1, 3])

    def test_log_redaction_handles_overlapping_passwords(self):
        text = "secretLONG + secret + 中文@abc"
        result = redact_secrets(text, ["secret", "secretLONG", "中文@abc"])
        self.assertEqual(result, "[已隐藏] + [已隐藏] + [已隐藏]")

    def test_gui_first_volume_rename_includes_unselected_companion(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            files = [folder / "a.7z.001删", folder / "a.7z.002删"]
            for path in files:
                path.write_bytes(b"source")
            root = TkinterDnD.Tk()
            root.withdraw()
            try:
                app = ArchiveApp(root, preset_path=folder / "presets.json")
                app._add_paths(files)
                selected = next(iid for iid, path in app.rows.items() if path == files[0])
                self.assertEqual(app._rename([selected]), ([selected], 0))
                self.assertEqual(set(app.rows.values()), {folder / "a.7z.001", folder / "a.7z.002"})
                self.assertTrue((folder / "a.7z.002").is_file())
            finally:
                root.destroy()

    def test_gui_retry_and_result_state_survive_preview_refresh(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            sources = [folder / "a.7z", folder / "b.7z"]
            for path in sources:
                path.write_bytes(b"source")
            root = TkinterDnD.Tk()
            root.withdraw()
            try:
                app = ArchiveApp(root, preset_path=folder / "presets.json")
                app._add_paths(sources)
                rows = {path: iid for iid, path in app.rows.items()}
                failed, successful = rows[sources[0]], rows[sources[1]]
                app.outcomes[failed] = JobOutcome(sources[0], "failed")
                app.outcomes[successful] = JobOutcome(sources[1], "success", folder / "b_解压")
                app.row_statuses[successful] = "完成"
                app._refresh_preview()
                self.assertEqual(app.table.item(successful, "values")[2], "完成")
                with patch.object(app, "_start_extract") as start:
                    app._retry_failed()
                start.assert_called_once_with([failed])
                app.events.put(("progress", JobProgress(sources[0], "cancelled")))
                app._process_events()
                self.assertEqual(app.table.item(failed, "values")[2], "已取消")
            finally:
                root.destroy()

    def test_gui_export_removes_passwords_from_all_batches(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            root = TkinterDnD.Tk()
            root.withdraw()
            try:
                app = ArchiveApp(root, preset_path=folder / "presets.json")
                app.session_secrets.add("old@secret")
                app.password_vars[0].set("new@secret")
                app._log("old@secret new@secret")
                with patch("archive_gui.filedialog.asksaveasfilename", return_value=str(folder / "log.txt")):
                    app._export_log()
                exported = (folder / "log.txt").read_text(encoding="utf-8")
                self.assertNotIn("old@secret", exported)
                self.assertNotIn("new@secret", exported)
            finally:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
