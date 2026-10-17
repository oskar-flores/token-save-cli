# llocal explore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `llocal run --explore PATH` lets a local model answer questions over a whole repo or a
multi-MB log through read-only tools, and llocal mechanically verifies every cited claim before
the agent sees the answer.

**Architecture:** Everything stays in `skills/local-offload/scripts/llocal.py`. Four new
layers: path-safe read-only tools, a citation verifier, tool-calling support in `chat()`, and
an explore loop wired into `cmd_run`. A separate tools benchmark gives routing a `tools` score
without touching the existing categories. A new stdlib `unittest` suite with a scripted fake
OpenAI-compatible server tests everything without a GPU.

**Tech Stack:** Python 3 standard library only (`unittest`, `http.server`, `re`, `glob`, `json`).
Backends: Ollama `/api/chat` and OpenAI-compatible `/v1/chat/completions`, both with `tools`.

**Spec:** `docs/superpowers/specs/2026-10-17-local-explore-design.md`

## Global Constraints

- `llocal.py` runs with plain `python3`: standard library only, single file.
- Expected errors are `Fail(code, msg, hint)`; no tracebacks for input or backend failures.
- Exit codes 0–4 keep their meaning. New: **5 = answered, some claims unverified**.
- Never two models loaded at once; never kill processes llocal didn't start.
- Tools are read-only and never reach outside the root (`realpath` check, symlinks included).
- `run` never reads stdin unless `-` is passed.
- Tests always set `LLOCAL_HOME` to a temp dir.
- README.md, SKILL.md and agents/local-worker.md change together; all text in English.
- `tools` is not in `CATEGORIES`; `overall` and routing for plain `run` are unchanged.
- "verified N/N" counts claims (every factual bullet/sentence), not only citations present.

## Deviation from the spec (my call)

The spec says `SUITE_VERSION → 4`. Bumping it makes `bench` re-run the whole suite, which the
spec's own note forbids. Instead: `SUITE_VERSION` stays 3 and the tools test gets its own
`TOOLS_SUITE_VERSION = 1`, stored per model as `tools_suite`. `bench` runs only the tools test
on models that lack a current one. Same outcome, no per-test merging needed.

## Review Focus

1. **Invalid regex from the model** (`grep` pattern `"(foo"`): the tool returns an error string
   to the model and the loop continues; no traceback. Test in Task 1.
2. **Model never stops calling tools:** at `tool_rounds` llocal sends a final message without
   `tools` that asks for the answer now; the run still ends with an answer or exit 2. Test in Task 4.
3. **Backend rejects tools** (Ollama 400 "does not support tools", or a server ignoring
   `tools`): exit 2 with a hint to pick a tools-capable model, not a crash. Test in Task 3.
4. **Quotes that differ only in whitespace, case of smart quotes, or contain `"`:** normalize
   whitespace and curly quotes before matching; a quote with an inner `"` is matched up to the
   last `"` before the next citation or end of line. Test in Task 2.
5. **Binary files, `.git`, and a single 50 KB line in a log:** skipped / truncated in every tool,
   so one call can't flood the context. Test in Task 1.

---

### Task 1: Read-only tools

**Files:**
- Modify: `skills/local-offload/scripts/llocal.py` (new section `# ---- explore tools`, after `read_files`)
- Create: `tests/test_explore.py`, `tests/__init__.py` (empty)

**Interfaces:**
- Produces:
  - `explore_root(path: str) -> tuple[str, str|None]`: `(root_dir, single_file_rel_or_None)`; raises `Fail(1, ...)` if missing.
  - `safe_path(root: str, rel: str) -> str`: absolute real path; raises `ToolError` if outside root or missing.
  - `class ToolError(Exception)`: message goes back to the model as the tool result.
  - `run_tool(root: str, name: str, args: dict, log: dict) -> str`: dispatches; catches `ToolError`, `re.error`, `TypeError`, returns `"error: ..."`. Records `log["counts"][pattern] = total` for `count` and `log["read_chars"] += len(result)`.
  - `TOOL_SPECS: list[dict]`: OpenAI-style `{"type":"function","function":{name, description, parameters}}` for `list_files{glob}`, `grep{pattern, glob?}`, `read_lines{path, start, end}`, `count{pattern, glob?}`.
  - Caps (module constants): `LIST_CAP = 200`, `GREP_CAP = 50`, `READ_CAP = 200` lines, `LINE_CAP = 400` chars.
