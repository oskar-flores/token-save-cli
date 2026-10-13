# llocal: spend fewer API tokens by offloading mechanical work to a local model

Your coding agent (Claude Code, Codex, Cursor, Gemini CLI…) burns paid tokens summarizing
files, reading logs, extracting JSON and writing docstrings. `llocal` does that work on an
**Ollama** or **llama.cpp** model on your machine. Files go straight to the local model and
never enter the agent's context.

- Picks the model **by benchmark** (code checked by real tests, JSON, summaries, reasoning,
  long context, tok/s) and only uses models that fit your GPU/memory.
- Gives the agent a **cheat sheet** of what each local model is good at, so a strong agent can
  route the work itself.
- **Fails fast** with a clear exit code when no local model is good enough, so the agent does
  the task itself.
- One Python file, no dependencies, no PATH changes needed.

## Try it

Requirements: `python3`, and [Ollama](https://ollama.com) with a model (`ollama pull qwen2.5-coder:7b`).

```bash
# 1. Install the skill (CLI included) into your agents
npx skills add oskar-flores/token-save-cli -g     # or from a local clone: npx skills add ./token-save-cli -g

# 2. Check your setup and benchmark once: ~2–4 min per model (mostly loading it).
#    To start fast, benchmark just one:  python3 $L bench --quick --model qwen
L=~/.claude/skills/local-offload/scripts/llocal.py   # Claude Code (Codex and others: ~/.agents/skills/...)
python3 $L doctor
python3 $L bench --quick        # every model that fits; already-measured ones are skipped
python3 $L sheet
```

From now on your agent uses it when it fits (summaries, logs, extraction, docs, tests…).
Try it by hand:

```bash
python3 $L run --task summary -f README.md "Summarize in 5 bullets"
```

Want a `llocal` command on your PATH and the Claude Code `local-worker` subagent? Run `./install.sh`.

## Commands

```bash
llocal doctor                                  # GPU/memory, backends, models and whether they fit
llocal bench [--quick] [--redo] [--model X]    # benchmark (only what's missing or outdated)
llocal report                                  # score table and best model per task
llocal sheet                                   # compact cheat sheet for agents that pick a model
llocal run --task logs -f app.log "What failed?"
git diff | llocal run --task docs "commit message" -
llocal pick --task code                        # which model would be used
llocal feedback ok|bad [--note ...]            # was the last answer right? improves routing
llocal stats                                   # tokens offloaded and estimated savings
llocal stop                                    # free the memory
```

Tasks: `code tests refactor boilerplate` · `json extract classify` ·
`summary docs explain translate logs` · `reason` · `general`.

## Exit codes

Every error prints `llocal: error: …` and `llocal: next: …` (what to do), so the agent knows
what happened without guessing.

| exit | meaning |
|---|---|
| 0 | ok |
| 1 | bad input: file, task, empty prompt, too large |
| 2 | backend down, GPU busy, timeout or broken reply |
| 3 | no local model good enough, or no benchmark yet: the agent should do it |
| 4 | `--json` requested and the output is not JSON |

## Good practices

- **Benchmark once, yourself.** Without a benchmark `llocal` trusts no model (exit 3). Re-run
  `bench` when you add models or update llocal (outdated results are re-measured automatically).
- **Let the agent choose.** A strong agent reads `llocal sheet` (what each model is good at,
  speed, weak spots) and picks with `--model`; otherwise `llocal` picks by benchmark.
- **Close the loop.** After checking an answer, `llocal feedback ok|bad`: models that fail in
  real use drop in routing, even if they scored well in the benchmark.
- **One model at a time.** `llocal` unloads the previous model before loading another; don't
  run several calls in parallel.
- **Verify.** The local model is weaker: run the code it writes, and `grep` any fact a decision
  depends on.
- **Local only.** Ollama `:cloud` models are ignored so your files never leave the machine.
  Audio and embedding models are ignored too.
- **Tune memory** if the system swaps or the GPU runs short (table below).
- To make your whole team use it, add to the repo's `AGENTS.md` / `CLAUDE.md`:
  `Before summarizing/extracting/documenting large files, use the local-offload skill.`

## Configuration (`~/.llocal/config.json`, or `LLOCAL_HOME`)

| key | default | what it does |
|---|---|---|
| `vram_fraction` | 0.9 | with an NVIDIA GPU: max model size = 90 % of VRAM |
| `mem_fraction` | 0.62 | without NVIDIA (Mac, CPU): 62 % of RAM |
| `max_model_gb` | `null` | set the limit by hand (overrides both above) |
| `ctx` | 16384 | model context; lower it if memory is tight |
| `min_score` | 0.6 | minimum score (0–1) to use a model for a category |
| `min_tps` | 5 | models slower than this (tok/s) are dropped during `bench` |
| `request_timeout` | 600 | seconds to wait (or `run --timeout`) |
| `ollama_url` | `http://localhost:11434` | |
| `servers` | `[]` | other OpenAI-compatible endpoints already running |
| `gguf_dirs` | `~/models`, llama.cpp/LM Studio caches | where to look for `.gguf` files |
| `price_in_per_mtok` / `price_out_per_mtok` | 3 / 15 | only for the savings shown by `llocal stats` |

## Notes

- The benchmark **runs the code the model writes** (temp dir, `python -I`, 10 s limit) to see
  whether it passes the tests.
- "Thinking" modes (Qwen3, etc.) are disabled, or `<think>` blocks are stripped.
- Fully local subagents are possible with
  [claude-code-router](https://github.com/musistudio/claude-code-router), but small models
  handle the tool loop poorly. The skill is the recommended path.
