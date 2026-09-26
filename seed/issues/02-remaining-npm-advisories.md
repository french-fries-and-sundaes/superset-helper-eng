title: Clear the remaining npm advisories via overrides (js-yaml, pacote, smol-toml, image-size, fflate)
labels: type:dependency
---
## Bug Description
`npm audit` in superset-frontend still reports advisories that a package.json `overrides` entry can clear:

- js-yaml 4.3.1 (GHSA-2883-xcg3-v3hh): raise the three existing overrides that pin ^4.3.1 to ^4.3.2 (leave the ^3.15.1 overrides alone: different major, unaffected).
- pacote 21.0.1 via lerna (GHSA-w4pp-8pjf-rmxw): override to ^21.5.1.
- smol-toml 1.6.1 via nx (GHSA-7w5x-hrqm-74c2): override to ^1.7.2.
- image-size <=2.0.2 via texture-compressor (GHSA-w3rx-r6r6-pgpr): override to ^2.0.4.
- fflate 0.7.4 under @loaders.gl/compression (GHSA-px8p-9vwx-vf98): override to ^0.8.3.

Do NOT use `npm audit fix --force`: it proposes downgrading direct dependencies (lerna, deck.gl).

## How to Triage / Test
Run `npm audit` in `superset-frontend/` and confirm the advisories above are listed. After the change, run `npm install` and `npm audit` again; no advisories should remain at moderate or above.

## Verify command
```verify
cd superset-frontend && npm audit --audit-level=moderate
```

## Details
- Severity: high
- Source: audit of the fork (see the audit report)
