title: Override underscore to ^1.13.8 (GHSA-cf4h-3jhx-xvhq)
labels: type:dependency
---
## Bug Description
underscore@1.6.0 (installed at node_modules/nomnom/underscore via po2json@0.4.5 -> nomnom@1.8.1) has an arbitrary code execution advisory, CVSS 9.8 (GHSA-cf4h-3jhx-xvhq, affected >=1.3.2 <1.12.1). npm also flags nomnom and po2json as critical: same single root cause.

Exposure is dev-only (scripts/po2json.sh, `npm run build-translation`), never bundled, but it is a critical finding in the lockfile.

## How to Triage / Test
Run `npm audit` in `superset-frontend/` and look for critical advisories that mention underscore, nomnom, or po2json.

NOTE FOR REVIEWERS: an earlier pull request in this fork already applied this override, so this issue is expected to be a FALSE POSITIVE. It is here to demonstrate that triage stops and asks instead of writing an unnecessary change.

## Verify command
```verify
cd superset-frontend && npm audit --audit-level=critical
```

## Details
- Severity: critical
- Source: audit of the fork (see the audit report)
