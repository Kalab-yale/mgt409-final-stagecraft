"""Step 7 test: Agent 1 (Triage) -> Agent 2 (Solver) handoff on YBVS Preset 2 (Room 2400 AV failure + 118 RSVPs).

Two live agent runs on gpt-6-luna (well under a cent). Uses a throwaway database so the demo DB is untouched.
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
from backend.agents.common import CrisisInputBlocked  # noqa: E402
from backend.agents.solver_agent import ResolutionPlan, plan_to_changes, run_solver_agent  # noqa: E402
from backend.agents.triage_agent import run_triage_agent  # noqa: E402

YBVS = "evt_ybvs_2026"
AI_PANEL = "ses_ybvs_09"


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def show_plan(name: str, p: ResolutionPlan) -> None:
    rooms = {r.id: r for r in db.get_rooms("ven_evans")}
    print(f"\n  {name}: {p.option_label}")
    print(f"     strategy={p.strategy_type}  score={p.optimization_score}  catering_saved=${p.catering_penalty_saved_usd:,.0f}")
    for m in p.proposed_changes:
        print(f"     MOVE {m.session_title}: {rooms[m.old_room_id].name} {m.old_start_time}-{m.old_end_time} -> "
              f"{rooms[m.new_room_id].name} {m.new_start_time}-{m.new_end_time}  (cap {rooms[m.new_room_id].capacity})")
        print(f"          why: {m.change_rationale}")
    for u in p.room_status_updates:
        print(f"     ROOM {rooms[u.room_id].name} -> {u.new_status}")
    print(f"     disruption: {p.attendee_disruption_minimized_note}")
    print(f"     tradeoffs:  {p.tradeoffs}")


def test_blocked_crisis_never_reaches_agent(run_id: str) -> None:
    header("0. Red-team preset is stopped at Layer 1 before Agent 1 runs")
    text = next(c for c in db.get_crisis_presets(YBVS) if c.category == "Red-Team Prompt Injection").prompt_text
    try:
        run_triage_agent(YBVS, text, run_id)
        blocked = False
    except CrisisInputBlocked as exc:
        blocked = True
        print(f"     {exc.screen['alert']}")
    check("CrisisInputBlocked raised, no tokens spent", blocked and tokenomics.get_workflow_run_metric(run_id).agent_calls == [])


def test_triage(run_id: str):
    header("1. Agent 1 - Triage & Impact Analyst on Preset 2")
    preset = next(c for c in db.get_crisis_presets(YBVS) if c.id == "crisis_ybvs_2")
    print(f"  crisis: {preset.prompt_text}\n")
    a = run_triage_agent(YBVS, preset.prompt_text, run_id, cost_saver=True)
    print(a.model_dump_json(indent=2))
    print()
    check("primary affected session is the AI panel in Room 2400", AI_PANEL in a.primary_affected_session_ids)
    check("Room 2400 listed as affected and reported 'AV Degraded'",
          "rm_evans_2400" in a.affected_room_ids and a.room_status_overrides().get("rm_evans_2400") in ("AV Degraded", "Closed"))
    check("RSVP surge captured (118)", a.attendance_overrides().get(AI_PANEL) == 118)
    check("live New Haven weather/transit was checked", any("weather" in t for t in a.tool_calls_made) and a.live_weather_transit_summary)
    check("tool trace recorded for the UI", len(a.tool_calls_made) >= 2)
    return a


def test_solver(run_id: str, triage):
    header("2. Agent 2 - Constraint Solver (+ Layer 2 verification)")
    out = run_solver_agent(YBVS, triage, run_id, cost_saver=True)
    show_plan("RECOMMENDED", out.recommended_option)
    show_plan("ALTERNATIVE", out.alternative_option)
    print(f"\n  reasoning: {out.solver_reasoning_summary}")
    print(f"  tool calls: {out.tool_calls_made}")
    print(f"  guardrail report: {out.guardrail_report}\n")

    check("deterministic_verification_passed", out.deterministic_verification_passed)
    for name, plan in (("recommended", out.recommended_option), ("alternative", out.alternative_option)):
        res = security.validate_proposed_resolution(YBVS, plan_to_changes(plan, triage))
        reasons = [r for rej in res["rejections"] for r in rej["reasons"]]
        check(f"{name}: independently re-validated by Layer 2 (is_safe, zero capacity violations)",
              res["is_safe"] and not any(r.startswith("Capacity") for r in reasons))
    rec_move = next((m for m in out.recommended_option.proposed_changes if m.session_id == AI_PANEL), None)
    check("recommended plan moves the AI panel out of the failed Room 2400 into a room seating >= 118",
          rec_move is not None and rec_move.new_room_id != "rm_evans_2400" and db.get_room(rec_move.new_room_id).capacity >= 118)
    check("solver used the exact-solver tool", any(t.startswith("propose_optimal_swaps_tool") for t in out.tool_calls_made))
    check("nothing committed yet (DB unchanged until human approval)",
          db.get_session(AI_PANEL).room_id == "rm_evans_2400" and db.get_room("rm_evans_2400").status == "Operational")
    return out


def test_repair_path(triage, real_output) -> None:
    header("2b. Malfunction drill: a hallucinating solver plan is caught and repaired (no LLM call)")
    from backend.agents.solver_agent import ScheduleMutation, _verify_and_repair

    bad = real_output.recommended_option.model_copy(update={
        "option_label": "Option A (hallucinated): squeeze into Nooyi",
        "proposed_changes": [ScheduleMutation(
            session_id=AI_PANEL, session_title="AI Agents", old_room_id="rm_evans_2400", new_room_id="rm_evans_2230",
            old_start_time="14:45", new_start_time="14:45", old_end_time="15:45", new_end_time="15:45",
            change_rationale="Nooyi is free")]})
    ghost = bad.model_copy(update={"option_label": "Option B (hallucinated): Grand Ballroom", "proposed_changes": [
        bad.proposed_changes[0].model_copy(update={"new_room_id": "rm_grand_ballroom_9"})]})
    repaired = _verify_and_repair(real_output.model_copy(update={"recommended_option": bad, "alternative_option": ghost}), triage, YBVS)
    for line in repaired.guardrail_report:
        print(f"     {line}")
    print(f"     -> recommended now: {repaired.recommended_option.option_label}")
    print(f"     -> alternative now: {repaired.alternative_option.option_label}")
    check("118-into-60 overflow and invented room both rejected", "overflow deficit of 58 seats" in repaired.guardrail_report[0]
          and any("rm_grand_ballroom_9" in r for r in repaired.guardrail_report))
    check("recommended replaced by a verified solver option", repaired.deterministic_verification_passed
          and repaired.recommended_option.proposed_changes[0].new_room_id == "rm_evans_zhang")
    check("alternative replaced by a different verified option",
          repaired.alternative_option.proposed_changes[0].new_start_time != repaired.recommended_option.proposed_changes[0].new_start_time)


def test_usage(run_id: str) -> None:
    header("3. Token usage & cost per agent (tokenomics)")
    m = tokenomics.get_workflow_run_metric(run_id)
    for c in m.agent_calls:
        print(f"  {c.agent_name:<12} input={c.input_tokens:>6,} cached={c.cached_input_tokens:>5,} output={c.output_tokens:>5,} "
              f"latency={c.latency_ms:>7,.0f} ms  cost=${c.cost_usd:.6f}  (pruned ~{c.context_tokens_pruned:,} ctx tokens)")
    print(f"  {'RUN TOTAL':<12} tokens={m.total_input_tokens + m.total_output_tokens:,}  cost=${m.total_cost_usd:.6f}  "
          f"latency={m.total_latency_ms:,.0f} ms  baseline(no Cost-Saver)=${m.baseline_unoptimized_cost_usd:.6f}  savings={m.savings_pct}%")
    cfg = security.get_guardrail_config()
    print(f"  budget used: {100 * (m.total_input_tokens + m.total_output_tokens) / cfg.max_tokens_per_run:.0f}% of "
          f"{cfg.max_tokens_per_run:,} tokens, {100 * m.total_cost_usd / cfg.max_cost_per_run_usd:.2f}% of ${cfg.max_cost_per_run_usd}")
    check("both agents metered", [c.agent_name for c in m.agent_calls] == ["TriageAgent", "SolverAgent"])
    check("run stayed inside the Layer 3 budget", m.total_input_tokens + m.total_output_tokens <= cfg.max_tokens_per_run)

    incidents = [i for i in security.get_security_incidents(YBVS) if i.defense_layer == security.L2]
    if incidents:
        print("\n  Layer 2 intercepted model proposals during this run:")
        for i in incidents:
            print(f"     [{i.severity}] {i.blocked_reason[:160]}")


if __name__ == "__main__":
    db.reset_demo_db()
    tokenomics.reset_token_logs()
    security.clear_security_logs()
    run = tokenomics.start_workflow_run(YBVS, "Room 2400 AV Failure + RSVP Surge", cost_saver_enabled=True)
    test_blocked_crisis_never_reaches_agent(run)
    assessment = test_triage(run)
    solver_out = test_solver(run, assessment)
    test_repair_path(assessment, solver_out)
    test_usage(run)
    print("\nPASS - Step 7 verified.")
