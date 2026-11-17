"""remove_tree survives an rmtree handler call it cannot repeat."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import wtjanitor  # noqa: E402


class RemoveTreeTest(unittest.TestCase):
    def test_removes_a_tree_with_a_read_only_folder(self):
        root = Path(tempfile.mkdtemp())
        locked = root / "a" / "b"
        locked.mkdir(parents=True)
        (locked / "f.txt").write_text("x")
        os.chmod(locked, 0o500)
        self.assertTrue(wtjanitor.remove_tree(root))

    def test_removes_a_plain_file(self):
        root = Path(tempfile.mkdtemp())
        target = root / "stale.log"
        target.write_text("x")
        self.assertTrue(wtjanitor.remove_tree(target))
        self.assertFalse(target.exists())
        os.rmdir(root)

    def test_removes_a_symlink(self):
        root = Path(tempfile.mkdtemp())
        target = root / "link"
        target.symlink_to(root / "missing")
        self.assertTrue(wtjanitor.remove_tree(target))
        self.assertFalse(target.is_symlink())
        os.rmdir(root)

    def test_handler_given_os_open_does_not_raise(self):
        original = wtjanitor.shutil.rmtree

        def fake_rmtree(path, onexc=None, onerror=None):
            handler = onexc or onerror
            handler(os.open, path, OSError("denied"))

        wtjanitor.shutil.rmtree = fake_rmtree
        try:
            self.assertFalse(wtjanitor.remove_tree(tempfile.mkdtemp()))
        finally:
            wtjanitor.shutil.rmtree = original


if __name__ == "__main__":
    unittest.main()
