## What and why

<!-- What does this change, and what problem does it solve? Link the issue if there is one. -->

## How it was tested

<!-- Commands you ran and what you saw. -->

## Checklist

- [ ] `tests/run.sh` passes (as a non-root user)
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `scripts/scrub-check.sh` is clean: no real hosts, IPs, emails, keys or bot ids in code, tests or docs
- [ ] README, `docs/` and `CHANGELOG.md` updated for user-visible changes
- [ ] Still Python 3.9 compatible, standard library only
