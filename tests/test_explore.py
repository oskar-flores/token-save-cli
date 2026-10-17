"""Tests for `llocal run --explore`: read-only tools, citation checks, tool calling.

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "skills", "local-offload", "scripts", "llocal.py")


def load_llocal():
    os.environ["LLOCAL_HOME"] = tempfile.mkdtemp()
    spec = importlib.util.spec_from_file_location("llocal", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = load_llocal()
ERROR_LINE = "2026-10-14T14:02:17 ERROR db pool exhausted"


def make_tree(root):
    """src/a.py, logs/app.log (300 lines, ERROR at 50 and 90), .git, a binary, one huge line, a symlink out."""
    os.makedirs(os.path.join(root, "src"))
    os.makedirs(os.path.join(root, "logs"))
    os.makedirs(os.path.join(root, ".git"))
    with open(os.path.join(root, "src", "a.py"), "w") as f:
        f.write("import os\n\ndef main():\n")
    lines = [f"2026-10-14T14:00:{i % 60:02d} INFO tick {i}" for i in range(1, 301)]
    lines[49] = ERROR_LINE
    lines[89] = "2026-10-14T14:05:00 ERROR db pool exhausted again"
    with open(os.path.join(root, "logs", "app.log"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(root, ".git", "config"), "w") as f:
        f.write("ERROR x\n")
    with open(os.path.join(root, "img.bin"), "wb") as f:
        f.write(b"ERROR\x00x\x00")
    with open(os.path.join(root, "big.log"), "w") as f:
        f.write("x" * 50000 + "\n")
    os.symlink("/etc", os.path.join(root, "out"))


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        make_tree(self.root)
        self.log = {"counts": {}, "read_chars": 0}


class ToolsTest(TreeCase):
    def test_safe_path_rejects_escape(self):
        for bad in ["../x", "/etc/passwd", "out/passwd"]:
            with self.assertRaises(L.ToolError):
                L.safe_path(self.root, bad)

    def test_grep_hits_and_total(self):
        r = L.run_tool(self.root, "grep", {"pattern": "ERROR", "glob": "logs/*"}, self.log)
        self.assertIn("logs/app.log:50:", r)
        self.assertIn("logs/app.log:90:", r)
        self.assertIn("2 matches", r)

    def test_grep_bad_regex_is_error_text(self):
        self.assertTrue(L.run_tool(self.root, "grep", {"pattern": "(foo"}, self.log).startswith("error:"))

    def test_bad_args_are_error_text(self):
        self.assertTrue(L.run_tool(self.root, "read_lines", {"path": "logs/app.log", "start": "a"},
                                   self.log).startswith("error:"))
        self.assertTrue(L.run_tool(self.root, "nope", {}, self.log).startswith("error:"))

    def test_count_records_log(self):
        L.run_tool(self.root, "count", {"pattern": "ERROR"}, self.log)
        self.assertEqual(self.log["counts"]["ERROR"], 2)

    def test_read_lines_numbered_and_capped(self):
        r = L.run_tool(self.root, "read_lines", {"path": "logs/app.log", "start": 1, "end": 999}, self.log)
        self.assertTrue(r.startswith("1: "))
        self.assertEqual(len(r.splitlines()), L.READ_CAP + 1)  # +1 truncation note
        self.assertEqual(self.log["read_chars"], len(r))

    def test_skips_git_binary_and_truncates_long_lines(self):
        r = L.run_tool(self.root, "list_files", {"glob": "**/*"}, self.log)
        self.assertNotIn(".git", r)
        self.assertNotIn("img.bin", r)
        self.assertNotIn("passwd", r)
        self.assertIn("src/a.py", r)
        g = L.run_tool(self.root, "grep", {"pattern": "x"}, self.log)
        self.assertIn("big.log:1:", g)
        self.assertTrue(all(len(line) <= L.LINE_CAP + 40 for line in g.splitlines()))

    def test_single_file_root(self):
        root, one = L.explore_root(os.path.join(self.root, "logs/app.log"))
        self.assertEqual(one, "app.log")
        self.assertIn("app.log:50:", L.run_tool(root, "grep", {"pattern": "ERROR"}, self.log))

    def test_single_file_root_limits_tools(self):
        root, one = L.explore_root(os.path.join(self.root, "src/a.py"))
        log = {"counts": {}, "read_chars": 0, "only": one}
        os.makedirs(os.path.join(root, "x"))
        with open(os.path.join(root, "x", "b.py"), "w") as f:
            f.write("import sys\n")
        self.assertNotIn("b.py", L.run_tool(root, "grep", {"pattern": "import"}, log))
        self.assertTrue(L.run_tool(root, "read_lines", {"path": "x/b.py", "start": 1, "end": 1},
                                   log).startswith("error:"))

    def test_missing_root_fails(self):
        with self.assertRaises(L.Fail) as c:
            L.explore_root("/nope/nothing")
        self.assertEqual(c.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
