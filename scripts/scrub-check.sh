#!/bin/sh
# Secret scan. Fails if tracked files contain an IP address other than 127.0.0.1, an email address,
# or a token-like string (API keys, private keys, long bearer tokens, long hex or base64 blobs).
# Strong patterns (keys, bearer tokens) are checked everywhere; the long-blob heuristic skips URLs and
# lockfiles, so a documentation link or a sha512 integrity hash does not fail the build.
#   scripts/scrub-check.sh             # tracked files
#   scripts/scrub-check.sh --history   # also every commit message and patch on every branch
# A hit line that contains any fixed string listed in scripts/scrub-allow.txt is skipped (known fakes only).
# If .git/scrub-patterns.txt exists (a private list of extended regexes, one per line, never committed),
# tracked files and, with --history, every commit are also checked against it, case-insensitively.
set -eu
cd "$(git rev-parse --show-toplevel)"

history=0
case "${1:-}" in
    --history) history=1 ;;
    "") ;;
    *) echo "usage: $0 [--history]" >&2; exit 2 ;;
esac

IP='\b([0-9]{1,3}\.){3}[0-9]{1,3}\b'
EMAIL='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
# high-confidence secrets, checked everywhere including commit messages
STRONG='(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-{5}BEGIN[A-Z ]*PRIVATE KEY|Bearer [A-Za-z0-9._-]{20,})'
# long hex or base64 blobs: heuristic, so it skips URLs (a doc link matches it) but never skips a strong hit
BLOB='([A-Fa-f0-9]{40,}|[A-Za-z0-9+/]{40,}={0,2})'
PRIVATE=.git/scrub-patterns.txt
hits_file=$(mktemp)
allow_file=$(mktemp)
trap 'rm -f "$hits_file" "$allow_file"' EXIT
grep -v -e '^#' -e '^$' scripts/scrub-allow.txt > "$allow_file" || true

LOCKFILES='(^|/)(bun\.lock|pnpm-lock\.yaml|package-lock\.json)$'

files() {   # grep tracked files: files <grep options...>
    git ls-files -z | xargs -0 grep -nIH "$@" -- || true
}

files_noblob() {   # same, minus lockfiles: their sha512 integrity hashes match the blob heuristic
    git ls-files -z | grep -zvE "$LOCKFILES" | xargs -0 grep -nIH "$@" -- || true
}

strip_hashes() {   # drop sha512 integrity lines only: a hash is not a secret, the rest of a lockfile is still scanned
    grep -vE 'sha512-[A-Za-z0-9+/=]+' || true
}


history_text() {   # every commit message and patch, without hashes or author lines
    git log -p --all --format='%s%n%b' | strip_hashes
}


history_text_nomerges() {   # same, but not merge commits: their subject quotes two 40-hex commit ids
    git log -p --all --no-merges --format='%s%n%b' | strip_hashes
}

report() {   # report <label>: stdin = hit lines; runs inside pipelines, so results go to a file
    hits=$(grep -vF -f "$allow_file" || true)
    if [ -n "$hits" ]; then
        printf '%s\n%s\n\n' "== $1" "$hits" >> "$hits_file"
    fi
}

files -E "$IP" | sed 's/127\.0\.0\.1//g' | grep -E "$IP" | report "IP addresses other than 127.0.0.1 (tracked files)"
files -E "$EMAIL" | report "email addresses (tracked files)"
files -E "$STRONG" | report "token-like strings (tracked files)"
files_noblob -E "$BLOB" | sed -E 's#https?://[^ )>"]+#URL#g' | grep -E "$BLOB" | report "long blobs outside URLs (tracked files)"
if [ "$history" = 1 ]; then
    history_text | grep -nE "$IP" | sed 's/127\.0\.0\.1//g' | grep -E "$IP" | report "IP addresses other than 127.0.0.1 (history)"
    history_text | grep -nE "$EMAIL" | report "email addresses (history)"
    history_text | grep -nE "$STRONG" | report "token-like strings (history)"
    history_text_nomerges | grep -vE 'https?://[^ )>"]+' | grep -nE "$BLOB" | report "long blobs outside URLs (history)"
fi

if [ -f "$PRIVATE" ]; then
    files -iE -f "$PRIVATE" | report "private patterns from $PRIVATE (tracked files)"
    if [ "$history" = 1 ]; then
        history_text | grep -niE -f "$PRIVATE" | report "private patterns from $PRIVATE (history)"
    fi
fi
if [ -s "$hits_file" ]; then
    cat "$hits_file"
    echo "scrub-check: FAILED" >&2
    exit 1
fi
echo "scrub-check: clean$([ "$history" = 1 ] && echo ' (files and history)' || echo ' (tracked files)')"
