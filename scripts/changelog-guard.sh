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
# is a standing permission: remove the entry once the change that needed it has merged. An entry the
# base revision already carries fails the guard only when it is provably spent, and the reason that
# fired is printed with it: its reason comment carries `# expires: YYYY-MM-DD` and that date is past
# (#350), or the base no longer carries the line the entry permits to delete, so the change it allowed
# has already merged (#321). An inherited entry that is neither is a permission for a deletion that has
# not happened yet: it is printed as a NOTE and does not fail, because failing it reds every change
# between adding the entry and making the deletion (#350). An entry this change adds is only reported,
# unless its own expiry is already past.
# Staleness is decided offline: a date is compared, never fetched, so no network state can decide a
# build. A `# stale-when: <issue>` marker needs a GitHub lookup the test job has no token for, so the
# guard refuses it as unverifiable instead of reading it as a standing permission.
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
baseallow=$(mktemp)
spent=$(mktemp)
pending=$(mktemp)
fresh=$(mktemp)
basenorm=$(mktemp)
headmarkers=$(mktemp)
basemarkers=$(mktemp)
expired=$(mktemp)
trap 'rm -f "$base_text" "$head_text" "$missing" "$lost" "$listed" "$released" "$unused" "$baseallow" "$spent" "$pending" "$fresh" "$basenorm" "$headmarkers" "$basemarkers" "$expired"' EXIT

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

# The base revision's own allowlist: an entry it already carries was merged before this change. A base
# that predates the file has no entries, and that is not an error.
: > "$baseallow"
if git cat-file -e "$base:scripts/changelog-allow.txt" 2>/dev/null; then
    git show "$base:scripts/changelog-allow.txt" | awk '
        { sub(/\r$/, "") }
        /^[ \t]*#/ || /^[ \t]*$/ { next }
        { sub(/[ \t]+$/, ""); if (substr($0, 1, 2) == "\\#") $0 = substr($0, 2); print }
    ' > "$baseallow"
fi

# A date is the only staleness marker that needs no network: `# expires: YYYY-MM-DD` in the reason
# above an entry fails the guard once that date is past. The day is injectable so a test can plant one
# (WAKE_CHANGELOG_TODAY), and a value that is not a real calendar date is refused, never compared.
is_iso_date() {
    awk -v d="$1" 'BEGIN {
        if (d !~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$/) exit 1
        year = substr(d, 1, 4) + 0; month = substr(d, 6, 2) + 0; day = substr(d, 9, 2) + 0
        last[1] = 31; last[3] = 31; last[4] = 30; last[5] = 31; last[6] = 30
        last[7] = 31; last[8] = 31; last[9] = 30; last[10] = 31; last[11] = 30; last[12] = 31
        last[2] = (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) ? 29 : 28
        exit !(month >= 1 && month <= 12 && day >= 1 && day <= last[month])
    }'
}

# One record per entry line: `entry <US> line <US> marker <US> value`, with US = \037 so a space or a
# tab inside an entry is not a separator. A blank line ends a reason, as the reason check above assumes.
write_markers() {
    awk '
        { sub(/\r$/, "") }
        /^[ \t]*$/ { kind = ""; value = ""; next }
        /^[ \t]*#/ {
            body = $0
            sub(/^[ \t]*#[ \t]*/, "", body)
            if (body ~ /^expires:/) {
                sub(/^expires:[ \t]*/, "", body)
                sub(/[ \t]+$/, "", body)
                kind = "expires"; value = body
            } else if (body ~ /^stale-when:/) {
                sub(/^stale-when:[ \t]*/, "", body)
                sub(/[ \t]+$/, "", body)
                kind = "stale-when"; value = body
            }
            next
        }
        {
            entry = $0
            sub(/[ \t]+$/, "", entry)
            if (substr(entry, 1, 2) == "\\#") entry = substr(entry, 2)
            printf "%s\037%d\037%s\037%s\n", entry, FNR, kind, value
        }
    '
}

: > "$basemarkers"
if git cat-file -e "$base:scripts/changelog-allow.txt" 2>/dev/null; then
    git show "$base:scripts/changelog-allow.txt" | write_markers > "$basemarkers"
fi
write_markers < "$allow" > "$headmarkers"

today=${WAKE_CHANGELOG_TODAY:-$(date +%F)}
if ! is_iso_date "$today"; then
    echo "changelog-guard: WAKE_CHANGELOG_TODAY is not a YYYY-MM-DD date: $today" >&2
    exit 2
fi

