"""Agent 3 - Stakeholder Comms & Run-of-Show Updater.

Turns the chosen resolution plan into ready-to-send drafts: speaker/moderator messages, internal war-room
notes, a volunteer checklist, an executive brief, and a public banner. A quality gate sends the draft back
if someone affected isn't told or a new room is never named; then every string goes through the outbound
scrubber, and contacts are replaced with the ones on file (the model never gets to invent an address).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from backend import database as db
from backend import security, tokenomics
from backend.agents.common import guarded_run
from backend.agents.solver_agent import ResolutionPlan
from backend.agents.triage_agent import DisruptionAssessment
from backend.config import get_agent_settings, get_pydantic_ai_model

AGENT_NAME = "CommsAgent"
INTERNAL_ID_RE = re.compile(r"\b(?:ses|rm|spk|evt|ven|cat|crisis)_[a-z0-9_]+\b")


# ---- Structured output -------------------------------------------------------------------------


class StakeholderNotification(BaseModel):
    recipient_name: str
    recipient_role: str = Field(description='e.g. "VIP Speaker", "Panel Moderator", "Catering Vendor", "AV & Facilities Lead", "Registration Desk"')
    channel: Literal["Email", "SMS / WhatsApp Urgent", "Slack War-Room"]
    recipient_contact: str
    subject_or_header: str
    message_body: str


class VolunteerTask(BaseModel):
    task_id: str
    assigned_team: str
    priority: Literal["IMMEDIATE (Next 5 Mins)", "HIGH (Next 15 Mins)"]
    instruction: str


class CommsPackage(BaseModel):
    executive_brief: str = Field(description="2 sentences for the Summit Director: what broke and how it was resolved")
    speaker_and_moderator_messages: list[StakeholderNotification]
    volunteer_action_checklist: list[VolunteerTask]
    public_attendee_banner_alert: str = Field(description="one line, under 140 characters")
    scrubber_redactions: int = Field(default=0, description="set by the system")


# ---- Agent ---------------------------------------------------------------------------------------


@dataclass
class CommsDeps:
    must_notify: list[str]       # speaker names who must get a message
    new_room_names: list[str]    # every new room must be named somewhere


INSTRUCTIONS = """You are Agent 3, Stakeholder Comms & Run-of-Show for the Stagecraft conference war-room.
The resolution plan below is final. Write ready-to-send drafts.

Messages (speaker_and_moderator_messages):
- One message to EVERY person listed under PEOPLE TO NOTIFY, using their exact name and contact.
  Tier 1 speakers and anyone In Transit get channel "SMS / WhatsApp Urgent"; other speakers get "Email".
- Internal teams via "Slack War-Room": the AV & Facilities Lead (new-room setup, broken room) and the Registration Desk
  (app/signage update). Message the catering vendor only if a session now overlaps a meal window or catering timing changes.
- Every message states the OLD room/time and the NEW room (with floor) and time explicitly, plus what the reader must do now.
  SMS bodies stay under 320 characters. Warm, calm, specific. Sign off as "Stagecraft War-Room, <event name>".
