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

# An entry without a reason comment above it is refused: the entry is a permission someone will read
# in review, so it has to say why. A reason covers the entry lines that follow it until a blank line,
# because a reflowed bullet is several lines and they share one reason.
# A comment is a line whose first non-blank character is #, so an entry standing for a CHANGELOG line
# that itself starts with # is written with a leading backslash (\### Fixed); a bare heading is a
# comment. Indentation is part of the line - a wrapped bullet's continuation lines start with spaces -
# so an entry that matches no removed line is reported back instead of being guessed at.
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
released=$(mktemp)
unused=$(mktemp)
trap 'rm -f "$base_text" "$head_text" "$missing" "$lost" "$listed" "$released" "$unused"' EXIT

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

# Allowed deletions are reported, not silenced: the reviewer sees what the allowlist bought. A listed
# line that the base carries under a released heading is reported again as a released deletion, so
# the allowlist cannot make a released note look like a reflow of an unreleased one.
awk -v allow="$allow" -v lost="$lost" -v listed="$listed" -v released="$released" -v unused="$unused" '
    BEGIN {
        entries = 0
        while ((getline entry < allow) > 0) {
            sub(/\r$/, "", entry)
            if (entry ~ /^[ \t]*#/ || entry ~ /^[ \t]*$/) continue
            sub(/[ \t]+$/, "", entry)
            if (substr(entry, 1, 2) == "\\#") entry = substr(entry, 2)
            ok[entry] = 1
            if (!(entry in seen)) { seen[entry] = 1; order[++entries] = entry }
        }
        close(allow)
    }
    FILENAME == ARGV[1] {
        line = $0
        sub(/[ \t\r]+$/, "", line)
        if (line ~ /^## /) { section = line; next }
        if (line == "") next
        if (section ~ /^## \[Unreleased\]/) unreleased[line] = 1
        else if (section ~ /^## /) under[line] = section
        next
    }
    {
        if ($0 in ok) {
            print > listed
            used[$0] = 1
            # Text that also appears under Unreleased is not a released-only line: the line compared
            # by content cannot say which copy went, so it is not reported as a released deletion.
            if (($0 in under) && !($0 in unreleased)) print under[$0] "\t" $0 > released
        } else print > lost
        next
    }
    END {
        for (i = 1; i <= entries; i++) {
            entry = order[i]
            if (!(entry in used)) print entry > unused
        }
    }
' "$base_text" "$missing"

if [ -s "$unused" ]; then
    echo "changelog-guard: NOTE: $(wc -l < "$unused" | tr -d ' ') allowlist entry/entries match no line this base loses:"
    sed 's/^/  /' "$unused"
    printf '%s\n' '  -> an entry that is not removed now is a standing permission; a typo or a missing indent is why this prints'
fi
if [ -s "$lost" ]; then
    echo "changelog-guard: CHANGELOG.md drops $(wc -l < "$lost" | tr -d ' ') line(s) that $base still carries:"
    sed 's/^/  /' "$lost"
    echo "  -> restore them, or list each exact line in $allow with a reason comment above it"
    if grep -q '^#' "$lost"; then
        printf '%s\n' '  -> a line that starts with # is listed with a leading backslash, e.g. \### Fixed'
    fi
    echo "changelog-guard: FAILED" >&2
    exit 1
fi
if [ -s "$listed" ]; then
    echo "changelog-guard: $(wc -l < "$listed" | tr -d ' ') of $total removed line(s) listed in $allow:"
    sed 's/^/  /' "$listed"
fi
if [ -s "$released" ]; then
    echo "changelog-guard: NOTE: $(wc -l < "$released" | tr -d ' ') allowed line(s) come from a released section, not Unreleased:"
    awk -F'\t' '{ printf "  %s\n    %s\n", $1, $2 }' "$released"
    printf '%s\n' '  -> a released note is the owner deciding in the pull request, not a reflow'
fi
echo "changelog-guard: clean"
