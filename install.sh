#!/usr/bin/env bash
# Optional. The usual way is `npx skills add`, which needs no PATH changes (see README).
# This adds a `llocal` command to your PATH and the Claude Code local-worker subagent.
#   ./install.sh             -> llocal command + skill + subagent + SessionStart hook (Claude Code)
#   ./install.sh --no-agent  -> skip the local-worker subagent
#   ./install.sh --no-hook   -> skip the SessionStart hook (puts `llocal sheet` in each new session)
set -euo pipefail

AGENT=1 HOOK=1
for a in "$@"; do
  case "$a" in
    --no-agent) AGENT=0 ;;
    --no-hook) HOOK=0 ;;
    *) echo "✗ unknown option: $a (use --no-agent, --no-hook)" >&2; exit 1 ;;
  esac
done
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

if [[ $AGENT == 1 ]]; then
  mkdir -p "$HOME/.claude/agents"
  install -m 644 agents/local-worker.md "$HOME/.claude/agents/local-worker.md"
  echo "✓ subagent -> ~/.claude/agents/local-worker.md"
fi

if [[ $HOOK == 1 ]]; then
  # Merge into ~/.claude/settings.json without touching other settings; idempotent.
  python3 - "$HOME/.claude/settings.json" "$SKILL_DIR/scripts/llocal.py" <<'PY'
import json, os, sys
path, script = sys.argv[1], sys.argv[2]
cfg = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            cfg = json.load(f)
    except ValueError:
        sys.exit(f"✗ {path} is not valid JSON; hook not added (rerun with --no-hook to skip)")
groups = cfg.setdefault("hooks", {}).setdefault("SessionStart", [])
if any("llocal.py" in h.get("command", "") and " sheet" in h.get("command", "") for g in groups for h in g.get("hooks", [])):
    print("✓ hook    -> already in " + path)
    sys.exit(0)
groups.append({"matcher": "startup|clear|compact", "hooks": [{
    "type": "command", "timeout": 10,
    "command": f"python3 '{script}' sheet 2>/dev/null || true"}]})
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path + ".tmp", "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
os.replace(path + ".tmp", path)
print("✓ hook    -> SessionStart `llocal sheet` in " + path)
PY
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo
     echo "⚠  $BIN is not on your PATH. Add to your shell rc:  export PATH=\"$BIN:\$PATH\""
     echo "   (optional: the skill runs python3 .../scripts/llocal.py)" ;;
esac

echo
"$BIN/llocal" doctor || true
