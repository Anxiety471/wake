# Publish checklist

Review before this repository goes public. Nothing here has been pushed anywhere.

## Still needs the owner

These are decisions or GitHub settings, not code changes.

- [ ] **Webhook API shape.** `POST <base>/<bot-id>/webhook` with a bearer key, `Idempotency-Key`, and the JSON fields in `docs/protocol.md` describe a specific bot platform. Confirm that describing it publicly is fine, and whether to name the platform.
- [ ] **Key helper not shipped.** `wake --setup` needs a host command implementing `has-key` / mint / `enable-bot` (`WAKE_KEY_HELPER`). It depends on the platform's admin API and is not in this repo. Decide whether to publish a reference implementation.
- [ ] **Session extension not shipped.** The agent-harness extension that registers an inbox, exports `WAKE_INBOX` and injects replies as messages is not in this repo. Publish it separately (after its own scrub) or leave as documented.
- [ ] **Session extension inbox ids.** `wake-inbox-io` now refuses any inbox hash that is not 6 to 32 lowercase hex characters, and any `read` path other than `$WAKE_STATE_DIR/inbox/<hash>.jsonl`. Confirm the live extension produces such hashes before deploying this version.
- [ ] **World-writable thread and inbox files** (`666`, dirs `1777`/`2777`). By design: any local user on the host can read and forge thread lines. Documented in README; confirm acceptable.
- [ ] **Sender context in payloads.** Every payload appends `host=`, `user=`, `cwd=` of the sender. Documented; confirm acceptable for public users.
- [ ] **Login shell on the host.** Host mode runs thread I/O through `bash -lc` (sources profiles), as the live copy did. Documented in README; keep it or switch to `bash -c`.
- [ ] **Self-asserted sender labels** (`--source`, `WAKE_SOURCE`). Documented as labels, not proof; confirm.
- [ ] **`/bots/<id>` working-directory convention** used by `wake --me` and the default sender label is platform-specific. Documented in README with `WAKE_SOURCE` as the alternative; decide whether to make it configurable.
- [ ] **Working-directory leftovers.** `TASK.md`, `TASK2.md`, `omp.log` and `omp2.log` in the checkout contain private details. They are excluded only through `.git/info/exclude` (local, not part of the repo). Do not `git add -A` with them present; publish from a fresh clone.
- [x] **GitHub settings after the repo exists** (applied 2026-11-15): private vulnerability reporting on, Dependabot alerts and security updates on, secret scanning with push protection on, and branch protection on `main` requiring the eight CI jobs (`api (bun)`, `lint`, `secret-scan`, `test (Python 3.9)` to `test (Python 3.13)`) with the administrator able to bypass and force-push and deletion blocked. `ubuntu-latest` still offers Python 3.9 through `actions/setup-python` v6; the v7 bump was closed for that reason, so the matrix and the README claim stay.
- [x] **Release tag.** `v0.2.0` is tagged on `main` to match `VERSION` and the `CHANGELOG.md` section dated 2026-11-15.

## Done

- [x] **Commit author metadata.** Every commit is authored by Joshua M. Valderrama with the GitHub noreply address. Check with `git log --format='%an <%ae>' | sort -u`.
- [x] **Copyright holder.** `LICENSE` names Joshua M. Valderrama.
- [x] **`wake-inbox-io` arguments.** Hashes must match `[0-9a-f]{6,32}`, `read` only accepts `<inbox dir>/<hash>.jsonl`, and every interpolated value goes through `shlex.quote`. Tests cover shell metacharacters (`x; touch /tmp/pwned`, `$(...)`, backticks), path traversal and guest mode.
- [x] **`WAKE_REGISTRY_GROUP` default.** Empty by default; `wake --sync` only runs `chgrp` when it is set. Tested.
- [x] **Version marker.** `VERSION = "0.1.0"` in `bin/wake_config.py`, `wake --version`, `User-Agent: wake-cli/<version>`, MCP `serverInfo.version`, and `CHANGELOG.md`.
- [x] **Python versions.** Tests pass on 3.9, 3.10, 3.11, 3.12 and 3.13 (the `bin/` scripts run under the same interpreter); README claims 3.9+. CI runs the same matrix.
- [x] **`sg` fallback tests as root.** Skipped with an explicit reason, and `tests/run.sh` prints a note when run as root. CI runs as a non-root user, so both run there.
- [x] **Name collision.** README notes that `wake` is a generic name and that `install.sh` overwrites same-named files.
- [x] **CI and repo hygiene.** `.github/workflows/ci.yml` (tests on 3.9-3.13, ruff check and format, shellcheck, compileall, secret scan), `pyproject.toml` ruff config, Dependabot for actions, `CODEOWNERS`, PR and issue templates, `SECURITY.md`, `CONTRIBUTING.md`.

