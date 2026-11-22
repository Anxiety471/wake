# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org/).
The version lives in `bin/wake_config.py` (`VERSION`) and is sent as `User-Agent: wake-cli/<version>`.

## [Unreleased]

### Added

- **A store dir the gate cannot list is refused by name, never answered as an empty store.**
  Every answer `rakazo-wake-gate` gives is built from a listing, and `list_dir()` / `list_active()`
  turn a lost directory into `[]`: a store that cannot be read then answers "no threads" with rc 0,
  which is the answer a quiet store gives
  ([#168](https://github.com/Anxiety471/wake/issues/168)). The v8 gate holds that line only by
  accident - each handler that lists the store lets the opendir's `EACCES` escape as an `OSError`,
  which `run()` prints as `io error: Permission denied` with no path, and the routes that do not
  touch the broken dir answer normally - so the guarantee sits in three handlers instead of in the
  check, and one `except OSError: return []` around the same call would drop it. `GateStoreListingTests`
  in `tests/test_gate_shapes.py` now holds the contract against a staged gate: a store dir made
  searchable but not listable must refuse every route with `store path cannot be listed: <dir>` and
  an empty body, while a readable store and an empty one keep their answers. It fails against the
  v8 gate and passes from v9, which proves listability once in `_check_store()` before any handler
  runs - the order the two artifacts ship in: gate first
  ([#195](https://github.com/Anxiety471/wake/issues/195)).

- **Every command wake sends is pinned, so a shape that drifts fails CI instead of a host.**
  `rakazo-wake-gate` performs only the shapes it knows and refuses everything else, and a refusal
  is silent inside wake: the call fails and `--inbox` answers from the per-thread fallback.
  `tests/test_gate_shapes.py` checks the allow-list, but only against a gate staged on the machine,
  and CI has none - so #166 shipped a new store-wide read whose gate route had to be written by
  hand into the candidate, and nothing in the repository would have gone red without it.
  `tests/test_gate_pins.py` pins all eleven shapes `bin/wake` and `bin/wake-inbox-io` build, with
  the state paths, thread ids and pids normalized to `{T}` `{I}` `{A}` `{K}` `{P}`, and drives each
  one from a real run: the ask/reply/inbox flow through a host stub, the register/beat/read of
  `wake-inbox-io`, the store read refused by a gate that predates it, the host inbox read, and an
  unreadable thread file. A change to any of them fails until `PINNED` is edited in the same
  commit, which is the review signal that the installed gate needs the new route - gate first,
  client second ([#167](https://github.com/Anxiety471/wake/issues/167)).
- **A thread carries a reply token, so the bot a doorbell woke can answer it.** The woken bot held
  no agent token, so an API-born thread advertised a route it could not use, and only the shared
  `mcp` identity existed - which the route refuses with `409`, because it is the asker of every
  thread it opened. `POST /threads` now mints one token per thread, stores only its sha256 and an
  expiry (the thread's TTL) on the row (migration `013_thread_reply_token.sql`), and ships the
  plaintext once in the recipient's doorbell payload as a top-level `replyToken`, outside `detail`.
  `POST /threads/:id/reply` accepts it in `X-Wake-Reply-Token` instead of a bearer: scoped to that
  thread, spent before the message is stored so a race cannot answer twice, refused on replay with
  the existing `409 thread already answered`, for another thread's token, and after expiry.
  `bin/wake --reply` sends it when `WAKE_AGENT_TOKEN` is absent and `WAKE_REPLY_TOKEN` is set, and
  names the missing setting when neither is. No agent token, no shared file, no new client
  ([#93](https://github.com/Anxiety471/wake/issues/93)).

- **`.editorconfig` is guarded, and the binary sniff reads both ends.** The final-newline sweep
  trusted a file nothing read: deleting `insert_final_newline = true` left the suite green while
  the editor request told the next contributor something it no longer said. `EditorConfigTest`
  parses the `[*]` section and requires `insert_final_newline = true` and `charset = utf-8`, so
  the editor half of the rule cannot rot silently. The sweep's binary sniff also reads the last
  8000 bytes now, not just the first: a file whose first NUL sits past that window was judged
  text and reported for its last byte, a false positive waiting for the first asset with a long
  ASCII header. The unreachable empty-file branch inside the sniff is gone, and a text file
  larger than the window without a newline is still an offender
  ([#71](https://github.com/Anxiety471/wake/issues/71)).

- **`wake_fleet`: the routing table inside the MCP session.** The bridge advertised only the
  server's ten thread tools, so a model could see which agents were *registered on the API*
  (`wake_bots`) but not which bots this machine can reach by alias. That knowledge lives in
  `WAKE_REGISTRY` (`bots.tsv`), which the server never sees, so the bridge answers it itself and
  advertises it in `tools/list` only when the file is readable: alias, id, name, kind and role
  for each row. A call is never forwarded, so it costs no API request and no rate limit.

- **A code of conduct.** [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) says what is expected in
  issues and reviews, and where to report conduct privately without committing an email
  address. `SECURITY.md` now names 0.2.x as the supported line.
- **The other four operator helpers, generalised.** `wake-gate-check` reviews a gate artifact
  (hash, size, its selftest, an adversarial harness, an e2e run in a copied checkout) and
  `wake-shape-check` checks every command a wake copy builds against that gate's matcher, so a
  shape the gate would refuse is caught before it turns a call into a silent no-op.
  `wake-deploy` installs a checkout's scripts — core files, `wake_config.py`, every helper — into
  a prefix locally or on an `ssh:DEST` host as one tar stream and verifies the copy there;
  `wake-host-deploy` puts the API on a host as a native systemd service in idempotent steps
  (`db`, `user`, `build`, `install`, `env`, `unit`, `status`, `log`), with `WAKE_DB_PASSWORD` and
  `WAKE_REGISTRATION_SECRET` read from the environment and written through stdin so they never
  reach a command line or the output. Every host-specific value is a `WAKE_*` variable now, and
  `tests/test_ops_deploy.py` drives all four with a stubbed `ssh`, host runner and builder.
  Loading a helper or a gate is `compile()` plus `exec`, never a loader import, so a run leaves
  no `__pycache__` behind: a clean checkout stays clean, and a check that lists `bin/ops` sees
  the helpers and nothing else. The suite passes `PYTHONDONTWRITEBYTECODE=1` for the same reason.
- **`bin/ops` shellcheck covers the shell helpers only.** `ci.yml` and the README name the five
  bash helpers instead of a glob, and `tests/test_ops_scripts.py` fails if a Python helper is
  handed to shellcheck or a shell helper is left out.

### Changed

- **A host sets its store once in `/etc/wake/config.env`.** A deployment that kept the store
  somewhere other than the default patched the installed `bin/*` scripts, and every deploy put the
  patch back to `/var/lib/wake`, a directory the host does not have - so an installed
  `wake-inbox-io` looked at nothing and the session inbox broke on the next copy. `wake_config`
  now reads `/etc/wake/config.env` before `$WAKE_CONFIG_DIR/config.env`; the user's file still
  wins, `WAKE_SYSTEM_CONFIG` names the site file (an empty value switches it off), an explicit
  `WAKE_CONFIG` still replaces the search, and no script carries a site path
  ([#125](https://github.com/Anxiety471/wake/issues/125)).

- **The doorbell names the reply token, not only an agent token.** The reply hint still told a
  thread's recipient to answer `with an agent token (not in the CLI's thread store)` while the
  payload it rides in carries `replyToken`; it says `with this payload's replyToken, or with an
  agent token` now. `docs/protocol.md`'s payload sample also names `api-thread:<id>` for a thread
  the API opened.

- **`REGISTRATION_SECRET` needs 32 characters now, and a missing one stops the boot.**
  `apps/api/src/options.ts` enforced 32 characters on `DASHBOARD_SECRET` and nothing on
  `REGISTRATION_SECRET`, so the 22-character `<generated, 32+ chars>` in `deploy/README.md`
  booted a service that answered `POST /agents` with `201` and a token. Both secrets share
  the floor now, and leaving the registration secret out is a boot error instead of an open
  registration route: `REGISTRATION_SECRET is not set, so POST /agents would accept anyone`,
  exit 1. The open case is still there for a dev box behind `WAKE_ALLOW_OPEN_REGISTRATION=1`.
  **Upgrading:** put a 32+ character secret in `/etc/wake/api.env` before you deploy, or the
  service will not start. `wake-host-deploy env` refuses a short one before it runs a host
  command, so a deploy cannot write an env file the service will not start with
  ([#22](https://github.com/Anxiety471/wake/issues/22)).

### Fixed

- **A suppression inside a linted file is pinned, so one comment cannot disarm CI.** #185 closed the
  config files a linter loads on its own; a suppression comment is the same trust boundary one level
  in, and nothing read it. At `e4ca592` a standalone `# ruff: noqa` anywhere in a test file left
  `ruff check .` at "All checks passed!", rc 0, with a real F821 further down the file (the whole
  suite at 489 OK), a bare `# noqa` on that offending line did the same for one line, a
  `# shellcheck disable=SC2154` above a real `SC2154` in `scripts/scrub-check.sh` took the shipped
  shellcheck step from rc 1 to rc 0, and `# fmt: off` at line 1 hid a misformatted file from
  `ruff format --check .`. Three more spellings ruff honours did the same at the first head of this
  branch: `# yapf: disable` at line 1 left `ruff format --check .` at rc 0 with the file
  misformatted (rc 1 with the comment removed), `# isort: skip_file` - and the same directive
  written `# ruff: isort: skip_file`, and `# isort: off` with no `on` closing the block - took a
  real I001 to rc 0, and `# ruff: disable[F821]` / `# ruff: ignore[F821]` took a real F821 further
  down the file to rc 0. `tests/test_inline_suppressions.py` reads the comments out of the files
  the two linters actually read - `tokenize` for python, so a directive quoted in a docstring is not
  one, and line by line for shell, where the directive must begin its line as shellcheck requires -
  and refuses a file-level `# ruff: noqa` / `# flake8: noqa`, a `noqa` that names no code, the
  `# fmt:` and `# yapf:` formatter directives, the `# isort:` / `# ruff: isort:` import-order
  directives, a `# ruff: disable[CODE]` / `enable[CODE]` / `ignore[CODE]` rule switch, and a
  `# shellcheck disable=` that names more than one code or carries no justification. The tree's own
  21 code-scoped suppressions pass unchanged, and the two shipped shellcheck directives are listed
  by path and line, so a new one is a deliberate edit to the pin
  ([#191](https://github.com/Anxiety471/wake/issues/191)).
- **A `*.jsonl` directory or a dangling `*.jsonl` symlink no longer turns a quiet inbox into a
  store error.** The empty answer's confirmation asked only whether a listed name ends in
  `.jsonl`, so `mkdir stray.jsonl` in a store holding no records answered `cannot read the thread
  store`, exit 1, and sent the reader to `WAKE_HOSTOPS` and permissions that were fine. The
  confirmation asks `ls -F` now and reads each entry's type mark: the store read keeps only
  regular files (`[ -f "$f" ] || continue`), so a directory, a dangling symlink, a fifo or a
  socket under a thread's name is an entry it was right to leave out, and only a regular thread
  file the read did not return still means the store could not be entered (#177). The marked
  listing is a new command shape, so a gate that predates it refuses the call and the plain
  `ls <store>` question is asked instead - the answer #177 shipped, which keeps a quiet store
  quiet and an unenterable one loud. `tests/test_gate_pins.py` pins the new shape, and the tests
  build every shape: a `*.jsonl` directory, a dangling symlink, a fifo and a socket, the
  gate-refused fallback, and a `threads` that is an executable regular file, whose `*` mark is
  what `entry_name` is for ([#186](https://github.com/Anxiety471/wake/issues/186)).
- **The files the linters read for themselves are pinned.** `lint`'s workflow was pinned by value
  (#174, #178, #182), its tool configuration was not: at `33578c5` one line in `[tool.ruff]`,
  `extend-exclude = ["tests"]`, left `ruff check .` at "All checks passed!" with a planted F821 in
  `tests/test_wake.py` (26 files read became 12) while both lint pins and the whole suite stayed
  green, and a root `.shellcheckrc` reading `disable=all` silenced the shipped shellcheck line.
  `tests/test_linter_configs.py` reads `pyproject.toml` as ruff does (a small parser, because the
  test job starts at Python 3.9 and has no `tomllib`) and pins every `[tool.ruff*]` table, key and
  value, with `extend-include` computed from the tree's python scripts that have no `.py`
  suffix. It also refuses a second linter config file at any depth (`.ruff.toml`, `ruff.toml`,
  another `pyproject.toml`, `.shellcheckrc`, `shellcheckrc`), and a `.gitignore` pattern that
  matches a tracked file, which ruff honours (`git check-ignore --no-index`, since plain
  `check-ignore` never reports a tracked path). `.ignore` counts as well: ruff's ignore-file
  support reads it, a tracked root `.ignore` naming `tests/` left `ruff check .` at rc 0 with a
  planted F401 (27 files read became 12) while this module stayed green, and
  `git check-ignore --no-index` never reports it either, so it is refused by name
  ([#188](https://github.com/Anxiety471/wake/issues/188)). Measured with ruff 0.17.5 and
  shellcheck 0.11.0: 22 mutations each fail the module. A deliberate change edits the pin in the
  same commit ([#185](https://github.com/Anxiety471/wake/issues/185)).

- **The `lint` pin reads the interpreter too, and every test file runs on the module path.**
  Three holes left by #178's pin: `python-version` inside `lint`'s `actions/setup-python` step
  was not read, so `"3.13"` → `"3.9"` kept both lint pins green while a different interpreter
  ran ruff and shellcheck; the step-key comment cited a Kanban board id in a public file; and
  `python3 -m unittest tests.test_ops_scripts` died on `ModuleNotFoundError: No module named
  'test_scrub_check'` after the step parser was imported from a sibling test module. The `with:`
  section is asserted by value (as `secret-scan` asserts checkout's `fetch-depth: 0`), the
  comment cites the issue instead of a board id, and the three test modules that import a
  sibling fall back to the dotted path, so both invocations run the suite
  ([#182](https://github.com/Anxiety471/wake/issues/182)).

- **The `lint` job cannot be disarmed through a key any more.** #174 pinned `runs-on`'s value and
  left the job around it open: at `06c2c75` the full suite stayed green with `runs-on:
  ubuntu-24.04` untouched under a job `container:`, `if: false`, `continue-on-error: true`, a job
  `defaults` that swaps the run shell, a step `if: false` and a step `continue-on-error: true` - a
  skipped job reports green, a neutral one does too, and a `container` runs every step in another
  image. `lint` is one of the eight required checks on main, so
  `tests/test_ops_scripts.py::test_ci_lint_job_keys_cannot_disarm_it` pins its job keys (`runs-on`,
  `steps`), refuses a step-level `if` or `continue-on-error`, pins every step's name and `run`
  payload by value, and allowlists each step's own keys - the pin `backgrounded` and `secret-scan`
  already carry ([#178](https://github.com/Anxiety471/wake/issues/178)). The step keys are their
  own hole: `working-directory: /tmp` on `ruff check` runs it in an empty directory, which exits 0
  with a warning, and a step `shell:` or an added `env:` decides what the command reads without
  touching a pinned value, so the allowlist is what makes the sentence above true
  ([#181](https://github.com/Anxiety471/wake/issues/181)).

- **The `lint` job's runner is pinned by value, not just present in a key list.** #172 pinned the
  `secret-scan` image and left `lint` on `ubuntu-latest`: at `24ca37d` nothing read that job's
  `runs-on` - `grep -rn lint tests/` was 0 hits, and the key list in `tests/test_ops_scripts.py` is
  `backgrounded`'s - and `lint`'s verdict comes from the runner's own `shellcheck` and `python`, so
  the machine is part of the check. Measured on a
  fresh copy of `test_scrub_check` + `test_ops_scripts` at `24ca37d`: `runs-on: self-hosted` on
  `lint` kept all 101 tests green. `runs-on` is `ubuntu-24.04` now, the image every other job pins,
  and `test_ci_lint_job_runs_on_a_pinned_image` reads the value and refuses anything else, so a
  floating tag or a self-hosted machine fails CI instead of quietly changing what lint reads
  ([#174](https://github.com/Anxiety471/wake/issues/174)).

- **The secret scan's runner and the workflow's triggers are pinned, not just named.** #169 pinned
  the `secret-scan` job's keys and left two values open, both measured by mutation at `97b9436`:
  `runs-on: self-hosted` kept the whole suite green with the scan running on a machine whose `git`,
  `grep` or global `git config` is not the image's, and replacing the workflow's
  `on: push / pull_request` with `on: workflow_dispatch:` kept it green with no run at all - not a
  green check but an absent one, so the only history scan in CI was off. `runs-on` is
  `ubuntu-24.04` now, the same image the test jobs pin for a stated reason, and
  `tests/test_scrub_check.py` asserts the value; `tests/test_ops_scripts.py` pins the two triggers
  by name and refuses an inline `on:`, a `branches:` filter or a `types:` list. A rename of the scan
  step is pinned too, so a reviewer sees it in the diff
  ([#172](https://github.com/Anxiety471/wake/issues/172)).

- **A store directory that lists but cannot be entered is reported, not answered as an empty
  inbox.** The #168 confirmation used the listing's exit status, and listing needs only `r` on the
  directory: a `threads/` holding thread files at mode 444 or 644 printed every name to `ls` while
  the store read's glob expanded to nothing, so the answer stayed `no threads opened by <source>`,
  exit 0. The listing is now also compared with what came back: a `.jsonl` name the read did not
  return is a store that could not be entered, reported as `cannot read the thread store`, exit 1.
  Same single call, same `ls <store> 2>/dev/null` shape, so no gate change; an empty listing stays
  a quiet inbox ([#177](https://github.com/Anxiety471/wake/issues/177)).

- **A store directory that cannot be read is reported, not answered as an empty inbox.** `--inbox`
  answered `no threads opened by <source>` with exit 0 when `threads/` was missing, unreadable, or
  a regular file, so a mistyped `WAKE_HOSTOPS`, a wrong state dir, or a permissions problem read as
  a quiet morning. Every store command silences stderr, because that is the shape the host gate
  matches, so a store with no files and a store the shell could not enter printed the same thing.
  When the answer is empty the client now confirms it with `ls <store> 2>/dev/null`, whose exit
  status is the difference, and reports `cannot read the thread store`, exit 1, otherwise. The
  confirmation costs one call and only when the answer is empty, and `ls <store> 2>/dev/null` is
  the gate's own `thread list` route, so this needs no gate change
  ([#168](https://github.com/Anxiety471/wake/issues/168)).

- **CI's secret scan cannot be quietly switched off any more.** #164 pinned the flag `ci.yml`
  passes and left the job around it open: an `if:`, a `continue-on-error:` or a job `defaults` on
  `secret-scan` skips the only history scan in CI, or reports a failed one as neutral, while every
  test stayed green - the class #162 pinned for `backgrounded`. `tests/test_scrub_check.py` now
  pins the job's keys, its two steps, the scan step's `name`/`run` pair and the run body, and
  refuses `if` and `continue-on-error` at job and step level, including the dash-line and YAML
  anchor forms. It pins the checkout's `with: fetch-depth: 0` too, now load-bearing for
  `--history-head`: a depth-1 clone holds one commit, so the history scan sees nothing and stays
  green (a test clones the fixture at depth 1 and shows the same hit scan clean).
  `scripts/scrub-check.sh`'s own header comment said the private list covers history only "with
  `--history`", which was false for `--history-head`
  ([#164](https://github.com/Anxiety471/wake/issues/164), [#169](https://github.com/Anxiety471/wake/issues/169)).

- **A branch nobody merged can no longer turn main's secret scan red.** CI checks out with full
  history, which also fetches every remote branch, and `scripts/scrub-check.sh --history` reads
  every ref, so text that only an open pull request carried failed the run on main with no commit
  on main to fix (main was red at the merge of #159 for prose that lived only on #161's branch).
  The new `--history-head` reads only the commits the checked-out commit reaches, and CI uses it:
  a pull request's own run still scans its whole branch, and main's run scans main. `--history`
  stays as the every-branch scan to run before publishing
  ([#163](https://github.com/Anxiety471/wake/issues/163)).

- **`wake --inbox` reads the whole store in one host call, not one per thread.** A guest pays
  420-519 ms for every bridge call and `threads_mine()` made one `cat` per thread, so a
  128-thread store took 51 s and every thread anyone ever opened made the next run slower: under
  a 30 s timeout the command was a hang with nothing on stdout. One command now prints a marker
  line and cats each file; a record is one JSON object per line, so a line that starts with the
  marker is never a record, and each `cat` sees whatever is there, so an append during the read
  tears nothing. `thread_read()` still serves `--thread` and `--reply`, an empty store is still
  `no threads opened by <source>` in one call, an unreadable store still exits 1 naming it, and
  a host whose gate predates the new shape refuses the one call and gets the old per-thread
  read, so one client works against either gate. A single *unreadable file* is named by a
  second marker (`@@wake-fail`) instead of disappearing: the loop prints it after a `cat` it
  could not do, the CLI turns it into the same exit 1, and the per-thread fallback reports it,
  because a short inbox must never hide the reply someone is waiting on. The 1.1 s number is
  the ungated path: a gated host keeps the old speed until its gate is taught the store-wide
  read, and the gate is a host change ([#165](https://github.com/Anxiety471/wake/issues/165)).

- **The secret scan masks a GitHub noreply address per match, never the whole line.** The allowlist
  entry added in [#156](https://github.com/Anxiety471/wake/pull/156) skipped every line holding the
  address, so a key on that line went unseen, and a live-looking address on a longer domain that
  merely starts with the noreply domain was skipped with it. The address is now masked in the email
  stage like a reserved test name, anchored at the end of the domain, and the allowlist entry is gone.
- **Every other allowlisted fake blanks only its own string, so a secret beside it is still
  caught.** `report()` dropped the whole hit line with `grep -vF -f`,
  so one known fake - `@Anxiety471`, the fake desk key, the fake bearer - hid every other secret
  sharing that line: an `AKIA` key or a routable address planted beside the owner's handle came out
  `scrub-check: clean`, in tracked files and in history. Each fixed string is now blanked out of the
  line (`mask_allow()`) and every stage rescans what is left, private patterns included. The
  `0.0.0.0` of the bind-all assertion joins the allowlist because that line carries two of them,
  and it is blanked only as a whole address: a routable address that merely contains it is still
  reported ([#160](https://github.com/Anxiety471/wake/issues/160)).
- **A failed read of `api.env` names its exit status, never the file's text.** The command that
  names the file's state printed the file, and it went through `_run`'s generic failure message,
  which repeats the last 800 characters of output: a transport that died after `cat` had emitted
  bytes - a dropped `ssh` connection, a failing `printf` in the `then` branch - put the tail of
  `api.env`, `DATABASE_URL`'s password and `REGISTRATION_SECRET` included, into the deploy log.
  That is the one promise the file header makes ("never prints a secret"). The read now runs
  through `_capture()`, which hands back the exit status, stdout and stderr with no echo, and a
  nonzero status refuses before the answer is parsed (`the command that names ... exited N;
  refusing to rewrite the file`). It is refused rather than read with `check=False`: a transport
  cut off after the marker still ends in `__wake_env:read__`, so reusing that stdout would write
  half a file back - the state `#146`'s F3 refusal exists to prevent. Every other step keeps the
  echo, because a failed `systemctl` has to say why it failed
  ([#151](https://github.com/Anxiety471/wake/issues/151)).
- **The CI job that runs the suite backgrounded refuses the one-line disarm keys, and
  `wake-keys` reads every field the resolver answers.** Four leftovers from the #149/#150 reviews.
  Adding
  `continue-on-error: true` to the `backgrounded` job made GitHub report a failed suite as neutral
  and left `tests.test_ops_scripts` green - the blindness `#142` was filed about, one YAML line
  away - so the test now pins the job's top-level keys to exactly `name`, `runs-on` and `steps`,
  which also refuses `if:`, and refuses `if` or `continue-on-error` at a step's own indent, on
  its dash line (`- if: false` above `name:`) or behind a YAML anchor (`- &a if: false`), any of
  which skips or neutralises the run step without touching the job's keys. Those four shapes are
  refused. The run step's own keys are pinned to `run` and `env`, so a step-level
  `shell: '/bin/true {0}'` - which replaces the interpreter and never runs the suite - now fails
  by name instead of passing; its body is pinned to `tests/run.sh &` and `wait $!`, so an
  `exit 0` above the suite (which left every key guard green, measured at 88aa476) fails too;
  and its env block is pinned to `PYTHON: python`, because `PYTHON: /bin/true` execs nothing and
  exits 0, the same disarm one indent deeper (measured). The file's own top-level keys are pinned
  to `name`, `on`, `permissions` and `jobs`, because a workflow-level `defaults: {run: {shell:
  '/bin/true {0}'}}` swaps the shell for every run step in the file without touching the job
  (measured). A YAML merge key at the file, the job or the run step fails the same way, as `<<`
  (measured): the key lists are allowlists. `wake-keys` split the
  resolver's answer on a tab, which is IFS
  *whitespace*: an emptied `WAKE_KEY_DIR=` in a site file collapsed and shifted every later value
  one place left, so the helper took the host store as the key dir and the registry as the store,
  then said "no registry" while the file named a readable one. It reads one value per line now,
  and an empty `WAKE_REGISTRY` is refused instead of being silently replaced by `$CFG/bots.tsv` -
  a different file than the one `bin/wake` would read. `WAKE_HOSTOPS` also comes from the resolver
  now: it was environment-only while `wake_config.load()` reads it from the site and user files, so
  a machine whose bridge is named in its config printed `none (no key in the host store)` for every
  bot and never called the host. The unjoinable-marker sentence for a path that vanished mid-check
  promised the next poll would name what is there; with the path free it creates the marker and
  prints the outage line instead, and the sentence says so now
  ([#152](https://github.com/Anxiety471/wake/issues/152)).
- **CI runs the suite as a background job, and the `wake-wait` interrupt test stops racing its
  baseline.** Deleting `tests/test_ops_scripts.py`'s `preexec_fn` left the suite green, because CI
  ran it in the foreground, where the test's result does not depend on the shell's `SIGINT`
  disposition, while the same test failed for anyone who ran the suite backgrounded. A CI job now
  runs it in exactly that shape (`tests/run.sh & wait $!`), which fails without the `SIG_DFL` reset.
  The test also waited for one recorded stub call, but the stub records before `wake-wait` writes
  its baseline, so an interrupt could land while the first poll was in flight and the mutant then
  failed by a 20 s `TimeoutExpired` instead of by the assertion, two runs in five; it waits for two
  calls now, which makes the comment true and the failure the assertion's. One redundant assertion
  is gone, and `tearDown` removes the per-run temp directory - 14,079 had accumulated in one
  container ([#142](https://github.com/Anxiety471/wake/issues/142)).
- **`wake-keys` reads every location from the resolver, and only when it runs.** Asking it for the
  store alone left `WAKE_CONFIG_DIR`, `WAKE_KEY_DIR` and `WAKE_REGISTRY` environment-only, so a
  site file that names the registry and the key dir - the shape a deploy writes - made the helper
  say "no registry" while the CLI beside it read all three. One call now returns all four, in the
  order `wake_config.load()` uses, and `--help` makes no call at all: it used to start an
  interpreter to print its usage text. The bare-copy fallback to `/var/lib/wake` stays and is
  pinned. The unjoinable-marker line also stops asking the reader to do what sticky `/tmp` forbids:
  it names the owner and says only that account or root can remove it
  ([#135](https://github.com/Anxiety471/wake/issues/135)).

- **The `#144` review's three Lows are closed, and one of them was a secret printed by a refusal.**
  `parse_env_answer()` echoed up to 120 characters of the line it did not understand, and the last
  line of an `api.env` is normally a `KEY=value`: a `DATABASE_URL=` line printed the database
  password and a `REGISTRATION_SECRET=` line printed the registration secret in the exit message.
  The refusal names the line's length now, never its text. An answer that names no state at all -
  what a transport that exits 0 with no stdout hands back - read as an absent file and rewrote
  `api.env` from the managed keys alone, dropping every other key with exit 0; it is a refusal now.
  The once-per-uid outage marker was trusted whenever it was a regular file, so `touch` at
  `/tmp/.wake-inbox-io-warned-<uid>` (sticky `/tmp`, any account) silenced the line for good: it
  must be a regular file this account owns, and a path it does not own is named once per
  obstruction, cleared with the marker. The missing-`scp` message no longer claims the staged file
  was removed while the cleanup ran unchecked: the cleanup is checked, and a host that refused it
  says the file may still be there ([#146](https://github.com/Anxiety471/wake/issues/146)).

- **A later outage of the store is reported again, and a `root:root` store is told to `chgrp`.**
  `wake-inbox-io` wrote its once-per-uid line marker and never removed it, so the first outage in
  the life of an account was the only one ever reported: every later one, and any other store,
  exited 1 with an empty stderr. The marker is now cleared on every route that reaches the store,
  so break, recover and break again reports twice. A store owned by group `root` - the ownership
  mistake these routes exist to catch - no longer suggests joining group `root`, which nobody is
  added to; it says to `chgrp` the store to a group the account is in
  ([#135](https://github.com/Anxiety471/wake/issues/135)).

- **`wake-keys` reads the site file.** It is a shell helper and resolved `WAKE_STATE_DIR` from the
  environment only, so on a host whose store is set in `/etc/wake/config.env` it looked in
  `/var/lib/wake` and printed `none` with exit 0 for every bot: a silent "no key". It now asks the
  same resolver the CLI ships (`wake_config.load()`), which reads the environment, the user file and
  the site file in that order; `WAKE_HOST_KEY_DIR` still wins, and the built-in default stands when
  the resolver is not beside the script
  ([#135](https://github.com/Anxiety471/wake/issues/135)).

- **`wake-host-deploy` no longer turns a missing local `scp` into a traceback and a stranded file,
  and cannot silently empty `api.env`.** A machine without `scp` raised `FileNotFoundError` (an
  `OSError`, not the `SystemExit` the cleanup caught), leaving the staged bundle in the host's
  `/tmp`; the cleanup now covers both. A host without `node` printed `FAILED: command -v node`
  instead of the message that names `WAKE_NODE`, because the probe exited on a failed command. And
  the `api.env` read answered the same for a missing file and for a refused read, so a rewrite
  merged from an empty string dropped every key the file carried: the three cases are now named
  apart, and an unreadable file stops the step
  ([#134](https://github.com/Anxiety471/wake/issues/134),
  [#131](https://github.com/Anxiety471/wake/issues/131)).

- **A review of the store-path batch found four holes, and they are closed here.** `env_file_text()`
  read the state marker from the last line, and `cat` of an `api.env` whose last line has no
  newline put the marker on the end of that line (`5__wake_env:read__`): no state matched, the read
  came back empty, and the rewrite dropped every key - the failure #131 is about. The shell now
  prints a newline before the marker, and an answer that names none of the three states refuses the
  rewrite instead of reading as an empty file. The bridge builds its child environment from
  scratch, so `tests/run.sh`'s `WAKE_SYSTEM_CONFIG=` never reached it and a host site file carrying
  `WAKE_MCP_TOKEN` failed three bridge tests; that environment now names the empty site path. A
  directory planted at `/tmp/.wake-inbox-io-warned-<uid>` (sticky `/tmp`, any account) silenced the
  once-per-outage line for good, because the marker is created `O_EXCL` and
  `clear_unreachable_marker` cannot unlink a directory: a path that is not this account's regular
  file now prints the line, names what holds it and says who can remove it. And a missing local `scp` is one line instead
  of a traceback, which is what the entry above already claimed
  ([#135](https://github.com/Anxiety471/wake/issues/135)).

- **The `wake-wait` interrupt test no longer measures how the suite was launched.** It slept a
  blind 1.5 s and then sent `SIGINT`, so it silently required the first poll to return inside that
  window - and it failed every time the runner was a background job, because POSIX starts those
  with `SIGINT` ignored, Python keeps an inherited `SIG_IGN`, and bash cannot trap a signal that
  was ignored on entry: the wait polled until the test's own 20 s window closed and reported a
  `TimeoutExpired` that said nothing about the trap. Measured here: the test passes 50 times out of
  50 (25 quiet, 25 under four CPU hogs), and passes against a stub whose first poll takes 2 s - a
  slow poll only defers the trap, which is correct - while the same suite started with `nohup`
  fails. The test now gives the child `SIG_DFL` before exec, waits for the stub's first recorded
  call instead of guessing, and asserts that the wait really polled
  ([#121](https://github.com/Anxiety471/wake/issues/121)).
- **`deploy/README.md` says when to write `/etc/wake/config.env`, not only what goes in it.** The
  section named the file and the harness that reads it but not the order, and the wrong order is
  silent rather than loud: with no site file, `wake --inbox` exits 0 printing
  `no threads opened by human:<account>`, `wake-inbox-io read` prints nothing, and the next append
  runs `mkdir -p` and creates the default `/var/lib/wake` beside the real store. The runbook now
  writes the file before the first deploy, shows the two commands that print the resolved
  store (from the checkout before the deploy, from the installed prefix after it),
  states the precedence highest-first, and says to keep the file root-owned 644 with no secrets;
  `tests/test_deploy_docs.py` runs that command against a site file and fails if the order or the
  command drifts ([#135](https://github.com/Anxiety471/wake/issues/135)).
- **A failed `scp` no longer leaves its half-written bundle in the host's `/tmp`.** `put()` sent the
  file, checked `scp`'s status and exited *before* the `try` that collects a refused install, so a
  dropped connection left a truncated `index.mjs` staged with nothing to remove it - the same leak
  as #117, one branch earlier. The transfer and the install now share one cleanup: any `SystemExit`
  after the host has named a staging file removes it, and a test drives an `scp` that writes half a
  file and says `lost connection`
  ([#129](https://github.com/Anxiety471/wake/issues/129)).
- **The four `host_out()` reads that had no test now have one each.** Three of the seven call sites
  were pinned; reverting `command -v node` (`:605`), `cat index.commit` (`:597`) or either
  `psql -tAc` probe (`:266`, `:271`) to `host()` left the module green, and no install test drove
  the node fallback at all because they all set `WAKE_NODE` - the branch that once produced
  `install: cannot stat`. Each now has a test that a warning on stderr changes nothing: the node
  path stays a path, the commit prints alone, and the probes still answer `db: already present` and
  `owner: wake` instead of running `createdb` against an existing database
  ([#129](https://github.com/Anxiety471/wake/issues/129)).
- **The scp tests stop leaking real `/tmp/.wake-deploy.*` files.** The fake host answered `mktemp`
  and swallowed `install … && rm -f`, so every install the class drove left a staged file behind
  (three per module run) - invisible to a green CI. The stub now runs the cleanup the real command
  carries, and a test requires the file `scp` wrote to be gone after the run
  ([#129](https://github.com/Anxiety471/wake/issues/129)).
- **`--help` states that `WAKE_HOSTOPS` wins when both transports are set.** `transport_kind()` made
  that the rule and `deploy/README.md` says so, but the text an operator reads at 2 a.m. still
  offered "one is required" and left the tie unexplained; a test pins the sentence
  ([#129](https://github.com/Anxiety471/wake/issues/129)).
- **`wake-inbox-io` reaches the store from a session that started before its group existed.**
  Linux fixes a process's supplementary groups at exec time, so a shell opened before the `wake`
  group was created never sees it and every child inherits the same list: the store (`root:wake`
  mode 2770) stays unreachable for the life of that session, though a new login would work. The
  bridge re-enters through `sg <group>` when `/etc/group` lists this account for the store's group,
  which is the fallback `bin/wake` already had for an unreadable key file. When no route reaches
  the store it now prints one line and exits 1 before running a command that can only fail, and
  says it once per uid instead of once per poll - the session extension polls every 3 s and its
  stderr reaches the terminal. A write needs the inbox writable and a read only readable, so a
  read-only store still answers `read`, which is what it did before the route decision existed
  ([#125](https://github.com/Anxiety471/wake/issues/125)).
- **`install` stops chmodding a migrations glob it did not fill.** A build carrying no migrations
  printed `migrations: 0 (this build carries none)` and then ran
  `chown -R … && chmod 644 …/migrations/*.sql` anyway. On a first install that directory is empty,
  the shell leaves the pattern literal, `chmod` exits 1 on a path that does not exist, and the step
  reported `FAILED` after `index.mjs` and `index.commit` were already in place - a partial deploy
  that says nothing changed, and re-running failed the same way. The `chown` is its own command now
  (the directory always exists, because the same step creates it) and the `chmod` names the files
  the build actually shipped instead of globbing, so a directory holding something else cannot be
  reached by it. Two tests drive the real helper against a recording host: one installs a build with
  no migrations and fails if any `*.sql` reaches the host at all, the other requires each shipped
  migration to be chmodded by name; both fail against the old line
  ([#106](https://github.com/Anxiety471/wake/issues/106)).
- **`install` stages and cleans up as root, so the documented non-root operator works.** `put()`
  wrote the staging file with `cat >` as the operator into root-owned `/opt`, and the line after the
  swap ran `rm -f` unprivileged: a `rakops` deploy holding `sudo -n` failed at the first staging
  write, and after a hand-made staging file it exited `FAILED` with the new `index.mjs` already in
  place and the staging file left behind, which reads as "nothing changed" to anything that trusts
  the exit code. `put()` now creates the file through `install(1)` and fills it through `priv()`
  (the ssh transport lands the bytes in the operator's own `/tmp` first, because scp cannot write
  through sudo), both cleanups are privileged, and `WAKE_SUDO=""` still yields a command with no
  prefix for the root case. Tests assert the exact host commands for a non-root and a root operator,
  and that a dry run writes nothing
  ([#109](https://github.com/Anxiety471/wake/issues/109)).
- **The ssh transport stages under a name the host creates, and a host notice cannot become part
  of that name.** `put()` built the staging path from the pid and the basename, so any local account
  on the host could pre-create `/tmp/wake-put-<pid>-<file>` and have the operator's `scp` write
  through it; and `host()` returns stdout and stderr together, so a `Warning: Permanently added …`
  line was spliced into the name `scp` was handed, `install` then failed with
  `No such file or directory` and both staged files were left behind. The path now comes from
  `mktemp /tmp/.wake-deploy.XXXXXX` on the host and only its first line is used. The scp branch had
  no test at all - every deploy test drives the `WAKE_HOSTOPS` branch - so two now drive it against
  a fake `ssh` and `scp`: one requires two consecutive installs to stage under different names that
  already existed `0600`, the other requires a notice on stderr to leave the staged path alone. Both
  fail against the old line
  ([#117](https://github.com/Anxiety471/wake/issues/117)).

- **The reply token never reaches the request log.** A woken bot's whole credential is the
  `X-Wake-Reply-Token` header on `POST /threads/:id/reply`, the one request the API logs without a
  bearer, and the `incoming request` line wrote it in plaintext: `headersForLog` and `REDACT_PATHS`
  redacted `authorization`, `x-registration-secret` and `x-dashboard-secret` only. A refused reply
  keeps the token unspent and valid for the thread's TTL, so anyone who could read the log could
  answer that thread. The header is redacted in both places now, including a header copied straight
  into a log record, and the test that asserts it needs no database.

- **The reply-token hook matches the reply route by path, anchored.** `request.url` carries the
  query string, so the unanchored pattern also matched `POST /messages?q=/threads/x/reply`: an
  unauthenticated request with any `X-Wake-Reply-Token` header passed the `onRequest` hook and
  reached a handler with no agent, which answered `500` instead of `401`. The pattern is anchored
  and tested against the path alone, with a test for the bypass string.

- **`bin/wake --reply` sends one credential, not two.** With both `WAKE_AGENT_TOKEN` and
  `WAKE_REPLY_TOKEN` set, it sent the bearer and the thread token together, and the route reads the
  bearer first, so the thread token was never spent - against the docs, which say the reply token is
  sent when there is no agent token. The bearer now wins and the reply token is not sent at all.

- **A refused inbox says so, instead of printing a path to nothing.** `wake-inbox-io` discarded the
  exit status of every host command it ran, so a gate refusal was indistinguishable from success:
  `register` printed an inbox path for a file nothing had created and exited 0, and the session
  exported `WAKE_INBOX` pointing at nothing. `register` and `beat` now return the host command's
  status and print its stderr, and a failed `register` prints no path. `read` is unchanged: an
  unreadable inbox still reads as empty, because that is what a quiet inbox looks like.

- **The doorbell no longer sends an API thread's recipient to `wake --reply`, which cannot answer it.**
  The API copied the CLI's payload wording, so an MCP `wake_ask` arrived as
  `[thread <uuid>] reply: wake --reply <uuid> "YOUR ANSWER"` with `ref: thread:<uuid>`, and
  `bin/wake` refused with "no thread ...: it was never opened": the id is in Postgres, and the CLI
  reads the shared-folder store. The hint now names the route that answers it
  (`POST /threads/<id>/reply` with an agent token, the same route as the MCP tool `wake_reply`),
  `ref` is `api-thread:<id>` so a routine that routes on the CLI's `thread:` prefix does not fire
  the CLI, and `wake --reply <uuid>` names the store and the route instead of claiming the thread
  was never opened. CLI threads are unchanged: their hint, prefix and refusal are what they were.
  **The CLI can answer one now**: with `WAKE_AGENT_TOKEN` (this bot's own token from `POST
  /agents`) and `WAKE_API` (the API root) set, `wake --reply <uuid>` posts to
  `POST /threads/<id>/reply` as this bot, prints the new `seq` and surfaces the API's refusal
  verbatim; with neither set it names the missing setting instead of claiming the thread never
  existed. The shared `mcp` token is not a substitute: it is the asker of every thread it opened,
  and the route answers `409 the asker cannot answer its own ask` (`threads.ts`). Deploying a
  per-bot token to the fleet is [#93](https://github.com/Anxiety471/wake/issues/93); the false
  instruction fixed here is [#96](https://github.com/Anxiety471/wake/issues/96).
- **`wake-host-deploy` can no longer ship a tree it cannot name.** `build` ran `bun build`
  inside `WAKE_REPO` and read nothing about the checkout, so a stale branch, a half-rebased
  worktree or a bot's verification clone all shipped as `main`: on 2026-11-16 00:44 PHT the
  shared clone stood on an unmerged pull request's tree and `install` would have put it on
  the host with no record of it. `build` now refuses uncommitted changes (naming the files)
  and a `HEAD` that is not `origin/main`'s commit (naming both ids), prints the commit it
  built and records it beside the artifact, so `install` says which one it shipped.
  `WAKE_ALLOW_ANY_REF` is the deliberate hotfix and it is an allowlist: `1`, `true`, `yes`
  or `on` builds the checked-out commit and says so on stderr, while the default word the
  documentation uses, `off`, means off ([#75](https://github.com/Anxiety471/wake/issues/75)).

  Review found the promise was not true yet, in four places, and each one is a test now:
  `install` printed the build's commit and then tarred `apps/api/src/migrations` from the
  live checkout, so an uncommitted `.sql` added after the build ran against the live
  database at the next boot - the build carries its own copy of the migrations and `install`
  reads only that; `git status --porcelain` honoured `status.showUntrackedFiles=no`, so
  untracked work was invisible to the clean check - it reads `--untracked-files=all`; the
  tree was read before the builder and never again, so a builder that committed mid-run
  left an artifact stamped with a commit it did not belong to - the tree is re-read after
  the build and a moved checkout is not recorded; and `status` printed the last build's
  commit from the operator's state directory, which build B after installing A answered
  "B" while the host ran A - `install` writes the commit beside the installed file and
  `status` prints that.

- **A reserved test address is not a secret, and one branch's fixture failed every branch's scan.**
  `scripts/scrub-check.sh` flagged `user.email=simulation@example.invalid` in
  `tests/test_publish_checklist.py`: an address on an RFC 2606 reserved name, which can never resolve
  or receive mail. The email stage ignores an address whose domain ends in a reserved test name
  (`example.com`, `example.net`, `example.org` and their subdomains, `*.invalid`, `*.test`, `*.example`,
  `*.localhost`), one occurrence at a time: a live address sharing the line still fails, a reserved
  name that only prefixes a live domain still fails, and `.local` or `.internal` names stay secrets.
  The scan reads `git log -p --all` on a `fetch-depth: 0` checkout, so while that fixture lived on
  any branch every pull request's secret-scan failed, not just the branch that added it
  ([#84](https://github.com/Anxiety471/wake/issues/84)).
- **The publish-checklist guard could not be green on a release commit.** It required every
  claimed tag to exist in the clone, so the one commit that prepares a release - `VERSION`
  bumped, the new tag named, the tag itself cut after the merge - failed by construction:
  `v0.3.0 is named but is not a tag in this clone`. A claim for exactly `v<VERSION>` may now name
  a tag this clone does not have yet, and its id is checked as soon as the tag exists, which is
  when it is corrected to the merge commit. Every other claim keeps its teeth: an older release
  line whose tag is missing, or on another commit, still fails, so the stale `2375c80` line that
  started this guard is still rejected. `ReleaseSimulationTest` builds a real repository, tags
  `v0.2.0`, prepares `v0.3.0` and cuts the tag, so the release path is proved against git instead
  of a stub ([#73](https://github.com/Anxiety471/wake/issues/73)).

- **A NUL in `WAKE_REGISTRY` still emptied `tools/list`.** `fleet()` caught only
  `OSError`, but `open()` raises `ValueError` for a path with an embedded NUL, and a
  `config.env` can carry that byte even though an environment variable cannot: one NUL
  produced the same `-32603` and zero tools as the UTF-8 bug below. It catches both
  now, and `reach` quotes the alias with `shlex.quote`, so an operator-written alias
  cannot turn the suggested command into a second one
  ([#79](https://github.com/Anxiety471/wake/issues/79)).

- **One non-UTF-8 byte in `bots.tsv` emptied `tools/list`.** `fleet()` opened the registry with
  the locale codec and caught only `OSError`, so a single undecodable byte raised
  `UnicodeDecodeError` out of the tool list: the client got `-32603` and zero tools, with no
  session left to report the reason on. The registry and `config.env` are read as UTF-8 with
  replacement now, so a damaged byte costs one character instead of the session. `wake_fleet`
  also takes the row shape `bin/wake` accepts (four or five fields), so it never names an alias
  the CLI calls unknown; it is never answered when it arrives as a notification; and a
  `WAKE_CONFIG` that exists but cannot be read says so instead of reporting a missing token line.

- **`WAKE_CONFIG_DIR` was the same silent downgrade one variable over (issue #76).** The
  guard added for `WAKE_CONFIG` read that variable alone, so naming a directory whose
  `config.env` was missing, unreadable or tokenless still exited 0 with the legacy two-tool
  helper and an empty stderr. Naming either variable is a request for the bridge now, and the
  message names the one that was set and the path it resolved to.

- **The API e2e failed on a machine that has a registry.** Its stdio step asserted
  `tools/list | length == 10`, but the bridge appends `wake_fleet` whenever `WAKE_REGISTRY`
  is readable, which is the operator's own box and never a bare CI runner: the step stopped
  with `rc=1` and no `PASS stdio bridge` line for a correct bridge. It now asserts the ten
  server tools by name and allows `wake_fleet` as the only extra, so ten and eleven both pass
  and a missing or unknown tool still fails.

- **A `WAKE_CONFIG` that cannot supply a token exited 0 with two tools (issue #76).** The bridge
  picked its mode from the token alone, and `wake_config.load()` treats an unreadable file as no
  settings, so a path that was moved or wiped produced a successful session with the legacy
  `wake_bot` / `wake_list` pair and nothing on stderr. A named `WAKE_CONFIG` — or a
  `WAKE_CONFIG_DIR` naming the `config.env` inside it — that does not exist, or exists
  without `WAKE_MCP_TOKEN`, now exits 2 with a message naming the path; the legacy mode
  is still one unset away, on purpose, when neither variable names a config.

- **`bin/wake-mcp` was refused by Cloudflare (403, error 1010).** The bridge builds its
  headers explicitly and sent urllib's default agent, `Python-urllib/*`, which the bot rule
  on the public host blocks before authentication: the same request with `curl` answered
  `401`, and with `curl -A 'Python-urllib/3.13'` it answered `403`. It sends
  `wake-mcp/<version>` now, the way the CLI sends `wake-cli/<version>`, so a stdio client
  that points at the public URL works instead of failing with what looks like an auth
  error. A test against the fake API fails if the agent goes back to the interpreter's
  ([#72](https://github.com/Anxiety471/wake/issues/72)). `docs/mcp.md` also records that
  `GET`/`DELETE /mcp` answer `401` before `405` when the request carries no token.
- **The threads e2e runs in CI, and the publish checklist cannot name a stale commit.**
  `scripts/e2e-threads.ts` had never run in CI, so a pull request could change the API's boot
  rule and still show every check green; the `api (bun)` job now gives it a database of its own
  and runs it after the build ([#63](https://github.com/Anxiety471/wake/issues/63)).
  `docs/PUBLISH-CHECKLIST.md` still said `v0.2.0` was at `2375c80`, the commit the tag pointed at
  before the history rewrite, and nothing read that line; `tests/test_publish_checklist.py`
  compares it with `git rev-parse refs/tags/v0.2.0`, so a moved tag or a stale id fails, which is
  why the test job now fetches tags ([#64](https://github.com/Anxiety471/wake/issues/64)).
- **Small review leftovers from 0.2.0.** The code of conduct says what to do when GitHub does not
  offer **Report to repository admins** (a first-time commenter may not see it): report abuse
  instead. `wake-api-e2e` checks its dependencies before it mints the registration secret, so a
  host without `openssl` is told the tool is missing instead of being told
  `WAKE_E2E_SECRET is 0 characters` about a variable it never set, and an `openssl` that prints
  nothing gets its own message. `scripts/e2e-threads.ts` drains both child pipes from spawn and
  prints the API's last twenty lines when a check fails, so a failed run shows the API's own
  error instead of only the assertion that noticed
  ([#65](https://github.com/Anxiety471/wake/issues/65)).
- **`env` emptied the API's environment file before it wrote the new one.** The step ran
  `install /dev/null /etc/wake/api.env`, then `tee` into it, so a write that failed or was
  interrupted left the service with an empty file and the previous one gone. It writes
  `api.env.new` at 0640 and renames over the live file, so the running service keeps a
  working file until the new one is complete.
- **`WAKE_DEPLOY_STATE` could make the tool chmod a directory it did not create.** The build
  step chmodded whatever the variable named, so `WAKE_DEPLOY_STATE=/tmp` - or any shared
  directory the operator owns but did not make - came back `0700` and stopped working for
  everyone else using it. (It runs as an operator and elevates per host command, so on a
  directory someone else owns the chmod fails instead of succeeding; either way the directory
  was not its to change.) The state directory is created `0700` when it is absent and refused,
  never chmodded, when it already exists and is not private.
- **`install --dry-run` exited 1 on a stale recorded build before printing anything.** A dry
  run installs nothing, so it now names the stale record, says a real run stops there, and
  prints the plan.
- **`wake-host-deploy` could install a bundle it never built.** `install` took the newest
  `/tmp/wake-build-*` by mtime, so a stale directory - or one planted by another local user - was
  installed as root. `build` now records its artifact under `WAKE_DEPLOY_STATE` (0700 directory,
  0600 pointer, and it must be a private directory of yours) and `install` reads only that record.
  The same step staged migrations in `/tmp/wake-*.sql` and copied them with a glob, so anything
  matching the glob became an installed migration while a migration the checkout no longer had
  stayed forever; they now travel as one base64 tar stream with exact names, a name that is not a
  migration name is refused, and a `.sql` the checkout no longer has is removed. `user` creates the
  group before `useradd --gid wake` (which failed on a clean host, at the second documented step),
  `install --dry-run` no longer exits on an empty `command -v node`, and `env` merges instead of
  overwriting: lines the operator added stay, and `WAKE_MCP_ENABLED` comes from the environment
  with a default of false, so a deploy does not republish a halted `/mcp`.
- **The shellcheck step listed its own targets.** `bin/ops/wake-wait` was not shellchecked the day
  it landed. CI and the README now filter `bin/ops/wake-*` by shebang, so a new bash helper is
  covered without an edit, and a test proves the filter picks exactly the bash helpers.
- **`wake-deploy` now renames each file into place.** A wake running from the prefix could exec a
  half-written script; it copies to `.new` and `os.replace`s it.
- **`wake-host-deploy env` writes only values the `db` step would accept, and `wake-deploy` leaves
  `~` to the remote shell.** `WAKE_DB_PASSWORD` is held to the one alphanumeric rule both steps
  share now, so a password `db` refuses can no longer be written into `api.env`, and every value
  the `env` step writes is refused a control character: `WAKE_MCP_ENABLED="true\nINJECTED_EVIL=1"`
  used to add that line to the file because only two named values were checked. Both are refused
  before any host command runs. Separately, `wake-deploy` expanded `~` in the prefix once for every
  target, so `ssh:DEST` with the default prefix installed into *this* machine's home path on the
  remote; a leading `~` on an ssh target is the remote user's `$HOME` now, a local install still
  expands it here, and a `~user` prefix is refused for an ssh target instead of being quoted into a
  relative path under `$HOME` that `verify_ssh` then blessed.
- **A bare `python3 -m unittest` run leaves `bin/` clean now.** `tests/test_wake.py` put
  `bin/` on `sys.path` and imported `wake_config`, so the interpreter cached it in
  `bin/__pycache__` even with the helper and gate loaders on `compile()` plus `exec` and
  every child held to `PYTHONDONTWRITEBYTECODE=1`; `tests/run.sh` sets that variable, which
  hid it. The module is loaded the same way those loaders load theirs, and a test runs a
  bare interpreter over the suite and fails if `bin/__pycache__` appears.

- **The reply-route guard compiles the pattern instead of looking for `{36}`.** A substring check
  passed `[0-9a-f]{36}` (no dashes, so it matches no thread id) and `.{36}` (any 36 characters),
  and the method and credential checks looked for `return 405` and `return 401` anywhere in the
  block, so `= POST` and `!= 0` passed as well and the order was never asserted. The guard now
  compiles §6.5's pattern and requires it to match a real uuid and refuse a 35-character path and a
  non-hex one, pins the two condition lines, and requires the 405 before the 401 so a
  non-POST is refused as a method rather than answered with a 401 that invites a retry
  ([#118](https://github.com/Anxiety471/wake/issues/118)).
- **The reply-route guard refuses a path that runs on past the id, and checks itself.** Two
  classes that passed every earlier probe, `[0-9a-fA-F-]{36}.*` and `([0-9a-fA-F-]{36})?`, also
  accept `/threads/<uuid>x/reply` and `/threads//reply`, so the guard probes both. The width
  probe and the `not a valid regex` branch had no test of their own: deleting either left the
  doc suite green, and a runbook whose class does not compile reported no problems at all
  ([#117](https://github.com/Anxiety471/wake/issues/117)).
- **A value read from the host is stdout only, and a refused install leaves nothing behind.**
  `host()` returns stdout and stderr together, which is right for a block a human reads and
  wrong for a value: a warning from a wrapper around the runner became the answer, so
  `getent passwd` reported an account that did not exist and the step skipped `useradd`,
  `command -v node` handed `install` a path with the warning spliced into it, and the existing
  `api.env` was read with the warning inside it and written back that way. `host_out()` is the
  value read, and every one of those reads uses it. In the same file, `put()` decided between
  `scp` and the runner from `WAKE_HOST_SSH` while every command around it preferred
  `WAKE_HOSTOPS`, so with both set the bytes took a different route than the commands: one
  `transport_kind()` answers for both. The staged copy is removed even when the install that
  follows it fails, which the single `&&` chain could not do
  ([#117](https://github.com/Anxiety471/wake/issues/117)).

### Security

- **`wake-pingall` keeps every alias a bot name before it becomes a path or an argument.**
  `wake-pingall "a b"` asked two bots called `a` and `b`, and `--` asked a bot called `--`
  and wrote `--.log`, because `LIST=$*` re-split the names on whitespace; `""` and `"   "`
  exited 2 with `no alias left to ping` and asked nobody. The aliases are kept as given; a
  blank one and one that starts with a hyphen are named and skipped like any other bad
  alias, with exit 2, and the refusal shipped in #36 (`../escape`, `.hidden`, `a/b`, `/abs`,
  from arguments and from the registry) now has tests.

- **The inbox register no longer asks for a world-writable file.** `wake-inbox-io register`
  ran `chmod 666` on the inbox and its active marker right after creating them, and on an
  ungated host the registering process owns both files, so the mode took: a session opened with
  `wake` on the host left an inbox that any user who could reach the directory could append to.
  The tail now asks for `660` — owner plus group `wake`, which is every writer the 2770 parent
  directories admit — and a test asserts the mode the files actually get, not only the string.
  The host gate matches the register command as an exact literal including this tail, so the
  gate is widened first: a gate with both routes accepts either client while the fleet's copies
  are refreshed, and the previous one refuses the new tail
  ([#89](https://github.com/Anxiety471/wake/issues/89)).

## [0.2.0] - 2026-11-15

### Changed

- **The tree names examples, not anyone's infrastructure.** The example config, the README
  table, the deploy runbook and the MCP verify default no longer carry a real state
  directory, webhook domain or nginx file name; the runbook's rollback and verify steps
  are otherwise unchanged. `tests/test_public_defaults.py` allowlists the hosts a public
  repository may name (loopback, the example domains, the repository itself), so a real
  hostname fails the suite instead of shipping. Every tracked file is scanned for a host
  in a URL and for a bare hostname alike (bare names used to be checked only under
  `deploy/`, and a `user:token@` prefix was read as the host); a sentence naming a real
  host now fails like a default does.

### Security

- **Site names, an owner's domain and a personal path are out of the tree.** They were
  documentation and defaults. One code default moved with them: `deploy/ops/mcp-verify.py`
  names `example.com` when no URL is passed, and an explicit URL argument behaves as
  before.

### Fixed

- **`wake-wait` refuses `--from` with no bot name.** `--from=`, `--from ""` and a blank
  `--from "   "` used to run the "any new reply" rule without saying so, and a three-space alias
  waited the whole timeout for a bot that cannot exist. Both flag forms now exit 1 with
  `--from needs a bot alias` before the thread is read; an empty `WAKE_WAIT_FROM` stays the
  ordinary unset case.

### Added

- **`tests/test_gate_shapes.py` no longer carries one machine's staging directory.**
  The directory is `WAKE_GATE_STAGE` now (default: unset, so the allow-list tests skip),
  `WAKE_GATE` still names one artifact, and a test pins the empty case. Where a gate is
  staged is a property of a machine, never of the repository.
- **`--help` on every command, including the installer.** Seven commands treated the flag as
  data: `wake-api-e2e --help` ran the whole end-to-end suite, `wake-pingall --help` asked a bot
  named `--help` a question, `wake-mcp --help` started the stdio server, `wake-keys --help` ran
  the key sync, `wake-hook --help` read stdin as a hook payload, and `wake-inbox-io` / `wake-pg`
  printed usage on stderr with exit 1 and 2. `./install.sh --help` was worse: it installed. Each
  now answers `-h`/`--help` with its usage, exit codes and environment on stdout, before anything
  else runs, and refuses an argument it does not have instead of ignoring it (`install.sh`,
  `wake-keys --forc`, `wake-api-e2e --anything`). `tests/test_command_help.py` walks `bin/` and
  `bin/ops` and fails on a command that does not answer help, one with no `# desc:` line, and one
  no README names; with only that file on the bytes before this change, 14 checks fail.
  `wake-pingall` also refuses an alias that is not letters, digits, hyphen or underscore, so
  `<log dir>/<alias>.log` cannot be written outside the log directory.
- **Three agent skills under `skills/`.** `wake-ops` covers the operator helpers (a scratch
  database, the HTTP end-to-end run, keys, a fleet ping, the two installers and a gate
  review); `wake-mcp` covers the stdio bridge, the public `/mcp` endpoint and its single
  identity, including what a 401, a 404 and a 405 mean there; `wake-deploy` covers the
  order of operations on a host, the one nginx location, the check from outside and an
  exact rollback. Each is a `SKILL.md` with `name` and `description` frontmatter.
- **Operator helpers in `bin/ops`.** `wake-pg` runs a throwaway local Postgres for the API tests;
  `wake-api-e2e` starts the real API on a scratch database and proves the public surface over HTTP
  (`/mcp` initialize, ten tools, `wake_ask` and `wake_read_thread`, the inbox, deliveries,
  private-bot refusal, `mcp_audit`, audit prune, `GET /mcp` 405, the stdio bridge) with a
  `REGISTRATION_SECRET` generated per run, so no secret sits in the file; `wake-keys` copies
  host-minted webhook keys into the local key dir and never prints one; `wake-pingall` asks every
  open bot in the registry at once. `OPS=1 ./install.sh` installs them beside the core scripts,
  `bin/ops/README.md` says what each one needs, and `tests/test_ops_scripts.py` keeps personal
  absolute paths, real addresses and undocumented `WAKE_*` variables out of them.
- **`wake-wait`.** A fifth operator helper: it blocks until an existing thread has its answer and
  prints the reply line(s) exactly as `wake --thread <id>` shows them, so a script that asked a bot
  earlier can wait for the answer instead of re-asking (a second ask is a second question) or
  polling by hand. `wake-wait <thread-id> [--timeout SECONDS] [--interval N] [--from BOT]`; exit `0`
  answered, `2` no answer within the timeout, `1` error (bad usage or id, no `wake`, unreadable
  store, no such thread). Without `--from` only a reply that arrives after the wait starts counts;
  `--from BOT` also accepts that bot's earlier answer. `WAKE_WAIT_TIMEOUT` (180 s),
  `WAKE_WAIT_INTERVAL` (3 s), `WAKE_WAIT_FROM` and `WAKE_WAIT_QUIET` tune it.
- **A deploy runbook, a systemd unit and two operator scripts.** `deploy/README.md` runs the
  API on the host: a `wake` service account, `/opt/wake` (bundle, `node`, migrations beside it),
  `/etc/wake/api.env` as `root:wake 640` and an `mcp-token` nobody else can read, a unit that
  restarts and stops cleanly, and `POST /mcp` published by one nginx `location = /mcp` — an exact
  match, so the loopback-only operator routes (`/mcp/identity`, `/mcp/identity/rotate`,
  `DELETE /mcp/identity`, `/mcp/audit/prune`) stay off the internet. `deploy/ops/mcp-identity.py`
  provisions, rotates, revokes or shows the single public identity and never prints the token
  (0600, written beside its target and renamed into place); `deploy/ops/mcp-verify.py` drives
  initialize, tools/list and one tools/call over HTTPS, refusing redirects so the bearer cannot be
  re-sent to another host, and prints the status before parsing a body. Both are pinned by
  `tests/test_deploy_ops.py` against a stub that answers Fastify's rules, and
  `tests/test_deploy_docs.py` keeps the published snippet an exact match with a bounded body.
- **The MCP endpoint (Switchboard merge step 3).** `POST /mcp` speaks MCP 2025-06-18 over
  Streamable HTTP, off unless `WAKE_MCP_ENABLED=true`. Ten tools are thin wrappers over the API
  (`wake_ask`, `wake_reply`, `wake_read_thread`, `wake_thread_read`, `wake_threads`, `wake_inbox`,
  `wake_inbox_read`, `wake_bots`, `wake_send`, `wake_search`): each dispatches through
  `app.inject` with the caller's own bearer, so auth, schemas and thread rules stay the routes'
  own. Stateless (no session id), JSON answers with a one-event SSE fallback, `GET`/`DELETE` 405,
  batching refused, protocol version checked. The whole public surface has one credential: the
  `mcp` agent's ordinary agent token, provisioned/rotated/revoked with `POST /mcp/identity`,
  `POST /mcp/identity/rotate` and `DELETE /mcp/identity` behind `x-registration-secret`; a bot
  token is refused with 401. Calls are rate limited per token
  (`WAKE_MCP_RATE_LIMIT_PER_MINUTE`, default 60, in-process) and audited in `mcp_audit` (method,
  tool, ok, error, status, duration, remote; no bodies or tokens). Contract:
  [docs/mcp.md](docs/mcp.md).
- **The stdio bridge (step 4).** `bin/wake-mcp` with `WAKE_MCP_TOKEN` set now bridges stdin/stdout
  to `POST /mcp`: each line is forwarded with that token (plus `MCP-Protocol-Version` once
  `initialize` has answered), and JSON or single-event SSE replies come back as one line each.
  Notifications are forwarded and never answered; a batch, an unreachable API or a body that is not
  JSON-RPC becomes a JSON-RPC error on the asking id. The tool table, schemas, rate limit, audit and
  private-bot wall stay the server's — the bridge implements none of them. A `3xx` is refused
  instead of followed (urllib would re-send the bearer token to `Location`), a `429` is answered
  on the asking id instead of `null`, a reply cut short answers `-32603` and leaves the session
  running, and a server-initiated notification is forwarded verbatim. The API's own REST errors
  (`{"error":"unauthorized"}`, `{"error":"not found"}`), an empty `200`/`202` to a request and a
  JSON-RPC-looking body delivered with a `3xx` are reported as `-32603`, never relayed as a reply
  a client would drop or wait on forever. `WAKE_MCP_API`
  (default `http://127.0.0.1:3001`) names the server — `https` for any non-loopback host, since
  the token is a bearer credential — and `config.env` should be mode 600 (the bridge warns).
  Without a token the binary stays the legacy two-tool helper (`wake_bot`, `wake_list`).
  Contract: [docs/mcp.md](docs/mcp.md).
- **Private bots.** `agents.is_private` plus `PUT /agents/:name/private`: a private bot is hidden
  from and unreachable by the public MCP identity (404 for `ask`/`send`, absent from `wake_bots`,
  skipped by a broadcast) while bot-to-bot traffic is unchanged. `GET /agents` reports the flag.
- **Delivery outcomes.** `wake_deliveries` records one row per rung recipient as each delivery
  finishes, and `GET /messages/:id/deliveries` lets the sender read them back (`delivered`,
  `reason`, `attempts`, last HTTP `status`, time). Sender only; anyone else gets the same 404 as a
  missing id.
- **`mcp_audit` retention.** `POST /mcp/audit/prune` (operator secret) deletes
  rows older than a window — 90 days by default, 1 to 3650 via `?days=`, `400` otherwise — and
  answers `{deleted, before}`. A bare `POST` needs no payload, so a host timer can run it daily.
- **A report that admits it does not know.** If the recipients cannot be read at all, the `wake`
  report now says `error: "recipients_unreadable"` instead of the `deliveries: []` that means "no
  recipient had a target".

### Changed

- **Doorbell hardening.** The timeout ceiling drops to 9 s, so two attempts plus backoff (18.2 s)
  stay under the CLI's 20 s send timeout. Responses are read to 16 KiB and redirects are not
  followed (a 3xx is `rejected`, never retried). A per sender and recipient hourly cap
  (`WAKE_DOORBELL_MAX_PER_HOUR`, default 30, the CLI's `HOURLY_CAP` number, 0 disables) stops
  one sender ringing one bot at API speed; over the cap makes no request and reports
  `rate_limited`. The scope is deliberately narrower than the CLI's, whose single bucket per
  target bot is shared by every sender on the machine.
  An empty target map is checked before the URL/timeout are parsed, so a stale bad value cannot
  break the boot of a doorbell that is off. `WAKE_DOORBELL_TIMEOUT_MS` now accepts 100..9000.
- **A reply wake carries `ref: ""`.** It cannot be answered, so it must not look answerable; a
  receiver that routes on `ref` no longer fires `wake --reply` into a guaranteed 409. `threadId`
  still names the thread.
- **Key issuance wording corrected.** Keys are minted by the host's `rakazo-webhook-key`, through
  the `bot-webhook-key` wrapper a bot container can run, not by a human rotating them in Rakazo.

- **The doorbell (Switchboard merge step 2).** A committed message rings the recipient bot's
  Rakazo webhook (`POST /api/v1/bots/<botId>/webhook`) from the API server, never the CLI, so the
  per-bot webhook secret stays server-side. `POST /messages`, `POST /threads` and
  `POST /threads/:id/reply` now answer with `wake` (`enabled`, plus one `deliveries` entry per
  recipient that has a target: `agent`, `delivered`, `reason`, `attempts`). Rakazo's
  `200 {"ok":true,"ignored":true}` — the bot has no active `webhookEnabled` routine, so nobody
  wakes — is **undelivered** (`no_routine`), not success, and is not retried; only `5xx`, network
  errors and timeouts are retried, with the same bytes and the same `Idempotency-Key` (the message
  row id) every time, so a retry cannot run a bot twice. The payload keeps the CLI's field names,
  `ref: thread:<id>` and `depth: 0` (the depth the CLI sends for a normal bot, so a receiver that
  exports it can still wake), and `detail` carries the reply hint for a `request` and
  `reply on thread <id>: <text>` for a `reply`, which is what the CLI sends the asker: the asker
  cannot answer its own ask. A failed delivery never fails the send: the message is already
  committed, and `POST /threads` releases its pool connection before it rings, so a full pool
  cannot turn an ask into a silent drop. `WAKE_DOORBELL_BOTS` (JSON, agent → `{botId, secret}`),
  `WAKE_DOORBELL_URL` and `WAKE_DOORBELL_TIMEOUT_MS`; an empty map leaves the doorbell off. 33 new
  tests (24 in `doorbell.test.ts`, 9 in `doorbell-routes.test.ts`), including a stub doorbell that asserts the ignored, 401 and 413 paths do not retry and a
  one-connection pool that asserts the ask still rings. Contract:
  [docs/doorbell.md](docs/doorbell.md).
- **API (`apps/api`, Switchboard merge step 1).** The Fastify + Postgres service, moved in from
  Switchboard with its tests and run on Bun. Threads: `POST /threads`, `GET /threads`,
  `GET /threads/:id`, `POST /threads/:id/reply`, `POST /threads/:id/read`, with migration
  `009_threads.sql` (`threads`, `thread_participants`, `messages.thread_id` / `messages.thread_seq`).
  One answer per ask: the asker never posts to its own ask and the recipient answers once (both
  `409`), `WAKE_TTL_HOURS` (`0` or whole hours `1..720`, default 24) refuses late answers with `410`,
  only participants can read or answer (`404` otherwise), and every thread message also lands in the
  recipient's inbox carrying its `threadId`. Inbox and search payloads gain that `threadId`. This is
  stricter than the CLI in two places, which README states: the CLI still allows a clarification from
  the asker on its own open ask and a second reply from the recipient, and `--force` has no route.
  `GET /threads` pages by creation, not by the mutable `updated_at`, so a thread answered between
  pages cannot jump above the cursor; `POST /inbox/read` also moves the thread read position, so the
  inbox and the thread agree on `unread`. 86 tests against a real Postgres; new CI job `api (bun)`
  with a `postgres:17` service.
- `wake --update` reports every bot's key state in one table (and mints missing keys when a key helper is configured), `--update --ping` sends a quiet ping to each open bot that can be woken, and `--all "<message>"` sends one message to every open bot, skipping private bots and the sender itself.
- `own_bot()` falls back to `<config>/self` (an alias or bot id), so a harness that runs from a neutral working directory is identified as its bot instead of `bot:container`.
- A thread read that fails because the host bridge is unreachable now says so, instead of reporting the thread as never opened.
- `tests/test_gate_shapes.py` checks every command wake builds against the allow-list of a staged `rakazo-wake-gate`, including `wake-inbox-io`'s `register`, `beat` and `read`, so the register route's literal-matched `chmod` tail cannot be dropped silently; a companion test keeps that shape even on a machine with no gate staged.
- `WAKE_TTL_HOURS` (default `24`): a thread older than this refuses answers, and says the age, because the asker has moved on and the answer would land unread. `0` disables it.
- One round per thread: once a thread has an answer, the asker cannot answer back on the same thread, so A to B to A cannot ping-pong. Ask again with `--ask` if there is more to say. `--force` overrides it.
- An audit line per ask, reply and refusal in `$WAKE_RATE_DIR/wake-audit-<uid>.log`, so a refused or failed answer is visible instead of looking like a delivered one.

### Fixed

- **The doorbell's hourly cap keyed on the recipient alone**, so one sender's 30 wakes made another
  sender's first wake to the same bot `rate_limited`: message stored, no ring, and the API has no
  `--force`. The key is now `sender:recipient`: the CLI's number, but a deliberate divergence from
  its scope, because the CLI's bucket is one per target bot shared by every sender on the machine
  (all bots run as one uid, `wake-rate-<uid>.json`). A test with the cap at 1 fails on the old bytes
  and passes here.
- **`POST /mcp/audit/prune` ignored a body, and its window was asserted only on the reported
  cutoff.** A caller that sent `{"days":30}` pruned at the 90-day default — the more destructive
  window — with no complaint; any body is now `400 body not accepted`, while the bare `POST` a host
  timer runs is unchanged. A new test ages three rows for real (200, 40 and 0 days) and proves
  `?days=3650` deletes nothing, the 90-day default deletes the 200-day row, and `?days=30` deletes
  what the default spared.
- **The MCP 429 body was `{"error":"rate_limited"}`, not JSON-RPC.** It now carries a JSON-RPC error
  object with code `-32000`, so a client that parses every body as JSON-RPC never meets a bare
  error, while the status stays `429` with `Retry-After: 60`.
- **`wake_inbox_read` accepted more ids than the 100 it advertises**, forwarding the list to
  `POST /inbox/read` and returning that route's `400`. It now refuses over 100 with `-32602` and
  makes no call.
- **`PUT /agents/mcp/private` was allowed**, which could hide the public identity behind its own
  wall. It answers `409` now; an ordinary bot is unaffected.
- A reply that lost the race raised a `23502` (`last_read_seq` was set from an empty `inserted`
  CTE), so `POST /threads/:id/reply` answered `500` where it documents `409`. The read position is
  now kept when nothing was inserted, and a row-locked test proves one `201` and one `409` — never a
  `500`.
- A read receipt naming two messages of the same thread left the thread read position on whichever
  row Postgres happened to match, so `GET /threads` still reported `unread: 1`. The position now
  moves to the newest message the receipt covers, and only the threads the receipt names are
  updated. A test that asks, replies, replies, then reads both messages in one receipt fails on every
  run before the fix and passes after.
- `010_drop_threads_updated_at_idx.sql` drops the index `009` created before paging moved to
  `created_at`, for databases that ran that earlier `009`. Fresh databases never create it.
- The history secret scan masks `sha512` integrity hashes instead of dropping the whole line, so a
  registry token sitting on the same line as an integrity hash in a lockfile is still found. Proven:
  a repo whose history holds an `sk-…` token on such a `bun.lock` line is clean under the dropping
  filter and fails under this one.

### Changed

- The TypeScript workspace uses `bun install` / `bun test` (Switchboard's pnpm + vitest setup is
  gone) and TypeScript 6 with `@types/bun`. `bun run lint` still runs eslint 10 on node, because
  eslint 10 uses `util.styleText`, which Bun does not implement.
- `--sync` from a guest (bot computer) no longer runs `sudo tee` on the host registry: it prints the rows it would have written and exits 1 with `registry sync is an operator action; run it on the host`. `--add` still appends the row locally, and `--update` reports key state but exits 1 for the same reason. A guest ingests untrusted content, so the routing table every bot trusts stays an operator write.
- `thread_append` no longer re-applies `1777`/`666` to the store. The store is provisioned once on the host — `threads/` `root:wake 3770` (setgid + sticky), `inbox/`
  `2770`, files `660` — and wake only appends, so a tightened store stays tightened. README and `docs/protocol.md` state the contract.
- `inbox_live()` reads a marker with `cat <path> 2>/dev/null`, the shape the gate allow-list matches, so a live-session delivery is no longer silently skipped.
- CI runs the test matrix on `ubuntu-24.04`, because `ubuntu-latest` (Ubuntu 26) no longer has a Python 3.9 build to install.
- `scripts/scrub-check.sh` keeps the strong secret patterns (keys, bearer tokens, private keys) on every file and commit, but the long-blob heuristic now skips URLs and the scan no longer fails on a dependency bot's own `Signed-off-by` trailer, so a documentation link or a dependabot merge cannot fail the build.

## [0.1.0] - 2026-11-14

First public release.

### Added

- `wake` CLI: one-way wakes, `--ask` / `--reply` threads, `--inbox`, `--thread`, `--setup` key creation, registry `--add` / `--sync`, `--list` / `--who` / `--info` / `--me`, `--version`.
- Helpers: `wake-run`, `wake-hook` (Claude Code and Codex hooks), `wake-mcp` (MCP stdio server), `wake-inbox-io` (session inbox plumbing).
- Configuration through environment variables or `config.env`; host mode and guest mode (`WAKE_HOSTOPS`).
- Loop guard (depth limit and 30 wakes per bot per hour) and `Idempotency-Key` on every request.
- Offline test suite with a fake webhook server; CI on Python 3.9 to 3.13 with ruff, shellcheck and a secret scan.

### Security

- `wake-inbox-io` accepts only hex inbox ids (`[0-9a-f]{6,32}`) and inbox paths directly inside `$WAKE_STATE_DIR/inbox`, and quotes every value it puts into a shell command.
- `WAKE_REGISTRY_GROUP` is empty by default, so `wake --sync` no longer changes the registry's group unless configured.
