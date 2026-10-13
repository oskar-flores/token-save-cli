#!/usr/bin/env bash
# Opcional. Lo normal es `npx skills add`, que no necesita PATH (ver README).
# Esto añade el comando `llocal` al PATH y el subagente local-worker de Claude Code.
#   ./install.sh            -> comando llocal + skill + subagente (Claude Code)
#   ./install.sh --no-agent -> comando llocal + skill
set -euo pipefail
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "✗ falta python3" >&2; exit 1; }

BIN="${LLOCAL_BIN:-$HOME/.local/bin}"
SKILL_DIR="$HOME/.claude/skills/local-offload"
mkdir -p "$BIN" "$SKILL_DIR/scripts"

install -m 755 skills/local-offload/scripts/llocal.py "$BIN/llocal"
install -m 644 skills/local-offload/SKILL.md "$SKILL_DIR/SKILL.md"
install -m 755 skills/local-offload/scripts/llocal.py "$SKILL_DIR/scripts/llocal.py"
echo "✓ comando -> $BIN/llocal"
echo "✓ skill   -> $SKILL_DIR"

if [[ "${1:-}" != "--no-agent" ]]; then
  mkdir -p "$HOME/.claude/agents"
  install -m 644 agents/local-worker.md "$HOME/.claude/agents/local-worker.md"
  echo "✓ subagente -> ~/.claude/agents/local-worker.md"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo
     echo "⚠  $BIN no está en tu PATH. Añádelo a tu shell rc:  export PATH=\"$BIN:\$PATH\""
     echo "   (no es obligatorio: el skill usa python3 .../scripts/llocal.py)" ;;
esac

echo
"$BIN/llocal" doctor || true
