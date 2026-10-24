"""Agent 2 - Constraint Solver & Re-Scheduler.

Takes the Triage assessment, asks the exact solver for options, and returns two resolution plans.
Then the code - not the model - has the final word: both plans go through the Layer 2 action lock
(`validate_proposed_resolution`). If the recommended plan fails, or doesn't actually fix the disruption,
it is replaced with the solver's best verified option, so `recommended_option` is always conflict-free.
Nothing is written to the database here; committing happens later, after human approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from backend import database as db
from backend import security, tokenomics, tools
from backend.agents.common import guarded_run, id_humanizer
from backend.agents.triage_agent import DisruptionAssessment
from backend.config import get_agent_settings, get_pydantic_ai_model
from backend.models import HHMM, RoomStatus

AGENT_NAME = "SolverAgent"
StrategyType = Literal["Direct Room Move", "Two-Way Room Swap", "Time Slot Swap", "Schedule Compression & Buffer Trim"]


# ---- Structured output -------------------------------------------------------------------------


class ScheduleMutation(BaseModel):
    session_id: str
    session_title: str
    old_room_id: str
    new_room_id: str
    old_start_time: HHMM
    new_start_time: HHMM
    old_end_time: HHMM
    new_end_time: HHMM
    change_rationale: str


class RoomStatusChange(BaseModel):
    room_id: str
    new_status: RoomStatus
    reason: str = ""


class ResolutionPlan(BaseModel):
    option_label: str = Field(description="e.g. 'Option A: Direct Move to Zhang Auditorium (same 14:45 slot)'")
    strategy_type: StrategyType
    proposed_changes: list[ScheduleMutation] = Field(description="only sessions whose room or time changes")
    room_status_updates: list[RoomStatusChange] = Field(default_factory=list)
    optimization_score: int = Field(ge=0, le=100)
    catering_penalty_saved_usd: float = Field(default=0.0, ge=0)
    attendee_disruption_minimized_note: str
    tradeoffs: str


class SolverOutput(BaseModel):
    recommended_option: ResolutionPlan
    alternative_option: ResolutionPlan
    solver_reasoning_summary: str
    deterministic_verification_passed: bool = Field(default=False, description="set by the system, not the model")
    alternative_verified: bool = Field(default=False, description="set by the system: alternative passed Layer 2")
    tool_calls_made: list[str] = Field(default_factory=list, description="filled in by the system")
    guardrail_report: list[str] = Field(default_factory=list, description="filled in by the system")


# ---- Agent ---------------------------------------------------------------------------------------


@dataclass
class SolverDeps:
    event_id: str
    triage: DisruptionAssessment
    tool_log: list[str] = field(default_factory=list)
    searches_done: set[tuple] = field(default_factory=set)

    @property
    def flagged_sessions(self) -> list[str]:
        return self.triage.primary_affected_session_ids + self.triage.cascading_affected_session_ids

    @property
    def overrides(self) -> dict:
        return {"speaker_eta_overrides": self.triage.speaker_etas() or None,
                "room_status_overrides": self.triage.room_status_overrides() or None}


INSTRUCTIONS = """You are Agent 2, the Constraint Solver in the Stagecraft conference war-room.
You receive Agent 1's triage assessment and the event context. Your tools already know the triage facts
(RSVP surges, broken rooms, speaker ETAs) and are scoped to this event.

Process:
1. Call propose_optimal_swaps once for each session Triage flagged (primary first, then cascading if it must move) -
   it only accepts those sessions. Its options come from an exact solver (capacity, room status, AV equivalents,
   double-booking, speaker overlaps and buffers) - prefer them. Two-way swap partners are found for you.
2. Return two materially different plans: recommended_option (best) and alternative_option (a different room,
   time, or strategy). Check any change you invent yourself with check_hard_constraints before using it.
3. If the crisis delays the programme, call calculate_catering_sla_impact and prefer plans that protect catering
   cutoffs; catering_penalty_saved_usd = penalty the plan avoids (0 if none was at risk).

Rules:
- proposed_changes lists only sessions whose room or time changes; use exact ids, room ids and HH:MM times from the
  tools/context; old_* fields are the session's current values. A two-way swap lists both sessions.
