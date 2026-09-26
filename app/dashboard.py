"""Server-rendered HTML dashboard: answers "how would I know this is working?"

Design (follows the data-viz guidance): one hero figure (fixes merged); stat tiles with
proportional numerals; tabular figures only inside tables; status colors are always paired
with an icon and a text label (never color alone) and text stays in ink colors; the daily
session guard is a meter whose fill turns warning/critical as it fills. No JavaScript.
Everything dynamic is HTML-escaped.
"""
from __future__ import annotations

import html
import json
import time
from typing import Any

from .metrics import compute_metrics, humanize
from .orchestrator import Orchestrator
from .states import HUMAN_NEEDED, State

e = html.escape

WINDOWS = {"24h": ("last 24 hours", 24 * 3600), "7d": ("last 7 days", 7 * 24 * 3600), "all": ("all time", None)}

GLYPH = {
    State.PROPOSED: "◇",
    State.READY: "○",
    State.IN_PROGRESS: "●",
    State.BLOCKED: "▲",
    State.IN_REVIEW: "◎",
    State.FAILED: "✕",
    State.REJECTED: "⊘",
    State.COMPLETED: "✓",
    State.NOT_NEEDED: "∅",
}

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--hair:rgba(11,11,11,.10);--grid:#e1e0d9;--accent:#2a78d6;--track:#cde2fb;--good:#0ca30c;--good-ink:#006300;
--warning:#fab219;--serious:#ec835a;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--hair:rgba(255,255,255,.10);--grid:#2c2c2a;--accent:#3987e5;--track:#0d366b;--good-ink:#0ca30c}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0;font-weight:600}h2{font-size:16px;margin:32px 0 10px;font-weight:600}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.sub{color:var(--ink2);margin:2px 0 12px;overflow-wrap:anywhere}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 4px}
.chip{border:1px solid var(--hair);background:var(--surface);border-radius:999px;padding:2px 10px;font-size:13px;color:var(--ink2)}
.flash{background:var(--surface);border:1px solid var(--hair);border-left:4px solid var(--accent);padding:10px 14px;border-radius:6px;margin:14px 0}
.hero{display:flex;flex-wrap:wrap;gap:16px;align-items:stretch;margin-top:16px}
.card{background:var(--surface);border:1px solid var(--hair);border-radius:10px;padding:16px}
.hero .big{flex:1 1 280px}
.big .num{font-size:56px;line-height:1.05;font-weight:600}
.big .cap{color:var(--ink2)}
.tiles{flex:3 1 520px;min-width:0;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.tile .lab{color:var(--ink2);font-size:13px}.tile .val{font-size:30px;font-weight:600;line-height:1.2}
.tile .note{color:var(--muted);font-size:12px}
.meter{height:8px;border-radius:4px;background:var(--track);margin:8px 0 4px;overflow:hidden}
.meter>span{display:block;height:100%;border-radius:4px}
.tbl{overflow-x:auto;background:var(--surface);border:1px solid var(--hair);border-radius:10px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--grid);vertical-align:top;font-size:14px;overflow-wrap:break-word}
th{color:var(--ink2);font-weight:500;font-size:12px;text-transform:none}
tr:last-child td{border-bottom:0}
td.num,th.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{white-space:nowrap}.pill i{font-style:normal;margin-right:6px}
.st-completed i,.st-not-needed i{color:var(--good)}.st-blocked i{color:var(--warning)}.st-failed i{color:var(--critical)}
.st-in-progress i{color:var(--accent)}.st-in-review i,.st-ready i,.st-proposed i,.st-rejected i{color:var(--muted)}
.empty{color:var(--muted);padding:12px 0}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:12px}
dl{margin:0;display:grid;grid-template-columns:1fr auto;gap:6px 16px}dt{color:var(--ink2)}dd{margin:0;font-variant-numeric:tabular-nums;font-weight:600}
button{font:inherit;background:var(--accent);color:#fff;border:0;border-radius:6px;padding:7px 14px;cursor:pointer}
button:hover{filter:brightness(1.08)}
.muted{color:var(--muted)}.small{font-size:13px}
.tabs a{margin-right:10px}.tabs .on{font-weight:600;color:var(--ink);text-decoration:underline}
pre{white-space:pre-wrap;background:var(--surface);border:1px solid var(--hair);border-radius:8px;padding:10px;font-size:13px}
@media (max-width:700px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.big .num{font-size:44px}th,td{padding:8px}table{min-width:620px}}
"""


def _page(title: str, body: str, refresh: int | None = 15) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{meta}<title>{e(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"
    )


def _pill(state: str) -> str:
    s = State(state)
    return f'<span class="pill st-{e(s.value)}"><i aria-hidden="true">{GLYPH[s]}</i>{e(s.value)}</span>'


def _age(ts: int, now: float) -> str:
    return humanize(now - ts)


def _issue_link(repo: str, number: int, title: str) -> str:
    return (
        f'<a href="/tasks/{number}">#{number}</a> '
        f'<a href="https://github.com/{e(repo)}/issues/{number}" title="open on GitHub">{e(title[:90])}</a>'
    )


def _safe_link(url: str, text: str, prefixes: tuple[str, ...]) -> str:
    return f'<a href="{e(url)}">{e(text)}</a>' if url.startswith(prefixes) else ""


def _links(task: dict[str, Any], orch: Orchestrator) -> str:
    out = [_safe_link(u, f"PR #{u.rstrip('/').split('/')[-1]}", ("https://github.com/",)) for u in task["pr_urls"]]
    sessions = orch.store.sessions_for_task(task["id"])
    if sessions:
        out.append(_safe_link(sessions[-1]["url"], "Session log", ("https://app.devin.ai/",)))
    return " · ".join(x for x in out if x)


def _need(task: dict[str, Any], orch: Orchestrator) -> str:
    st = State(task["state"])
    if st is State.PROPOSED:
        return "Approve by applying <code>devin:ready</code>, or close the issue to reject."
    if st is State.BLOCKED:
        return f"<b>Question:</b> {e(task['blocked_question'] or 'Devin is waiting for input.')}<br><span class=\"muted small\">Answer in a comment, then re-apply devin:ready.</span>"
    if st is State.FAILED:
        return f"{e((task['last_summary'] or 'Devin could not finish.')[:300])}<br><span class=\"muted small\">Comment a hint, then re-apply devin:ready.</span>"
    prs = orch.store.prs_for_task(task["id"])
    v = ", ".join(f"PR #{p['pr_number']} verification {p['verify_status'] or 'not reported'}" for p in prs)
    return f"Review the pull request. <span class=\"muted small\">{e(v)}</span>"


def _table(headers: list[tuple[str, bool]], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<div class="empty">{e(empty)}</div>'
    head = "".join(f'<th class="num">{e(h)}</th>' if num else f"<th>{e(h)}</th>" for h, num in headers)
    body = "".join(
        "<tr>" + "".join(f'<td class="num">{c}</td>' if headers[i][1] else f"<td>{c}</td>" for i, c in enumerate(r)) + "</tr>"
        for r in rows
    )
    return f'<div class="tbl"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _meter(used: int, limit: int) -> str:
    pct = 0 if limit <= 0 else min(100, round(100 * used / limit))
    color = "var(--critical)" if pct >= 90 else "var(--warning)" if pct >= 70 else "var(--accent)"
    status = "at the limit" if pct >= 100 else "approaching the limit" if pct >= 70 else "within budget"
    return (
        f'<div class="meter" role="img" aria-label="{used} of {limit} sessions used"><span style="width:{pct}%;background:{color}"></span></div>'
        f'<div class="note">{used} of {limit} in the last 24h · {status}</div>'
    )


def render_dashboard(orch: Orchestrator, window: str = "7d", msg: str = "") -> str:
    now = time.time()
    label, secs = WINDOWS.get(window, WINDOWS["7d"])
    window = window if window in WINDOWS else "7d"
    store = orch.store
    summary = orch.status_summary()
    m = compute_metrics(store, now, secs)
    tasks = store.list_tasks()
    counts = summary["counts"]
    verifying = summary["verifying"]
    working = counts["in-progress"] - verifying
    needs = [t for t in tasks if State(t["state"]) in HUMAN_NEEDED]
    needs.sort(key=lambda t: t["updated_at"])

    s = orch.settings
    chips = "".join(
        f'<span class="chip">{e(c)}</span>'
        for c in (
            f"Devin: {summary['devin_mode']}",
            f"GitHub sync: {summary['github_sync']}",
            f"Verification: {summary['verify_mode']}",
        )
    )
    flash = f'<div class="flash">{e(msg)}</div>' if msg else ""
    stamp = time.strftime("%H:%M:%S", time.gmtime(now))
    refresh = (
        f'<div class="muted small" style="margin-top:6px">Updated {stamp} UTC · refreshes itself every 15 seconds · '
        f'<a href="/?window={e(window)}">Refresh now</a></div>'
    )
    tabs = " ".join(
        f'<a href="/?window={k}" class="{"on" if k == window else ""}">{v[0]}</a>' for k, v in WINDOWS.items()
    )

    hero = (
        f'<div class="card big"><div class="cap">Fixes merged ({e(label)})</div>'
        f'<div class="num">{m["completed_in_window"]}</div>'
        f'<div class="cap small"><b>{m["not_needed_in_window"]}</b> caught before coding (no fix needed)</div>'
        f'<div class="cap small muted">{counts["completed"]} merged and {counts["not-needed"]} not needed, all time · {m["tasks_total"]} tasks tracked</div>'
        f'<div class="small tabs" style="margin-top:8px">{tabs}</div></div>'
    )

    def tile(lab: str, val: int, note: str = "") -> str:
        return f'<div class="card tile"><div class="lab">{e(lab)}</div><div class="val">{val}</div><div class="note">{e(note)}</div></div>'

    tiles = (
        tile("Backlog", counts["ready"], "approved, waiting for a session")
        + tile("Working now", working, "Devin sessions running")
        + tile("Verifying", verifying, "PR checks running")
        + tile("Out for review", counts["in-review"], "waiting for a human")
        + tile("Needs a human", len(needs), "proposed, blocked, failed, in review")
        + f'<div class="card tile"><div class="lab">Sessions today</div>{_meter(summary["sessions_last_24h"], summary["max_sessions_per_day"])}</div>'
    )

    need_rows = [
        [_issue_link(t["repo"], t["issue_number"], t["title"]), _pill(t["state"]), _need(t, orch), _age(t["updated_at"], now), _links(t, orch)]
        for t in needs
    ]
    flight = [t for t in tasks if State(t["state"]) in (State.READY, State.IN_PROGRESS)]
    flight_rows = [
        [
            _issue_link(t["repo"], t["issue_number"], t["title"]),
            _pill(t["state"]) + (' <span class="muted small">verifying PR</span>' if t["verify_status"] == "pending" else ""),
            str(t["attempt"]),
            _age(t["updated_at"], now),
            _links(t, orch),
        ]
        for t in flight
    ]
    done = [t for t in tasks if t["state"] == State.COMPLETED.value]
    if secs is not None:
        done = [t for t in done if now - t["updated_at"] <= secs]
    done_rows = [[_issue_link(t["repo"], t["issue_number"], t["title"]), _pill(t["state"]), _age(t["updated_at"], now), _links(t, orch)] for t in done]
    caught = [t for t in tasks if t["state"] == State.NOT_NEEDED.value]
    if secs is not None:
        caught = [t for t in caught if now - t["updated_at"] <= secs]
    caught_rows = [[_issue_link(t["repo"], t["issue_number"], t["title"]), _pill(t["state"]), _age(t["updated_at"], now), _links(t, orch)] for t in caught]
    rej = [t for t in tasks if t["state"] == State.REJECTED.value]
    rej_rows = [[_issue_link(t["repo"], t["issue_number"], t["title"]), _pill(t["state"]), _age(t["updated_at"], now)] for t in rej]

    def pct(v: int | None) -> str:
        return "n/a" if v is None else f"{v}%"

    ver = m["verification"]
    pb, pr_ = m["pushbacks"], m["proposals"]
    metrics = (
        '<div class="card"><dl>'
        f"<dt>Merge rate (merged fixes / attempted fixes)</dt><dd>{pct(m['merge_rate_pct'])}</dd>"
        f"<dt>First-pass rate (reached review on the first attempt)</dt><dd>{pct(m['first_pass_rate_pct'])}</dd>"
        f"<dt>Independent verification pass rate</dt><dd>{pct(ver['pass_rate_pct'])} ({ver['passed']} passed, {ver['failed']} failed)</dd>"
        f"<dt>Pushbacks at triage</dt><dd>{pb['total']} ({pb['closed_not_needed']} closed as not needed, {pb['answered_and_continued']} answered and continued, {pb['waiting']} waiting)</dd>"
        f"<dt>Scanner proposals approved</dt><dd>{pct(pr_['approval_rate_pct'])} ({pr_['approved']} approved, {pr_['rejected']} rejected, {pr_['pending']} pending)</dd>"
        f"<dt>Median Devin time to review-ready</dt><dd>{humanize(m['median_work_to_review'])}</dd>"
        f"<dt>Median wait before a session starts</dt><dd>{humanize(m['median_queue_wait'])}</dd>"
        f"<dt>Tasks that were ever blocked</dt><dd>{pct(m['blocked_rate_pct'])}</dd>"
        f"<dt>Tasks that ever failed</dt><dd>{pct(m['failed_rate_pct'])}</dd>"
        f"<dt>Review rounds requested</dt><dd>{m['changes_requested']}</dd>"
        f"<dt>Sessions per task</dt><dd>{m['sessions_per_task'] if m['sessions_per_task'] is not None else 'n/a'}</dd>"
        "</dl></div>"
    )

    def job_card(kind: str, title: str, blurb: str, action: str) -> str:
        jobs = store.list_jobs(kind, limit=1)
        last = ""
        if jobs:
            j = jobs[0]
            res = j["result"]
            link = _safe_link(j["url"], "session", ("https://app.devin.ai/",))
            detail = e((j["summary"] or "")[:200])
            if j["status"] == "done" and kind == "scan":
                detail = e(
                    f"filed {len(res.get('filed', []))}, skipped {len(res.get('skipped', []))}"
                    + (f", held back {res['truncated']}" if res.get("truncated") else "")
                )
            elif j["status"] == "done" and kind == "learn":
                prs = " ".join(_safe_link(u, "PR", ("https://github.com/",)) for u in res.get("prs", []))
                detail = e(f"{res.get('rules_proposed', 0)} rule(s) proposed") + (f" · {prs}" if prs else "")
            last = (
                f'<div class="small" style="margin-top:8px">Last run {e(_age(j["created_at"], now))} ago: '
                f'<b>{e(j["status"])}</b> · {detail} {link}</div>'
            )
        return (
            f'<div class="card"><b>{e(title)}</b><div class="muted small">{e(blurb)}</div>'
            f'<form method="post" action="{e(action)}" style="margin-top:10px"><button type="submit">Run {e(kind)} now</button></form>{last}</div>'
        )

    jobs_html = '<div class="two">' + job_card(
        "scan", "Sweep for problems", "A Devin session runs audits and linters and files findings as devin:proposed issues for approval.", "/actions/scan"
    ) + job_card(
        "learn", "Learn from feedback", "Turns human feedback since the last run into one batched PR against knowledge/.", "/actions/learn"
    ) + (
        '<div class="card"><b>Sync from GitHub</b>'
        '<div class="muted small">Imports open issues labeled devin:proposed or devin:ready that the bot has not seen '
        '(for example, created before the webhook existed). Also runs once at startup.</div>'
        '<form method="post" action="/actions/sync" style="margin-top:10px"><button type="submit">Sync now</button></form></div>'
    ) + "</div>"

    events = store.list_events(limit=12)
    by_id = {t["id"]: t for t in tasks}
    ev_rows = []
    for ev in events:
        t = by_id.get(ev["task_id"]) if ev["task_id"] else None
        who = f'<a href="/tasks/{t["issue_number"]}">#{t["issue_number"]}</a>' if t else "system"
        ev_rows.append([_age(ev["ts"], now) + " ago", who, e(ev["kind"]), e(ev["detail"][:160])])

    body = (
        f"<h1>superset-helper-eng</h1>"
        f'<div class="sub">Devin fixes issues in <a href="https://github.com/{e(s.target_repo)}">{e(s.target_repo)}</a>; humans review and merge.</div>'
        f'<div class="chips">{chips}</div>{refresh}{flash}'
        f'<div class="hero">{hero}<div class="tiles">{tiles}</div></div>'
        "<h2>Needs a human</h2>"
        + _table([("Task", False), ("State", False), ("What we need from you", False), ("Waiting", True), ("Links", False)], need_rows, "Nothing is waiting on a person.")
        + "<h2>In flight</h2>"
        + _table([("Task", False), ("State", False), ("Attempt", True), ("Updated", True), ("Links", False)], flight_rows, "No tasks are queued or running.")
        + f"<h2>Completed ({e(label)})</h2>"
        + _table([("Task", False), ("State", False), ("When", True), ("Links", False)], done_rows, "No merged fixes in this window yet.")
        + f"<h2>Caught before coding ({e(label)})</h2>"
        + _table([("Task", False), ("State", False), ("When", True), ("Links", False)], caught_rows, "Nothing yet: these are issues Devin flagged as not needing a fix, which a human then closed.")
        + "<h2>Rejected</h2>"
        + _table([("Task", False), ("State", False), ("When", True)], rej_rows, "Nothing rejected.")
        + "<h2>Is it working?</h2>" + metrics
        + "<h2>Actions</h2>" + jobs_html
        + "<h2>Recent activity</h2>"
        + _table([("When", True), ("Task", False), ("Event", False), ("Detail", False)], ev_rows, "No activity yet.")
        + '<p class="muted small">This page refreshes every 15 seconds. JSON: <a href="/api/status">/api/status</a> · <a href="/api/metrics">/api/metrics</a> · <a href="/api/tasks">/api/tasks</a></p>'
    )
    return _page("superset-helper-eng", body)


def render_task(orch: Orchestrator, issue_number: int) -> str | None:
    task = orch.store.get_task_by_issue(orch.settings.target_repo, issue_number)
    if task is None:
        return None
    now = time.time()
    store = orch.store
    sessions = store.sessions_for_task(task["id"])
    prs = store.prs_for_task(task["id"])
    events = store.list_events(task["id"], limit=200, ascending=True)
    feedback = store.feedback_since(0, task["id"])

    s_rows = [
        [str(s["attempt"]), _safe_link(s["url"], s["session_id"][:12], ("https://app.devin.ai/",)), e(s["status"] or ""), e(s["outcome"] or "in progress"), _age(s["created_at"], now) + " ago"]
        for s in sessions
    ]
    p_rows = [
        [_safe_link(p["pr_url"], f"PR #{p['pr_number']}", ("https://github.com/",)), e(p["state"]), e(p["verify_status"] or "not reported"), _safe_link(p["verify_url"], "run", ("https://github.com/",))]
        for p in prs
    ]
    e_rows = [[time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ev["ts"])) + " UTC", e(ev["kind"]), e(ev["detail"][:300])] for ev in events]
    f_rows = [[_age(f["ts"], now) + " ago", e(f["author"] or ""), e(f["kind"]), e(f["text"][:400])] for f in feedback]
    guidance = f"<h2>Guidance carried into the next session</h2><pre>{e(task['guidance'])}</pre>" if task["guidance"] else ""
    question = f"<h2>Devin's question</h2><pre>{e(task['blocked_question'])}</pre>" if task["blocked_question"] else ""
    body = (
        '<p><a href="/">&larr; Dashboard</a></p>'
        f"<h1>#{issue_number} {e(task['title'])}</h1>"
        f'<div class="sub">{_pill(task["state"])} · attempt {task["attempt"]} · source: {e(task["source"])} · '
        f'<a href="https://github.com/{e(task["repo"])}/issues/{issue_number}">open on GitHub</a></div>'
        f"{question}{guidance}"
        "<h2>Sessions</h2>" + _table([("Attempt", True), ("Devin session", False), ("Status", False), ("Outcome", False), ("Started", True)], s_rows, "No sessions yet.")
        + "<h2>Pull requests and verification</h2>" + _table([("PR", False), ("State", False), ("Verification", False), ("Run", False)], p_rows, "No pull requests yet.")
        + "<h2>Human feedback</h2>" + _table([("When", True), ("Who", False), ("Kind", False), ("Text", False)], f_rows, "No feedback recorded.")
        + "<h2>Timeline</h2>" + _table([("Time", True), ("Event", False), ("Detail", False)], e_rows, "No events.")
        + f'<h2>Issue text sent to Devin</h2><pre>{e(task["body"])}</pre>'
    )
    return _page(f"#{issue_number} {task['title']}", body)


def metrics_json(orch: Orchestrator, window: str = "all") -> dict[str, Any]:
    secs = WINDOWS.get(window, WINDOWS["all"])[1]
    return compute_metrics(orch.store, time.time(), secs)


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)
