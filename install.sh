#!/bin/sh
# Install the wake scripts into $PREFIX/bin (default ~/.local/bin) and, optionally, the agent skill.
#   ./install.sh                         # scripts only
#   PREFIX=/usr/local ./install.sh       # system-wide (run as root)
#   SKILL_DIR=~/.claude/skills ./install.sh   # also copy docs/agent-skill/SKILL.md to $SKILL_DIR/wake-bot/
set -eu
cd "$(dirname "$0")"
PREFIX=${PREFIX:-$HOME/.local}
mkdir -p "$PREFIX/bin"
for f in wake wake-hook wake-run wake-mcp wake-inbox-io; do
    install -m 755 "bin/$f" "$PREFIX/bin/$f"
done
install -m 644 bin/wake_config.py "$PREFIX/bin/wake_config.py"
echo "installed wake into $PREFIX/bin"

if [ -n "${SKILL_DIR:-}" ]; then
    mkdir -p "$SKILL_DIR/wake-bot"
    install -m 644 docs/agent-skill/SKILL.md "$SKILL_DIR/wake-bot/SKILL.md"
    echo "installed skill into $SKILL_DIR/wake-bot"
fi

CFG=${WAKE_CONFIG_DIR:-$HOME/.config/wake}
if [ ! -e "$CFG/bots.tsv" ]; then
    echo "next: mkdir -p $CFG && cp bots.example.tsv $CFG/bots.tsv && cp config.example.env $CFG/config.env, then edit both"
fi
