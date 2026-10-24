"""Master orchestrator for the Stagecraft war-room desk.

run_crisis_war_room_pipeline():  A. Security gate (Layer 1)  ->  B. Triage  ->  C. Solver (+ Layer 2)  ->  D. Comms
                                 with a Layer 3 budget check after every agent. Nothing is written to the schedule.
commit_resolution_plan():        the human-approved "one click": re-verify (Layer 2), apply to SQLite in one
                                 transaction, record history for the "Updated via Stagecraft War-Room" badge,
                                 and return an .ics calendar file for the moved sessions.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from backend import database as db
from backend import security, tokenomics
from backend.agents.comms_agent import run_comms_agent
from backend.agents.common import BudgetExceeded, CrisisInputBlocked
from backend.agents.solver_agent import ResolutionPlan, run_solver_agent, verify_plan
from backend.agents.triage_agent import DisruptionAssessment, run_triage_agent

RunStatus = Literal["SUCCESS", "BLOCKED_BY_SECURITY_GUARDRAIL", "HALTED_BY_BUDGET_BREAKER", "NEEDS_HUMAN_REVIEW", "FAILED"]


class HandoffStep(BaseModel):
    """One card in the UI's agent hand-off trace."""

    step: str
    agent_name: str
    status: Literal["PASSED", "BLOCKED", "COMPLETED", "HALTED", "FAILED", "SKIPPED"]
    latency_ms: float = 0.0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    tools_called: list[str] = Field(default_factory=list)
    summary: str
    handoff_to: Optional[str] = None
    incident_id: Optional[str] = None


class WarRoomRunResult(BaseModel):
    run_id: str
    event_id: str
    status: RunStatus
    crisis_text: str
    scenario_title: str
    cost_saver_enabled: bool
    started_at: str
    total_latency_ms: float = 0.0
    security_gate: dict = Field(default_factory=dict)
    security_incident: Optional[dict] = None
    triage: Optional[dict] = None
    solver: Optional[dict] = None
    comms: Optional[dict] = None
    agent_handoff_trace: list[HandoffStep] = Field(default_factory=list)
    tokenomics_summary: dict = Field(default_factory=dict)
    error: Optional[str] = None


