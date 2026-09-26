# superset-helper-eng

An event-driven automation that uses **Devin as its worker** to fix engineering issues in a fork of
[Apache Superset](https://github.com/apache/superset) (`french-fries-and-sundaes/superset`).

A human labels a GitHub issue `devin:ready` → a webhook fires → this service starts **one Devin session
per issue** through the Devin API → Devin triages, fixes, and opens a pull request → **a human reviews and
merges**. Nothing ever merges automatically.

> **Status:** milestones M0 to M2 are done (Devin client, state database and worker with a daily session
> guard, signed webhook with idempotency). M3 to M9 (GitHub label sync, dashboard, triage handling, GitHub
> Actions verification, scan, learning loop, packaging) are next. See [Roadmap](#roadmap).

## How it works

```
GitHub issue labeled devin:ready ──webhook──▶ this service ──Devin API──▶ Devin session
                                               │  SQLite state              │ 1. triage first
                                               │  daily session guard       │ 2. fix, verify
                                               ▼                            │ 3. open PR ("Closes #N")
                                        /api/status, /api/tasks ◀───polls───┘
```

| Label | Meaning | Human needed? | Exit |
|---|---|---|---|
| `devin:proposed` | Scanner filed it; awaiting approval | yes | apply `devin:ready` |
| `devin:ready` | Approved; backlog until a session exists | no | session starts (triage first) |
| `devin:in-progress` | Devin is working | no | to in-review / blocked / failed |
| `devin:blocked` | Devin needs a fact or decision (unclear, false positive, stuck) | yes | comment or edit the issue, re-apply `devin:ready` |
| `devin:in-review` | PR open, waiting for review | yes | merge, request changes, or close |
| `devin:failed` | Devin tried and could not finish | yes | comment, re-apply `devin:ready` |
| `devin:rejected` | A human said no (PR closed unmerged, or issue closed "not planned") | no | terminal |

`devin:ready` is the one human "go" signal for proposed, blocked, and failed tasks. Only repository
collaborators can apply labels, so it is also an **access control**: strangers can file issues on a public
repo, but only someone you trust can send one to Devin.

### Design decisions worth knowing
- **Explicit outcome from Devin.** Against the real API, `status: running` + `status_detail: waiting_for_user`
  appears both while Devin waits for an answer *and* after it finished and went idle. So each session must
  report an `outcome` (`done` / `blocked` / `failed`) in structured output, and the bot reads that
  (`app/outcome.py`). A session that ends `suspended` for inactivity is a normal end, not an error.
- **Compare-and-set state changes.** The webhook thread and the worker loop can run concurrently; a task is
  claimed (`ready → in-progress`) before the Devin call, so two triggers never start two sessions.
- **Idempotent webhooks.** Duplicate deliveries are dropped by delivery id, and an issue created with the
  label (which can emit both `opened` and `labeled`) still yields one task.
- **Spend guard.** `MAX_SESSIONS_PER_DAY` (default 20, rolling 24h). The API's `acus_consumed` did not report
  usage in our org, so the bot does not rely on it. Also tune Devin's own *Message usage limit* setting and,
  optionally, `MAX_ACU_PER_SESSION`.
- **Rules as code.** `knowledge/*.md` is injected into every prompt, so guidance is version-controlled and
  reviewed like any other change.

## Quick start (no Devin key, no GitHub, no cost)

```bash
cp .env.example .env            # DEVIN_MODE=fake is the default
docker compose up --build       # service on http://localhost:8000
```

In a second terminal, simulate GitHub events (signed exactly like GitHub signs them):

```bash
export GITHUB_WEBHOOK_SECRET=change-me      # same value as in .env

python scripts/simulate_webhook.py --issue 1 --title "Override underscore"            # -> in-review
python scripts/simulate_webhook.py --issue 2 --title "[sim:blocked] Paramiko SHA-1"   # -> blocked (asks a question)
python scripts/simulate_webhook.py --issue 3 --title "[sim:fail] Coverage raise"      # -> failed
python scripts/simulate_webhook.py --issue 4 --title "Scanner finding" --label devin:proposed   # -> proposed
python scripts/simulate_webhook.py --issue 1 --title "Override underscore" --twice    # idempotency: 2nd is a duplicate
python scripts/simulate_webhook.py --issue 1 --title x --bad-signature                # 401

curl -s localhost:8000/api/status | python -m json.tool     # counts + "needs a human" list
curl -s "localhost:8000/api/tasks?state=in-review"
```

To answer a blocked task, edit the issue text and re-apply the label:
`python scripts/simulate_webhook.py --issue 2 --title "Paramiko SHA-1" --body "Answer: use the config mitigation."`
→ a fresh session starts with the updated text.

No Docker? `pip install -r requirements.txt`, export the variables from `.env.example`, then
`uvicorn app.main:create_app --factory --port 8000`. There is also a CLI: `python -m app.cli status | enqueue | tick | worker`.

## Using the real Devin API

1. In the Devin app: Settings → Devin API → create a **service user** and key (`cog_...`). Give it permission to
   create and manage sessions (`ManageOrgSessions`). Note your org id (`org-...`).
2. Set in `.env`: `DEVIN_MODE=real`, `DEVIN_API_KEY`, `DEVIN_ORG_ID`, and a random `GITHUB_WEBHOOK_SECRET`.
3. **Try one session first (M0)**, capped at 2 ACUs, no database involved:
   ```bash
   python scripts/m0_run_session.py --title "probe" --max-acu 2 \
     --prompt "Do not change code. Ask me which of red or blue I prefer, then finish."
   ```
4. Register the webhook on the fork: Settings → Webhooks → payload URL `https://<your-tunnel>/webhook/github`,
   content type `application/json`, the same secret, and the **Issues** event. GitHub cannot reach `localhost`,
   so use a tunnel (smee.io, ngrok, or Cloudflare Tunnel) during development.
5. Create the labels on the fork (`devin:ready`, `devin:proposed`, ...), file an issue, add `devin:ready`.

Devin needs its GitHub integration connected to the fork so it can push branches and open PRs.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DEVIN_MODE` | `real` (example file sets `fake`) | `fake` simulates sessions for free |
| `DEVIN_API_KEY`, `DEVIN_ORG_ID` | | Devin service-user key and org id (real mode) |
| `GITHUB_WEBHOOK_SECRET` | | Required. Signature check on every webhook |
| `TARGET_REPO` | `french-fries-and-sundaes/superset` | Only events from this repo are acted on |
| `MAX_SESSIONS_PER_DAY` | `20` | Rolling 24h cap on new sessions |
| `MAX_ACU_PER_SESSION` | none | Optional Devin-side cost cap per session |
| `POLL_INTERVAL_SECONDS` | `15` | How often the worker polls Devin |
| `DB_PATH` | `data/tasks.db` | SQLite file (a Docker volume) |

## Tests

```bash
python -m unittest discover -s tests -t .      # 49 tests, standard library only
```

The outcome tests use the exact response shapes observed against the real API. The suite was also
mutation-checked: disabling the daily limit, the signature check, the idle-vs-blocked rule, or duplicate
detection each makes a test fail.

## Roadmap

| # | Milestone | Status |
|---|---|---|
| M0 | Devin client + `scripts/m0_run_session.py` | done |
| M1 | SQLite state machine, worker loop, daily session guard | done |
| M2 | Signed webhook, idempotency, simulate script | done |
| M3 | Sync labels, comments, and PR link back to GitHub; `issues.closed` / PR events -> completed / rejected | next |
| M4 | HTML dashboard and status CLI | next |
| M5 | Triage handling and blocked-question comments on the issue | next |
| M6 | Independent verification with GitHub Actions on the fork | next |
| M7 | `scan --now` (Devin sweep files `devin:proposed` issues) | next |
| M8 | `learn --now` (feedback -> one batched PR to `knowledge/`) | next |
| M9 | Seed script, demo script, final Docker polish | next |

**Known limitations today:** a `done` session moves straight to `in-review` (the independent verification gate
arrives in M6); the bot does not yet write labels or comments back to GitHub (M3), so state is visible via
`/api/status`; and re-applying `devin:ready` restarts from the edited issue text (comment handoff arrives with M3).
