# AGENTS.md

Guide for coding agents (Claude Code, Codex, etc.) working in this repo.

## What it is

`llocal` offloads mechanical work from coding agents (Claude Code, Codex, Cursor…), such as
summaries, JSON extraction, docstrings and test drafts, to local **Ollama** or **llama.cpp**
models picked by benchmark, to spend fewer API tokens. Target hardware: a Mac with unified
memory (16 GB+) or a PC with an NVIDIA GPU.

## Layout

| Path | What it is |
|---|---|
| `skills/local-offload/SKILL.md` | The skill (standard format, works in Claude Code, Codex, Cursor…): when and how to call `llocal run`. |
| `skills/local-offload/scripts/llocal.py` | The whole CLI, one file, **Python standard library only**. It lives inside the skill so `npx skills add` installs it without touching PATH. |
| `agents/local-worker.md` | Optional Claude Code subagent (Haiku) that only coordinates and delegates to `llocal`. |
| `install.sh` | Optional: `llocal` command in `~/.local/bin`, skill and agent in `~/.claude/`. |
| `README.md` | User documentation. |

Runtime state: `~/.llocal/` (`config.json`, `bench.json`, `usage.jsonl`, `feedback.jsonl`,
`server.json`, `llama-server.log`). Redirect it with `LLOCAL_HOME=/other/path`;
**always do that in tests** so the user's real data is never touched.

## Rules

- No external dependencies: `llocal.py` must run with plain `python3`.
- The skill must work without PATH: always invoke the script as `python3 <skill>/scripts/llocal.py`.
- Fail fast: expected errors are raised as `Fail(code, msg, hint)` and print as
  `llocal: error:` + `llocal: next:` on stderr. No tracebacks for input or backend failures.
- Never kill or unload processes `llocal` didn't start (`is_our_server`). If another program
  holds the GPU, report it and exit 2.
- Local models only: Ollama cloud models (`remote_host`) are always excluded.
- Keep the **exit codes** stable; the skill and agent depend on them:
  `0` ok · `1` bad input · `2` backend error · `3` no suitable local model ·
  `4` `--json` without valid JSON.
- Memory: never two models loaded at once (`stop_server` / `unload_ollama_all`). Don't add
  parallel model execution.
- `llocal run` must not read stdin unless `-` is passed (inside agents stdin can be an open
  pipe and it would hang).
- If you change flags, tasks (`TASK_TO_CATEGORY`) or exit codes, update `README.md`,
  `skills/local-offload/SKILL.md` and `agents/local-worker.md` together.
- Benchmark tasks (`SUITE`) need a deterministic `check` returning 0–1. If you change the
  suite, bump `SUITE_VERSION`: `llocal bench` then re-measures outdated results. Validate
  every new `check` first with one good and one bad answer, and run it on a real model:
  a test that strong models fail usually has an unclear prompt.
- Routing blends benchmark scores with `feedback.jsonl` (`blended`, `PRIOR_WEIGHT`).
  `llocal sheet` is the compact view for agents: keep it short (one line per model).
- The benchmark runs model-generated code (`run_python_tests`). Don't widen what runs, and
  don't remove the `timeout` / `python -I`.
- Everything is in English: docs, skill, code, comments and CLI messages.

## Testing

There is no test suite. Minimum checks after a change:

```bash
L=skills/local-offload/scripts/llocal.py
python3 -m py_compile $L
export LLOCAL_HOME="$(mktemp -d)"
python3 $L --help
python3 $L doctor          # works even without Ollama or models
python3 $L pick --task code; echo "exit=$?"   # no benchmark -> "none", exit 3
python3 $L run --task bogus x; echo "exit=$?" # one-line error + next:, exit 1
npx skills add ./ --list   # the skill is detected
```

With a GPU, loading a large model takes a while: use long timeouts and run `bench` in the
background. To test `run`/`bench` without real models, start a fake OpenAI-compatible server
(`/v1/models`, `/v1/chat/completions`) and add it to `servers` in `LLOCAL_HOME`'s `config.json`.
