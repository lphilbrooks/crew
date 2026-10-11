"""Scratch pruning and the shared build cache. No vendor CLIs or network needed."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREW = ROOT / "skills" / "crew" / "scripts" / "crew.py"
sys.path.insert(0, str(CREW.parent))
import crew  # noqa: E402


def temp_dir():
    try:
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    except TypeError:  # Python < 3.10
        return tempfile.TemporaryDirectory()


def write(path: Path, size: int = 10, text: str = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text, encoding="utf-8")
    else:
        path.write_bytes(b"x" * size)


def make_link(link: Path, target: Path) -> bool:
    """A directory symlink, or a junction on Windows where symlinks need privileges."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return r.returncode == 0
    return False


class TestPruneScratch(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.scratch = Path(self.tmp.name) / "scratch"

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_output_goes_evidence_stays(self):
        write(self.scratch / "target" / "debug" / "deps" / "lib.rlib", 5000)
        write(self.scratch / "target" / "big-artifact.bin", crew.KEEP_LOOSE_BYTES + 1)
        write(self.scratch / "target" / "plan-final-verify.log", text="test result: ok. 8 passed")
        write(self.scratch / "copy" / "src" / "main.rs", text="fn main() {}")
        write(self.scratch / "copy" / "target" / "release" / "app", 7000)
        write(self.scratch / "web" / "node_modules" / "left-pad" / "index.js", 300)
        write(self.scratch / "notes.txt", text="probe output")
        freed, errors = crew.prune_scratch(self.scratch)
        self.assertEqual(errors, 0)
        self.assertEqual(freed, 5000 + crew.KEEP_LOOSE_BYTES + 1 + 7000 + 300)
        self.assertFalse((self.scratch / "target" / "debug").exists())
        self.assertFalse((self.scratch / "target" / "big-artifact.bin").exists())
        self.assertTrue((self.scratch / "target" / "plan-final-verify.log").exists())
        self.assertTrue((self.scratch / "copy" / "src" / "main.rs").exists())
        self.assertFalse((self.scratch / "copy" / "target" / "release").exists())
        self.assertFalse((self.scratch / "web" / "node_modules" / "left-pad").exists())
        self.assertTrue((self.scratch / "notes.txt").exists())

    def test_links_are_never_followed(self):
        outside = Path(self.tmp.name) / "precious"
        write(outside / "keep.txt", text="must survive")
        (self.scratch / "target").mkdir(parents=True)
        if not make_link(self.scratch / "target" / "linked", outside):
            self.skipTest("cannot create a directory link here")
        write(self.scratch / "target" / "debug" / "x.o", 100)
        crew.prune_scratch(self.scratch)
        self.assertTrue((outside / "keep.txt").exists())
        self.assertFalse(os.path.lexists(self.scratch / "target" / "linked"))

    def test_link_named_like_a_build_folder_is_not_entered(self):
        outside = Path(self.tmp.name) / "elsewhere"
        write(outside / "debug" / "keep.bin", 100)
        self.scratch.mkdir(parents=True)
        if not make_link(self.scratch / "target", outside):
            self.skipTest("cannot create a directory link here")
        crew.prune_scratch(self.scratch)
        self.assertTrue((outside / "debug" / "keep.bin").exists())

    def test_read_only_files_are_removed(self):
        f = self.scratch / "target" / "debug" / ".git" / "objects" / "ab"
        write(f, 50)
        os.chmod(f, 0o444)
        freed, errors = crew.prune_scratch(self.scratch)
        self.assertEqual((freed, errors), (50, 0))

    def test_missing_scratch_is_fine(self):
        self.assertEqual(crew.prune_scratch(self.scratch), (0, 0))


class TestSharedCache(unittest.TestCase):
    def test_one_folder_per_repository(self):
        a = crew.shared_cache("/repos/alpha", "/repos/alpha/sub")
        self.assertEqual(a, crew.shared_cache("/repos/alpha", "/elsewhere"))
        self.assertNotEqual(a, crew.shared_cache("/repos/beta", None))
        self.assertTrue(a.name.startswith("alpha-"))
        self.assertEqual(a.parent, crew.CREW_HOME / "cache")


if __name__ == "__main__":
    unittest.main()