- Test helper (in `tests/test_explore.py`): `load_llocal()` imports the script via `importlib.util.spec_from_file_location` after setting `os.environ["LLOCAL_HOME"]` to a `tempfile.mkdtemp()`.

- [ ] **Step 1: Write failing tests** (class `ToolsTest`, fixture tree built in `setUp` under a temp dir: `src/a.py` 3 lines, `logs/app.log` with 120 lines where lines 50 and 90 contain `ERROR`, a `.git/config`, a binary `img.bin` with `\x00`, `big.log` with one 50 000-char line, and a symlink `out -> /etc`).

```python
def test_safe_path_rejects_escape(self):
    for bad in ["../x", "/etc/passwd", "out/passwd"]:
        with self.assertRaises(L.ToolError):
            L.safe_path(self.root, bad)

def test_grep_hits_and_total(self):
    r = L.run_tool(self.root, "grep", {"pattern": "ERROR", "glob": "logs/*"}, self.log)
    self.assertIn("logs/app.log:50:", r); self.assertIn("logs/app.log:90:", r); self.assertIn("2 matches", r)

def test_grep_bad_regex_is_error_text(self):
    self.assertTrue(L.run_tool(self.root, "grep", {"pattern": "(foo"}, self.log).startswith("error:"))

def test_count_records_log(self):
    L.run_tool(self.root, "count", {"pattern": "ERROR"}, self.log)
    self.assertEqual(self.log["counts"]["ERROR"], 2)

def test_read_lines_numbered_and_capped(self):
    r = L.run_tool(self.root, "read_lines", {"path": "logs/app.log", "start": 1, "end": 999}, self.log)
    self.assertTrue(r.startswith("1: ")); self.assertEqual(len(r.splitlines()), L.READ_CAP + 1)  # +1 truncation note

def test_skips_git_binary_and_truncates_long_lines(self):
    r = L.run_tool(self.root, "list_files", {"glob": "**/*"}, self.log)
    self.assertNotIn(".git", r); self.assertNotIn("img.bin", r)
    g = L.run_tool(self.root, "grep", {"pattern": "x"}, self.log)
    self.assertTrue(all(len(line) <= L.LINE_CAP + 40 for line in g.splitlines()))

def test_single_file_root(self):
    root, one = L.explore_root(os.path.join(self.root, "logs/app.log"))
    self.assertEqual(one, "app.log")
    self.assertIn("app.log:50:", L.run_tool(root, "grep", {"pattern": "ERROR"}, self.log))
```

- [ ] **Step 2: Run, expect failures:** `python3 -m unittest tests.test_explore -v` → `AttributeError: ... has no attribute 'safe_path'`.

- [ ] **Step 3: Implement the functions and constants above.** Walk with `os.walk`, pruning `.git`, `node_modules`, `__pycache__`; a file is binary if its first 4 KB contain `\x00`. Paths in results are root-relative. For a single-file root, `root_dir` is the file's directory and every tool is limited to that one file. `grep` output ends with `"(N matches, showing M)"`.

- [ ] **Step 4: Run, expect PASS:** `python3 -m unittest tests.test_explore -v`

- [ ] **Step 5: Commit** — `git add tests skills/local-offload/scripts/llocal.py && git commit -m "Add read-only explore tools"`

---

### Task 2: Citation verifier

**Files:**
- Modify: `skills/local-offload/scripts/llocal.py` (section `# ---- citations`)
- Test: `tests/test_explore.py` (class `VerifyTest`)

