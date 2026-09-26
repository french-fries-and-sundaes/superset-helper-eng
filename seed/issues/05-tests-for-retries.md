title: Add unit tests for superset/utils/retries.py (retry_call)
labels: type:tests
---
## Bug Description
`retry_call` in `superset/utils/retries.py` wraps the `backoff` library and is used by the alert/report code, but has no direct unit tests.

Add `tests/unit_tests/utils/retries_test.py` covering: succeeds on first call; retries a failing call until it succeeds; gives up after max tries and re-raises; only retries the configured exception type; passes `fargs`/`fkwargs` through. Keep the tests fast (no real sleeping).

## How to Triage / Test
Search `tests/` for `retry_call`: there should be no direct tests today.

## Verify command
```verify
pip install -r requirements/development.txt && pytest tests/unit_tests/utils/retries_test.py
```

## Details
- Severity: low
- Source: audit of the fork (see the audit report)
