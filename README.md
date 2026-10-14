# llocal

**Spend fewer API tokens: offload mechanical work to a local model.**

Your coding agent (Claude Code, Codex, Cursor, Gemini CLI…) burns paid tokens on summaries,
logs, JSON extraction and docstrings. `llocal` runs that work on an **Ollama** or
**llama.cpp** model on your machine. Files go straight to the local model and never enter
the agent's context.

- Picks the model **by benchmark**, and only models that fit your GPU/memory.
- Gives the agent a **cheat sheet** of what each local model is good at.
- **Fails fast** with a clear exit code when no local model is good enough.
- One Python file, no dependencies, no PATH changes.

<br>

## Quick start

You need `python3` and [Ollama](https://ollama.com) with a model:

```bash
ollama pull qwen2.5-coder:7b
```

**1. Install the skill** (CLI included):

```bash
npx skills add oskar-flores/token-save-cli -g
```

From a local clone: `npx skills add ./token-save-cli -g`

**2. Check your setup and benchmark** (~2–4 min per model):

```bash
L=~/.claude/skills/local-offload/scripts/llocal.py   # Codex and others: ~/.agents/skills/...

python3 $L doctor
python3 $L bench --quick     # or just one model: --model qwen
python3 $L sheet
```

**3. Done.** Your agent now uses it when it fits. Try it by hand:

```bash
python3 $L run --task summary -f README.md "Summarize in 5 bullets"
```

> Want a `llocal` command on your PATH and the Claude Code `local-worker` subagent?
> Run `./install.sh`.

<br>

## Commands

Examples use `llocal`; without `install.sh`, use `python3 $L`.

```bash
llocal doctor                                 # GPU/memory, backends, models
llocal bench [--quick] [--redo] [--model X]   # benchmark what's missing or outdated
llocal report                                 # scores and best model per task
llocal sheet                                  # compact cheat sheet for agents
llocal pick --task code                       # which model would be used

llocal run --task logs -f app.log "What failed?"
git diff | llocal run --task docs "commit message" -

llocal feedback ok|bad [--note ...]           # rate the last answer
llocal stats                                  # tokens offloaded and savings
llocal stop                                   # free the memory
```

**Tasks**

| category | tasks |
|---|---|
| code | `code` `tests` `refactor` `boilerplate` |
| json | `json` `extract` `classify` |
| summary | `summary` `docs` `explain` `translate` `logs` |
| reason | `reason` |
| any | `general` |

<br>

## Exit codes

Errors print `llocal: error: …` and `llocal: next: …` (what to do).

| exit | meaning |
|---|---|
| 0 | ok |
| 1 | bad input: file, task, empty prompt, too large |
| 2 | backend down, GPU busy, timeout or broken reply |
| 3 | no local model good enough (or no benchmark): the agent does it |
| 4 | `--json` requested but the output is not JSON |

<br>

## Good practices

- **Benchmark once.** No benchmark = no trusted model (exit 3). Re-run `bench` after adding
  models or updating llocal.
- **Let the agent choose.** It reads `llocal sheet` and picks with `--model`.
- **Close the loop.** `llocal feedback ok|bad` after checking an answer improves routing.
- **One model at a time.** Don't run calls in parallel.
- **Verify.** Local models are weaker: run their code, check their facts.
- **Local only.** Ollama `:cloud`, audio and embedding models are ignored.
- **Team use.** Add to your repo's `AGENTS.md` / `CLAUDE.md`:
  `Before summarizing/extracting/documenting large files, use the local-offload skill.`

<br>

## Configuration

File: `~/.llocal/config.json` (or set `LLOCAL_HOME`).

| key | default | what it does |
|---|---|---|
| `vram_fraction` | 0.9 | NVIDIA GPU: max model size = 90 % of VRAM |
| `mem_fraction` | 0.62 | Mac / CPU: 62 % of RAM |
| `max_model_gb` | `null` | fixed limit (overrides both above) |
| `ctx` | 16384 | model context; lower it if memory is tight |
| `min_score` | 0.6 | minimum score (0–1) to use a model |
| `min_tps` | 5 | slower models (tok/s) are dropped in `bench` |
| `request_timeout` | 600 | seconds to wait (or `run --timeout`) |
| `ollama_url` | `http://localhost:11434` | |
| `servers` | `[]` | other OpenAI-compatible endpoints |
| `gguf_dirs` | `~/models`, llama.cpp/LM Studio caches | where to find `.gguf` files |
| `price_in_per_mtok` / `price_out_per_mtok` | 3 / 15 | only for `llocal stats` |

<br>

## Notes

- The benchmark **runs the code the model writes** (temp dir, `python -I`, 10 s limit).
- "Thinking" modes (Qwen3, etc.) are disabled, or `<think>` blocks are stripped.
- Fully local subagents are possible with
  [claude-code-router](https://github.com/musistudio/claude-code-router), but small models
  handle the tool loop poorly. The skill is the recommended path.