**Interfaces:**
- Consumes: `safe_path`, `ToolError` (Task 1); `log["counts"]`.
- Produces:
  - Citation syntax (also used verbatim in the system prompt, Task 4): line citation `[path:LINE] "exact quote"`; count citation `[count "PATTERN" = N]`.
  - `verify_answer(answer: str, root: str, log: dict) -> dict` with keys `text` (answer with `[UNVERIFIED]` appended to each failed claim), `claims: int`, `verified: int`, `failures: list[str]` (one human-readable reason per failed claim, e.g. `"logs/app.log:51: quote not found within ±2 lines"`).
  - Claim unit: each non-empty line; a line that is a bullet or paragraph is split into sentences on `(?<=[.!?])\s+(?=[A-Z\[])`. Exempt (not a claim): Markdown headings, lines ending in `:`, and sentences with no digit, no `/`, no `` ` `` and no quote. A claim passes only if it has ≥1 citation and all its citations verify.

- [ ] **Step 1: Write failing tests** (same fixture as Task 1; line 50 of `logs/app.log` is `2026-10-14T14:02:17 ERROR db pool exhausted`).

```python
def test_good_answer_verifies(self):
    L.run_tool(self.root, "count", {"pattern": "ERROR"}, self.log)
    a = ('## Incident\nFirst error at 14:02:17 [logs/app.log:50] "14:02:17 ERROR db pool exhausted".\n'
         'There were 2 errors [count "ERROR" = 2].')
    v = L.verify_answer(a, self.root, self.log)
    self.assertEqual((v["claims"], v["verified"]), (2, 2)); self.assertNotIn("[UNVERIFIED]", v["text"])

def test_wrong_line_tolerance(self):
    a = 'First error [logs/app.log:52] "ERROR db pool exhausted".'      # within ±2
    self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 1)
    a = 'First error [logs/app.log:60] "ERROR db pool exhausted".'      # too far
    self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 0)

def test_uncited_fact_is_unverified(self):
    a = 'First error [logs/app.log:50] "ERROR db pool exhausted". It happened 46 times.'
    v = L.verify_answer(a, self.root, self.log)
    self.assertEqual((v["claims"], v["verified"]), (2, 1)); self.assertIn("46 times. [UNVERIFIED]", v["text"])

def test_count_must_match_a_real_count_call(self):
    a = 'There were 3 errors [count "ERROR" = 3].'
    self.assertEqual(L.verify_answer(a, self.root, {"counts": {"ERROR": 2}})["verified"], 0)
    self.assertEqual(L.verify_answer(a, self.root, {"counts": {}})["verified"], 0)

def test_quote_normalization(self):
    a = 'Error [logs/app.log:50] "14:02:17   ERROR  db pool exhausted".'
    self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 1)
    a = 'Error [logs/app.log:50] “14:02:17 ERROR db pool exhausted”.'
    self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 1)

def test_escape_path_fails_claim(self):
    a = 'Secret [../../etc/passwd:1] "root".'
    self.assertEqual(L.verify_answer(a, self.root, self.log)["verified"], 0)

def test_zero_citations_all_unverified(self):
    v = L.verify_answer("The pool was exhausted at 14:02.", self.root, self.log)
    self.assertEqual((v["claims"], v["verified"]), (1, 0))
```

- [ ] **Step 2: Run, expect failures** (`no attribute 'verify_answer'`).
- [ ] **Step 3: Implement `verify_answer`.** Normalization: collapse whitespace, map `“”„` to `"`, `‘’` to `'`, case-sensitive substring match. For a single-file root, a citation path may be the bare file name.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `git commit -am "Add citation verifier for explore answers"`

---

### Task 3: Tool calling in `chat()` and the fake server

**Files:**
- Modify: `skills/local-offload/scripts/llocal.py:468-532` (`chat`, `_chat`)
- Create: `tests/fake_server.py`
- Test: `tests/test_explore.py` (class `ChatToolsTest`)

