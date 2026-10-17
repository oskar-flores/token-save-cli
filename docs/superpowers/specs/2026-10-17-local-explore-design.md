# llocal explore: read-only tools + verified citations

Date: 2026-10-17. Status: approved by the user (2026-10-17).

## Why

The behavior eval (3 tasks, with/without the skill) showed offloading saved nothing:
same quality, ~5% more Claude tokens, ~2x slower. Two reasons:

1. `llocal run -f` pastes the whole file into the prompt, so input is capped by `ctx`
   (16k → ~48 KB). Inputs that small are cheap for Claude to read anyway. The real cases the
   user has (questions about a codebase, reading logs) are much bigger.
2. The local model was wrong in both offloaded runs (wrong first-error timestamp; 3 missed and
   1 extra refund id). The skill tells Claude to verify, and Claude verified by re-reading the
   input, which spent the tokens the offload was meant to save.

The user's goal, in their words: use local models for tasks they can be good at and save
tokens, while being sure the answers from the model can be trusted. Not "push as many
tokens as possible to the local model".

## Goals

- Local model answers questions over inputs far bigger than its context (a whole repo, a
  multi-MB log) by searching and reading pieces itself, through tools llocal runs.
- Every factual claim in the answer cites `path:line` with a quote, and **llocal checks the
  citations mechanically** before Claude sees the answer. Claude trusts verified citations
  without re-reading, and only spot-checks the lines llocal could not verify.
- Works with any agent (Claude Code, Codex, Cursor), with or without superpowers.

## Non-goals (this step)

- Writing files or running tests: that is step 2 (implement mode, below), designed separately.
- A shell tool, network access, or tools outside the given root.
- Parallel models or parallel tool calls across models.
- Replacing `run -f`: it stays as is for small, whole-file jobs.

## Anti-goals (failure even if it "works")

- An answer presented as verified when a citation was not actually checked.
- Claude still needing to re-read the input to trust the answer.
- Any read outside the root, or following a symlink out of it.
- New dependencies, or breaking existing flags and exit codes 0–4.

## Constraints (from AGENTS.md)

Single file, stdlib only; fail fast with `Fail(code, msg, hint)`; never two models loaded;
never kill processes llocal didn't start; English everywhere; README, SKILL.md and
local-worker.md updated together; a suite change needs deterministic checks validated on a
good and a bad answer, and a `SUITE_VERSION` bump.

## Design

### Command

`llocal run --task <t> --explore PATH "question"`

- `PATH` is a directory or a single file; it becomes the root. Nothing is pasted up front:
  the model gets the question, a short root listing (or the file's size and line count), and
  the tools.
- Can be combined with `--model`, `--out`, `--max-tokens`, `--timeout`. Not with `-f`/`--glob`
  (exit 1 with a hint), to keep the two modes simple.

### Tools (read-only, all scoped to the root)

| Tool | Args | Returns |
|---|---|---|
| `list_files` | `glob` (default `**/*`) | matching paths, capped (e.g. 200), sizes |
| `grep` | `pattern` (regex), `glob` (optional) | `path:line: text` hits, capped (e.g. 50), with total count |
| `read_lines` | `path`, `start`, `end` | numbered lines, max ~200 per call |
| `count` | `pattern`, `glob` (optional) | number of matching lines per file and total |

`count` exists because "how many ERROR lines" is the kind of fact models get wrong by eye.
Paths are resolved with `realpath` and must stay under the root; `.git`, binary files and
oversized lines are skipped/truncated. Tool errors go back to the model as text, not crashes.

### Loop

- Native tool calling: Ollama `/api/chat` with `tools`, OpenAI-compatible
  `/v1/chat/completions` with `tools` (llama-server needs `--jinja`). `chat()` gains an
  optional `tools` argument and returns tool calls when present.
- At most `tool_rounds` rounds (config, default 12). llocal tracks the conversation size;
  when it nears `ctx`, it truncates old tool outputs and tells the model to answer now.
- Final answer contract (in the system prompt): each fact ends with a citation
  `[path:LINE] "exact quote"`; counts cite the `count` call instead of a line.

### Verification

After the final answer, llocal checks each citation:

- the path is under the root and exists; the line exists;
- the quote (whitespace-normalized) appears on that line or within ±2 lines;
- numbers attributed to `count` match a real `count` result from this run.

Then:

- All verified → print the answer, plus a stderr line `[llocal] verified 6/6 citations`.
- Some failed → one retry: the model is told exactly which citations failed and why.
- Still failing → the answer is printed (and written to `--out`) with failed claims marked
  `[UNVERIFIED]`, the stderr line says which, and llocal exits **5** ("answered, some claims
  unverified"). *(User's decision.)* Codes 0–4 keep their meaning; the three docs gain one
  line for 5.
- Unit of verification *(user's decision)*: **every bullet or sentence that states a fact
  must carry at least one citation.** Uncited ones are marked `[UNVERIFIED]` like failed
  citations. Headings and connecting text without facts are exempt (the builder picks the
  heuristic, e.g. lines ending in `:` or without digits, paths or quotes, and validates it on
  good and bad answers). "verified N/N" counts claims, not just citations present.

### Model choice

- Only models that can call tools: Ollama capability `tools` (already returned by
  `/api/show`, which `discover` reads); for llama.cpp/OpenAI servers, detected on first use.
- Ranking for `--explore` uses the `tools` score only; models without tool support are
  filtered out before ranking, and the model must pass `min_score` on `tools`. *(Builder's
  call, settled in review.)*
- `tools` is **not** added to `CATEGORIES`, so the overall score and routing for plain
  `run` stay as they are. Models without tool support show `-` in the sheet column. *(My call.)*
- New benchmark test, real loop: llocal generates a small fixture tree (a few files, a log
  with a planted first error and a known ERROR count); the check is deterministic (right
  `path:line`, right count, citations verify). New category `tools`, shown as a column in
  `sheet`; `SUITE_VERSION` → 4. *(My call:)* `bench` re-measures only the tests a model has no
  result for, so adding this test doesn't trigger hours of re-benchmarking; changed tests
  still need a bump that invalidates them.

### Usage and cost

- No automatic `feedback bad` for unverified runs; it only shows in the usage row and
  `stats`. *(My call.)*
- The usage row records rounds, bytes read through tools, and the verified/failed count.
- For `net_saving`, tokens returned by tools count as `in_ctx`, the input Claude would
  otherwise have read.

### Agent-side rules (SKILL.md, local-worker.md, README)

- Use `--explore` for questions over a codebase or a big log; `-f` stays for small whole-file
  jobs.
- Verified citations are trusted: do **not** re-read the input. Spot-check only
  `[UNVERIFIED]` lines, reading those lines only.
- `[local: ...]` plan tags may use `--explore`.

### Small fix bundled in: feedback attribution

`llocal feedback ok|bad` without `--model` attaches to the latest run in `usage.jsonl`, which
is the wrong run when two agents share `LLOCAL_HOME` (seen in the eval). The hint printed
after a run will include `--model <id> --task <task>`, so the agent's feedback is exact.

## Left to the builder

Exact caps (hits, lines, files), quote-matching tolerance, how the root listing is
summarized, how tool support is detected on OpenAI-compatible servers, the fixture contents
of the new benchmark test, and internal function layout.

## Step 2 (later, separate design): implement mode

Plan steps detailed enough for a local model: the local model edits only the files Claude
names (`write_file`), runs the test command Claude gives (`run_tests`, with timeout and
sandboxing), and iterates a few rounds. Claude (or any reviewer, e.g. a superpowers review,
not required) reviews only the final diff and test output.