# The base's own lines, normalized the way the comparison does it, so an entry can be matched against
# the text its base still carries.
: > "$basenorm"
if [ -s "$base_text" ]; then
    awk '{ sub(/[ \t\r]+$/, ""); if ($0 != "") print }' "$base_text" > "$basenorm"
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
    while IFS= read -r entry; do
        # The allowlist line this change carries, so the failure names file:line and not just text.
        where=$(awk -F'\037' -v e="$entry" '$1 == e { print $2; exit }' "$headmarkers")
        # The marker that governs is the one the base carries; a marker this change adds applies only
        # when the base has none, so deleting the marker line cannot disarm an entry that expired.
        marker=$(awk -F'\037' -v e="$entry" '$1 == e && $3 != "" { print $3 "=" $4; exit }' "$basemarkers")
        if [ -z "$marker" ]; then
            marker=$(awk -F'\037' -v e="$entry" '$1 == e && $3 != "" { print $3 "=" $4; exit }' "$headmarkers")
        fi
        kind=
        value=
        if [ -n "$marker" ]; then
            kind=${marker%%=*}
            value=${marker#*=}
        fi
        if [ "$kind" = "stale-when" ]; then
            echo "changelog-guard: cannot verify $allow:$where: '# stale-when: $value' needs a GitHub lookup, and staleness here is decided offline" >&2
            echo "  -> use '# expires: YYYY-MM-DD' instead, or remove the entry" >&2
            echo "changelog-guard: FAILED" >&2
            exit 2
        fi
        if [ "$kind" = "expires" ]; then
            if ! is_iso_date "$value"; then
                echo "changelog-guard: cannot verify $allow:$where: '# expires: $value' is not a YYYY-MM-DD calendar date" >&2
                echo "changelog-guard: FAILED" >&2
                exit 2
            fi
            if awk -v today="$today" -v expiry="$value" 'BEGIN { exit !(today > expiry) }'; then
                printf '%s\037%s\037%s\n' "$where" "$entry" "$value" >> "$expired"
                continue
            fi
        fi
        if ! grep -Fxq -- "$entry" "$baseallow"; then
            printf '%s\037%s\n' "$where" "$entry" >> "$fresh"
            continue
        fi
        # The base carries this entry. It is spent when the line it permits to delete is already gone
        # from the base: the change merged and only the entry stayed, so the guard fails. While the base
        # still carries that line the permission is for a deletion that has not happened yet, and
        # failing it would red every change between adding the entry and making the deletion (#350).
        if grep -Fxq -- "$entry" "$basenorm"; then
            printf '%s\037%s\n' "$where" "$entry" >> "$pending"
        else
            printf '%s\037%s\n' "$where" "$entry" >> "$spent"
        fi
    done < "$unused"
fi
if [ -s "$expired" ]; then
    echo "changelog-guard: $(wc -l < "$expired" | tr -d ' ') allowlist entry/entries past their '# expires:' date (today $today):"
    awk -F'\037' -v allow="$allow" '{
        printf "  %s:%s  %s\n", allow, $1, $2
        printf "    reason: expired on %s; remove it in its own commit (#350)\n", $3
    }' "$expired"
    echo "changelog-guard: FAILED" >&2
    exit 1
fi
if [ -s "$spent" ]; then
    echo "changelog-guard: $(wc -l < "$spent" | tr -d ' ') allowlist entry/entries inherited from $base match no line this base loses and no longer appear in its CHANGELOG.md:"
    awk -F'\037' -v allow="$allow" '{ printf "  %s:%s  %s\n", allow, $1, $2 }' "$spent"
    echo "  -> reason: spent; the change that needed them has merged: remove each entry in its own commit (#321)"
    echo "changelog-guard: FAILED" >&2
    exit 1
fi
if [ -s "$pending" ]; then
    echo "changelog-guard: NOTE: $(wc -l < "$pending" | tr -d ' ') allowlist entry/entries inherited from $base match no line this base loses:"
    awk -F'\037' -v allow="$allow" '{ printf "  %s:%s  %s\n", allow, $1, $2 }' "$pending"
    printf '%s\n' '  -> each is a permission for a deletion this base has not made yet; it fails the guard only once it is spent'
fi
if [ -s "$fresh" ]; then
    echo "changelog-guard: NOTE: $(wc -l < "$fresh" | tr -d ' ') allowlist entry/entries match no line this base loses:"
    awk -F'\037' -v allow="$allow" '{ printf "  %s:%s  %s\n", allow, $1, $2 }' "$fresh"
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
