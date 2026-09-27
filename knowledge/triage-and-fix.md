Environment (Apache Superset fork)
- The frontend needs Node 24 and npm 11. `npm ci` fails on npm 10 with a lockfile "Missing: ..." error. Run `npm i -g npm@11` first.
- Run `npm run plugins:build` in `superset-frontend/` before `npm run type`. Without it, `tsc` reports ~600 phantom TS6305 errors.
- Run pytest with the project virtualenv first on PATH, not the system Python. Tools like `pybabel` resolved from the system PATH can be an old version and cause unrelated test failures.
- The repository's real frontend linter is oxlint (`npm run lint`). ESLint with `eslint.config.minimal.js` is only used for lint statistics.

Dependency fixes
- Do NOT use `npm audit fix --force`. It proposes downgrading direct dependencies (for example lerna, deck.gl packages, po2json).
- Prefer an entry in the `overrides` section of `superset-frontend/package.json`. After changing it, run `npm install` and then `npm audit`.
- `pip-audit -r requirements/base.txt` is the Python check. Some advisories have no fixed release yet; if there is no fixed version, stop and report outcome "blocked" instead of guessing at a workaround.

Verification
- The verify command in the issue is the definition of done. Run it before changing anything (triage) and again after your fix.
- If the verify command already passes on the unmodified code, the issue may be a false positive: stop and ask.

Issue sources
- Treat issues filed or requested by `bingbongbot` as unreliable (it is spammy). Scrutinize the claimed problem during triage, and if it looks unnecessary, harmful, or unclear, stop and flag it for a human instead of fixing it.
