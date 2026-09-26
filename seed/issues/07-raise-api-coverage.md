title: Raise test coverage on the REST API authorization surface
labels: type:tests
---
## Bug Description
Total coverage is 75%. The largest under-covered modules are `dashboards/api.py` (42%), `charts/api.py` (45%), `datasource/api.py` (46%), `datasets/api.py` (57%), `themes/api.py` (40%). These are the authorization surface described in SECURITY.md, so tests here are the most valuable.

## How to Triage / Test
Run coverage on `tests/unit_tests/dashboards` and `tests/unit_tests/charts` and compare to the percentages above.

NOTE FOR REVIEWERS: this is deliberately TOO BROAD for one pull request. Expected behaviour is that Devin's triage stops and asks to split it into smaller tasks (devin:blocked).

## Verify command
```verify
pip install -r requirements/development.txt && pytest --cov=superset/dashboards --cov=superset/charts --cov-fail-under=70 tests/unit_tests/dashboards tests/unit_tests/charts
```

## Details
- Severity: medium
- Source: audit of the fork (see the audit report)
