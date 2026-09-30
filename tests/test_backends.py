"""Optional vendor runtime tests. Set ARCHIVEFIXER_WINRAR / ARCHIVEFIXER_BANDIZIP.

Vendor programs remain external; fixtures contain synthetic data only.
"""

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
import zipfile

from archive_engine import ExtractionEngine, ExtractionJob, expand_dropped_folder, discover_archive_inputs
from archive_extractors import find_extractor


class BackendRuntimeTests(unittest.TestCase):
    def test_five_archives_and_three_folders_produce_eight_results(self):
        executable = find_extractor("7zip")
        if executable is None:
            self.skipTest("7-Zip runtime unavailable")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(8):
                parent = root
                if index >= 5:
                    parent = root / f"folder_{index}"
                    parent.mkdir()
                with zipfile.ZipFile(parent / f"item_{index}.zip", "w") as stream:
                    stream.writestr("payload.txt", str(index))
            inputs = expand_dropped_folder(root)
            self.assertEqual(len(inputs), 8)
            jobs = [ExtractionJob(path, path.stem if path.is_file() else path.name,
                                  path.is_dir(), tuple(discover_archive_inputs(path)) if path.is_dir() else ())
                    for path in inputs]
            result = ExtractionEngine(executable, "", lambda message: None).run(jobs, None, True, max_concurrent=4)
            self.assertEqual((result.successful_jobs, result.failed_jobs, result.final_files), (8, 0, 8))
            self.assertEqual(len(list(root.iterdir())), 8)
            for index in range(8):
                folder = root / (f"item_{index}_解压" if index < 5 else f"folder_{index}")
                self.assertEqual([path.name for path in folder.iterdir()], ["payload.txt"])
                self.assertEqual((folder / "payload.txt").read_text(), str(index))

    def check_corruption(self, backend):
        override = os.environ.get(f"ARCHIVEFIXER_{backend.upper()}")
        executable = Path(override) if override else find_extractor(backend)
        if executable is None or not executable.is_file():
            self.skipTest(f"{backend} runtime unavailable")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "corrupt.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as stream:
                stream.writestr("good.txt", b"complete file")
                stream.writestr("bad.txt", b"damaged content")
            data = archive.read_bytes().replace(b"damaged content", b"Damaged content", 1)
            archive.write_bytes(data)
            messages = []
            result = ExtractionEngine(executable, "", messages.append, backend).run(
                [ExtractionJob(archive, "corrupt")], None, True)
            self.assertEqual((result.successful_jobs, result.failed_jobs), (0, 1), "\n".join(messages))
            self.assertEqual(archive.read_bytes(), data)
            self.assertFalse((root / "corrupt_解压").exists())

    def test_7zip_partial_crc_error_preserves_original(self):
        self.check_corruption("7zip")

    def test_winrar_partial_crc_error_preserves_original(self):
        self.check_corruption("winrar")

    def test_bandizip_partial_crc_error_preserves_original(self):
        self.check_corruption("bandizip")

    def check_backend(self, backend):
        creator = find_extractor("7zip")
        override = os.environ.get(f"ARCHIVEFIXER_{backend.upper()}")
        executable = Path(override) if override else find_extractor(backend)
        if creator is None or executable is None or not executable.is_file():
            self.skipTest(f"{backend} runtime or fixture creator is unavailable")
        for format_name in ("zip", "7z", "split"):
            with self.subTest(format=format_name), TemporaryDirectory() as temporary:
                root = Path(temporary)
                payload = root / "中文 内容.txt"
                content = bytes(range(256)) * 32
                payload.write_bytes(content)
                # ZIP writers can restrict passwords to ASCII even with AES.
                password = "Test@abc" if format_name == "zip" else "测试@abc"
                inner = root / ("inner.zip" if format_name == "zip" else "inner.7z")
                args = [str(creator), "a", "-y", "-m0=Copy", f"-p{password}", str(inner), payload.name]
                if format_name == "zip":
                    args.remove("-m0=Copy")
                    args.append("-mem=AES256")
                if format_name == "split":
                    args += ["-v2k", "-mhe=on"]
                subprocess.run(args, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
                nested = root / "nested"
                nested.mkdir()
                for archive in root.glob("inner.*"):
                    target = nested / (archive.name + "删" if format_name == "split" else archive.name)
                    archive.rename(target)
                outer = root / "outer.7z"
                subprocess.run([str(creator), "a", "-y", f"-p{password}", str(outer), "nested"],
                               cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
                messages = []
                result = ExtractionEngine(executable, ["incorrect", password], messages.append, backend).run(
                    [ExtractionJob(outer, "outer")], None, True, max_concurrent=1)
                self.assertEqual(result.errors, (), "\n".join(messages))
                self.assertEqual(result.warnings, (), "\n".join(messages))
                self.assertEqual((result.successful_jobs, result.layers), (1, 2))
                self.assertEqual((root / "outer_解压" / "nested" / payload.name).read_bytes(), content)
                self.assertFalse(outer.exists())
                self.assertFalse(any(password in line for line in messages))

    def test_7zip_nested_formats_passwords_and_split(self):
        self.check_backend("7zip")

    def test_winrar_nested_formats_passwords_and_split(self):
        self.check_backend("winrar")

    def test_bandizip_nested_formats_passwords_and_split(self):
        self.check_backend("bandizip")


if __name__ == "__main__":
    unittest.main()