# ---- Run storage (so a later commit uses exactly what the organizer reviewed) -----------------------

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS war_room_runs (
    run_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, result_json TEXT NOT NULL
);
"""


def _save_run(result: WarRoomRunResult) -> None:
    with db.connect() as conn:
        conn.executescript(RUNS_SCHEMA)
        conn.execute("INSERT OR REPLACE INTO war_room_runs VALUES (?, ?, ?, ?, ?)",
                     (result.run_id, result.event_id, result.status, result.started_at, result.model_dump_json()))


def get_war_room_run(run_id: str) -> dict | None:
    with db.connect() as conn:
        conn.executescript(RUNS_SCHEMA)
        row = conn.execute("SELECT result_json FROM war_room_runs WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row["result_json"]) if row else None


def list_war_room_runs(event_id: str | None = None, limit: int = 20) -> list[dict]:
    with db.connect() as conn:
        conn.executescript(RUNS_SCHEMA)
        sql = "SELECT result_json FROM war_room_runs" + (" WHERE event_id = ?" if event_id else "") + " ORDER BY created_at DESC LIMIT ?"
        rows = conn.execute(sql, (event_id, limit) if event_id else (limit,)).fetchall()
    return [json.loads(r["result_json"]) for r in rows]


# ---- Pipeline --------------------------------------------------------------------------------------


def _agent_card(step: str, run_id: str, agent_name: str, tools: list[str], summary: str, handoff_to: str | None) -> HandoffStep:
    call = next(c for c in reversed(tokenomics.get_workflow_run_metric(run_id).agent_calls) if c.agent_name == agent_name)
    return HandoffStep(step=step, agent_name=agent_name, status="COMPLETED", latency_ms=call.latency_ms,
                       input_tokens=call.input_tokens, cached_input_tokens=call.cached_input_tokens, output_tokens=call.output_tokens,
                       total_tokens=call.total_tokens, cost_usd=call.cost_usd, tools_called=tools, summary=summary, handoff_to=handoff_to)


def _scenario_title(event_id: str, crisis_text: str) -> str:
    preset = next((c for c in db.get_crisis_presets(event_id) if c.prompt_text.strip() == crisis_text.strip()), None)
    return preset.title if preset else (crisis_text.strip().splitlines()[0][:80] or "Ad-hoc crisis")


def _tokenomics_summary(run_id: str) -> dict:
    m = tokenomics.get_workflow_run_metric(run_id)
    cfg = security.get_guardrail_config()
    total = m.total_input_tokens + m.total_output_tokens
    return {
        "total_input_tokens": m.total_input_tokens, "total_cached_tokens": m.total_cached_tokens,
        "total_output_tokens": m.total_output_tokens, "total_tokens": total,
        "total_cost_usd": m.total_cost_usd, "total_agent_latency_ms": m.total_latency_ms,
        "baseline_unoptimized_cost_usd": m.baseline_unoptimized_cost_usd, "savings_pct": m.savings_pct,
        "agent_calls": len(m.agent_calls),
        "budget_used_pct": {"tokens": round(100 * total / cfg.max_tokens_per_run, 1),
                            "cost": round(100 * m.total_cost_usd / cfg.max_cost_per_run_usd, 2)},
        "per_agent": [{"agent": c.agent_name, "tokens": c.total_tokens, "cached": c.cached_input_tokens,
                       "cost_usd": c.cost_usd, "latency_ms": c.latency_ms} for c in m.agent_calls],
    }


def run_crisis_war_room_pipeline(event_id: str, crisis_text: str, cost_saver: Optional[bool] = None) -> dict:
    """Run the whole desk on one crisis. Returns a WarRoomRunResult dict for the UI. Never writes the schedule."""
    saver = tokenomics.get_cost_saver_mode() if cost_saver is None else cost_saver
    started = time.perf_counter()
    title = _scenario_title(event_id, crisis_text)
    run_id = tokenomics.start_workflow_run(event_id, title, cost_saver_enabled=saver)
    res = WarRoomRunResult(run_id=run_id, event_id=event_id, status="FAILED", crisis_text=crisis_text, scenario_title=title,
                           cost_saver_enabled=saver, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def finish(status: RunStatus, error: str | None = None) -> dict:
        res.status, res.error = status, error
        res.total_latency_ms = round((time.perf_counter() - started) * 1000, 1)
        res.tokenomics_summary = _tokenomics_summary(run_id)
        _save_run(res)
        return res.model_dump(mode="json")

    def halt_on_budget(step_no: int, after: str) -> bool:
        guard = security.check_run_budget(run_id, iteration_count=step_no)
        if guard["tripped"]:
            res.agent_handoff_trace.append(HandoffStep(step="!", agent_name="Budget Circuit Breaker (Layer 3)", status="HALTED",
                                                       summary=f"Tripped after {after}: {'; '.join(guard['reasons'])}",
                                                       incident_id=guard["incident_id"]))
        return guard["tripped"]

    # A. Pre-flight security gate --------------------------------------------------------------------
    t = time.perf_counter()
    screen = security.inspect_inbound_input(event_id, crisis_text, source="crisis_feed")
    res.security_gate = screen
    gate = HandoffStep(step="A", agent_name="Security Gate (Layer 1)", latency_ms=round((time.perf_counter() - t) * 1000, 1),
                       status="PASSED" if screen["allowed"] else "BLOCKED", summary=screen["alert"],
                       tools_called=[f"inspect_inbound_input -> {len(screen['findings'])} pattern(s)"],
                       handoff_to="TriageAgent" if screen["allowed"] else None, incident_id=screen.get("incident_id"))
    res.agent_handoff_trace.append(gate)
    if not screen["allowed"]:
        incident = next((i for i in security.get_security_incidents(event_id, limit=50) if i.incident_id == screen["incident_id"]), None)
        res.security_incident = incident.model_dump() if incident else None
        return finish("BLOCKED_BY_SECURITY_GUARDRAIL", "Crisis text blocked by the Layer 1 firewall before any agent ran.")

    try:
        # B. Triage ------------------------------------------------------------------------------------
        triage = run_triage_agent(event_id, crisis_text, run_id, cost_saver=saver)
        res.triage = triage.model_dump(mode="json")
        res.agent_handoff_trace.append(_agent_card(
            "B", run_id, "TriageAgent", triage.tool_calls_made, handoff_to="SolverAgent",
            summary=f"{triage.disruption_category} | {triage.severity} | {len(triage.primary_affected_session_ids)} primary, "
                    f"{len(triage.cascading_affected_session_ids)} cascading session(s) | delay {triage.estimated_delay_mins} min"))
        if halt_on_budget(1, "Triage"):
            return finish("HALTED_BY_BUDGET_BREAKER", "Run budget exceeded after Triage.")

        # C. Solver ------------------------------------------------------------------------------------
        solver = run_solver_agent(event_id, triage, run_id, cost_saver=saver)
        res.solver = solver.model_dump(mode="json")
        rec = solver.recommended_option
        res.agent_handoff_trace.append(_agent_card(
            "C", run_id, "SolverAgent", solver.tool_calls_made, handoff_to="CommsAgent" if solver.deterministic_verification_passed else None,
            summary=f"Recommended: {rec.option_label} (score {rec.optimization_score}) | Layer 2 verified: "
                    f"{'YES' if solver.deterministic_verification_passed else 'NO'}"))
        if halt_on_budget(2, "Solver"):
            return finish("HALTED_BY_BUDGET_BREAKER", "Run budget exceeded after Solver.")
        if not solver.deterministic_verification_passed:
            return finish("NEEDS_HUMAN_REVIEW", "No resolution passed the Layer 2 action lock; comms not drafted.")

        # D. Comms -------------------------------------------------------------------------------------
        comms = run_comms_agent(event_id, triage, rec, run_id, cost_saver=saver)
        res.comms = comms.model_dump(mode="json")
        res.agent_handoff_trace.append(_agent_card(
            "D", run_id, "CommsAgent", [f"outbound_pii_scrubber -> {comms.scrubber_redactions} redaction(s)"], handoff_to="Organizer (approve & commit)",
            summary=f"{len(comms.speaker_and_moderator_messages)} notifications, {len(comms.volunteer_action_checklist)} volunteer tasks drafted"))
        if halt_on_budget(3, "Comms"):
            return finish("HALTED_BY_BUDGET_BREAKER", "Run budget exceeded after Comms.")
        return finish("SUCCESS")

    except BudgetExceeded as exc:
        res.agent_handoff_trace.append(HandoffStep(step="!", agent_name="Budget Circuit Breaker (Layer 3)", status="HALTED",
                                                   summary=str(exc), incident_id=exc.incident_id))
        return finish("HALTED_BY_BUDGET_BREAKER", str(exc))
    except CrisisInputBlocked as exc:
        return finish("BLOCKED_BY_SECURITY_GUARDRAIL", str(exc))
    except Exception as exc:  # model/API failure: report it to the UI instead of crashing the war-room
        res.agent_handoff_trace.append(HandoffStep(step="!", agent_name="Orchestrator", status="FAILED", summary=f"{type(exc).__name__}: {exc}"))
        return finish("FAILED", f"{type(exc).__name__}: {exc}")


# ---- Commit + .ics -----------------------------------------------------------------------------------


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line: str) -> list[str]:
    """RFC 5545: lines longer than 75 octets continue on the next line, which starts with a space."""
    out, current = [], ""
    for ch in line:
        limit = 75 if not out else 74
        if len((current + ch).encode("utf-8")) > limit:
            out.append(current)
            current = ch
        else:
            current += ch
    return [out[0], *(" " + c for c in out[1:]), (" " if out else "") + current] if out else [current]


def build_ics(event_id: str, plan: ResolutionPlan, sequence: int = 1) -> str:
    """iCalendar file with one VEVENT per moved session. Times are floating local time at the venue;
    the UID is stable per session so calendar apps update the existing entry (SEQUENCE increases)."""
    state = db.get_event_full_state(event_id)
    rooms = {r.id: r for r in state.rooms}
    day = state.event.date.strftime("%Y%m%d")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Stagecraft AI//War-Room//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_ics_escape(state.event.name)} - schedule updates"]
    for m in plan.proposed_changes:
        old, new = rooms[m.old_room_id], rooms[m.new_room_id]
        lines += [
            "BEGIN:VEVENT",
            f"UID:{m.session_id}@{state.event.slug}.stagecraft.example",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{day}T{m.new_start_time.replace(':', '')}00",
            f"DTEND:{day}T{m.new_end_time.replace(':', '')}00",
            f"SEQUENCE:{sequence}",
            f"SUMMARY:{_ics_escape(m.session_title)}",
            f"LOCATION:{_ics_escape(f'{new.name} ({new.floor_or_zone}), {state.venue.name}, {state.venue.city}')}",
            f"DESCRIPTION:{_ics_escape(f'Updated via Stagecraft War-Room. Moved from {old.name} {m.old_start_time}-{m.old_end_time} to {new.name} {m.new_start_time}-{m.new_end_time}.')}",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for line in lines for part in _ics_fold(line)) + "\r\n"


def build_event_calendar(event_id: str, only_updated: bool = False) -> str:
    """iCalendar for the event's CURRENT schedule (or only war-room-updated sessions). Same UIDs as `build_ics`,
    with SEQUENCE = number of war-room changes, so re-importing updates existing calendar entries."""
    state = db.get_event_full_state(event_id)
    rooms = {r.id: r for r in state.rooms}
    speakers = {sp.id: sp.name for sp in state.speakers}
    history = db.get_resolution_history(event_id)
    changes: dict[str, list[dict]] = {}
    for entry in reversed(history):
        for c in entry["changes"]:
            changes.setdefault(c["session_id"], []).append(c)
    day = state.event.date.strftime("%Y%m%d")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Stagecraft AI//War-Room//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_ics_escape(state.event.name)}"]
    for s in state.sessions:
        updates = changes.get(s.id, [])
        if only_updated and not updates:
            continue
        room = rooms.get(s.room_id)
        who = ", ".join(speakers[i] for i in s.speaker_ids if i in speakers)
        note = f"Updated via Stagecraft War-Room: moved from {updates[-1]['from']} to {updates[-1]['to']}. " if updates else ""
        lines += [
            "BEGIN:VEVENT",
            f"UID:{s.id}@{state.event.slug}.stagecraft.example",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{day}T{s.start_time.replace(':', '')}00",
            f"DTEND:{day}T{s.end_time.replace(':', '')}00",
            f"SEQUENCE:{len(updates)}",
            f"SUMMARY:{_ics_escape(s.title)}",
            f"LOCATION:{_ics_escape(f'{room.name} ({room.floor_or_zone}), {state.venue.name}, {state.venue.city}' if room else state.venue.name)}",
            f"DESCRIPTION:{_ics_escape(note + s.track + (' · Speakers: ' + who if who else ''))}",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for line in lines for part in _ics_fold(line)) + "\r\n"


def commit_resolution_plan(event_id: str, run_id: str, option_choice: str = "recommended", custom_plan: Optional[dict] = None) -> dict:
    """Apply a reviewed plan: stale check -> Layer 2 re-verification -> one SQLite transaction -> history -> .ics."""
    run = get_war_room_run(run_id)
    if run is None or run["event_id"] != event_id:
        return {"ok": False, "errors": [f"Run '{run_id}' not found for event '{event_id}'."]}
    if db.run_already_committed(run_id):
        return {"ok": False, "errors": [f"Run '{run_id}' was already committed."]}
    if run["triage"] is None or (run["status"] != "SUCCESS" and custom_plan is None):
        return {"ok": False, "errors": [f"Run status is {run['status']}; only a SUCCESS run (or an explicit custom_plan) can be committed."]}
    if option_choice not in ("recommended", "alternative") and custom_plan is None:
        return {"ok": False, "errors": ["option_choice must be 'recommended' or 'alternative'."]}
    triage = DisruptionAssessment.model_validate(run["triage"])
    try:
        plan = ResolutionPlan.model_validate(custom_plan if custom_plan is not None else run["solver"][f"{option_choice}_option"])
    except ValidationError as exc:
        return {"ok": False, "errors": [f"Malformed plan: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}"]}

    # Stale check: the schedule must still look the way it did when the plan was made.
    stale = []
    for m in plan.proposed_changes:
        s = db.get_session(m.session_id)
        if s is None or s.event_id != event_id:
            stale.append(f"Session '{m.session_id}' is not in this event.")
        elif (s.room_id, s.start_time, s.end_time) != (m.old_room_id, m.old_start_time, m.old_end_time):
            stale.append(f"'{s.title}' changed since this plan was made (now {s.room_id} {s.start_time}-{s.end_time}).")
    if stale:
        return {"ok": False, "errors": stale}
    ok, reasons = verify_plan(plan, triage, event_id)
    if not ok:
        return {"ok": False, "errors": [f"Layer 2 rejected the plan: {r}" for r in reasons]}

    rooms = {r.id: r for r in db.get_rooms(db.get_event(event_id).venue_id)}
    statuses = triage.room_status_overrides() | {u.room_id: u.new_status for u in plan.room_status_updates}
    attendance = triage.attendance_overrides()
    db.apply_resolution(
        [{"session_id": m.session_id, "room_id": m.new_room_id, "start_time": m.new_start_time, "end_time": m.new_end_time}
         for m in plan.proposed_changes], statuses, attendance)

    changes = [{"session_id": m.session_id, "title": m.session_title,
                "from": f"{rooms[m.old_room_id].name} {m.old_start_time}-{m.old_end_time}",
                "to": f"{rooms[m.new_room_id].name} {m.new_start_time}-{m.new_end_time}"} for m in plan.proposed_changes]
    prior = sum(1 for h in db.get_resolution_history(event_id) for c in h["changes"] if c["session_id"] in {m.session_id for m in plan.proposed_changes})
    ics = build_ics(event_id, plan, sequence=prior + 1)
    comms_matches = custom_plan is None and option_choice == "recommended" and run["comms"] is not None
    comms = run["comms"] if comms_matches else {}
    history_id = db.record_resolution(event_id, run_id, plan.option_label, plan.strategy_type, changes,
                                      comms.get("public_attendee_banner_alert", ""), comms.get("executive_brief", ""), ics)
    return {
        "ok": True, "event_id": event_id, "run_id": run_id, "history_id": history_id,
        "option_choice": "custom" if custom_plan is not None else option_choice, "option_label": plan.option_label,
        "applied_changes": changes, "room_status_updates": statuses, "attendance_updates": attendance,
        "ics": ics, "ics_filename": f"{db.get_event(event_id).slug}-update-{history_id}.ics",
        "comms_match_committed_plan": comms_matches, "errors": [],
    }
