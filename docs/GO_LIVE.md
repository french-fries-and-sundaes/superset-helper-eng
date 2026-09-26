# Going live: real Devin, real GitHub

Do these in order. Each step says how to check that it worked. Nothing here spends money until step 8.

## 0. What you need
- Docker Desktop (or Python 3.10+), the GitHub CLI (`brew install gh`, then `gh auth login`)
- A Devin service user API key (`cog_...`) and your org id (`org-...`)
- Admin/write access to `french-fries-and-sundaes/superset` (the fork) and `.../superset-helper-eng` (this repo)

## 1. Devin
1. Settings, Devin API: create a **service user** with permission to create and manage sessions
   (`ManageOrgSessions`). Copy the key once.
2. Settings, Integrations: connect GitHub and grant Devin access to **both repos**: the fork (so it can push
   branches and open PRs) and this solution repo (so the `learn` job can open its knowledge PR).
3. Settings, Usage & limits: **lower the "Message usage limit"** (default $20) to something like $5 to $10 while
   testing. It is the last line of defense if a task runs away.
4. Check: `python3 scripts/m0_run_session.py --title probe --max-acu 2 --prompt "Do not change code. Ask me red or blue, then finish."`
   prints the session link and ends with a `blocked` outcome. (Needs `pip install -r requirements.txt` in a venv.)

## 2. GitHub token for the bot
Create a **fine-grained personal access token** (Settings, Developer settings): resource owner = the
`french-fries-and-sundaes` org, repositories = **only** the fork and this repo. Permissions:
Issues: read/write, Pull requests: read/write, Actions: read, Metadata: read. (If the org requires approval for
tokens, an owner must approve it.) This is how the bot labels and comments; Devin uses its own connection.

## 3. Configure
```bash
cp .env.example .env
```
Set `DEVIN_MODE=real`, `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `GITHUB_TOKEN`, a random `GITHUB_WEBHOOK_SECRET`
(`openssl rand -hex 24`), and **`DASHBOARD_PASSWORD`** (the tunnel in step 6 exposes the whole app).

## 4. Prepare the fork (one command)
```bash
gh auth refresh -s workflow        # lets gh add workflow files
python3 scripts/seed_fork.py --install-files --disable-other-workflows
```
This creates the labels, the seven demo issues (as `devin:proposed`, so nothing starts yet), installs the
`devin-verify` workflow, its allowlist script and the issue template into the fork, and disables the fork's
inherited Superset workflows so they do not run on every Devin PR. Use `--dry-run` first to see the plan.
Check: the fork's Issues tab shows seven `devin:proposed` issues, and Actions lists only `devin-verify`.

Optional but recommended: fork Settings, Branches, add a rule on the default branch requiring a pull request
review, so "humans must merge" is enforced by GitHub and not only by convention.

## 5. Start the service
```bash
docker compose up --build
```
Open http://localhost:8000 (username anything, password = `DASHBOARD_PASSWORD`). The chips at the top should read
`Devin: real`, `GitHub sync: live`, `Verification: actions`.

## 6. Webhook
GitHub cannot reach localhost, so start a tunnel (`ngrok http 8000`, Cloudflare Tunnel, or smee.io) and use its
public URL. On the fork: Settings, Webhooks, Add webhook:
- Payload URL: `https://<tunnel>/webhook/github`, content type `application/json`, secret = `GITHUB_WEBHOOK_SECRET`
- Events: **Issues, Issue comments, Pull requests, Pull request reviews, Workflow runs**

Check: the webhook's "Recent Deliveries" tab shows the ping with a green tick (HTTP 200).

## 7. First real run (one issue, small blast radius)
1. On issue "Add unit tests for superset/utils/retries.py", add the label **`devin:ready`**.
2. Watch the dashboard: the task moves to In progress, a comment with the Devin session link appears on the issue.
3. Devin runs the verify command first (triage), fixes, opens a PR that says `Closes #N`.
4. The `devin-verify` workflow runs on the PR; when it finishes the task moves to **Out for review**.
5. Review the PR like any other. Merge it, request changes, or close it, and watch the task follow.

## 8. The demo set
Approve the rest one at a time (`devin:ready`). What each should do:
| Issue | Expected |
|---|---|
| Override underscore | **Blocked**: the problem is already fixed (PR #1); triage stops and asks |
| Clear remaining npm advisories | In review with a package.json overrides change |
| mypy errors in date_parser.py | In review with annotation-only changes |
| check_pot_drift Babel | In review |
| Tests for retries.py | In review |
| paramiko SHA-1 | **Blocked**: no fixed release exists; asks which mitigation |
| Raise API coverage | **Blocked**: too broad; asks to split |

## 9. Measure cost (for the business case)
The API's per-session ACU field did not report usage in our org. Before starting the run, note "Your on-demand
usage" on Devin's Usage & limits page; after, note it again and divide the difference by the number of sessions
(dashboard: sessions). If the included quota absorbed part of the run, say so.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| Chips say `GitHub sync: dry-run` | `GITHUB_TOKEN` is empty or not loaded; restart the container after editing `.env` |
| Webhook delivery shows 401 | Secret differs between `.env` and the webhook, or the payload was altered |
| Webhook delivery shows 503 | `GITHUB_WEBHOOK_SECRET` is not set |
| Label applied but nothing starts | Event not subscribed; wrong repo in `TARGET_REPO`; or the daily session limit was reached |
| Task stuck at "verifying PR" | Workflow not installed, or the webhook lacks the **Workflow runs** event, or the PR body has no `Closes #N` |
| Verification fails immediately | The verify command uses a program outside the allowlist (see the workflow log) |
| A session never appears | Check the container log for `session_start_failed`; a 403 means the service user lacks permission |
| Labels missing | Run `python3 scripts/seed_fork.py --labels-only` |
