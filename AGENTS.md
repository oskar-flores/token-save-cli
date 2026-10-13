# AGENTS.md

Guía para agentes de código (Claude Code, Codex, etc.) que trabajen en este repo.

## Qué es

`llocal` delega trabajo mecánico de agentes de código (Claude Code, Codex, Cursor…):
resúmenes, extracción a JSON, docstrings, borradores de tests… a modelos locales de **Ollama**
o **llama.cpp**, elegidos por benchmark, para gastar menos tokens de API. Hardware objetivo:
Mac con memoria unificada (16 GB+) o PC con GPU NVIDIA.

## Estructura

| Ruta | Qué es |
|---|---|
| `skills/local-offload/SKILL.md` | Skill (formato estándar, sirve para Claude Code, Codex, Cursor…): cuándo y cómo llamar a `llocal run`. |
| `skills/local-offload/scripts/llocal.py` | CLI completo, un solo fichero, **solo librería estándar de Python**. Va dentro del skill para que `npx skills add` lo instale sin tocar el PATH. |
| `agents/local-worker.md` | Subagente opcional (Haiku) que solo coordina y delega en `llocal`. |
| `install.sh` | Opcional: comando `llocal` en `~/.local/bin`, skill y agente en `~/.claude/`. |
| `README.md` | Documentación de usuario (en español). |

Estado en tiempo de ejecución: `~/.llocal/` (`config.json`, `bench.json`, `usage.jsonl`,
`server.json`, `llama-server.log`). Se puede redirigir con `LLOCAL_HOME=/otra/ruta`;
**úsalo siempre en pruebas** para no tocar los datos reales del usuario.

## Reglas

- No añadas dependencias externas: `llocal.py` tiene que funcionar con `python3` a secas.
- El skill debe funcionar sin PATH: invoca siempre el script como `python3 <skill>/scripts/llocal.py`.
- Fallar rápido: los errores esperados se lanzan con `Fail(code, msg, hint)` y salen como
  `llocal: error:` + `llocal: next:` en stderr. Nada de tracebacks para fallos de entrada o backend.
- Nunca mates ni descargues procesos que no haya arrancado `llocal` (`is_our_server`). Si la GPU
  está ocupada por otro programa, informa y sal con 2.
- Solo modelos locales: los modelos cloud de Ollama (`remote_host`) se excluyen siempre.
- Mantén estables los **códigos de salida**, el skill y el agente dependen de ellos:
  `0` ok · `1` error de entrada · `2` error de backend · `3` ningún modelo local apto ·
  `4` `--json` sin JSON válido.
- Memoria: nunca debe haber dos modelos cargados a la vez (`stop_server` / `unload_ollama_all`).
  No introduzcas ejecución en paralelo de modelos.
- `llocal run` no debe leer stdin salvo que se pase `-` (dentro de agentes stdin puede ser una
  tubería abierta y se colgaría).
- Si cambias flags, tareas (`TASK_TO_CATEGORY`) o códigos de salida, actualiza a la vez
  `README.md`, `skills/local-offload/SKILL.md` y `agents/local-worker.md`.
- Las tareas del benchmark (`SUITE`) deben tener un `check` determinista que devuelva 0–1.
  Si cambias la suite, avisa de que hay que repetir `llocal bench --redo`.
- El benchmark ejecuta código generado por el modelo (`run_python_tests`). No amplíes lo que
  se ejecuta ni quites el `timeout` / `python -I`.
- Documentación y textos de usuario en español; código, comentarios y mensajes del CLI en inglés.

## Probar

No hay suite de tests. Comprobaciones mínimas tras un cambio:

```bash
L=skills/local-offload/scripts/llocal.py
python3 -m py_compile $L
export LLOCAL_HOME="$(mktemp -d)"
python3 $L --help
python3 $L doctor          # funciona aunque no haya Ollama ni modelos
python3 $L pick --task code; echo "exit=$?"   # sin benchmark -> "none", exit 3
python3 $L run --task bogus x; echo "exit=$?" # error de una línea + next:, exit 1
npx skills add ./ --list   # el skill se detecta
```

Con GPU, cargar un modelo grande tarda: usa timeouts largos y lanza `bench` en segundo plano.
Para probar `run`/`bench` sin modelos reales, levanta un servidor falso compatible con OpenAI
(`/v1/models`, `/v1/chat/completions`) y añádelo en `servers` del `config.json` de `LLOCAL_HOME`.