- room_status_updates: record each broken room from the triage (e.g. the failed room -> "AV Degraded").
- Never move or cancel a locked keynote. Never invent ids.
- optimization_score: the tool's score for that option (0-100).
"""


@lru_cache(maxsize=1)
def build_solver_agent() -> Agent[SolverDeps, SolverOutput]:
    agent = Agent(
        get_pydantic_ai_model(),
        name=AGENT_NAME,
        deps_type=SolverDeps,
        output_type=SolverOutput,
        instructions=INSTRUCTIONS,
        model_settings=get_agent_settings(max_tokens=3000),
        retries=2,
    )

    @agent.tool
    def propose_optimal_swaps(ctx: RunContext[SolverDeps], disrupted_session_id: str, min_start_time: str = "", reason: str = "") -> list[dict]:
        """Exact solver: top 3 feasible Direct Moves / Two-Way Swaps for a session Triage flagged, best first.
        min_start_time HH:MM optional. Sessions Triage did not flag are refused."""
        d = ctx.deps
        if disrupted_session_id not in d.flagged_sessions:
            d.tool_log.append(f"propose_optimal_swaps_tool({disrupted_session_id}) -> skipped (not flagged by Triage)")
            return [{"note": f"Only sessions flagged by Triage can be re-solved: {d.flagged_sessions}. Leave other sessions where they are."}]
        key = (disrupted_session_id, min_start_time)
        if key in d.searches_done:
            d.tool_log.append(f"propose_optimal_swaps_tool({disrupted_session_id}) -> skipped (duplicate search)")
            return [{"note": "Duplicate search - reuse the options you already have."}]
        d.searches_done.add(key)
        out = tools.propose_optimal_swaps_tool(
            d.event_id, disrupted_session_id, min_start_time or (d.triage.earliest_feasible_start_time or ""), reason,
            expected_attendance_override=d.triage.attendance_overrides().get(disrupted_session_id), **d.overrides)
        best = out[0] if out and "error" not in out[0] else None
        d.tool_log.append(f"propose_optimal_swaps_tool({disrupted_session_id}) -> {len(out)} option(s)"
                          + (f", best: {best['option_type']} score {best['score']}" if best else ""))
        return out

    @agent.tool
    def check_hard_constraints(ctx: RunContext[SolverDeps], session_id: str, target_room_id: str, new_start_time: str, new_end_time: str) -> dict:
        """Would moving a session to (room, start-end) break capacity, room status, AV, double-booking, speaker, or keynote rules?"""
        d = ctx.deps
        out = tools.check_hard_constraints_tool(d.event_id, session_id, target_room_id, new_start_time, new_end_time,
                                                expected_attendance_override=d.triage.attendance_overrides().get(session_id), **d.overrides)
        d.tool_log.append(f"check_hard_constraints_tool({session_id} -> {target_room_id} {new_start_time}-{new_end_time}) -> "
                          + ("error" if "error" in out else f"valid={out['is_valid']} score={out['score']}"))
        return out

    @agent.tool
    def calculate_catering_sla_impact(ctx: RunContext[SolverDeps], proposed_schedule_shift_mins: int, affected_time_window: str = "12:00-13:30") -> dict:
        """Penalty exposure if the programme slides by N minutes, plus a recovery plan."""
        out = tools.calculate_catering_sla_impact_tool(ctx.deps.event_id, proposed_schedule_shift_mins, affected_time_window)
        ctx.deps.tool_log.append(f"calculate_catering_sla_impact_tool(shift={proposed_schedule_shift_mins}) -> ${out.get('total_penalty_usd', 0):,.0f} penalty")
        return out

    return agent


# ---- Deterministic verification & repair --------------------------------------------------------


def _ground_truth(plan: ResolutionPlan, event_id: str) -> ResolutionPlan:
    """Overwrite 'old_*' fields and titles from the database (never trust the model's copy) and drop no-ops."""
    sessions = {s.id: s for s in db.get_sessions(event_id)}
    fixed = []
    for m in plan.proposed_changes:
        s = sessions.get(m.session_id)
        if s is not None:
            m = m.model_copy(update={"session_title": s.title, "old_room_id": s.room_id,
                                     "old_start_time": s.start_time, "old_end_time": s.end_time})
            if (m.new_room_id, m.new_start_time, m.new_end_time) == (s.room_id, s.start_time, s.end_time):
                continue
        fixed.append(m)
    # Display text is for people: swap any internal ids the model used for real names.
    humanize = id_humanizer(event_id)
    fixed = [m.model_copy(update={"change_rationale": humanize(m.change_rationale)}) for m in fixed]
    return plan.model_copy(update={"proposed_changes": fixed, "option_label": humanize(plan.option_label),
                                   "attendee_disruption_minimized_note": humanize(plan.attendee_disruption_minimized_note),
                                   "tradeoffs": humanize(plan.tradeoffs)})


def plan_to_changes(plan: ResolutionPlan, triage: DisruptionAssessment) -> list[dict]:
    """Layer 2 change list for a plan. Triage room failures are always included as facts."""
    attendance = triage.attendance_overrides()
    changes = [{"action": "move_session", "session_id": m.session_id, "room_id": m.new_room_id,
                "new_start_time": m.new_start_time, "new_end_time": m.new_end_time,
                "expected_attendance": attendance.get(m.session_id), "speaker_eta": triage.speaker_etas() or None}
               for m in plan.proposed_changes]
    statuses = triage.room_status_overrides() | {u.room_id: u.new_status for u in plan.room_status_updates}
    return changes + [{"action": "update_room_status", "room_id": rid, "status": st} for rid, st in statuses.items()]


def _unresolved(plan: ResolutionPlan, triage: DisruptionAssessment, event_id: str) -> list[str]:
    """A plan that leaves a primary session where it is must leave it somewhere that now works."""
    moved = {m.session_id for m in plan.proposed_changes}
    problems = []
    for sid in triage.primary_affected_session_ids:
        if sid in moved:
            continue
        s = db.get_session(sid)
        check = tools.check_hard_constraints_tool(
            event_id, sid, s.room_id, s.start_time, s.end_time,
            expected_attendance_override=triage.attendance_overrides().get(sid),
            speaker_eta_overrides=triage.speaker_etas() or None, room_status_overrides=triage.room_status_overrides() or None)
        if not check.get("is_valid", False):
            problems.append(f"'{s.title}' is left in place but its current slot still fails: {check['violations'][0]}")
    return problems


def verify_plan(plan: ResolutionPlan, triage: DisruptionAssessment, event_id: str) -> tuple[bool, list[str]]:
    result = security.validate_proposed_resolution(event_id, plan_to_changes(plan, triage))
    reasons = [r for rej in result["rejections"] for r in rej["reasons"]] + _unresolved(plan, triage, event_id)
    return not reasons, reasons


def _deterministic_plans(triage: DisruptionAssessment, event_id: str) -> list[ResolutionPlan]:
    """Fallback plans straight from the exact solver for the first primary session that needs to move."""
    sessions = {s.id: s for s in db.get_sessions(event_id)}
    status_updates = [RoomStatusChange(room_id=r, new_status=st, reason="Reported in crisis triage")
                      for r, st in triage.room_status_overrides().items()]
    for sid in triage.primary_affected_session_ids:
        options = tools.propose_optimal_swaps_tool(
            event_id, sid, triage.earliest_feasible_start_time or "", "deterministic fallback",
            expected_attendance_override=triage.attendance_overrides().get(sid),
            speaker_eta_overrides=triage.speaker_etas() or None, room_status_overrides=triage.room_status_overrides() or None)
        options = [o for o in options if "error" not in o]
        if not options:
            continue
        plans = []
        for o in options:
            muts = []
            for mv in o["moves"]:
                s = sessions[mv["session_id"]]
                start, end = mv["to_time"].split("-")
                muts.append(ScheduleMutation(session_id=s.id, session_title=s.title, old_room_id=s.room_id, new_room_id=mv["to_room_id"],
                                             old_start_time=s.start_time, new_start_time=start, old_end_time=s.end_time, new_end_time=end,
                                             change_rationale=o["explanation"]))
            same_times = all(m.new_start_time == m.old_start_time for m in muts)
            strategy = "Direct Room Move" if o["option_type"] == "Direct Move" else "Two-Way Room Swap" if same_times else "Time Slot Swap"
            target = o["moves"][0]
            plans.append(ResolutionPlan(
                option_label=f"Solver Option {o['rank']}: {o['option_type']} to {target['to_room']} ({target['to_time']})",
                strategy_type=strategy, proposed_changes=muts, room_status_updates=status_updates,
                optimization_score=o["score"], catering_penalty_saved_usd=0.0,
                attendee_disruption_minimized_note=f"Seat buffer {o['seat_buffer']}, AV match {o['av_match_pct']}%, "
                                                   f"time shift {o['time_shift_mins']:+d} min.",
                tradeoffs="; ".join(o["warnings"]) or "None flagged by the solver.",
            ))
        return plans
    return []


def _verify_and_repair(output: SolverOutput, triage: DisruptionAssessment, event_id: str) -> SolverOutput:
    report: list[str] = []
    rec = _ground_truth(output.recommended_option, event_id)
    alt = _ground_truth(output.alternative_option, event_id)
    rec_ok, rec_reasons = verify_plan(rec, triage, event_id)
    alt_ok, alt_reasons = verify_plan(alt, triage, event_id)
    fallbacks = None

    def next_fallback(exclude: list[ResolutionPlan]) -> ResolutionPlan | None:
        nonlocal fallbacks
        fallbacks = fallbacks if fallbacks is not None else _deterministic_plans(triage, event_id)
        used = [[(m.session_id, m.new_room_id, m.new_start_time) for m in p.proposed_changes] for p in exclude]
        for p in fallbacks:
            if [(m.session_id, m.new_room_id, m.new_start_time) for m in p.proposed_changes] not in used and verify_plan(p, triage, event_id)[0]:
                return p
        return None

    if rec_ok:
        report.append("Recommended plan passed the Layer 2 action lock.")
    else:
        report.append(f"Recommended plan REJECTED by Layer 2: {'; '.join(rec_reasons)}")
        if alt_ok:
            rec, alt = alt, None
            report.append("Promoted the verified alternative to recommended.")
        else:
            rec = next_fallback([])
            report.append("Replaced with the exact solver's best verified option." if rec else "No verified fallback exists.")
        rec_ok = rec is not None

    key = lambda p: [(m.session_id, m.new_room_id, m.new_start_time) for m in p.proposed_changes]  # noqa: E731
    if alt is not None and alt_ok and (rec is None or key(alt) != key(rec)):
        report.append("Alternative plan passed the Layer 2 action lock.")
        alt_verified = True
    else:
        if alt is not None and not alt_ok:
            report.append(f"Alternative plan REJECTED by Layer 2: {'; '.join(alt_reasons)}")
        elif alt is not None:
            report.append("Alternative duplicated the recommended plan.")
        alt = next_fallback([rec] if rec else [])
        alt_verified = alt is not None
        report.append("Alternative replaced with the next verified solver option." if alt else "No second verified option exists.")
        alt = alt or rec

    if rec is None:  # nothing verifiable at all: return the model's plans, clearly marked as failed
        rec = _ground_truth(output.recommended_option, event_id)
        alt = alt or _ground_truth(output.alternative_option, event_id)
    return output.model_copy(update={"recommended_option": rec, "alternative_option": alt,
                                     "deterministic_verification_passed": rec_ok,
                                     "alternative_verified": alt_verified and rec_ok, "guardrail_report": report})


def run_solver_agent(event_id: str, triage: DisruptionAssessment, run_id: str, cost_saver: bool = True) -> SolverOutput:
    """Agent 2 with focused (or full) context -> metered -> every plan checked by Layer 2 and repaired if needed."""
    focus = triage.primary_affected_session_ids + triage.cascading_affected_session_ids
    context = tokenomics.build_compact_context(event_id, focus or None, cost_saver=cost_saver)
    pruned = tokenomics.compare_context_sizes(event_id, focus or None)["tokens_pruned_est"] if cost_saver else 0
    prompt = f"{context}\n\n## TRIAGE ASSESSMENT (from Agent 1)\n{triage.model_dump_json(indent=1, exclude={'tool_calls_made', 'guardrail_notes'})}"
    deps = SolverDeps(event_id=event_id, triage=triage)
    result, _ = guarded_run(build_solver_agent(), prompt, deps=deps, run_id=run_id, event_id=event_id,
                            agent_name=AGENT_NAME, cost_saver=cost_saver, context_tokens_pruned=pruned)
    verified = _verify_and_repair(result.output, triage, event_id)
    return verified.model_copy(update={"tool_calls_made": deps.tool_log})
