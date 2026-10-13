# llocal — gasta menos API delegando trabajo mecánico a un modelo local

Tu agente (Claude Code, Codex, Cursor, Gemini CLI…) resume ficheros, lee logs, saca JSON y
escribe docstrings con tokens de pago. `llocal` hace ese trabajo con un modelo de **Ollama** o
**llama.cpp** en tu máquina. El fichero va directo al modelo local, así que no entra en el
contexto del agente.

- Elige el modelo **por benchmark** (código con tests reales, JSON, resúmenes, razonamiento,
  tok/s) y solo usa los que caben en tu GPU/memoria.
- Si ningún modelo es lo bastante bueno, **falla rápido** con un código claro y el agente lo
  hace él.
- Un fichero Python, sin dependencias. No hace falta tocar el PATH.

## Probarlo en 3 minutos

Requisitos: `python3`, y [Ollama](https://ollama.com) con algún modelo (`ollama pull qwen2.5-coder:7b`).

```bash
# 1. Instala el skill (incluye el CLI) en tus agentes
npx skills add oskar-flores/token-save-cli -g     # o desde una copia local: npx skills add ./token-save-cli -g

# 2. Comprueba y haz el benchmark una vez (con GPU, cargar cada modelo tarda un poco)
L=~/.claude/skills/local-offload/scripts/llocal.py   # Claude Code (Codex y otros: ~/.agents/skills/...)
python3 $L doctor
python3 $L bench --quick
python3 $L report
```

Desde aquí tu agente lo usa solo cuando toca (resumir, logs, extraer, docs, tests…).
Pruébalo a mano:

```bash
python3 $L run --task summary -f README.md "Resume en 5 viñetas"
```

¿Prefieres un comando `llocal` en el PATH y el subagente `local-worker` de Claude Code?
`./install.sh`.

## Comandos

```bash
llocal doctor                                  # memoria/GPU, backends, modelos y si caben
llocal bench [--quick] [--redo] [--model X]    # benchmark (solo los que falten)
llocal report                                  # notas y mejor modelo por tarea
llocal run --task logs -f app.log "¿Qué falló?"
git diff | llocal run --task docs "mensaje de commit" -
llocal pick --task code                        # qué modelo se usaría
llocal stats                                   # tokens delegados y ahorro estimado
llocal stop                                    # libera la memoria
```

Tareas: `code tests refactor boilerplate` · `json extract classify` ·
`summary docs explain translate logs` · `reason` · `general`.

## Códigos de salida

Cada error imprime `llocal: error: …` y `llocal: next: …` (qué hacer), para que el agente sepa
qué pasa sin adivinar.

| exit | significado |
|---|---|
| 0 | ok |
| 1 | entrada mala: fichero, tarea, prompt vacío, demasiado grande |
| 2 | backend caído, timeout o respuesta rota |
| 3 | ningún modelo local apto, o aún no hay benchmark: que lo haga el agente |
| 4 | `--json` pedido y la salida no es JSON |

## Buenas prácticas

- **Haz el benchmark tú, una vez.** Sin benchmark, `llocal` no confía en ningún modelo (exit 3).
  Repite `bench` cuando añadas modelos.
- **Un modelo cada vez.** `llocal` descarga el anterior antes de cargar otro; no lances varias
  llamadas en paralelo.
- **Verifica.** El modelo local es más débil: ejecuta el código que genere y comprueba con
  `grep` los datos de los que dependa una decisión.
- **Solo local.** Los modelos `:cloud` de Ollama se ignoran, para que tus ficheros no salgan
  de la máquina. Los de audio y embeddings, también.
- **Ajusta la memoria** si el sistema hace swap o la GPU se queda corta (tabla de abajo).
- Para que todo el equipo lo use, añade a `AGENTS.md` / `CLAUDE.md` del repo:
  `Antes de resumir/extraer/documentar ficheros grandes, usa el skill local-offload.`

## Configuración (`~/.llocal/config.json`, o `LLOCAL_HOME`)

| clave | por defecto | qué hace |
|---|---|---|
| `vram_fraction` | 0.9 | con GPU NVIDIA: tamaño máximo de modelo = 90 % de la VRAM |
| `mem_fraction` | 0.62 | sin NVIDIA (Mac, CPU): 62 % de la RAM |
| `max_model_gb` | `null` | fija el límite a mano (gana a los dos anteriores) |
| `ctx` | 16384 | contexto del modelo; bájalo si va justo de memoria |
| `min_score` | 0.6 | nota mínima (0–1) para usar un modelo en una categoría |
| `request_timeout` | 600 | segundos de espera (o `run --timeout`) |
| `ollama_url` | `http://localhost:11434` | |
| `servers` | `[]` | otros endpoints OpenAI-compatibles ya arrancados |
| `gguf_dirs` | `~/models`, cachés de llama.cpp/LM Studio | dónde buscar `.gguf` |
| `price_in_per_mtok` / `price_out_per_mtok` | 3 / 15 | solo para el ahorro de `llocal stats` |

## Notas

- El benchmark **ejecuta el código que genera el modelo** (directorio temporal, `python -I`,
  10 s de límite) para ver si pasa los tests.
- Los modos "thinking" (Qwen3, etc.) se desactivan o se quitan los `<think>`.
- Subagentes 100 % locales: posible con
  [claude-code-router](https://github.com/musistudio/claude-code-router), pero los modelos
  pequeños llevan mal el bucle de herramientas. El skill es la vía recomendada.
