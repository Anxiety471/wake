#!/bin/sh
# Install the wake scripts into $PREFIX/bin (default ~/.local/bin) and, optionally, the agent skill.
#   ./install.sh                         # scripts only
#   PREFIX=/usr/local ./install.sh       # system-wide (run as root)
#   SKILL_DIR=~/.claude/skills ./install.sh   # also copy docs/agent-skill/SKILL.md to $SKILL_DIR/wake-bot/
#   OPS=1 ./install.sh                   # also install the bin/ops operator helpers
set -eu
case ${1:-} in
    -h | --help)
        cat <<'EOF'
usage: ./install.sh [--help]

  Installs the core scripts into $PREFIX/bin (default ~/.local/bin): wake, wake-hook, wake-run,
  wake-mcp, wake-inbox-io and the wake_config.py they import.

  PREFIX=/usr/local ./install.sh             install system-wide (run as root)
  OPS=1 ./install.sh                         also install the bin/ops operator helpers
  SKILL_DIR=~/.claude/skills ./install.sh    also copy docs/agent-skill/SKILL.md into
                                             $SKILL_DIR/wake-bot/

  It overwrites a file of the same name in $PREFIX/bin, which is why checking "command -v wake"
  first is worth a minute. When ~/.config/wake has no bots.tsv it prints the two files to copy.

  --help is the only argument it takes; another one is a mistake, not a no-op.
EOF
        exit 0
        ;;
    '') ;;
    *)
        echo "install.sh takes no arguments; try --help" >&2
        exit 2
        ;;
esac

cd "$(dirname "$0")"
PREFIX=${PREFIX:-$HOME/.local}
mkdir -p "$PREFIX/bin"
for f in wake wake-hook wake-run wake-mcp wake-inbox-io; do
    install -m 755 "bin/$f" "$PREFIX/bin/$f"
done
install -m 644 bin/wake_config.py "$PREFIX/bin/wake_config.py"

if [ "${OPS:-0}" = 1 ]; then
    for f in bin/ops/wake-*; do
        [ -f "$f" ] || continue
        install -m 755 "$f" "$PREFIX/bin/$(basename "$f")"
    done
    echo "installed the bin/ops helpers too (see bin/ops/README.md)"
fi

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
