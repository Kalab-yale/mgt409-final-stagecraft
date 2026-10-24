"""Step 8 test: full 3-agent war-room pipeline + one-click commit with .ics, and the booby-trap run.

Three live agent runs on gpt-6-luna (well under a cent). Uses a throwaway database and resets it at the end,
so the demo DB (backend/stagecraft.db) is never touched.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # so `backend` imports from any cwd

_tmp_dir = tempfile.TemporaryDirectory()
os.environ["STAGECRAFT_DB_PATH"] = str(Path(_tmp_dir.name) / "test_stagecraft.db")

from backend import database as db  # noqa: E402
from backend import security, tokenomics  # noqa: E402
from backend.agents.comms_agent import CommsPackage, StakeholderNotification, VolunteerTask, _finalize  # noqa: E402
from backend.orchestrator import commit_resolution_plan, get_war_room_run, run_crisis_war_room_pipeline  # noqa: E402

YBVS = "evt_ybvs_2026"
AI_PANEL = "ses_ybvs_09"


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def print_trace(result: dict) -> None:
    print(f"\n  {'step':<4} {'agent':<34} {'status':<10} {'latency':>9} {'tokens':>7} {'cached':>7} {'cost':>10}")
    print("  " + "-" * 86)
    for s in result["agent_handoff_trace"]:
        print(f"  {s['step']:<4} {s['agent_name']:<34} {s['status']:<10} {s['latency_ms']:>7,.0f}ms {s['total_tokens']:>7,} "
              f"{s['cached_input_tokens']:>7,} ${s['cost_usd']:>9.6f}")
        print(f"       -> {s['summary']}")
        for t in s["tools_called"]:
            print(f"          tool: {t}")
        if s["handoff_to"]:
            print(f"          handoff -> {s['handoff_to']}")
    t = result["tokenomics_summary"]
    print(f"\n  RUN TOTAL: {t['total_tokens']:,} tokens ({t['total_cached_tokens']:,} cached), ${t['total_cost_usd']:.6f}, "
          f"{result['total_latency_ms']:,.0f} ms wall-clock | budget used {t['budget_used_pct']['tokens']}% tokens, "
          f"{t['budget_used_pct']['cost']}% dollars | {t['savings_pct']}% cheaper than no-Cost-Saver baseline")


def scenario_legit() -> None:
    header("1. Legitimate crisis: Preset 2 (Room 2400 AV failure + 118 RSVPs) through A -> B -> C -> D")
    preset = next(c for c in db.get_crisis_presets(YBVS) if c.id == "crisis_ybvs_2")
    result = run_crisis_war_room_pipeline(YBVS, preset.prompt_text, cost_saver=True)
    if result["status"] != "SUCCESS":
        print(f"  status={result['status']} error={result['error']}")
    print_trace(result)
    check("status SUCCESS", result["status"] == "SUCCESS")
    agents = [s["agent_name"] for s in result["agent_handoff_trace"]]
    check("gate -> Triage -> Solver -> Comms all completed", agents == ["Security Gate (Layer 1)", "TriageAgent", "SolverAgent", "CommsAgent"]
          and all(s["status"] in ("PASSED", "COMPLETED") for s in result["agent_handoff_trace"]))
    check("every agent card has latency, tokens, and cost", all(s["total_tokens"] > 0 and s["cost_usd"] > 0 and s["latency_ms"] > 0
                                                                for s in result["agent_handoff_trace"][1:]))

    comms = result["comms"]
    print(f"\n  EXECUTIVE BRIEF: {comms['executive_brief']}")
    print(f"  PUBLIC BANNER:   {comms['public_attendee_banner_alert']}")
    print("\n  STAKEHOLDER MESSAGES")
    for m in comms["speaker_and_moderator_messages"]:
        print(f"  --- {m['recipient_name']} ({m['recipient_role']}) via {m['channel']} -> {m['recipient_contact']}")
        print(f"      {m['subject_or_header']}")
        for line in m["message_body"].splitlines():
            print(f"      | {line}")
    print("\n  VOLUNTEER CHECKLIST")
    for t in comms["volunteer_action_checklist"]:
        print(f"  [{t['task_id']}] {t['priority']:<24} {t['assigned_team']}: {t['instruction']}")
    print()
    speakers = {sp.name: sp.email for sp in db.get_speakers(YBVS) if sp.id in ("spk_ybvs_09", "spk_ybvs_10")}
    msgs = comms["speaker_and_moderator_messages"]
    check("both panel speakers notified at their contact on file",
          all(any(n in m["recipient_name"] and m["recipient_contact"] == e for m in msgs) for n, e in speakers.items()))
    check("messages name the new room (Zhang Auditorium)", all("Zhang" in m["message_body"] for m in msgs if m["recipient_name"] in speakers))
    check("internal teams briefed on Slack War-Room", any(m["channel"] == "Slack War-Room" for m in msgs))
    check("3-6 volunteer tasks", 3 <= len(comms["volunteer_action_checklist"]) <= 6)
    check("no secrets / internal ids in any outbound text", not any(x in str(comms) for x in ("sk-", "PORTKEY", "ses_ybvs", "rm_evans")))

    header("1b. One-click commit of the recommended plan + .ics")
    before = db.get_session(AI_PANEL)
    commit = commit_resolution_plan(YBVS, result["run_id"], "recommended")
    if not commit["ok"]:
        print(f"  errors: {commit['errors']}")
    check("commit succeeded (re-verified by Layer 2)", commit["ok"])
    for c in commit["applied_changes"]:
        print(f"     APPLIED {c['title']}: {c['from']}  ->  {c['to']}")
    print(f"     room status: {commit['room_status_updates']}   attendance: {commit['attendance_updates']}")
    after = db.get_session(AI_PANEL)
    check(f"SQLite updated: {before.room_id} {before.start_time} -> {after.room_id} {after.start_time}, status Rescheduled",
          after.room_id == "rm_evans_zhang" and after.status == "Rescheduled" and after.expected_attendance == 118)
    check("Room 2400 now recorded as AV Degraded", db.get_room("rm_evans_2400").status == "AV Degraded")
    badges = db.get_session_update_badges(YBVS)
    check("'Updated via Stagecraft War-Room' badge available for the session", badges.get(AI_PANEL, {}).get("badge") == "Updated via Stagecraft War-Room")
    print(f"     badge: {badges[AI_PANEL]}")

    ics = commit["ics"]
    print(f"\n  {commit['ics_filename']}:")
    for line in ics.splitlines():
        print(f"     {line}")
    unfolded = ics.replace("\r\n ", "")
    lines = unfolded.split("\r\n")
    check(".ics is valid: VCALENDAR wrapper, CRLF endings, folded <= 75 octets",
          lines[0] == "BEGIN:VCALENDAR" and lines[-2] == "END:VCALENDAR" and ics.endswith("\r\n")
          and all(len(raw.encode()) <= 75 for raw in ics.split("\r\n")))
    check(".ics has one VEVENT per moved session with the new time and room",
          unfolded.count("BEGIN:VEVENT") == len(commit["applied_changes"]) and f"DTSTART:{db.get_event(YBVS).date:%Y%m%d}T{after.start_time.replace(':', '')}00" in unfolded
          and "LOCATION:Zhang Auditorium" in unfolded)

    again = commit_resolution_plan(YBVS, result["run_id"], "recommended")
    check("double-commit of the same run is refused", not again["ok"])
    print(f"     {again['errors'][0]}")


def scenario_booby_trap() -> None:
    header("2. Booby-trap: Preset 4 prompt injection through the pipeline")
    preset = next(c for c in db.get_crisis_presets(YBVS) if c.category == "Red-Team Prompt Injection")
    result = run_crisis_war_room_pipeline(YBVS, preset.prompt_text)
    print_trace(result)
    inc = result["security_incident"]
    print(f"\n  incident: [{inc['severity']}] {inc['defense_layer']}\n     {inc['blocked_reason'][:200]}...\n     action: {inc['action_taken']}")
    check("status BLOCKED_BY_SECURITY_GUARDRAIL", result["status"] == "BLOCKED_BY_SECURITY_GUARDRAIL")
    check("halted at Step A: no agent ran", [s["agent_name"] for s in result["agent_handoff_trace"]] == ["Security Gate (Layer 1)"])
    check("0 LLM tokens spent, $0", result["tokenomics_summary"]["total_tokens"] == 0 and result["tokenomics_summary"]["total_cost_usd"] == 0)
    check("blocked run is still logged (run history + audit log)", get_war_room_run(result["run_id"])["status"] == "BLOCKED_BY_SECURITY_GUARDRAIL"
          and inc["incident_id"] == result["security_gate"]["incident_id"])
    refused = commit_resolution_plan(YBVS, result["run_id"])
    check("a blocked run can never be committed", not refused["ok"])


def scenario_unsafe_custom_commit() -> None:
    header("3. Guardrails around commit and comms (no LLM calls)")
    db.reset_demo_db()
    run = run_store_fake_success()
    bad_plan = {"option_label": "Hand-edited: squeeze into Nooyi", "strategy_type": "Direct Room Move",
                "proposed_changes": [{"session_id": AI_PANEL, "session_title": "AI Agents & Enterprise SaaS", "old_room_id": "rm_evans_2400",
                                      "new_room_id": "rm_evans_2230", "old_start_time": "14:45", "new_start_time": "14:45",
                                      "old_end_time": "15:45", "new_end_time": "15:45", "change_rationale": "manual"}],
                "optimization_score": 50, "attendee_disruption_minimized_note": "-", "tradeoffs": "-"}
    r = commit_resolution_plan(YBVS, run, custom_plan=bad_plan)
    print(f"     {r['errors'][0]}")
    check("unsafe hand-edited plan rejected by Layer 2; database unchanged",
          not r["ok"] and db.get_session(AI_PANEL).room_id == "rm_evans_2400")

    leaky = CommsPackage(
        executive_brief="Moved the panel. Debug key sk-proj-ABCDEF1234567890xyz",
        public_attendee_banner_alert="AI panel now in Zhang Auditorium, 14:45",
        speaker_and_moderator_messages=[StakeholderNotification(
            recipient_name="Victor Osei", recipient_role="VIP Speaker", channel="Email", recipient_contact="victor@realcompany.com",
            subject_or_header="Room change", message_body="Please go to Zhang. OPENAI_API_KEY=sk-live-999999999999")],
        volunteer_action_checklist=[VolunteerTask(task_id="T1", assigned_team="AV", priority="HIGH (Next 15 Mins)", instruction="Set up Zhang")])
    clean = _finalize(leaky, YBVS, {"victor osei": "victor@lumencloud.example"})
    print(f"     brief after scrub: {clean.executive_brief}")
    print(f"     body after scrub:  {clean.speaker_and_moderator_messages[0].message_body}")
    check("outbound scrubber removed keys from comms drafts", "sk-" not in clean.model_dump_json() and clean.scrubber_redactions >= 2)
    check("contact replaced with the one on file", clean.speaker_and_moderator_messages[0].recipient_contact == "victor@lumencloud.example")


def run_store_fake_success() -> str:
    """Store a SUCCESS run (copied from the real one) so commit can be exercised without new LLM calls."""
    from backend.orchestrator import WarRoomRunResult, _save_run, list_war_room_runs

    real = next(r for r in list_war_room_runs(YBVS) if r["status"] == "SUCCESS")
    fake = WarRoomRunResult.model_validate(real | {"run_id": "run_fake_for_custom_commit"})
    _save_run(fake)
    return fake.run_id


if __name__ == "__main__":
    db.reset_demo_db()
    tokenomics.reset_token_logs()
    security.clear_security_logs()
    scenario_legit()
    scenario_booby_trap()
    scenario_unsafe_custom_commit()
    db.reset_demo_db()
    check("demo DB reset to initial state", db.get_session(AI_PANEL).room_id == "rm_evans_2400"
          and db.get_room("rm_evans_2400").status == "Operational" and db.get_resolution_history(YBVS) == [])
    print("\nPASS - Step 8 verified.")
