#!/bin/sh
# Secret scan. Fails if tracked files contain an IP address other than 127.0.0.1, an email address,
# or a token-like string (API keys, private keys, long bearer tokens, long hex or base64 blobs).
# Strong patterns (keys, bearer tokens) are checked everywhere; the long-blob heuristic skips URLs and
# lockfiles, so a documentation link or a sha512 integrity hash does not fail the build.
#   scripts/scrub-check.sh             # tracked files
#   scripts/scrub-check.sh --history   # also every commit message and patch on every branch
#   scripts/scrub-check.sh --history-head   # also the commits the checked-out commit can reach (CI)
# CI uses --history-head: a clone with full history holds every remote branch, so --history there would
# fail main for text that only an unmerged branch carries (#163). Each pull request's own run covers its
# branch, because the checked-out commit contains it. Run --history before publishing.
# Each fixed string listed in scripts/scrub-allow.txt is blanked out of a hit line (known fakes only),
# and the rest of that line is still scanned: a real secret sharing a line with a fake is still caught.
# A dotted-quad entry is blanked only as a whole address: a routable address that
# merely contains it is still reported.
# An address on an RFC 2606/6761 reserved test name (example.com, *.invalid, *.test, *.example,
# *.localhost) cannot resolve or receive mail, so it is a fake and is not reported either.
# A GitHub noreply address is public by design and is masked the same way, wherever it appears.
# The owner's own address, left in history by a squash trailer (#248), is masked the same way.
# If .git/scrub-patterns.txt exists (a private list of extended regexes, one per line, never committed),
# tracked files and, under --history or --history-head, every commit that scan reads are also checked
# against it, case-insensitively.
set -eu
cd "$(git rev-parse --show-toplevel)"

history=0
revs=--all
case "${1:-}" in
    --history) history=1 ;;
    --history-head) history=1; revs=HEAD ;;
    "") ;;
    *) echo "usage: $0 [--history|--history-head]" >&2; exit 2 ;;
esac

IP='\b([0-9]{1,3}\.){3}[0-9]{1,3}\b'
EMAIL='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
# Reserved test names (RFC 2606, RFC 6761): a real mailbox cannot sit there. The last group anchors the
# end of the domain, so a reserved name that is only a prefix of a longer live domain still fails; the
# local part is part of the match, so a live address sharing the line with a fake is still seen.
TEST_EMAIL='[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)*(invalid|test|example|localhost|example\.(com|net|org))([^.@A-Za-z0-9_%+-]|$)'
# GitHub noreply addresses are public by design: a squash merge leaves one in its co-author trailer.
# Masked per match, never skipped as a whole line; the last group anchors the end of the domain, so a
# live-looking address on a longer domain after it still fails.
NOREPLY='[A-Za-z0-9._%+-]+@users\.noreply\.github\.com([^.@A-Za-z0-9_%+-]|$)'
# The owner's own address, from the #241 squash trailer (#248): 6cd1f7a was authored with it,
# no GitHub account owns it, so the merge added a Co-authored-by line, and removing it would
# mean rewriting main. It is the owner's address, not a third party's, so it is masked per
# match like a noreply address and the rest of the line is still scanned. The escaped dot keeps
# this line from matching itself in the tracked-files stage. The boundaries are the ones
# NOREPLY carries: a longer domain after it, or a longer local part before it, is a
# different address and is still reported.
OWNER='(^|[^.@A-Za-z0-9_%+-])hitoribocchi471@gmail\.com([^.@A-Za-z0-9_%+-]|$)'
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

strip_hashes() {   # mask sha512 integrity hashes: a hash is not a secret, the rest of the line is still scanned
    sed -E 's/sha512-[A-Za-z0-9+/=]+/sha512-HASH/g' || true
}

mask_test_addresses() {   # RFC 2606 test addresses are fakes: what is left on the line is what counts
    sed -E "s/$TEST_EMAIL/ [test-address] /g" || true
}

