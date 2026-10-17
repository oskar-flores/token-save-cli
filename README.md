# llocal

**Spend fewer API tokens: offload mechanical work to a local model.**

Your coding agent (Claude Code, Codex, Cursor, Gemini CLI…) burns paid tokens on summaries,
logs, JSON extraction and docstrings. `llocal` runs that work on an **Ollama** or
**llama.cpp** model on your machine. Files go straight to the local model and never enter
the agent's context.

- Picks the model **by benchmark**, and only models that fit your GPU/memory.
- **`--explore`**: the local model searches a whole repo or a multi-MB log with read-only
  tools, and llocal **checks every citation** in its answer before the agent sees it.
- Gives the agent a **cheat sheet** of what each local model is good at.
- **Fails fast** with a clear exit code when no local model is good enough.
- Reports the **net** API cost saved, and warns when an offload wasn't worth it.
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

> Want a `llocal` command on your PATH, the Claude Code `local-worker` subagent and a
> SessionStart hook that puts `llocal sheet` in every new session? Run `./install.sh`
> (`--no-agent`, `--no-hook` to skip parts).

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
llocal run --task summary --glob 'src/**/*.py' "one line per module"
git diff | llocal run --task docs "commit message" -
llocal run --task logs --explore logs/worker.log "First error, count, cause"
llocal run --task explain --explore src "Where is auth checked? cite file:line"

llocal feedback ok|bad --model M --task T [--note ...]   # rate an answer (run prints this)
llocal stats                                  # tokens offloaded and net savings
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

## Explore: big inputs, verified answers

`-f` puts whole files in the prompt, so it stops at the model's context (~40 KB with the
default `ctx`). `--explore PATH` (a directory or one file) instead gives the model four
read-only tools, `list_files`, `grep`, `read_lines` and `count`, limited to that path
(no `.git`, no binary files, no symlinks out of it). The model searches, then answers with a
citation on every fact:

```
- First error at 14:02:17 [logs/worker.log:812] "14:02:17 ERROR db pool exhausted".
- 46 errors in total [count "ERROR" = 46].
```

llocal then checks every claim: the quote must be on the cited line (±2), every number in
the sentence must be in the cited line or count, and counts must match a real `count`/`grep`
total from that run. A claim that fails is sent back to the model once. If it still fails it is
marked `[UNVERIFIED]` and llocal exits **5**. The agent trusts the verified claims without
re-reading the input, and checks only the unverified ones.

Which models can do it shows in `sheet` (column `tool`, route `explore->`), measured by
`bench` with a real search over a generated tree. At most `tool_rounds` (12) search rounds
per answer.

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
| 5 | `--explore` answered, but some claims are `[UNVERIFIED]` (the rest were checked) |

<br>

## Good practices

- **Benchmark once.** No benchmark = no trusted model (exit 3). Re-run `bench` after adding
  models or updating llocal.
- **Let the agent choose.** It reads `llocal sheet` and picks with `--model`.
- **Plans.** The skill asks the agent to mark mechanical steps `[local: ...]` (with `-f` or
  `--explore`) when it writes a plan, so the decision is made once, by the agent that sees the
  whole job.
- **Close the loop.** `llocal feedback ok|bad` after checking an answer improves routing. Use
  the exact command `run` prints (with `--model`/`--task`): two agents sharing llocal would
  otherwise rate each other's runs.
- **Offload big inputs only.** It pays when files go in by `-f`/`--glob`/stdin and are ~1k+
  tokens, or a long answer goes to `--out`. Pasting text into the command costs more than
  doing it yourself. `llocal stats` shows the net saving (bad-rated runs don't count) and
  `run` warns on stderr when a run likely lost money.
- **One model at a time.** Don't run calls in parallel.
- **Verify.** Local models are weaker: run their code, check their facts. With `--explore`,
  llocal has already checked the cited facts.
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
| `tool_rounds` | 12 | `--explore`: max search rounds before the model must answer |
| `request_timeout` | 600 | seconds to wait (or `run --timeout`) |
| `ollama_url` | `http://localhost:11434` | |
| `servers` | `[]` | other OpenAI-compatible endpoints |
| `gguf_dirs` | `~/models`, llama.cpp/LM Studio caches | where to find `.gguf` files |
| `price_in_per_mtok` / `price_out_per_mtok` | 2 / 10 | API prices for savings estimates (Sonnet 5.5) |

<br>

## Notes

- The benchmark **runs the code the model writes** (temp dir, `python -I`, 10 s limit).
- "Thinking" modes (Qwen3, etc.) are disabled, or `<think>` blocks are stripped.
- **SessionStart hook** (Claude Code only; `install.sh` adds it). To add it by hand, in
  `~/.claude/settings.json`:

  ```json
  {"hooks": {"SessionStart": [{"matcher": "startup|clear|compact", "hooks": [{"type": "command",
    "command": "python3 ~/.claude/skills/local-offload/scripts/llocal.py sheet 2>/dev/null || true",
    "timeout": 10}]}]}}
  ```

  It only gives the agent the model list; the agent still decides when to offload.
- Decision models (e.g. Ollama `nimble`) are not chat models and are ignored.
- Fully local subagents are possible with
  [claude-code-router](https://github.com/musistudio/claude-code-router), but small models
  handle the tool loop poorly. The skill is the recommended path.
