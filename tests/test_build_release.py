from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from build_release import publish, require_local_child


class ReleaseBuildTests(unittest.TestCase):
    def test_publication_failure_restores_previous_outputs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, dist = root / "stage", root / "dist"
            stage.mkdir()
            dist.mkdir()
            for folder, text in ((stage, "new"), (dist, "old")):
                for name in ("package.zip", "hashes.txt"):
                    (folder / name).write_text(text)
            rename = Path.rename

            def fail_second(source, target):
                if source == stage / "hashes.txt":
                    raise PermissionError("locked")
                return rename(source, target)

            with patch.object(Path, "rename", fail_second), self.assertRaises(RuntimeError):
                publish([stage / "package.zip", stage / "hashes.txt"], dist)
            self.assertEqual((dist / "package.zip").read_text(), "old")
            self.assertEqual((dist / "hashes.txt").read_text(), "old")
            self.assertEqual({path.name for path in dist.iterdir()}, {"package.zip", "hashes.txt"})

    def test_linked_build_directory_is_rejected(self):
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            external = root / "external"
            external.mkdir()
            junction = root / "dist"
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if result.returncode:
                self.skipTest("Cannot create a Windows junction")
            with self.assertRaises(RuntimeError):
                require_local_child(junction, root)


if __name__ == "__main__":
    unittest.main()