mask_noreply() {   # a GitHub noreply address is public: mask it, and scan what is left on the line
    sed -E "s/$NOREPLY/ [noreply] /g" || true
}
mask_owner() {   # the owner's own address (#248): mask it, and scan what is left on the line
    sed -E "s/$OWNER/ [owner] /g" || true
}

mask_allow() {   # allowlisted fixed strings are known fakes: blank each one, keep the rest of the line
    awk -v allow="$allow_file" -v mask=' [allowlisted] ' '
        function quad(e) { return e ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ }
        function bounded(kept, line, at, len, left) {   # a dotted quad counts whole, never inside a longer address
            left = at > 1 ? substr(line, at - 1, 1) : substr(kept, length(kept), 1)
            return left !~ /[0-9.]/ && substr(line, at + len, 1) !~ /[0-9.]/
        }
        BEGIN {
            while ((getline entry < allow) > 0) {
                sub(/\r$/, "", entry)
                if (entry == "") continue
                n++
                fake[n] = entry
                tight[n] = quad(entry)
            }
            close(allow)
        }
        {
            line = $0
            for (i = 1; i <= n; i++) {
                kept = ""
                while ((at = index(line, fake[i])) > 0) {
                    if (tight[i] && !bounded(kept, line, at, length(fake[i]))) {
                        kept = kept substr(line, 1, at + length(fake[i]) - 1)
                        line = substr(line, at + length(fake[i]))
                        continue
                    }
                    kept = kept substr(line, 1, at - 1) mask
                    line = substr(line, at + length(fake[i]))
                }
                line = kept line
            }
            print line
        }
    '
}


history_text() {   # every commit message and patch, without hashes or author lines
    git log -p "$revs" --format='%s%n%b' | strip_hashes
}


history_text_nomerges() {   # same, but not merge commits: their subject quotes two 40-hex commit ids
    git log -p "$revs" --no-merges --format='%s%n%b' | strip_hashes
}

report() {   # report <label>: stdin = hit lines that survived masking; runs inside pipelines
    hits=$(cat || true)
    if [ -n "$hits" ]; then
        printf '%s\n%s\n\n' "== $1" "$hits" >> "$hits_file"
    fi
}

files -E "$IP" | mask_allow | sed 's/127\.0\.0\.1//g' | grep -E "$IP" | report "IP addresses other than 127.0.0.1 (tracked files)"
files -E "$EMAIL" | mask_allow | mask_test_addresses | mask_noreply | mask_owner | grep -E "$EMAIL" | report "email addresses (tracked files)"
files -E "$STRONG" | mask_allow | grep -E "$STRONG" | report "token-like strings (tracked files)"
files_noblob -E "$BLOB" | mask_allow | sed -E 's#https?://[^ )>\"]+#URL#g' | grep -E "$BLOB" | report "long blobs outside URLs (tracked files)"
if [ "$history" = 1 ]; then
    history_text | grep -nE "$IP" | mask_allow | sed 's/127\.0\.0\.1//g' | grep -E "$IP" | report "IP addresses other than 127.0.0.1 (history)"
    history_text | grep -nE "$EMAIL" | mask_allow | mask_test_addresses | mask_noreply | mask_owner | grep -E "$EMAIL" | report "email addresses (history)"
    history_text | grep -nE "$STRONG" | mask_allow | grep -E "$STRONG" | report "token-like strings (history)"
    history_text_nomerges | grep -vE 'https?://[^ )>\"]+' | mask_allow | grep -nE "$BLOB" | report "long blobs outside URLs (history)"
fi

if [ -f "$PRIVATE" ]; then
    files -iE -f "$PRIVATE" | mask_allow | grep -iE -f "$PRIVATE" | report "private patterns from $PRIVATE (tracked files)"
    if [ "$history" = 1 ]; then
        history_text | grep -niE -f "$PRIVATE" | mask_allow | grep -iE -f "$PRIVATE" | report "private patterns from $PRIVATE (history)"
    fi
fi
if [ -s "$hits_file" ]; then
    cat "$hits_file"
    echo "scrub-check: FAILED" >&2
    exit 1
fi
echo "scrub-check: clean$([ "$history" = 1 ] && echo ' (files and history)' || echo ' (tracked files)')"