**Interfaces:**
- Produces:
  - `chat(model, messages, cfg, max_tokens=2048, temperature=0.2, tools=None) -> dict`: adds keys `tool_calls: list[{"id": str, "name": str, "args": dict}]` (empty when none) and `message: dict` (the assistant message to append to `messages`, in the backend's own format).
  - `tool_result_message(model: dict, call: dict, content: str) -> dict`: Ollama → `{"role":"tool","tool_name":name,"content":...}`; OpenAI → `{"role":"tool","tool_call_id":id,"content":...}`.
  - Ollama: `payload["tools"] = tools`; reply `message.tool_calls[].function.{name, arguments(dict)}`; synthesize `id` as `f"call_{i}"`. OpenAI: `payload["tools"] = tools`; reply `choices[0].message.tool_calls[].{id, function.{name, arguments(JSON str)}}`; unparsable `arguments` → `args = {}`.
  - HTTP 400 whose body mentions `tools` → `RuntimeError(f"{name} does not support tool calling; pick a model with a 'tool' score in `llocal sheet`")` (exit 2 via `main`).
  - `tests/fake_server.py`: `start_fake(script: list[dict]) -> (url: str, requests: list, stop: callable)`. Serves `/v1/models` (one model `fake`) and `/v1/chat/completions`, replying with `script` items in order (each item is the `message` object to return; last item repeats). Records every request body in `requests`. A script item `{"status": 400, "body": "..."}` returns that error.

- [ ] **Step 1: Write failing tests.**

```python
def test_openai_tool_calls_parsed(self):
    url, reqs, stop = start_fake([{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": '{"pattern":"ERROR"}'}}]}])
    self.addCleanup(stop)
    m = {"id": "server:x#fake", "backend": "server", "name": "fake", "url": url}
    r = L.chat(m, [{"role": "user", "content": "q"}], self.cfg, tools=L.TOOL_SPECS)
    self.assertEqual(r["tool_calls"], [{"id": "c1", "name": "grep", "args": {"pattern": "ERROR"}}])
    self.assertEqual(reqs[0]["tools"], L.TOOL_SPECS)

def test_no_tools_key_when_not_asked(self):
    url, reqs, stop = start_fake([{"role": "assistant", "content": "hi"}]); self.addCleanup(stop)
    L.chat({"id": "s", "backend": "server", "name": "fake", "url": url}, [{"role": "user", "content": "q"}], self.cfg)
    self.assertNotIn("tools", reqs[0])

def test_tools_rejected_is_runtime_error(self):
    url, _, stop = start_fake([{"status": 400, "body": "model does not support tools"}]); self.addCleanup(stop)
    with self.assertRaisesRegex(RuntimeError, "does not support tool calling"):
        L.chat({"id": "s", "backend": "server", "name": "fake", "url": url}, [{"role": "user", "content": "q"}],
               self.cfg, tools=L.TOOL_SPECS)
```

- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** the `chat` changes, `tool_result_message`, and `tests/fake_server.py` (`http.server.ThreadingHTTPServer` on port 0 in a daemon thread).
- [ ] **Step 4: Run all tests, expect PASS:** `python3 -m unittest discover -s tests -v`. Then confirm plain runs are unchanged: `python3 -m py_compile skills/local-offload/scripts/llocal.py`.
- [ ] **Step 5: Commit** — `git commit -am "Support tool calling in chat()"` (with `git add tests/fake_server.py`).

---

### Task 4: Explore loop, `--explore`, exit 5, feedback hint

**Files:**
- Modify: `skills/local-offload/scripts/llocal.py` — `DEFAULT_CONFIG` (add `"tool_rounds": 12`), new `explore()`, `rank_tools()`, `cmd_run`, `main()` argparse, `cmd_run`'s final `eprint` hint.
- Test: `tests/test_explore.py` (class `ExploreRunTest`, runs the CLI as a subprocess with `LLOCAL_HOME` pointing at a temp dir whose `config.json` has `"servers": [fake_url]`, `"ollama_url": "http://127.0.0.1:9"`, `"gguf_dirs": []`, and a `bench.json` giving `server:<url>#fake` `{"scores": {...all 1.0}, "overall": 1, "tps": 50, "suite": 3, "tools": 1.0, "tools_suite": 1}`).

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `EXPLORE_SYSTEM: str`: tells the model it can only see files through the tools, must search before answering, and must end every factual bullet/sentence with a citation in the Task 2 syntax, using `count` for any number of occurrences.
  - `explore(model: dict, root: str, one_file: str|None, question: str, task: str, cfg: dict, max_tokens: int) -> dict` with `text, rounds, log, prompt_tokens, completion_tokens, seconds`. First user message: the question, plus either a 2-level root listing (max 60 entries) or `"<file>: N lines, X KB"`. Each round: `chat(..., tools=TOOL_SPECS)`; no tool calls → done; else append `res["message"]` and one `tool_result_message` per call. Before each call, if `est_tokens(json.dumps(messages)) > cfg["ctx"] - max_tokens - 512`, replace the content of the oldest tool results with `"(dropped to save context)"`. After `cfg["tool_rounds"]` rounds: one last `chat` **without** `tools`, user message `"Stop searching. Answer now with citations."`.
  - `rank_tools(cfg, models) -> list` like `rank()`, using `res["tools"]` blended with feedback for category key `"tools"`, models with `"tools"` key only, filtered by `min_score`.
  - Verification in `cmd_run`: `v = verify_answer(...)`; if `v["verified"] < v["claims"]`, one retry: append the answer and a user message listing `v["failures"]`, run `explore` again from those messages, re-verify. Print / `--out` `v["text"]`. stderr `[llocal] verified {verified}/{claims} claims`. If still short → `Fail(5, f"{n} of {claims} claims unverified", "trust the verified lines; check only the [UNVERIFIED] lines by reading those lines")` **after** printing and logging usage.
  - `--explore PATH` with `-f`, `--glob`, `-` or `--json` → `Fail(1, "--explore can't be combined with -f/--glob/stdin/--json", "pass a directory or one file to --explore")`.
  - Usage row adds `explore: true`, `rounds`, `claims`, `verified`; `in_ctx = est_tokens` of `log["read_chars"]` characters (i.e. `log["read_chars"] * 2 // 7`).
  - Hint after every run (also plain `run`): ``-> after checking it: `{LLOCAL} feedback ok|bad --model {nick(model['id'])} --task {args.task}` ``.
  - Explore runs are rated under the pseudo-task `tools`: the hint after an explore run prints
    `--task tools`; `cmd_feedback` accepts `tools` (skipping `check_task` for it) and
    `field_stats` maps task `tools` to category `"tools"`, which `rank_tools` blends.
    `TASK_TO_CATEGORY` is unchanged, so `run --task tools` stays an error.

- [ ] **Step 1: Write failing tests.**

```python
def test_explore_verified_exit0(self):
    script = [tool_call("count", {"pattern": "ERROR"}),
              tool_call("grep", {"pattern": "ERROR"}),
              final('First error [logs/app.log:50] "14:02:17 ERROR db pool exhausted". '
                    'There were 2 errors [count "ERROR" = 2].')]
    p = self.run_cli(script, ["run", "--task", "logs", "--explore", self.root, "When did it fail?"])
    self.assertEqual(p.returncode, 0); self.assertIn("verified 2/2 claims", p.stderr)
    self.assertIn("--model", p.stderr)                      # feedback hint is exact
    row = self.last_usage(); self.assertTrue(row["explore"]); self.assertEqual(row["rounds"], 2)

def test_explore_unverified_exit5_after_one_retry(self):
    bad = final("It failed 46 times at 14:18.")
    p = self.run_cli([bad, bad], ["run", "--task", "logs", "--explore", self.root, "q"])
    self.assertEqual(p.returncode, 5); self.assertIn("[UNVERIFIED]", p.stdout)
    self.assertEqual(len(self.requests), 2)   # exactly one retry

def test_round_cap_forces_answer(self):
    loop = tool_call("list_files", {"glob": "*"})
    p = self.run_cli([loop] * 3 + [final('x [logs/app.log:50] "ERROR db pool exhausted".')],
                     ["run", "--task", "logs", "--explore", self.root, "q"], cfg={"tool_rounds": 3})
    self.assertEqual(p.returncode, 0); self.assertNotIn("tools", self.requests[-1])

def test_explore_rejects_files(self):
    p = self.run_cli([], ["run", "--explore", self.root, "-f", "x", "q"])
    self.assertEqual(p.returncode, 1); self.assertIn("can't be combined", p.stderr)

def test_explore_missing_path(self):
    p = self.run_cli([], ["run", "--explore", "/nope", "q"]); self.assertEqual(p.returncode, 1)
```

(`tool_call(name, args)` and `final(text)` are tiny helpers in the test file building fake-server message items.)

- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run all tests and the AGENTS.md minimum checks, expect PASS / unchanged behavior:**
  `python3 -m unittest discover -s tests -v`, then the five commands in AGENTS.md "Testing" (`--help`, `doctor`, `pick --task code` → exit 3, `run --task bogus x` → exit 1).
- [ ] **Step 5: Commit** — `git commit -am "Add run --explore with verified citations and exit 5"`

---

### Task 5: Tools benchmark, sheet column, routing

**Files:**
- Modify: `skills/local-offload/scripts/llocal.py` — `discover` (record `tools` capability), new `TOOLS_SUITE_VERSION = 1`, `make_tools_fixture()`, `check_explore()`, `bench_tools()`, `cmd_bench`, `cmd_sheet`, `cmd_report`.
- Test: `tests/test_explore.py` (class `ToolsBenchTest`)

**Interfaces:**
- Consumes: `explore`, `verify_answer` (Tasks 2, 4).
- Produces:
  - `discover` adds `"tools": True/False/None` to Ollama models from `capabilities` (None if the field is absent); other backends `None`.
  - `make_tools_fixture(dirpath) -> dict` writes a deterministic tree (seeded `random.Random(4)`): `svc/worker.log` ~3 000 lines with the first `ERROR` at a fixed line and timestamp and exactly 17 `ERROR` lines, plus 3 small `.py` files where `RETRY_LIMIT = 7` is defined once. Returns `{"first_ts": ..., "first_line": ..., "errors": 17, "const_path": ..., "const_line": ...}`.
  - Question: `"When was the first ERROR in svc/worker.log, how many ERROR lines are there, and where is RETRY_LIMIT defined and to what value?"`
  - `check_explore(answer: str, root: str, log: dict, truth: dict) -> float` = mean of four 0/1 parts: first timestamp present, `17` present, `const_path:const_line` cited, `verify_answer` claims all verified.
  - `bench_tools(model, cfg) -> {"tools": float, "tools_suite": 1, "tools_detail": {"score", "rounds", "s", "answer"[:600] if score < 1}}`; a model that rejects tools gets `{"tools": 0.0, "tools_suite": 1, "tools_detail": {"error": ...}}`.
  - `cmd_bench`: after the existing suite decision, a model whose result lacks `tools_suite == TOOLS_SUITE_VERSION` (and whose `tools` capability is not `False`) runs `bench_tools` only and merges into its entry; `--redo` reruns both.
  - `cmd_sheet` header gains a `tool` column after `reas` (`-` when absent); routes line adds `explore->...` from `rank_tools`; the last line replaces "Models can't run tools." with "tool = can search files itself via `run --explore`."
  - `cmd_report` prints `tools` as an extra column; the `overall` value is unchanged.

- [ ] **Step 1: Write failing tests.**

```python
def test_check_explore_good_and_bad(self):
    d = tempfile.mkdtemp(); t = L.make_tools_fixture(d); log = {"counts": {"ERROR": 17}, "read_chars": 0}
    good = (f'First ERROR at {t["first_ts"]} [svc/worker.log:{t["first_line"]}] "{t["first_ts"]} ERROR". '
            f'There are 17 ERROR lines [count "ERROR" = 17]. '
            f'RETRY_LIMIT is 7 [{t["const_path"]}:{t["const_line"]}] "RETRY_LIMIT = 7".')
    self.assertEqual(L.check_explore(good, d, log, t), 1.0)
    bad = "First ERROR at 03:00, about 20 ERROR lines, RETRY_LIMIT is 5."
    self.assertEqual(L.check_explore(bad, d, log, t), 0.0)

def test_fixture_is_deterministic(self):
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    self.assertEqual(L.make_tools_fixture(a), L.make_tools_fixture(b))

def test_tools_not_in_overall(self):
    self.assertNotIn("tools", L.CATEGORIES)
```

- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run, expect PASS:** `python3 -m unittest discover -s tests -v`.
- [ ] **Step 5: Validate on a real model** (GPU free; check `nvidia-smi` first, never stop other GPU programs):
  `LLOCAL_HOME=$(mktemp -d)`, copy `~/.llocal/config.json` and `bench.json` into it, then
  `python3 skills/local-offload/scripts/llocal.py bench --model unseen-gemma4-26b` (run in the background, long timeout).
  Expected: only the tools test runs ("skip ... already benchmarked" for the old suite), the result gets `tools` ≥ 0.75, and `sheet` shows the `tool` column. If a strong model scores < 0.75, read `tools_detail.answer` and fix the prompt or the fixture, not the threshold.
- [ ] **Step 6: Commit** — `git commit -am "Benchmark tool use; show tool score and explore route in sheet"`

---

### Task 6: Docs and end-to-end check on the eval fixture

**Files:**
- Modify: `README.md`, `skills/local-offload/SKILL.md`, `agents/local-worker.md`, `AGENTS.md`

**Interfaces:**
- Consumes: the final CLI surface from Tasks 4–5.

- [ ] **Step 1: Update the three user docs together:**
  - `--explore PATH` usage, with two examples: a codebase question (`--explore src "Where is auth checked? cite file:line"`) and a log question (`--explore logs/worker.log "First error, count, cause"`).
  - When to prefer it over `-f`: a whole repo, or input larger than ~40 KB.
  - The trust rule: verified claims are trusted and the input is **not** re-read. On exit 5, read only the lines cited by `[UNVERIFIED]` claims.
  - Exit code 5 in every exit-code list.
  - `[local: ...]` plan tags may use `--explore`.
  - The feedback hint now includes `--model`/`--task`.
- [ ] **Step 2: Update `AGENTS.md`:**
  - the exit code list gains 5;
  - the Layout gains `tests/`;
  - Testing gains `python3 -m unittest discover -s tests -v`, and "There is no test suite" is removed;
  - a Rules line says explore tools stay read-only and root-scoped.
- [ ] **Step 3: Run the end-to-end check** with the real model on the behavior-eval fixture (`~/code/local-offload-workspace/behavior/fixture`), with `LLOCAL_HOME` set to `~/code/local-offload-workspace/behavior/llocal-home`:
  `llocal run --task logs --explore logs/worker.log "First ERROR timestamp, number of ERROR lines, root cause, recovery time"`.
  Expected: exit 0 or 5. If the answer includes the first timestamp `14:02:17` or the count `46`, those claims are verified. No claim is marked verified while being wrong.
- [ ] **Step 4: Run** `npx skills add ./ --list` (the skill is still detected) and the full unittest suite.
- [ ] **Step 5: Commit** — `git commit -am "Document run --explore, exit 5 and the trust rule"`
