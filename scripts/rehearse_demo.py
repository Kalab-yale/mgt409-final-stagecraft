"""Run the whole demo story once against the real app and database, then export the evidence for the report.

    python scripts/rehearse_demo.py            # about 3 minutes and about $0.04 of model calls
    python scripts/rehearse_demo.py --keep-data # leave the rehearsed state (processed inbox, approved fix) in place
    python scripts/rehearse_demo.py --export-only # just re-export the evidence from what is already logged ($0)

It uses the same API the browser uses: inbox processing, the daily brief, one question per tab assistant, a drafted
change confirmed by the organizer, three Resolve-an-issue runs plus the injection attempt, a fresh Cost-Saver OFF vs ON comparison on the
same starting schedule, approving one fix, and the attack test. The evidence goes to docs/evidence/stagecraft-evidence.json
and the headline numbers to docs/evidence/summary.md.

By default it then restores the clean demo data so the live demo starts fresh. AI usage, cost, and security logs are
kept either way (they are the evidence).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend import database as db  # noqa: E402
from backend.app import app  # noqa: E402

YBVS = "evt_ybvs_2026"
E = f"/api/events/{YBVS}"
OUT = REPO_ROOT / "docs" / "evidence"
QUESTIONS = [
    ("overview", "What should I focus on today?"),
    ("program", "Which speakers are still travelling?"),
    ("venue", "Which rooms are close to capacity?"),
    ("food", "What's our dietary breakdown?"),
    ("attendees", "How many people are on the waitlist?"),
    ("budget", "Where are we over budget?"),
    ("inbox", "Which emails need a reply?"),
]


def step(title: str) -> None:
    print(f"\n== {title}")


def ok(r):
    if r.status_code >= 400:
        raise SystemExit(f"   FAILED {r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}")
    return r.json()


def export_only() -> None:
    with TestClient(app) as c:
        rt = ok(c.post("/api/admin/security/red-team-test", json={"event_id": YBVS}))
        bundle = ok(c.get("/api/admin/export-evidence"))
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "stagecraft-evidence.json").write_text(json.dumps(bundle, indent=2, default=str))
        (OUT / "summary.md").write_text(summary_md(bundle, rt, bundle["benchmarks"][0]))
        print(f"wrote {OUT / 'stagecraft-evidence.json'} and summary.md")


def main(keep_data: bool) -> None:
    t0 = time.perf_counter()
    with TestClient(app) as c:
        db.reset_demo_db()
        presets = {p.title: p.prompt_text for p in db.get_crisis_presets(YBVS)}

        step("1. Inbox Watcher reads the demo inbox")
        r = ok(c.post(f"{E}/inbox/process"))
        print(f"   {r['processed']} emails: {r['needs_reply']} need a reply, {r['flags_raised']} flags, {r['changes_drafted']} drafted changes, "
              f"{r['quarantined']} quarantined · {r['total_tokens']:,} tokens · ${r['cost_usd']:.4f}")

        step("2. Daily brief")
        b = ok(c.get(f"{E}/brief?regenerate=true"))
        print(f"   {b['brief']['headline'] if b.get('brief') else b.get('error')}")

        step("3. One question per tab assistant")
        for tab, q in QUESTIONS:
            a = ok(c.post(f"{E}/assistants/{tab}/chat", json={"message": q}))
            print(f"   [{tab}] {q} -> {a['reply'][:110].replace(chr(10), ' ')}… (${a['usage']['cost_usd']:.4f})")

        step("4. An assistant drafts a change; the organizer confirms it")
        a = ok(c.post(f"{E}/assistants/program/chat", json={"message": "Mark the 'Speaker bios and headshots collected' deadline as done."}))
        drafted = a["changes"]
        print(f"   drafted: {[d['summary'] for d in drafted]}")
        for d in drafted:
            res = ok(c.post(f"{E}/changes/{d['id']}/confirm"))
            print(f"   confirmed -> {res['change']['status']}")

        step("5. Resolve an issue (three agents) on the YBVS presets")
        runs = {}
        for title in ("Flight & Amtrak Domino", "Morning Plenary Overrun vs. Catering SLA",
                      "Red-Team Booby-Trap: Prompt Injection in Speaker Bio Update", "Room 2400 AV Failure + RSVP Surge"):
            r = ok(c.post(f"{E}/crisis/run", json={"crisis_text": presets[title]}))
            runs[title] = r
            tk = r["tokenomics_summary"]
            rec = ((r.get("solver") or {}).get("recommended_option") or {}).get("option_label", "-")
            print(f"   {title}: {r['status']} · {tk['total_tokens']:,} tokens · ${tk['total_cost_usd']:.4f} · {r['total_latency_ms'] / 1000:.0f}s · {rec}")

        step("6. Cost-Saver OFF vs ON on the same issue, before anything is approved (fresh runs, gateway cache bypassed)")
        bench = ok(c.post("/api/admin/tokenomics/benchmark", json={"event_id": YBVS, "use_cached": False}))
        sv, q = bench["savings"], bench["quality"]
        print(f"   tokens {-sv['total_tokens_pct']:+}% · cost {-sv['cost_pct']:+}% · time {-sv['latency_pct']:+}% · {q['verdict']}")

        step("7. Approve the recommended fix for the Room 2400 failure")
        av = runs["Room 2400 AV Failure + RSVP Surge"]
        if av["status"] == "SUCCESS":
            res = ok(c.post(f"{E}/crisis/commit", json={"run_id": av["run_id"], "option_choice": "recommended"}))
            print(f"   {res['option_label']}: {res['applied_changes']}")
        else:
            print(f"   skipped: run status {av['status']}")

        step("8. Attack test (no model calls)")
        rt = ok(c.post("/api/admin/security/red-team-test", json={"event_id": YBVS}))
        for x in rt["results"]:
            print(f"   {'stopped' if x['passed'] else 'NOT STOPPED'}: {x['test']}")

        step("9. Export the evidence")
        bundle = ok(c.get("/api/admin/export-evidence"))
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "stagecraft-evidence.json").write_text(json.dumps(bundle, indent=2, default=str))
        (OUT / "summary.md").write_text(summary_md(bundle, rt, bench))
        print(f"   wrote {OUT / 'stagecraft-evidence.json'} and summary.md")

        if not keep_data:
            db.reset_demo_db()
            print("\nDemo data restored to the clean seed (AI usage, cost, and security logs kept).")
    print(f"\nDone in {time.perf_counter() - t0:.0f}s.")


def summary_md(b: dict, rt: dict, bench: dict) -> str:
    dash, feat = b["tokenomics"]["dashboard"], b["tokenomics"]["cost_by_feature"]
    cr, sec = b["crisis_resolution"], b["security"]
    ws = b["workspace"][YBVS]
    lines = [
        "# Evidence summary",
        "",
        f"Generated {b['generated_at']} by `scripts/rehearse_demo.py` from `stagecraft-evidence.json`. Model: {b['system']['model']} "
        f"(${dash['pricing_per_1m_tokens']['input']} / ${dash['pricing_per_1m_tokens']['cached_input']} cached / "
        f"${dash['pricing_per_1m_tokens']['output']} output per million tokens).",
        "",
        "## Cost per feature (measured, all logged runs)",
        "",
        "| Feature | Uses | AI calls | Tokens | Avg cost per use | Avg time |",
        "|---|---:|---:|---:|---:|---:|",
        *[f"| {k} | {v['uses']} | {v['ai_calls']} | {v['tokens']:,} | ${v['avg_cost_per_use_usd']:.4f} | {v['avg_seconds_per_use']}s |"
          for k, v in feat["measured"].items()],
        "",
        f"Estimated AI bill for one event per month: **${feat['monthly_estimate_one_event']['total_usd']:.2f}** "
        f"({feat['monthly_estimate_one_event']['label']}; usage assumed: "
        + ", ".join(f"{n} {k}" for k, n in feat["monthly_estimate_one_event"]["assumed_usage"].items()) + ").",
        "",
        "## Did cost cutting make it worse? (same issue, Cost-Saver OFF vs ON)",
        "",
        "| | Full context | Cost-Saver |",
        "|---|---:|---:|",
        f"| Tokens | {bench['unoptimized']['total_tokens']:,} | {bench['optimized']['total_tokens']:,} |",
        f"| Cost | ${bench['unoptimized']['cost_usd']:.4f} | ${bench['optimized']['cost_usd']:.4f} |",
        f"| Time | {bench['unoptimized']['latency_ms'] / 1000:.1f}s | {bench['optimized']['latency_ms'] / 1000:.1f}s |",
        f"| Fix chosen | {bench['unoptimized']['recommended_option']} | {bench['optimized']['recommended_option']} |",
        f"| Passed the rule checker | {bench['unoptimized']['layer2_verified']} | {bench['optimized']['layer2_verified']} |",
        "",
        f"Cost-Saver vs full context: {bench['savings']['total_tokens_pct']}% fewer tokens, {bench['savings']['cost_pct']}% lower cost, "
        f"{abs(bench['savings']['latency_pct'])}% {'faster' if bench['savings']['latency_pct'] >= 0 else 'slower'} (time varies run to run). "
        f"Quality: {bench['quality']['verdict']}",
        "",
        "Scale (Resolve an issue, 25 runs per organizer per month):",
        "",
        "| Organizers | Per month | Without Cost-Saver | Saved per year |",
        "|---:|---:|---:|---:|",
        *[f"| {x['users']:,} | ${x['monthly_optimized_usd']:,.2f} | ${x['monthly_unoptimized_usd']:,.2f} | ${x['annual_savings_usd']:,.2f} |" for x in dash["scale"]],
        "",
        "## Resolve an issue (multi-agent hand-off)",
        "",
        f"Runs by outcome: {cr['runs_by_status']}. Successful runs: average {cr['avg_resolution_seconds']}s, median {cr['median_resolution_seconds']}s "
        f"(vs about {cr['manual_baseline_minutes_assumed']} minutes by hand, an assumption).",
        "",
        "## Safety",
        "",
        f"Attack test: {'all ' + str(len(rt['results'])) + ' attacks stopped:' if rt['all_passed'] else 'NOT all stopped'} "
        + "; ".join(f"{x['test']} -> {x['layer'].split(':')[0]}" for x in rt["results"]) + ".",
        "",
        f"Incidents logged: {sec['incident_counts']['total']} ({', '.join(f'{k.split(':')[0]}: {v}' for k, v in sec['incident_counts']['by_layer'].items())}).",
        f"Limits: ${sec['guardrail_config']['max_cost_per_run_usd']} and {sec['guardrail_config']['max_tokens_per_run']:,} tokens per run, "
        f"{sec['guardrail_config']['max_agent_loop_iterations']} steps per agent.",
        "",
        "## Planner (YBVS, during the rehearsal)",
        "",
        f"Inbox: {ws['inbox']['emails']} emails, by category {ws['inbox']['by_category']}; drafted changes {ws['drafted_changes']['by_status']} "
        f"from {ws['drafted_changes']['by_origin']}; assistant questions by tab {ws['assistant_questions_by_tab']}; daily briefs {ws['daily_briefs']}.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    if "--export-only" in sys.argv:
        export_only()
    else:
        main(keep_data="--keep-data" in sys.argv)