## Behaviour differences from the live copy

The live scripts had hard-coded paths and identities; these are now configuration. With matching config the behaviour is the same except:

- **Host vs guest** is decided by `WAKE_HOSTOPS` being set. The live copy checked for a site-specific directory and a site-specific user name.
- **Host-mode key commands** run `$WAKE_KEY_HELPER` through `bash -c`. The live host copy (patched by its deploy script) ran `sudo -v` first. Set `WAKE_KEY_HELPER="sudo <helper>"` if the helper needs root.
- **Thread directory** is created without sudo first, falling back to `sudo mkdir` + `chmod 1777`. The live copy always used sudo.
- **Registry group.** `wake --sync` leaves the group of the host registry copy alone unless `WAKE_REGISTRY_GROUP` is set. The live copy always set `sudo`; set `WAKE_REGISTRY_GROUP=sudo` to keep that.
- **Inbox ids.** `wake-inbox-io` refuses non-hex hashes and inbox paths outside `$WAKE_STATE_DIR/inbox` (see above).
- **Clear errors instead of tracebacks** for: unreadable key when `sg` also fails, unreachable webhook host, missing registry, missing `WAKE_API_BASE`, missing key helper. Exit status stays 1.
- **Legacy form** `wake <source> <event> [detail]` reports to `WAKE_BOT` (default `coordinator`) instead of a hard-coded alias; `wake-hook` and `wake-run` use the same default.
- **Hint texts** no longer name specific people or bots.
- **Shell paths** built from config are quoted with `shlex.quote`.
- **User-Agent** is `wake-cli/0.1.0` instead of `wake-cli/2.1`.
- **Deploy script dropped.** The site-specific deploy script (copied files to named users' homes, patched the host copy with string replacement) is replaced by `install.sh` plus config. Host deployments set `WAKE_REGISTRY=$WAKE_STATE_DIR/bots.tsv` and leave `WAKE_HOSTOPS` empty.

Migrating the live setup to this version needs a private `config.env` (state dir, host bridge path, key helper name, API base, default bot, and `WAKE_REGISTRY_GROUP` if the old group should stay). Keep those values out of the repo.

## Scrub audit

The private pattern list (site/user/host names, bot ids and aliases, internal paths, personal names, the old webhook domain) is kept outside the tree at `.git/scrub-patterns.txt`, because a committed copy would itself leak them. `scripts/scrub-check.sh` uses it when present, together with the public checks (IPs other than `127.0.0.1`, email addresses, token-like strings). Rerun before publishing:

```sh
scripts/scrub-check.sh --history   # tracked files, every commit message and patch, plus .git/scrub-patterns.txt
```

`scripts/scrub-allow.txt` lists fixed strings whose lines are skipped: the test's fake minted key, and the owner's public identity (`Joshua M. Valderrama`, `@Anxiety471`). The private list contains the owner's first name, last name and GitHub handle, so without those entries `LICENSE`, `CODEOWNERS` and this file would fail; any other use of those names (for example in a home-directory path) still fails. CI runs the same script without the private list.

Last run (2026-11-14, after the second pass): `scripts/scrub-check.sh --history` clean with the private list, i.e. no private-pattern hits outside the allowlisted identity strings, no IPs other than `127.0.0.1`, no email addresses and no token-like strings in tracked files or any commit. `TASK.md`, `TASK2.md`, `omp.log` and `omp2.log` were never tracked in any commit.
