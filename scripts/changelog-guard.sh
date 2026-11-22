#!/bin/sh
# CHANGELOG.md deletion guard. Fails when a change removes a line that the base revision still
# carries, unless that exact line is listed in scripts/changelog-allow.txt with a reason above it.
# Nothing used to read CHANGELOG.md: a branch that predates a merged note, resolved by "take our
# side", deleted a released bullet and left every check green (#199).
#   scripts/changelog-guard.sh <base-rev>          # base revision against the working tree
#   scripts/changelog-guard.sh <base-rev> <rev>    # base against another revision
# Comparison is line-level and content-level, not diff-level: a line counts as deleted when the base
# holds more copies of it than the head does, so reordering and blank-line churn pass, and a reflow
# of a released bullet fails until its old wording is listed. Every non-comment line of the allowlist
# is a standing permission: remove the entry once the change that needed it has merged.
# The allowlist path can be overridden with WAKE_CHANGELOG_ALLOW (the tests use a temporary file).
set -eu

cd "$(git rev-parse --show-toplevel)" || exit 1

base=${1:-}
head=${2:-}
if [ -z "$base" ]; then
    echo "usage: scripts/changelog-guard.sh <base-rev> [head-rev]" >&2
    exit 2
fi
if ! git rev-parse --verify --quiet "$base^{commit}" >/dev/null 2>&1; then
    echo "changelog-guard: unknown base revision: $base" >&2
    exit 2
fi

allow=${WAKE_CHANGELOG_ALLOW:-scripts/changelog-allow.txt}
if [ ! -f "$allow" ]; then
    echo "changelog-guard: no allowlist at $allow" >&2
    exit 2
fi

# An entry without a reason comment directly above it is refused: the entry is a permission someone
# will read in review, so it has to say why. A blank line ends the comment block above an entry.
if ! awk '
    BEGIN { bad = 0 }
    { sub(/\r$/, "") }
    /^[ \t]*$/ { have = 0; next }
    /^[ \t]*#/ { have = 1; next }
    {
        if (!have) {
            printf "changelog-guard: allowlist entry with no reason comment above it: %s\n", $0 > "/dev/stderr"
            bad = 1
        }
        have = 0
    }
    END { exit bad }
' "$allow"; then
    echo "changelog-guard: fix $allow first" >&2
    exit 2
fi

base_text=$(mktemp)
head_text=$(mktemp)
missing=$(mktemp)
lost=$(mktemp)
listed=$(mktemp)
trap 'rm -f "$base_text" "$head_text" "$missing" "$lost" "$listed"' EXIT

: > "$base_text"
if git cat-file -e "$base:CHANGELOG.md" 2>/dev/null; then
    git show "$base:CHANGELOG.md" > "$base_text"
fi
: > "$head_text"
if [ -n "$head" ]; then
    if git cat-file -e "$head:CHANGELOG.md" 2>/dev/null; then
        git show "$head:CHANGELOG.md" > "$head_text"
    fi
elif [ -f CHANGELOG.md ]; then
    cat CHANGELOG.md > "$head_text"
fi

# A line the base carries more often than the head does is missing from the head. Trailing
# whitespace and CR are ignored on both sides; blank lines are not notes and are not compared.
awk '
    FILENAME == ARGV[1] {
        line = $0
        sub(/[ \t\r]+$/, "", line)
        if (line != "") base[++n] = line
        next
    }
    {
        line = $0
        sub(/[ \t\r]+$/, "", line)
        athead[line]++
    }
    END {
        for (i = 1; i <= n; i++) {
            line = base[i]
            seen[line]++
            if (seen[line] > athead[line]) print line
        }
    }
' "$base_text" "$head_text" > "$missing"

total=$(wc -l < "$missing" | tr -d ' ')

# Allowed deletions are reported, not silenced: the reviewer sees what the allowlist bought.
awk -v allow="$allow" -v lost="$lost" -v listed="$listed" '
    BEGIN {
        while ((getline entry < allow) > 0) {
            sub(/\r$/, "", entry)
            if (entry ~ /^[ \t]*#/ || entry ~ /^[ \t]*$/) continue
            sub(/[ \t]+$/, "", entry)
            ok[entry] = 1
        }
        close(allow)
    }
    {
        if ($0 in ok) print > listed
        else print > lost
    }
' "$missing"

if [ -s "$lost" ]; then
    echo "changelog-guard: CHANGELOG.md drops $(wc -l < "$lost" | tr -d ' ') line(s) that $base still carries:"
    sed 's/^/  /' "$lost"
    echo "  -> restore them, or list each exact line in $allow with a reason comment above it"
    echo "changelog-guard: FAILED" >&2
    exit 1
fi
if [ -s "$listed" ]; then
    echo "changelog-guard: $(wc -l < "$listed" | tr -d ' ') of $total removed line(s) listed in $allow:"
    sed 's/^/  /' "$listed"
fi
echo "changelog-guard: clean"
