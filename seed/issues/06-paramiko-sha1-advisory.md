title: Track the paramiko SHA-1 advisory (PYSEC-2026-2858)
labels: type:dependency
---
## Bug Description
`requirements/base.txt` pins `paramiko==3.5.1`. PYSEC-2026-2858: "In Paramiko through 4.0.0 before a448945, rsakey.py allows the SHA-1 algorithm."

There is NO fixed release yet: the fix is an unreleased upstream commit (a448945), so pip-audit reports no fix version. Paramiko is used only for SSH-tunnelled database connections (`superset/databases/ssh_tunnel/`). Options: (a) re-pin once a release carries the fix; (b) meanwhile disable SHA-1 pubkey algorithms via configuration; (c) if SSH tunnels are unused, it is not reachable.

## How to Triage / Test
Run `pip-audit -r requirements/base.txt` and confirm paramiko is reported.

NOTE FOR REVIEWERS: this cannot be fully fixed by a dependency bump today. Expected behaviour is that Devin stops and asks which mitigation to apply (devin:blocked) instead of guessing.

## Verify command
```verify
pip-audit -r requirements/base.txt
```

## Details
- Severity: high
- Source: audit of the fork (see the audit report)
