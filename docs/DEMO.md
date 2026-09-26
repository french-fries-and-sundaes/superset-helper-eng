# 5-minute demo script (What / How / Why / When)

Audience: a VP of Engineering and senior engineers who are curious about Devin. Keep every claim you make
demonstrable on screen. Record with the dashboard open; `PAUSE=3 bash scripts/demo.sh` paces the fake-mode run.

## Before you record
- **Confirm every expected outcome in a real run before you claim it on camera.** The blocked / false-positive / too-broad
  behaviours below are what the prompts are designed to produce; Devin's real behaviour is what you should show.
- Real run done at least once (docs/GO_LIVE.md, step 7) so you can show a real PR and a real session.
- Have open: the dashboard, the fork's Issues tab, one real Devin PR, and the Devin session page.
- Write down your cost measurement (GO_LIVE step 9) and your engineer-time estimates (see "Numbers" below).

## 0:00 What (45 s): the problem
- "Every team has a long tail of small, well-understood work: dependency advisories, type errors, missing tests.
  Each is cheap to fix and expensive to context-switch into, so the list only grows."
- Show the audit: 20 npm advisories (3 critical) and 1 Python advisory found on a real repo, Apache Superset.
- "The goal: humans approve and review; an agent does the fixing; and a leader can see whether it works."

## 0:45 How (2 min 15 s): show it running
1. **Trigger (20 s).** Show an issue with `devin:proposed`. Apply `devin:ready`. "That label is the event. It is
   also the access control: only collaborators can apply it."
2. **Devin at work (40 s).** Dashboard: task moves to *Working now*; the issue gets a comment with the session
   link. Open the Devin session. "One session per issue. Step one is triage: it runs the issue's verify command on
   unmodified code, and stops to ask if the problem doesn't exist."
3. **Independent verification (30 s).** The PR opens; dashboard shows *Verifying*. Show the `devin-verify`
   workflow on the PR. "Devin's 'done' is a claim. This check is the evidence, and it only runs allowlisted commands
   read from the issue through the API."
4. **Human in the loop (25 s).** Task lands in *Out for review*. Request changes in a review comment; show the
   revision going to the same PR. Merge; task completes.
5. **Pushback (20 s).** Show the *Needs a human* list: the underscore issue is blocked because it is already fixed
   (Devin declined to make a pointless change), paramiko is blocked because no fixed release exists, the coverage
   task is blocked as too broad. "Every request states exactly what it needs from a person."

## 3:00 Architecture (45 s): the decisions worth calling out
- Webhook → SQLite state machine → Devin API. Compare-and-set transitions; idempotent deliveries; signed webhooks.
- The `waiting_for_user` ambiguity: "the API reports the same status when Devin is blocked and when it is done and
  idle, so each session must report an explicit outcome in structured output."
- Layered spend guards: daily session cap, per-session cap, Devin's message limit.
- Show `app/outcome.py` and `fork-files/.github/scripts/devin_verify.py` (the allowlist).

## 3:45 Why Devin (45 s)
- "This is not a linter and not a script: each task needs reading unfamiliar code, choosing a fix, running the
  project's own toolchain, and judging when *not* to act. That judgment is what let it decline the already-fixed
  issue and stop on the unfixable one."
- "It runs in a real environment: it installed dependencies, ran the audit, ran the tests, opened the PR."
- "Sessions are cheap to parallelize, so the backlog burns down without anyone waiting."

## 4:30 When (30 s): next steps in a real engagement
- Fingerprint-based dedupe for scans; retry bounds and stale-task reminders; blocked-reason analytics.
- Run the sweep on a schedule; route different task types to different playbooks; team-wide deployment
  (Postgres, queue, SSO).
- Feed review outcomes into the knowledge PRs (already built: `learn`) and measure the trend in first-pass rate.

## Numbers (fill in, and label your assumptions)
| Metric | Where it comes from |
|---|---|
| Tasks merged, merge rate, first-pass rate | dashboard "Is it working?" |
| Median Devin time to review-ready | dashboard |
| Cost per merged fix | (on-demand balance before/after) / sessions; note included quota |
| Engineer time saved | YOUR estimate per issue type (e.g. 30 to 60 min for an advisory override incl. testing) x an hourly rate you state |
| Human review time per PR | measure it: time yourself reviewing |

Be honest about the limits: state which numbers are measured and which are your estimates, and mention what
Devin declined or got wrong. That credibility is worth more than a perfect success rate.
