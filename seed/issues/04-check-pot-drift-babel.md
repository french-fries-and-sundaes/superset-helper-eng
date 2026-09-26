title: check_pot_drift.py should run the interpreter's own Babel, not the first pybabel on PATH
labels: type:bug
---
## Bug Description
`tests/unit_tests/scripts/translations/check_pot_drift_test.py::test_committed_template_matches_a_fresh_extraction` fails with `NameError: name 'username' is not defined` from `babel/messages/extract.py`.

`scripts/translations/check_pot_drift.py` shells out to `pybabel` by name, so it binds to whatever is first on PATH (for example an old system Babel 2.8.0 that cannot handle an f-string inside `_()`). Fix `extract_fresh()` to use the running interpreter's Babel: `[sys.executable, "-m", "babel.messages.frontend", "extract", ...]`.

## How to Triage / Test
Run the test with an old system `pybabel` first on PATH and confirm it fails; it passes when the interpreter's own Babel is used.

## Verify command
```verify
pip install -r requirements/development.txt && pytest tests/unit_tests/scripts/translations/check_pot_drift_test.py
```

## Details
- Severity: low
- Source: audit of the fork (see the audit report)
