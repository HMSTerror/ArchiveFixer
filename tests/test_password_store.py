import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from password_store import load_password_presets, save_password_presets


@unittest.skipUnless(os.name == "nt", "DPAPI requires Windows")
class PasswordStoreTests(unittest.TestCase):
    def test_encrypted_round_trip_and_legacy_migration(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "presets.json"
            values = ["示例@abc", "second", "", "", ""]
            path.write_text(json.dumps({"passwords": values}), encoding="utf-8")
            self.assertEqual(load_password_presets(path, ""), values)
            save_password_presets(path, values)
            self.assertNotIn("passwords", path.read_text())
            self.assertNotIn("second", path.read_text())
            self.assertEqual(load_password_presets(path, ""), values)

    def test_failed_save_preserves_previous_file(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "presets.json"
            values = ["first", "", "", "", ""]
            save_password_presets(path, values)
            before = path.read_bytes()
            with patch.object(Path, "replace", side_effect=PermissionError("locked")), self.assertRaises(OSError):
                save_password_presets(path, ["other", "", "", "", ""])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_corrupt_or_foreign_encrypted_data_returns_empty_presets(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "presets.json"
            path.write_text('{"protection":"windows-dpapi","data":"YmFk"}')
            self.assertEqual(load_password_presets(path, ""), [""] * 5)


if __name__ == "__main__":
    unittest.main()
