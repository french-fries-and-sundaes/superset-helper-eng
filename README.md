# superset-helper-eng

An event-driven automation that uses **Devin as its worker** to fix engineering issues in a fork of
[Apache Superset](https://github.com/apache/superset) (`french-fries-and-sundaes/superset`).

A human approves a GitHub issue with one label, `devin:ready` → a webhook fires → this service starts **one
Devin session per issue** through the Devin API → Devin triages, fixes, and opens a pull request → an
**independent GitHub Actions check** verifies it → **a human reviews and merges.** Nothing ever merges
automatically, and every stage is visible on a dashboard.

## What problem this solves

Engineering teams carry a long tail of small, well-understood work: dependency advisories, lint and type
errors, missing tests. Each item is cheap to fix and expensive to *context-switch into*, so the list only grows.
This system turns that backlog into a pipeline: Devin does the fixing, and people spend their time on the two
things only people should do, **approving what is worth doing** and **reviewing what was done**.

## How it works

```
 scan (button / CLI) ──▶ Devin sweep ──▶ issues labeled devin:proposed ─┐
 a human files an issue ─────────────────────────────────────────────────┤   human approves
                                                                         ▼   with devin:ready
 GitHub ──webhook──▶ this service ──Devin API──▶ Devin session (one per issue)
                       │  SQLite state           1. TRIAGE first: run the verify command on unmodified code;
                       │  daily session guard       stop and ask if it already passes / is unclear / looks false
                       │  label + comment sync   2. fix, verify, open a PR ("Closes #N")
                       ▼                              │
                  dashboard, JSON API                 ▼
                                          GitHub Actions on the fork re-runs the issue's verify command
                                          (allowlisted commands only) ── pass ─▶ devin:in-review ─▶ human
                                                                        └ fail ─▶ devin:failed        merges
 human feedback (rejections, comments, review notes) ─▶ learn (button / CLI) ─▶ ONE batched PR to knowledge/
```

| Label | Meaning | Human needed? | Exit |
|---|---|---|---|
| `devin:proposed` | Scanner or template filed it; awaiting approval | yes | apply `devin:ready`, or close the issue |
| `devin:ready` | Approved; backlog until a session exists | no | session starts (triage first) |
| `devin:in-progress` | Devin is working, or its PR is being verified | no | to in-review / blocked / failed |
| `devin:blocked` | Devin needs a fact or decision (unclear, false positive, stuck) | yes | comment or edit the issue, re-apply `devin:ready` |
| `devin:in-review` | PR open and independently verified | yes | merge, request changes, or close |
| `devin:failed` | Devin tried and could not finish, or verification failed | yes | comment a hint, re-apply `devin:ready` |
| `devin:rejected` | A human declined a PR Devin produced, or a scanner proposal (issue closed "not planned", or PR closed unmerged) | no | terminal (reopen + `devin:ready` to retry) |
| *(no label)* completed | The PR was merged; the issue closes | no | terminal |
| *(no label)* not needed | Devin pushed back (blocked) and a human agreed and **closed the issue**: no fix was needed | no | terminal (reopen + `devin:ready` to retry) |

Human actions are ordinary GitHub actions: labels, comments, PR reviews, merge, close.
* **File a task yourself:** use the *Devin task* issue template (Bug Description, How to Triage / Test, Verify command). It
  arrives as `devin:proposed`; a collaborator who also adds `devin:ready` at creation skips straight to ready. The
  template never applies `devin:ready` itself, so the approval stays with people who can label.
* **Approve / unblock / retry:** re-apply `devin:ready`. A fresh session starts carrying the human's comments.
* **Request changes on the PR:** the feedback goes back to Devin as a revision on the *same* PR.
* **Merge:** the issue closes (`Closes #N`) and the task completes. **Close a PR unmerged:** rejected.
* **Close a *blocked* issue** (Devin said it's a duplicate, already fixed, or not real): **not needed.** What a
  close means depends on where the task was, not on which close button you clicked: only a merged PR ever counts
  as a completed fix, and agreeing with Devin's pushback is never counted against it.

### Design decisions worth knowing
- **Explicit outcome from Devin.** Against the real API, `status: running` + `status_detail: waiting_for_user`
  appears both while Devin waits for an answer *and* after it finished and went idle. Each session reports an
  `outcome` (`done` / `blocked` / `failed`) in structured output and the bot reads that (`app/outcome.py`).
- **Devin's "done" is a claim; the check is the evidence.** The fork-side workflow (`fork-files/`) re-runs the
  issue's verify command. It reads the command from the issue *through the API*, validates every command
  against a strict allowlist, and never interpolates issue text into YAML or a shell.
- **A revision never reuses the previous commit's passing result** (cleared when a revision session starts).
- **Compare-and-set state changes.** The webhook thread and the worker loop run concurrently; a task is claimed
  (`ready → in-progress`) before the Devin call, so two triggers never start two sessions.
- **Idempotent webhooks.** Duplicate deliveries are dropped by delivery id; an issue created with the label
  (which can emit both `opened` and `labeled`) still yields one task.
- **The store is the source of truth; GitHub is a view.** If GitHub is down, labels and comments catch up on the
  next tick, and each comment is posted once.
- **Spend guards, layered.** `MAX_SESSIONS_PER_DAY` (rolling 24h, includes scan/learn), optional
  `MAX_ACU_PER_SESSION`, and Devin's own *Message usage limit* setting. The API's `acus_consumed` did not
  report usage in our org, so the bot does not rely on it.
- **Simulated and real data never mix.** The database records the mode it was created in. The bot refuses to start
  (`SAFETY CHECK`) if you reuse a simulated database in real mode, or combine `DEVIN_MODE=fake` with a real
  `GITHUB_TOKEN`: either would write demo tasks to the real repository, or make a real approval collide with a
  simulated task number. To start fresh: `docker compose down && rm -rf data`.
- **`devin:ready` is also the access control.** Only collaborators can apply labels, so strangers can file
  issues but only a trusted person can send one to Devin. Issue text going to an agent is untrusted input.
- **Rules as code.** `knowledge/*.md` is injected into every prompt. The `learn` job proposes changes as a PR, so
  nothing Devin "learns" takes effect until a person merges it.

## Quick start: the full workflow with no keys and no cost

```bash
cp .env.example .env                # DEVIN_MODE=fake, GITHUB_TOKEN empty (dry run)
docker compose up --build           # dashboard on http://localhost:8000
```

In a second terminal (the script needs only Python 3, no installs):

```bash
export GITHUB_WEBHOOK_SECRET=change-me      # same value as in .env
bash scripts/demo.sh                        # plays the whole lifecycle; watch http://localhost:8000
PAUSE=3 bash scripts/demo.sh                # slower, for a screen recording
```

The demo walks through: approval → Devin works → **independent verification** → review → **changes requested**
→ revision → merge; a **blocked** task answered by a human; a **failed** task; a proposal that is **rejected**;
a **duplicate** delivery and a **bad signature**; a **sweep** that files proposals; and the **learn** job.

Individual events (fake mode numbers fix PRs as issue+100):

```bash
python3 scripts/simulate_webhook.py label --issue 1 --title "Override underscore"
python3 scripts/simulate_webhook.py label --issue 2 --title "[sim:blocked] Paramiko SHA-1"     # asks a question
python3 scripts/simulate_webhook.py label --issue 3 --title "[sim:fail] Coverage raise"        # fails
python3 scripts/simulate_webhook.py workflow-run --pr 101 --conclusion success                # verification result
python3 scripts/simulate_webhook.py review --pr 101 --state changes_requested --body "Add a test"
python3 scripts/simulate_webhook.py pr-close --pr 101 --merged
python3 scripts/simulate_webhook.py --help                                                      # all commands
```

No Docker? `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`, export the
variables from `.env.example`, then `uvicorn app.main:create_app --factory --port 8000`.

## Going live (real Devin, real GitHub)

Follow **[docs/GO_LIVE.md](docs/GO_LIVE.md)**: service user and token, `.env`, one command to create the labels and
seven issues on the fork (`scripts/seed_fork.py`), installing the verification workflow, the webhook, and a
first run. To try just the Devin side first, with no database and no GitHub:

```bash
python3 scripts/m0_run_session.py --title "probe" --max-acu 2 \
  --prompt "Do not change code. Ask me which of red or blue I prefer, then finish."
```

## The dashboard: "how would I know this is working?"

`http://localhost:8000` answers it in one screen: fixes merged (hero number, filterable by window), the
pipeline (backlog, working, verifying, out for review), a **needs-a-human list that states exactly what each task
needs from you** and how long it has waited, the daily session meter, and outcome metrics: merge rate, first-pass
rate, independent verification pass rate, **how pushbacks ended** (closed as not needed vs answered and continued),
scanner proposal approval rate, median Devin time to review-ready, blocked and failed rates, review rounds, sessions
per task. Outcomes are mutually exclusive and honest: merged fixes, **caught before coding** (false positives and
duplicates Devin flagged), rejected fixes, rejected proposals, and failures. Being right is never penalized: the
merge rate excludes no-change outcomes. Each task has a page with its sessions, PRs, verification, human feedback, and full
timeline. JSON: `/api/status`, `/api/tasks`, `/api/metrics`, `/api/jobs`. Buttons run the sweep, the learn job, and a
**Sync from GitHub** that imports labeled issues the bot never saw a webhook for. The page shows when it was last
updated, refreshes itself every 15 seconds, and has a **Refresh now** link.

## Configuration

See `.env.example` (every variable is documented there). The important ones:

| Variable | Default | Purpose |
|---|---|---|
| `DEVIN_MODE` | `real` (example file: `fake`) | `fake` simulates sessions for free |
| `GITHUB_TOKEN` | empty | empty = dry run: GitHub writes are logged, not sent |
| `VERIFY_MODE` | `actions` | `off` skips the independent verification gate |
| `MAX_SESSIONS_PER_DAY` | `20` | rolling 24h cap (fix + scan + learn sessions) |
| `DASHBOARD_PASSWORD` | empty | protects everything except the webhook; **set it behind a tunnel** |

## Tests

```bash
python3 -m unittest discover -s tests -t .      # 187 tests, standard library only
```

Outcome tests use response shapes observed against the real Devin API. The suite was mutation-checked: disabling
the daily limit, the signature check, the idle-vs-blocked rule, duplicate detection, stale-verification handling,
bot-comment filtering, HTML escaping, or the dashboard password each makes a test fail.

## Project layout

```
app/           orchestrator (state machine, worker, gate, jobs), webhook, dashboard, metrics, GitHub + Devin clients
fork-files/    what gets installed IN THE FORK: verification workflow, allowlist script, issue template
knowledge/     rules injected into Devin's prompts (reviewed by PR)
seed/issues/   the seven demo issues (one is a false positive, two are deliberately unfixable or too broad)
scripts/       demo.sh, simulate_webhook.py, seed_fork.py, m0_run_session.py
docs/          GO_LIVE.md (checklist), DEMO.md (5-minute video script)
tests/
```

## Known limitations and next steps

- **No real dedupe on scans.** The sweep prompt asks Devin to skip anything already covered by an existing issue
  and to report what it skipped, but a rejected or unapproved finding can be re-filed. Next: a stable fingerprint
  (advisory id / file / rule) in each issue, checked in code.
- **No retry bounds or timeouts** (deliberately, for simplicity): a blocked task waits forever, and revision
  rounds are unbounded. Next: caps, reminders, and auto-close of stale blocked tasks.
- **Blocked reasons are not categorized** (unclear vs false positive vs access). Next: a `blocked_reason` field
  to separate issue-quality problems from environment gaps on the dashboard.
- **Cost per fix is measured at batch level** (org on-demand balance before/after a run), because the API's
  per-session ACU field did not report usage.
- **Verify commands that need a heavy Python or Node environment** (`pytest`, `mypy` on Superset) must be able
  to run on a GitHub runner; tune the workflow's setup steps, or keep verify commands lightweight.
- **Single process, SQLite.** Right for one repo; a team-wide deployment would use Postgres and a queue.
- Resuming the same Devin session for a revision is possible (sessions are resumable) but the bot starts a fresh
  session with a handoff, which is simpler and survives session expiry.