volunteer_action_checklist: 3-6 tasks (task_id T1, T2, ...) for teams like wayfinding at the old room, AV crew at the
new room, speaker green room/escort, registration/app update, hospitality liaison. Use the venue's real floor/zone names.
public_attendee_banner_alert: one line under 140 characters with the new room and time.
executive_brief: exactly 2 sentences.
Never include internal ids (like ses_..., rm_...), API keys, or anyone else's contact details.
"""


@lru_cache(maxsize=1)
def build_comms_agent() -> Agent[CommsDeps, CommsPackage]:
    agent = Agent(
        get_pydantic_ai_model(),
        name=AGENT_NAME,
        deps_type=CommsDeps,
        output_type=CommsPackage,
        instructions=INSTRUCTIONS,
        model_settings=get_agent_settings(max_tokens=3500),
        retries=2,
    )

    @agent.output_validator
    def _quality_gate(ctx: RunContext[CommsDeps], pkg: CommsPackage) -> CommsPackage:
        problems = []
        recipients = " | ".join(m.recipient_name.lower() for m in pkg.speaker_and_moderator_messages)
        missing = [n for n in ctx.deps.must_notify if n.lower() not in recipients]
        if missing:
            problems.append(f"No message to: {', '.join(missing)}.")
        slack_roles = " | ".join(m.recipient_role.lower() + " " + m.recipient_name.lower()
                                 for m in pkg.speaker_and_moderator_messages if m.channel == "Slack War-Room")
        for team, keys in (("AV & Facilities Lead", ("av", "facilities")), ("Registration Desk", ("registration",))):
            if not any(k in slack_roles for k in keys):
                problems.append(f'Add a "Slack War-Room" message to the {team}.')
        bodies = " ".join(m.message_body for m in pkg.speaker_and_moderator_messages).lower()
        unnamed = [r for r in ctx.deps.new_room_names if r.lower() not in bodies]
        if unnamed:
            problems.append(f"No message names the new room(s): {', '.join(unnamed)}.")
        texts = [pkg.executive_brief, pkg.public_attendee_banner_alert] + [m.message_body for m in pkg.speaker_and_moderator_messages] \
            + [t.instruction for t in pkg.volunteer_action_checklist]
        ids = sorted({i for t in texts for i in INTERNAL_ID_RE.findall(t)})
        if ids:
            problems.append(f"Remove internal ids from the text: {ids}.")
        if not 3 <= len(pkg.volunteer_action_checklist) <= 6:
            problems.append("volunteer_action_checklist needs 3-6 tasks.")
        if problems:
            raise ModelRetry("Fix the comms package and return it in full:\n- " + "\n- ".join(problems))
        return pkg

    return agent


# ---- Brief builder + post-processing -------------------------------------------------------------


def _comms_brief(event_id: str, triage: DisruptionAssessment, plan: ResolutionPlan) -> tuple[str, list[str], list[str], dict[str, str]]:
    """Plain-language brief (room names, not ids) + who must be notified + their contacts on file."""
    state = db.get_event_full_state(event_id)
    rooms = {r.id: r for r in state.rooms}
    speakers = {sp.id: sp for sp in state.speakers}
    sessions = {s.id: s for s in state.sessions}
    lines = [f"## EVENT: {state.event.name} | {state.event.date} | {state.venue.name}, {state.venue.city}",
             f"## WHAT HAPPENED: {triage.crisis_summary}",
             f"## APPROVED PLAN: {plan.option_label} ({plan.strategy_type})"]
    notify_ids: list[str] = []
    for m in plan.proposed_changes:
        old, new = rooms[m.old_room_id], rooms[m.new_room_id]
        lines.append(f"- '{m.session_title}': {old.name} ({old.floor_or_zone}) {m.old_start_time}-{m.old_end_time}  ->  "
                     f"{new.name} ({new.floor_or_zone}) {m.new_start_time}-{m.new_end_time}. Why: {m.change_rationale}")
        notify_ids += sessions[m.session_id].speaker_ids if m.session_id in sessions else []
    attendance = triage.attendance_overrides()
    for sid, n in attendance.items():
        if sid in sessions:
            lines.append(f"- Expected attendance for '{sessions[sid].title}' is now {n}.")
    for u in plan.room_status_updates:
        lines.append(f"- {rooms[u.room_id].name} ({rooms[u.room_id].floor_or_zone}) is now {u.new_status}.")
    for sid in triage.affected_speaker_ids:
        if sid not in notify_ids:
            notify_ids.append(sid)
    notify = [speakers[i] for i in dict.fromkeys(notify_ids) if i in speakers]
    lines.append("## PEOPLE TO NOTIFY (exact names and contacts):")
    lines += [f"- {sp.name} | {sp.vip_tier} | {sp.title_org} | {sp.status} ({sp.travel_origin}) | {sp.email}" for sp in notify]
    lines.append("## CATERING ON FILE: " + "; ".join(f"{c.meal_name} {c.scheduled_time}-{c.hard_cutoff_time} by {c.vendor_name} in "
                                                     f"{rooms[c.room_id].name}" for c in state.catering_slas))
    contacts = {sp.name.lower(): sp.email for sp in notify}
    new_rooms = list(dict.fromkeys(rooms[m.new_room_id].name for m in plan.proposed_changes))
    return "\n".join(lines), [sp.name for sp in notify], new_rooms, contacts


def _finalize(pkg: CommsPackage, event_id: str, contacts: dict[str, str]) -> CommsPackage:
    """Contacts from the database, never the model's; every outbound string scrubbed (logged if anything is removed)."""
    slug = db.get_event(event_id).slug
    redactions = 0

    def scrub(text: str) -> str:
        nonlocal redactions
        out = security.scrub_outbound_message(event_id, text)
        redactions += len(out["redactions"])
        return out["text"]

    messages = []
    for m in pkg.speaker_and_moderator_messages:
        contact = next((email for name, email in contacts.items() if name in m.recipient_name.lower()), None)
        if contact is None:  # internal teams / vendors: keep phone or .example placeholders, never a real-looking address
            role = re.sub(r"[^a-z0-9]+", "-", m.recipient_role.lower()).strip("-") or "team"
            contact = m.recipient_contact if ("@" not in m.recipient_contact or m.recipient_contact.endswith(".example")) \
                else f"{role}@{slug}.example"
        messages.append(m.model_copy(update={"recipient_contact": scrub(contact), "subject_or_header": scrub(m.subject_or_header),
                                             "message_body": scrub(m.message_body)}))
    tasks = [t.model_copy(update={"instruction": scrub(t.instruction)}) for t in pkg.volunteer_action_checklist]
    return pkg.model_copy(update={
        "executive_brief": scrub(pkg.executive_brief),
        "public_attendee_banner_alert": scrub(pkg.public_attendee_banner_alert),
        "speaker_and_moderator_messages": messages,
        "volunteer_action_checklist": tasks,
        "scrubber_redactions": redactions,
    })


def run_comms_agent(event_id: str, triage: DisruptionAssessment, chosen_plan: ResolutionPlan, run_id: str,
                    cost_saver: bool = True) -> CommsPackage:
    """Agent 3 on the chosen plan -> quality gate -> contacts from DB -> outbound scrubber -> metered."""
    brief, must_notify, new_rooms, contacts = _comms_brief(event_id, triage, chosen_plan)
    pruned = 0
    if not cost_saver:  # baseline mode also ships the full event dump, like every other agent
        full = tokenomics.build_compact_context(event_id, cost_saver=False)
        brief = f"{full}\n\n{brief}"
    else:
        pruned = tokenomics.estimate_tokens(tokenomics.build_compact_context(event_id, cost_saver=False))
    deps = CommsDeps(must_notify=must_notify, new_room_names=new_rooms)
    result, _ = guarded_run(build_comms_agent(), brief, deps=deps, run_id=run_id, event_id=event_id,
                            agent_name=AGENT_NAME, cost_saver=cost_saver, context_tokens_pruned=pruned)
    return _finalize(result.output, event_id, contacts)
