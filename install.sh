#!/usr/bin/env bash
# Optional. The usual way is `npx skills add`, which needs no PATH changes (see README).
# This adds a `llocal` command to your PATH and the Claude Code local-worker subagent.
#   ./install.sh            -> llocal command + skill + subagent (Claude Code)
#   ./install.sh --no-agent -> llocal command + skill
set -euo pipefail
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "✗ python3 is missing" >&2; exit 1; }

BIN="${LLOCAL_BIN:-$HOME/.local/bin}"
SKILL_DIR="$HOME/.claude/skills/local-offload"
mkdir -p "$BIN" "$SKILL_DIR/scripts"

install -m 755 skills/local-offload/scripts/llocal.py "$BIN/llocal"
install -m 644 skills/local-offload/SKILL.md "$SKILL_DIR/SKILL.md"
install -m 755 skills/local-offload/scripts/llocal.py "$SKILL_DIR/scripts/llocal.py"
echo "✓ command -> $BIN/llocal"
echo "✓ skill   -> $SKILL_DIR"

if [[ "${1:-}" != "--no-agent" ]]; then
  mkdir -p "$HOME/.claude/agents"
  install -m 644 agents/local-worker.md "$HOME/.claude/agents/local-worker.md"
  echo "✓ subagent -> ~/.claude/agents/local-worker.md"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo
     echo "⚠  $BIN is not on your PATH. Add to your shell rc:  export PATH=\"$BIN:\$PATH\""
     echo "   (optional: the skill runs python3 .../scripts/llocal.py)" ;;
esac

echo
"$BIN/llocal" doctor || true
