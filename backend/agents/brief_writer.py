"""Daily Brief: one morning update per event.

The code builds a deterministic digest (exact deadlines, flags, changes in the last 24 hours, inbox results, pending
drafts). The BriefWriter agent only prioritizes and phrases it: it groups related flags into one risk and picks the 3-5
things that matter today. The digest ships with the brief, so the UI shows exact numbers next to the narrative.
One brief per event per day is cached; regenerating is explicit.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from backend import changes, planning, tokenomics
from backend import database as db
from backend.agents.common import guarded_run
from backend.config import demo_today, get_agent_settings, get_pydantic_ai_model
from backend.models import Dimension

AGENT_NAME = "BriefWriter"


class BriefItem(BaseModel):
    title: str = Field(description="What needs attention, in a few words")
    why: str = Field(description="One sentence: the consequence or deadline")
    next_step: str = Field(description="One concrete action, starting with a verb")
    dimension: Dimension
    urgency: Literal["Critical", "High", "Medium", "Low"]


class DailyBrief(BaseModel):
    headline: str = Field(description="One sentence on where the event stands today")
    top_priorities: list[BriefItem] = Field(description="3 to 5 items, most urgent first")
    risks: list[str] = Field(description="Up to 5 risks, related flags grouped into one line each")
    since_yesterday: list[str] = Field(default_factory=list, description="Up to 6 notable changes and arrivals in the last day")
    inbox_highlights: list[str] = Field(default_factory=list, description="Up to 5 emails worth knowing about")


INSTRUCTIONS = """You write the organizer's daily brief for one event from the DIGEST below. Plain, calm, specific language.
- Use only facts in the digest. Keep every number, date, and name exactly as given.
- Group related flags into one risk (for example several AV items are one AV risk; a cancellation and its task are one item).
- top_priorities: the 3-5 things to act on today, most urgent first. Overdue and due-this-week items, cancellations,
  cost increases, and suspicious emails come first.
- If there are drafted changes waiting, mention that they need review.
- One sentence per field. No jargon, no exclamation marks."""


@lru_cache(maxsize=1)
def build_brief_writer() -> Agent[None, DailyBrief]:
    return Agent(get_pydantic_ai_model(), name=AGENT_NAME, output_type=DailyBrief, instructions=INSTRUCTIONS,
                 model_settings=get_agent_settings(max_tokens=1500), retries=2)


def build_digest(event_id: str) -> dict:
    """Exact facts for today's brief (no AI)."""
    today = demo_today()
    event = db.get_event(event_id)
    health = planning.event_health(event_id, today)
    week = today + timedelta(days=7)
    deadlines = sorted(db.list_records("deadlines", event_id), key=lambda d: d.due_date)
    tasks = db.list_records("tasks", event_id)
    inbox = db.list_records("inbox_messages", event_id)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    pending = changes.list_changes(event_id)
    at_risk = []
    for i in db.list_records("initiatives", event_id):
        p = planning.initiative_progress(i, tasks, today)
        if p["status"] in ("At risk", "Blocked"):
            at_risk.append(f"{i.title} ({p['status']}, {p['progress_pct']}%, owner {i.owner or 'unassigned'})")
    return {
        "today": today.isoformat(),
        "event": {"name": event.name, "date": event.date.isoformat(), "days_to_go": health["days_to_go"], "phase": health["phase"],
                  "progress_pct": health["progress_pct"]},
        "deadlines_overdue": [f"{d.title} (was due {d.due_date}, {d.owner})" for d in deadlines if planning.deadline_status(d, today) == "Overdue"],
        "deadlines_this_week": [f"{d.title} ({d.due_date}, {d.owner})" for d in deadlines if not d.done and today <= d.due_date <= week],
        "flags": [f"[{f['severity']}] {f['title']}: {f['detail']}" for f in planning.get_flags(event_id)],
        "workstreams_at_risk": at_risk,
        "registrations": health["registrations"],
        "budget": health["budget"],
        "changes_last_24h": [a["summary"] for a in changes.list_activity(event_id, since=since, limit=20)],
        "drafts_waiting_for_review": [c["summary"] for c in pending][:8],
        "drafts_waiting_count": len(pending),
        "inbox": {
            "new_unprocessed": sum(m.status == "New" for m in inbox),
            "needs_reply": [f"{m.from_name}: {m.subject}" for m in inbox if m.status == "Needs Reply"],
            "quarantined": [f"{m.from_email}: {m.subject}" for m in inbox if m.status == "Quarantined"],
            "important": [f"[{m.urgency}] {m.from_name}: {m.ai_summary}" for m in sorted(inbox, key=lambda m: m.received_at, reverse=True)
                          if m.urgency in ("Critical", "High") and m.ai_summary][:8],
        },
    }


def _stored(event_id: str, brief_date: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT content_json FROM daily_briefs WHERE event_id = ? AND brief_date = ? ORDER BY created_at DESC LIMIT 1",
                           (event_id, brief_date)).fetchone()
    return json.loads(row["content_json"]) if row else None


def generate_brief(event_id: str) -> dict:
    digest = build_digest(event_id)
    run_id = tokenomics.start_workflow_run(event_id, f"Daily brief {digest['today']}", cost_saver_enabled=tokenomics.get_cost_saver_mode())
    result, metric = guarded_run(build_brief_writer(), "DIGEST:\n" + json.dumps(digest, indent=1), deps=None, run_id=run_id,
                                 event_id=event_id, agent_name=AGENT_NAME, cost_saver=tokenomics.get_cost_saver_mode())
    brief: DailyBrief = result.output
    brief = brief.model_copy(update={"top_priorities": brief.top_priorities[:5], "risks": brief.risks[:5],
                                     "since_yesterday": brief.since_yesterday[:6], "inbox_highlights": brief.inbox_highlights[:5]})
    content = {"id": f"brf_{uuid.uuid4().hex[:8]}", "event_id": event_id, "brief_date": digest["today"],
               "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "brief": brief.model_dump(), "digest": digest,
               "usage": {"total_tokens": metric.total_tokens, "cost_usd": metric.cost_usd, "latency_ms": metric.latency_ms}, "run_id": run_id}
    with db.connect() as conn:
        conn.execute("INSERT INTO daily_briefs VALUES (?, ?, ?, ?, ?, ?)",
                     (content["id"], event_id, digest["today"], content["generated_at"], json.dumps(content), run_id))
    changes.log_activity(event_id, "BriefWriter", "brief", f"Daily brief for {digest['today']} generated", content["id"])
    return content


def get_or_create_brief(event_id: str, force: bool = False) -> dict:
    """Today's brief: cached once per day; force=True writes a fresh one (e.g. after processing new email)."""
    if not force:
        cached = _stored(event_id, demo_today().isoformat())
        if cached:
            return cached | {"cached": True}
    return generate_brief(event_id) | {"cached": False}
