# Publish checklist

Items a human must review before this repository goes public. Nothing here has been pushed anywhere.

## Must decide

- [ ] **Commit author metadata.** Every local commit carries the git identity configured on the machine that made them (a personal name and email). Rewrite before pushing if that should not be public, e.g. `git -c user.name=... -c user.email=... rebase --root --exec 'git commit --amend --no-edit --reset-author'`.
- [ ] **Copyright holder.** `LICENSE` says "The wake authors". Replace with the legal owner if required.
- [ ] **Webhook API shape.** `POST <base>/<bot-id>/webhook` with a bearer key, `Idempotency-Key`, and the JSON fields in `docs/protocol.md` describe a specific bot platform. Confirm that describing it publicly is fine, and whether to name the platform.
- [ ] **Key helper not shipped.** `wake --setup` needs a host command implementing `has-key` / mint / `enable-bot` (`WAKE_KEY_HELPER`). It depends on the platform's admin API and is not in this repo. Decide whether to publish a reference implementation.
- [ ] **Session extension not shipped.** The agent-harness extension that registers an inbox, exports `WAKE_INBOX` and injects replies as messages is not in this repo. Publish it separately (after its own scrub) or leave as documented.
- [ ] **Working-directory leftovers.** `TASK.md` and `omp.log` in the checkout contain private details. They are excluded only through `.git/info/exclude` (local, not part of the repo). Do not `git add -A` with them present; publish from a fresh clone.

## Behaviour differences from the live copy

The live scripts had hard-coded paths and identities; these are now configuration. With matching config the behaviour is the same except:

- **Host vs guest** is decided by `WAKE_HOSTOPS` being set. The live copy checked for a site-specific directory and a site-specific user name.
- **Host-mode key commands** run `$WAKE_KEY_HELPER` through `bash -c`. The live host copy (patched by its deploy script) ran `sudo -v` first. Set `WAKE_KEY_HELPER="sudo <helper>"` if the helper needs root.
- **Thread directory** is created without sudo first, falling back to `sudo mkdir` + `chmod 1777`. The live copy always used sudo.
- **Clear errors instead of tracebacks** for: unreadable key when `sg` also fails, unreachable webhook host, missing registry, missing `WAKE_API_BASE`, missing key helper. Exit status stays 1.
- **Legacy form** `wake <source> <event> [detail]` reports to `WAKE_BOT` (default `coordinator`) instead of a hard-coded alias; `wake-hook` and `wake-run` use the same default.
- **Hint texts** no longer name specific people or bots.
- **Shell paths** built from config are quoted with `shlex.quote`.
- **Deploy script dropped.** The site-specific deploy script (copied files to named users' homes, patched the host copy with string replacement) is replaced by `install.sh` plus config. Host deployments set `WAKE_REGISTRY=$WAKE_STATE_DIR/bots.tsv` and leave `WAKE_HOSTOPS` empty.

Migrating the live setup to this version needs a private `config.env` (state dir, host bridge path, key helper name, API base, default bot). Keep those values out of the repo.

## Security review

- [ ] `bin/wake-inbox-io` interpolates its arguments (`<hash>`, `<inbox>` path) into shell commands unquoted. Through `WAKE_HOSTOPS` this runs on the host. Quote them or validate before relying on it with untrusted callers.
- [ ] Thread and inbox files are world-writable (`666`, dirs `1777`/`2777`) by design; any local user on the host can read and forge thread lines. Documented in README; confirm acceptable.
- [ ] Every payload appends `host=`, `user=`, `cwd=` of the sender. Documented; confirm acceptable for public users.
- [ ] Host mode runs thread I/O through `bash -lc` (login shell, sources profiles) as the live copy did.
- [ ] Sender labels (`--source`, `WAKE_SOURCE`) are self-asserted. Documented as labels, not proof.
- [ ] Default `WAKE_REGISTRY_GROUP=sudo` (kept from the live copy) is unusual for a public default.

## Smaller items

- [ ] `User-Agent: wake-cli/2.1` is the only version marker; there is no changelog or release tag.
- [ ] README claims Python 3.8+; tests were run on 3.11 only.
- [ ] The `/bots/<id>` working-directory convention used by `wake --me` is platform-specific.
- [ ] The two `sg` fallback tests are skipped when run as root.
- [ ] "wake" may collide with existing package/repo names.

## Scrub audit

The private pattern list (site/user/host names, bot ids and aliases, internal paths, personal names, the old webhook domain) is kept outside the tree at `.git/scrub-patterns.txt`, because a committed copy would itself leak them. Rerun before publishing:

```sh
P=.git/scrub-patterns.txt
git ls-files -z | xargs -0 grep -nIiE -f "$P"                               # tracked files
git log -p --all --format='%H %s%n%b' | grep -niE -f "$P"                  # every patch and message
git ls-files -z | xargs -0 grep -nIoE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' | grep -v '127\.0\.0\.1'   # IPs
git ls-files -z | xargs -0 grep -nIoE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'             # emails
git ls-files -z | xargs -0 grep -nIE '(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|xox[bp]-|AKIA[0-9A-Z]{16}|-----BEGIN|Bearer [A-Za-z0-9._-]{20,}|[A-Fa-f0-9]{40,}|[A-Za-z0-9+/]{40,}={0,2})'
```

Last run: no hits for the pattern list in tracked files or history, no IPs other than `127.0.0.1`, no email addresses. The token scan matches one line, `tests/test_wake.py`: `Bearer minted-bot-engineer-0002`, a fake key produced by the test's fake key helper.
