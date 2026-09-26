What the sweep looks for (run from a clean checkout of the default branch)

Dependencies (highest value: each finding names its own fix)
- Frontend: in `superset-frontend/`, run `npm audit`. Report critical and high advisories first. The usual fix is an entry in the `overrides` section of `package.json`. Verify with `cd superset-frontend && npm audit --audit-level=<severity>`.
- Backend: run `pip-audit -r requirements/base.txt`. If an advisory has no fixed release yet, still report it, but say so in the description: a human will decide on a mitigation.

Code quality (only if a check is cheap to verify)
- `mypy --check-untyped-defs superset/utils` (Python 3.11). Report errors grouped by file, one finding per file.
- `ruff check` with the repository's own configuration. If it is clean, do not invent findings.
- Frontend type check: `npm run plugins:build` then `npm run type` in `superset-frontend/`.

Test coverage
- Modules with no direct unit tests and clear, isolated behavior (small utilities) make good "add tests" findings. Do not propose broad "raise coverage" tasks: they are too large for one pull request.

How to write a finding
- One problem per finding, small enough for a single pull request.
- verify_command must exit 0 only once the problem is fixed, and must be runnable on a clean checkout.
- Never report a problem you did not observe yourself.
- Skip a finding if a merged pull request or an existing issue already covers it.
