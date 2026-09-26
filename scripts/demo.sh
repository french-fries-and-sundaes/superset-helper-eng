#!/usr/bin/env bash
# Plays the whole lifecycle against a RUNNING service in fake mode (DEVIN_MODE=fake), using
# simulated GitHub events. Watch it on http://localhost:8000 while it runs. Also used as the
# end-to-end test. Usage:
#   export GITHUB_WEBHOOK_SECRET=change-me      # same as in .env
#   ./scripts/demo.sh [base_url]                # default http://localhost:8000
#   PAUSE=3 ./scripts/demo.sh                   # slow down between steps for a screen recording
set -euo pipefail

BASE="${1:-http://localhost:8000}"
PAUSE="${PAUSE:-0}"
sim() { local cmd="$1"; shift; python3 "$(dirname "$0")/simulate_webhook.py" "$cmd" --url "$BASE/webhook/github" "$@"; }
AUTH=()
[ -n "${DASHBOARD_PASSWORD:-}" ] && AUTH=(-u "demo:${DASHBOARD_PASSWORD}")

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; sleep "$PAUSE"; }
state() { curl -fsS "${AUTH[@]}" "$BASE/api/tasks" | python3 -c "
import json,sys
for t in json.load(sys.stdin)['tasks']:
    if t['issue']==$1: print(t['state'], t.get('verify_status') or ''); break"; }
wait_for() {  # wait_for <issue> <state> [timeout_s]
  local end=$((SECONDS + ${3:-90})) s
  while [ $SECONDS -lt $end ]; do
    s="$(state "$1" | awk '{print $1}')"
    [ "$s" = "$2" ] && { echo "  #$1 -> $2"; return 0; }
    sleep 1
  done
  echo "  TIMEOUT waiting for #$1 to reach $2 (now: $(state "$1"))"; return 1
}

say "1. A human labels issue 1 devin:ready. Devin works, opens a PR; the PR waits for verification"
sim label --issue 1 --title "Clear remaining npm advisories via overrides"
wait_for 1 in-progress
sleep 5; echo "  state: $(state 1)  (in-progress + pending = PR is being verified)"

say "2. GitHub Actions verifies the PR (simulated success). It goes to human review"
sim workflow-run --pr 101 --conclusion success
wait_for 1 in-review 20

say "3. A reviewer requests changes. Feedback goes back to Devin (revision on the same PR)"
sim review --pr 101 --state changes_requested --body "Please add a regression test."
wait_for 1 in-progress 30
echo "  Devin pushes new commits; the check re-runs on the new commit"
sim pr-open --pr 101 --issue 1 --action synchronize --sha sha2
sleep 5; echo "  state: $(state 1)  (still waiting: the old passing result is not reused)"
sim workflow-run --pr 101 --conclusion success --sha sha2 --run-id 1002
wait_for 1 in-review 30

say "4. The human merges the PR. Task complete"
sim pr-close --pr 101 --merged
wait_for 1 completed 20

say "5. Issue 2 is unclear: Devin's triage stops and asks (blocked)"
sim label --issue 2 --title "[sim:blocked] Track the paramiko SHA-1 advisory"
wait_for 2 blocked

say "6. The human answers in a comment and re-applies devin:ready. A fresh session starts with the answer"
sim comment --issue 2 --body "Apply the config mitigation; do not wait for upstream."
sim label --issue 2 --title "Track the paramiko SHA-1 advisory" --body "Answer: apply the config mitigation."
wait_for 2 in-progress 30

say "7. Issue 3 fails. Issue 4 is a scanner proposal that a human rejects"
sim label --issue 3 --title "[sim:fail] Raise coverage on the REST API surface"
wait_for 3 failed
sim label --issue 4 --title "Scanner finding: unused lint rule" --label devin:proposed
sim comment --issue 4 --body "Not worth it: this rule is disabled on purpose."
sim close-issue --issue 4 --reason not_planned
wait_for 4 rejected 20

say "8. Idempotency and security: duplicate delivery, and a bad signature"
sim label --issue 5 --title "Duplicate test" --twice
sim label --issue 5 --title x --bad-signature || true

say "9. Sweep: Devin scans for problems and files devin:proposed issues (dry-run GitHub without a token)"
curl -fsS "${AUTH[@]}" -X POST "$BASE/api/scan"; echo
sleep 8
curl -fsS "${AUTH[@]}" "$BASE/api/jobs" | python3 -c "import json,sys; [print('  job', j['kind'], j['status'], j['summary'][:70]) for j in json.load(sys.stdin)['jobs']]"

say "10. Learn: human feedback becomes ONE batched PR to knowledge/ (needs a human to merge)"
curl -fsS "${AUTH[@]}" -X POST "$BASE/api/learn"; echo
sleep 8
curl -fsS "${AUTH[@]}" "$BASE/api/jobs" | python3 -c "import json,sys; [print('  job', j['kind'], j['status'], j['summary'][:70]) for j in json.load(sys.stdin)['jobs']]"

say "Done. Open $BASE to see the dashboard"
curl -fsS "${AUTH[@]}" "$BASE/api/status" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('  counts:', {k:v for k,v in d['counts'].items() if v})
print('  sessions in the last 24h:', d['sessions_last_24h'], 'of', d['max_sessions_per_day'])"
