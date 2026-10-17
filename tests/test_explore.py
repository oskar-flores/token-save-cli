"""Tests for `llocal run --explore`: read-only tools, citation checks, tool calling.

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.fake_server import start_fake

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


class VerifyTest(TreeCase):
    def test_good_answer_verifies(self):
        L.run_tool(self.root, "count", {"pattern": "ERROR"}, self.log)
        a = ('## Incident\nFirst error at 14:02:17 [logs/app.log:50] "14:02:17 ERROR db pool exhausted".\n'
             'There were 2 errors [count "ERROR" = 2].')
        v = L.verify_answer(a, self.root, self.log)
        self.assertEqual((v["claims"], v["verified"]), (2, 2))
        self.assertNotIn("[UNVERIFIED]", v["text"])

    def test_wrong_line_tolerance(self):
        a = 'First error [logs/app.log:52] "ERROR db pool exhausted".'      # within ±2
        self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 1)
        a = 'First error [logs/app.log:60] "ERROR db pool exhausted".'      # too far
        self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 0)

    def test_uncited_fact_is_unverified(self):
        a = 'First error [logs/app.log:50] "ERROR db pool exhausted". It happened 46 times.'
        v = L.verify_answer(a, self.root, self.log)
        self.assertEqual((v["claims"], v["verified"]), (2, 1))
        self.assertIn("46 times. [UNVERIFIED]", v["text"])
        self.assertEqual(len(v["failures"]), 1)

    def test_count_must_match_a_real_count_call(self):
        a = 'There were 3 errors [count "ERROR" = 3].'
        self.assertEqual(L.verify_answer(a, self.root, {"counts": {"ERROR": 2}})["verified"], 0)
        self.assertEqual(L.verify_answer(a, self.root, {"counts": {}})["verified"], 0)

    def test_quote_normalization(self):
        a = 'Error [logs/app.log:50] "14:02:17   ERROR  db pool exhausted".'
        self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 1)
        a = 'Error [logs/app.log:50] \u201c14:02:17 ERROR db pool exhausted\u201d.'
        self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 1)

    def test_quote_with_inner_quote_and_period(self):
        with open(os.path.join(self.root, "src", "a.py"), "a") as f:
            f.write('    print("done. Bye")\n')
        a = 'It prints a message [src/a.py:4] "print("done. Bye")". Main is defined [src/a.py:3] "def main():".'
        v = L.verify_answer(a, self.root, self.log)
        self.assertEqual((v["claims"], v["verified"]), (2, 2))

    def test_escape_path_fails_claim(self):
        a = 'Secret [../../etc/passwd:1] "root".'
        self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 0)

    def test_zero_citations_all_unverified(self):
        v = L.verify_answer("The pool was exhausted at 14:02.", self.root, self.log)
        self.assertEqual((v["claims"], v["verified"]), (1, 0))

    def test_wrong_number_with_real_citation_is_unverified(self):
        # the eval failure: a real line cited, but the claimed timestamp is not on it
        a = 'First error at 14:18:33 [logs/app.log:50] "ERROR db pool exhausted".'
        self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 0)
        a = 'First error at 14:02 [logs/app.log:50] "ERROR db pool exhausted".'
        self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 1)

    def test_headings_and_connectors_are_not_claims(self):
        a = '# Summary\nHere is what I found:\n- First error [logs/app.log:50] "ERROR db pool exhausted".'
        v = L.verify_answer(a, self.root, self.log)
        self.assertEqual((v["claims"], v["verified"]), (1, 1))
        self.assertTrue(v["text"].startswith("# Summary\nHere is what I found:\n- First error"))

    def test_single_file_root_accepts_bare_or_full_name(self):
        root, one = L.explore_root(os.path.join(self.root, "logs", "app.log"))
        log = {"counts": {}, "read_chars": 0, "only": one}
        for cite in ["app.log", "logs/app.log"]:
            a = f'First error [{cite}:50] "ERROR db pool exhausted".'
            self.assertEqual(L.verify_answer(a, root, log)["verified"], 1, cite)


class ChatToolsTest(unittest.TestCase):
    cfg = {"request_timeout": 10, "ctx": 16384, "ollama_url": "http://127.0.0.1:9"}

    def fake(self, script):
        url, reqs, stop = start_fake(script)
        self.addCleanup(stop)
        return {"id": "server:x#fake", "backend": "server", "name": "fake", "url": url}, reqs

    def test_openai_tool_calls_parsed(self):
        m, reqs = self.fake([{"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": '{"pattern":"ERROR"}'}}]}])
        r = L.chat(m, [{"role": "user", "content": "q"}], self.cfg, tools=L.TOOL_SPECS)
        self.assertEqual(r["tool_calls"], [{"id": "c1", "name": "grep", "args": {"pattern": "ERROR"}}])
        self.assertEqual(reqs[0]["tools"], L.TOOL_SPECS)
        self.assertEqual(r["message"]["tool_calls"][0]["id"], "c1")

    def test_unparsable_arguments_become_empty(self):
        m, _ = self.fake([{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": "{oops"}}]}])
        r = L.chat(m, [{"role": "user", "content": "q"}], self.cfg, tools=L.TOOL_SPECS)
        self.assertEqual(r["tool_calls"][0]["args"], {})

    def test_no_tools_key_when_not_asked(self):
        m, reqs = self.fake([{"role": "assistant", "content": "hi"}])
        r = L.chat(m, [{"role": "user", "content": "q"}], self.cfg)
        self.assertNotIn("tools", reqs[0])
        self.assertEqual((r["text"], r["tool_calls"]), ("hi", []))

    def test_tools_rejected_is_runtime_error(self):
        m, _ = self.fake([{"status": 400, "body": "model does not support tools"}])
        with self.assertRaisesRegex(RuntimeError, "does not support tool calling"):
            L.chat(m, [{"role": "user", "content": "q"}], self.cfg, tools=L.TOOL_SPECS)

    def test_tool_result_message_formats(self):
        call = {"id": "c1", "name": "grep", "args": {}}
        self.assertEqual(L.tool_result_message({"backend": "server"}, call, "out"),
                         {"role": "tool", "tool_call_id": "c1", "content": "out"})
        self.assertEqual(L.tool_result_message({"backend": "ollama"}, call, "out"),
                         {"role": "tool", "tool_name": "grep", "content": "out"})

    def test_ollama_tool_calls_parsed(self):
        m = {"id": "ollama:x", "backend": "ollama", "name": "x"}
        reply = {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "count", "arguments": {"pattern": "ERROR"}}}]}, "eval_count": 5}
        sent = []
        orig = (L.http_json, L.stop_server, L.unload_ollama_all)
        L.http_json = lambda url, payload=None, **k: sent.append(payload) or reply
        L.stop_server = lambda quiet=False: None
        L.unload_ollama_all = lambda cfg, keep=None: None
        try:
            r = L.chat(m, [{"role": "user", "content": "q"}], self.cfg, tools=L.TOOL_SPECS)
        finally:
            L.http_json, L.stop_server, L.unload_ollama_all = orig
        self.assertEqual(r["tool_calls"], [{"id": "call_0", "name": "count", "args": {"pattern": "ERROR"}}])
        self.assertEqual(sent[0]["tools"], L.TOOL_SPECS)


def tool_call(name, args):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def final(text):
    return {"role": "assistant", "content": text}


class ExploreRunTest(TreeCase):
    def run_cli(self, script, argv, cfg=None):
        url, self.requests, stop = start_fake(script)
        self.addCleanup(stop)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        conf = {"servers": [url], "ollama_url": "http://127.0.0.1:9", "gguf_dirs": [], **(cfg or {})}
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump(conf, f)
        scores = {c: 1.0 for c in L.CATEGORIES}
        with open(os.path.join(self.home, "bench.json"), "w") as f:
            json.dump({f"server:{url}#fake": {"scores": scores, "overall": 1, "tps": 50, "suite": L.SUITE_VERSION,
                                               "tools": 1.0, "tools_suite": 1}}, f)
        env = dict(os.environ, LLOCAL_HOME=self.home)
        return subprocess.run([sys.executable, SCRIPT] + argv, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def last_usage(self):
        with open(os.path.join(self.home, "usage.jsonl")) as f:
            return json.loads(f.read().splitlines()[-1])

    def test_explore_verified_exit0(self):
        script = [tool_call("count", {"pattern": "ERROR"}),
                  tool_call("grep", {"pattern": "ERROR"}),
                  final('First error [logs/app.log:50] "14:02:17 ERROR db pool exhausted". '
                        'There were 2 errors [count "ERROR" = 2].')]
        p = self.run_cli(script, ["run", "--task", "logs", "--explore", self.root, "When did it fail?"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("verified 2/2 claims", p.stderr)
        self.assertIn("--model", p.stderr)                      # feedback hint is exact
        self.assertIn("--task tools", p.stderr)
        row = self.last_usage()
        self.assertTrue(row["explore"])
        self.assertEqual((row["rounds"], row["claims"], row["verified"]), (2, 2, 2))
        self.assertGreater(row["in_ctx"], 0)
        self.assertEqual(self.requests[0]["tools"], L.TOOL_SPECS)
        tool_msgs = [m for m in self.requests[2]["messages"] if m["role"] == "tool"]
        self.assertIn("total: 2 matching lines", tool_msgs[0]["content"])

    def test_explore_unverified_exit5_after_one_retry(self):
        bad = final("It failed 46 times at 14:18.")
        p = self.run_cli([bad, bad], ["run", "--task", "logs", "--explore", self.root, "q"])
        self.assertEqual(p.returncode, 5, p.stderr)
        self.assertIn("[UNVERIFIED]", p.stdout)
        self.assertEqual(len(self.requests), 2)   # exactly one retry
        self.assertIn("could not verify", self.requests[1]["messages"][-1]["content"])
        self.assertFalse(self.last_usage()["verified"] == self.last_usage()["claims"])

    def test_retry_that_fixes_the_answer_exits0(self):
        p = self.run_cli([final("It failed at 14:18."),
                          final('It failed at 14:02:17 [logs/app.log:50] "ERROR db pool exhausted".')],
                         ["run", "--task", "logs", "--explore", self.root, "q"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("[UNVERIFIED]", p.stdout)

    def test_uncited_prose_only_answer_is_unverified(self):
        bad = final("The database pool ran out.")
        p = self.run_cli([bad, bad], ["run", "--task", "logs", "--explore", self.root, "q"])
        self.assertEqual(p.returncode, 5, p.stderr)

    def test_round_cap_forces_answer(self):
        loop = tool_call("list_files", {"glob": "*"})
        p = self.run_cli([loop] * 3 + [final('x [logs/app.log:50] "ERROR db pool exhausted".')],
                         ["run", "--task", "logs", "--explore", self.root, "q"], cfg={"tool_rounds": 3})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.requests), 4)
        self.assertNotIn("tools", self.requests[-1])

    def test_single_file_explore(self):
        script = [tool_call("grep", {"pattern": "ERROR"}),
                  final('First error [app.log:50] "ERROR db pool exhausted".')]
        p = self.run_cli(script, ["run", "--task", "logs", "--explore",
                                  os.path.join(self.root, "logs", "app.log"), "q"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("app.log: 300 lines", self.requests[0]["messages"][-1]["content"])

    def test_out_file_gets_marked_text(self):
        out = os.path.join(tempfile.mkdtemp(), "ans.md")
        bad = final("It failed 46 times.")
        p = self.run_cli([bad], ["run", "--explore", self.root, "--out", out, "q"])
        self.assertEqual(p.returncode, 5, p.stderr)
        with open(out) as f:
            self.assertIn("[UNVERIFIED]", f.read())

    def test_explore_rejects_files(self):
        for extra in (["-f", "x"], ["--glob", "*.py"], ["--json"], ["-"]):
            p = self.run_cli([], ["run", "--explore", self.root] + extra + ["q"])
            self.assertEqual(p.returncode, 1, extra)
            self.assertIn("can't be combined", p.stderr)

    def test_explore_missing_path(self):
        p = self.run_cli([], ["run", "--explore", "/nope", "q"])
        self.assertEqual(p.returncode, 1)

    def test_tools_rejected_exit2(self):
        p = self.run_cli([{"status": 400, "body": "model does not support tools"}],
                         ["run", "--explore", self.root, "q"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("does not support tool calling", p.stderr)

    def test_no_tools_score_exit3(self):
        self.run_cli([], ["doctor"])
        with open(os.path.join(self.home, "bench.json")) as f:
            b = json.load(f)
        for v in b.values():
            v.pop("tools"), v.pop("tools_suite")
        with open(os.path.join(self.home, "bench.json"), "w") as f:
            json.dump(b, f)
        env = dict(os.environ, LLOCAL_HOME=self.home)
        p = subprocess.run([sys.executable, SCRIPT, "run", "--explore", self.root, "q"], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 3, p.stderr)

    def test_feedback_on_explore_run_rates_tools(self):
        script = [final('First error [logs/app.log:50] "ERROR db pool exhausted".')]
        self.run_cli(script, ["run", "--task", "logs", "--explore", self.root, "q"])
        env = dict(os.environ, LLOCAL_HOME=self.home)
        p = subprocess.run([sys.executable, SCRIPT, "feedback", "ok"], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("'tools'", p.stdout)
        p = subprocess.run([sys.executable, SCRIPT, "run", "--task", "tools", "x"], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 1)


if __name__ == "__main__":
    unittest.main()
