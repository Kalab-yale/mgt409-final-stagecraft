"""Planning service: turns raw planning rows into what an organizer actually looks at.

- Deadline status (done / overdue / due soon / upcoming) and initiative progress, computed live from tasks.
- Red flags from deterministic rules (cheap, instant, never hallucinated) merged with stored flags raised by the
  Inbox Watcher, assistants, or people.
- One health summary per event for the home screen and the Overview dashboard.
- A starter plan (standard deadlines + workstreams) for events created from a prompt, CSV, or template.
"""

from __future__ import annotations

from datetime import date, timedelta

from backend import database as db
from backend import team as team_mod
from backend import tools
from backend.config import demo_today
from backend.models import DIMENSION_LABELS, Deadline, Initiative, Task

SEVERITY_RANK = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
DUE_SOON_DAYS = 3
CRITICAL_VENDOR_CATEGORIES = {"Catering", "AV and Production", "Transport"}
ACTIVE_RSVP = {"Registered", "Confirmed", "Checked In"}


# ---- Deadlines & progress ------------------------------------------------------------------------


def deadline_status(d: Deadline, today: date | None = None) -> str:
    today = today or demo_today()
    if d.done:
        return "Done"
    if d.due_date < today:
        return "Overdue"
    if (d.due_date - today).days <= DUE_SOON_DAYS:
        return "Due soon"
    return "Upcoming"


def initiative_progress(initiative: Initiative, tasks: list[Task], today: date | None = None) -> dict:
    """Progress = share of the initiative's tasks that are done. Status reflects blockers and time pressure."""
    today = today or demo_today()
    mine = [t for t in tasks if t.initiative_id == initiative.id]
    done = sum(t.status == "Done" for t in mine)
    pct = round(100 * done / len(mine)) if mine else 0
    overdue = [t for t in mine if t.status != "Done" and t.due_date and t.due_date < today]
    if mine and done == len(mine):
        status = "Done"
    elif initiative.blocked or any(t.status == "Blocked" for t in mine):
        status = "Blocked"
    elif overdue or ((initiative.target_date - today).days <= 7 and pct < 70):
        status = "At risk"
    else:
        status = "On track"
    return {"progress_pct": pct, "tasks_done": done, "tasks_total": len(mine), "status": status,
            "overdue_tasks": [t.title for t in overdue]}


# ---- Red flags -----------------------------------------------------------------------------------


def _flag(rule: str, ref: str, title: str, detail: str, dimension: str, severity: str) -> dict:
    return {"id": f"sys_{rule}_{ref}", "title": title, "detail": detail, "dimension": dimension,
            "dimension_label": DIMENSION_LABELS[dimension], "severity": severity, "status": "Open",
            "source": "system", "source_ref": ref}


def compute_system_flags(event_id: str, today: date | None = None) -> list[dict]:
    """Deterministic rules over the event's data. Recomputed on every read, so fixing the data clears the flag."""
    today = today or demo_today()
    event = db.get_event(event_id)
    days_out = (event.date - today).days
    flags: list[dict] = []

    for d in db.list_records("deadlines", event_id):
        status = deadline_status(d, today)
        if status == "Overdue":
            late = (today - d.due_date).days
            flags.append(_flag("overdue", d.id, f"Overdue: {d.title}", f"Was due {d.due_date:%b} {d.due_date.day} ({late} day{'s' if late != 1 else ''} ago). "
                               f"Owner: {d.owner or 'unassigned'}. {d.notes}".strip(), d.dimension, "Critical" if days_out <= 7 else "High"))
        elif status == "Due soon":
            left = (d.due_date - today).days
            flags.append(_flag("duesoon", d.id, f"Due {'today' if left == 0 else f'in {left} day' + ('s' if left != 1 else '')}: {d.title}",
                               f"Owner: {d.owner or 'unassigned'}. {d.notes}".strip(), d.dimension, "Medium"))

    budget = db.list_records("budget_items", event_id)
    for b in budget:
        if b.actual_usd > b.planned_usd:
            over = b.actual_usd - b.planned_usd
            pct = 100 * over / b.planned_usd if b.planned_usd else 100
            flags.append(_flag("overbudget", b.id, f"Over budget: {b.description}",
                               f"Spent ${b.actual_usd:,.0f} vs ${b.planned_usd:,.0f} planned (+${over:,.0f}, {pct:.0f}%).",
                               "Budget", "High" if pct > 20 else "Medium"))

    for v in db.list_records("vendors", event_id):
        if v.relationship == "Vendor" and v.category in CRITICAL_VENDOR_CATEGORIES and v.contract_status in ("Prospect", "Negotiating") and days_out <= 30:
            flags.append(_flag("unsigned", v.id, f"{v.category} contract not signed: {v.name}",
                               f"Still '{v.contract_status}' with {days_out} days to go. Contact: {v.contact_name} ({v.contact_email}).",
                               "Food" if v.category == "Catering" else "Venue", "High" if days_out <= 21 else "Medium"))

    for t in db.list_records("tasks", event_id):
        if t.status == "Blocked":
            flags.append(_flag("blocked", t.id, f"Blocked task: {t.title}", f"Owner: {t.owner or 'unassigned'}.", t.dimension, "Medium"))

    attendees = db.list_records("attendees", event_id)
    active = sum(a.rsvp_status in ACTIVE_RSVP for a in attendees)
    if active > event.attendee_count:
        flags.append(_flag("capacity", event_id, "Registrations exceed the event cap",
                           f"{active} active registrations for a cap of {event.attendee_count}.", "Attendees", "High"))

    state = db.get_event_full_state(event_id)
    for c in tools._detect_conflicts(state):
        if c["type"] == "Conflict Flagged":
            continue
        dim = "Program" if "Speaker" in c["type"] else "Venue"
        flags.append(_flag("schedule", "_".join(c["session_ids"]), c["type"], c["detail"], dim, "High"))

    if days_out <= 2:
        for sp in state.speakers:
            if sp.status != "Confirmed On-Site":
                flags.append(_flag("travel", sp.id, f"Speaker not on site: {sp.name}", f"{sp.status} ({sp.travel_origin}).", "Program", "Medium"))

    # Team: high-priority work with nobody on it, a team with no PMO, and anyone stretched too thin
    team = db.list_records("team_members", event_id)
    for t in db.list_records("tasks", event_id):
        if t.status != "Done" and t.priority == "High" and not t.assignees:
            flags.append(_flag("unassigned", t.id, f"Nobody owns: {t.title}", "High-priority task with no one assigned. Assign it on the Team tab.",
                               t.dimension, "High" if days_out <= 14 else "Medium"))
    if team and not any(m.is_pmo for m in team):
        flags.append(_flag("nopmo", event_id, "No PMO on the team", "Pick one person to run the plan and chase owners (Team tab).", "General", "High"))
    for m in team_mod.workload(event_id, today)["members"]:
        if m["stretched"]:
            flags.append(_flag("stretched", m["id"], f"{m['name']} is stretched thin", f"{m['open_tasks']} open tasks "
                               f"({m['overdue_tasks']} overdue). Consider sharing or reassigning some.", "General", "Medium"))
    return flags


def get_flags(event_id: str, include_closed: bool = False) -> list[dict]:
    """System flags + stored flags (inbox, assistant, user), most severe first."""
    stored = [f.model_dump() | {"dimension_label": DIMENSION_LABELS[f.dimension]} for f in db.list_records("flags", event_id)
              if include_closed or f.status == "Open"]
    return sorted(compute_system_flags(event_id) + stored, key=lambda f: (SEVERITY_RANK[f["severity"]], f["title"]))


# ---- Health & full planning state ----------------------------------------------------------------


def _phase(days_out: int) -> str:
    if days_out < 0:
        return "Wrap-up"
    if days_out == 0:
        return "Event day"
    if days_out <= 7:
        return "Final week"
    return "Planning"


def event_health(event_id: str, today: date | None = None) -> dict:
    today = today or demo_today()
    event = db.get_event(event_id)
    days_out = (event.date - today).days
    deadlines = sorted(db.list_records("deadlines", event_id), key=lambda d: d.due_date)
    tasks = db.list_records("tasks", event_id)
    inits = [initiative_progress(i, tasks, today) for i in db.list_records("initiatives", event_id)]
    flags = get_flags(event_id)
    attendees = db.list_records("attendees", event_id)
    budget = db.list_records("budget_items", event_id)
    vendors = db.list_records("vendors", event_id)
    inbox = db.list_records("inbox_messages", event_id)
    open_deadlines = [d for d in deadlines if not d.done]
    return {
        "event_id": event_id,
        "days_to_go": days_out,
        "phase": _phase(days_out),
        "progress_pct": round(sum(i["progress_pct"] for i in inits) / len(inits)) if inits else 0,
        "flags": {sev: sum(f["severity"] == sev for f in flags) for sev in SEVERITY_RANK} | {"total": len(flags)},
        "top_flag": flags[0]["title"] if flags else None,
        "overdue_deadlines": sum(deadline_status(d, today) == "Overdue" for d in deadlines),
        "next_deadlines": [{"id": d.id, "title": d.title, "due_date": d.due_date.isoformat(), "status": deadline_status(d, today),
                            "dimension": d.dimension} for d in open_deadlines[:3]],
        "registrations": {"active": sum(a.rsvp_status in ACTIVE_RSVP for a in attendees),
                          "waitlist": sum(a.rsvp_status == "Waitlist" for a in attendees), "cap": event.attendee_count},
        "budget": {"planned_usd": sum(b.planned_usd for b in budget), "spent_usd": sum(b.actual_usd for b in budget),
                   "sponsorship_signed_usd": sum(v.contract_value_usd for v in vendors
                                                 if v.relationship == "Sponsor" and v.contract_status in ("Signed", "Invoiced", "Paid"))},
        "inbox_new": sum(m.status == "New" for m in inbox),
    }


def get_planning_state(event_id: str) -> dict:
    """Everything the workspace tabs need, with computed statuses attached."""
    today = demo_today()
    tasks = db.list_records("tasks", event_id)
    attendees = db.list_records("attendees", event_id)
    budget = db.list_records("budget_items", event_id)

    def counts(field: str, rows: list) -> dict:
        out: dict[str, int] = {}
        for r in rows:
            out[getattr(r, field)] = out.get(getattr(r, field), 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    active = [a for a in attendees if a.rsvp_status in ACTIVE_RSVP]
    by_cat: dict[str, dict] = {}
    for b in budget:
        c = by_cat.setdefault(b.category, {"planned_usd": 0.0, "spent_usd": 0.0})
        c["planned_usd"] += b.planned_usd
        c["spent_usd"] += b.actual_usd
    return {
        "today": today.isoformat(),
        "health": event_health(event_id, today),
        "flags": get_flags(event_id),
        "deadlines": [d.model_dump(mode="json") | {"status": deadline_status(d, today)}
                      for d in sorted(db.list_records("deadlines", event_id), key=lambda d: d.due_date)],
        "initiatives": [i.model_dump(mode="json") | initiative_progress(i, tasks, today)
                        | {"tasks": [t.model_dump(mode="json") for t in tasks if t.initiative_id == i.id]}
                        for i in db.list_records("initiatives", event_id)],
        "tasks_without_initiative": [t.model_dump(mode="json") for t in tasks if not t.initiative_id],
        "team": team_mod.workload(event_id, today),
        "attendees": {
            "total": len(attendees),
            "by_status": counts("rsvp_status", attendees),
            "by_type": counts("attendee_type", attendees),
            "dietary_active": counts("dietary", active),
            "rows": [a.model_dump(mode="json") for a in sorted(attendees, key=lambda a: a.name)],
        },
        "vendors": [v.model_dump() for v in db.list_records("vendors", event_id)],
        "budget": {"items": [b.model_dump() for b in budget], "by_category": by_cat},
        "inbox": [m.model_dump() for m in sorted(db.list_records("inbox_messages", event_id), key=lambda m: m.received_at, reverse=True)],
    }


def home_cards() -> list[dict]:
    """One card per event for the home screen, soonest first."""
    venues = {v.id: v for v in db.get_venues()}
    cards = []
    for e in sorted(db.get_events(), key=lambda e: e.date):
        v = venues.get(e.venue_id)
        today = demo_today()
        upcoming = [{"title": d.title, "due_date": d.due_date.isoformat(), "status": deadline_status(d, today), "dimension": d.dimension}
                    for d in sorted(db.list_records("deadlines", e.id), key=lambda d: d.due_date)
                    if not d.done and (d.due_date - today).days <= 21]
        people = team_mod.members(e.id)
        cards.append(e.model_dump(mode="json") | {"venue_name": v.name if v else "", "venue_city": v.city if v else ""}
                     | {"health": event_health(e.id), "upcoming": upcoming,
                        "team": [{"name": m.name, "email": m.email, "is_pmo": m.is_pmo, "role": m.role} for m in people]})
    return cards


# ---- Starter plan for new events -----------------------------------------------------------------

STARTER_DEADLINES = [
    ("Venue contract signed", "Venue", -60), ("Speaker lineup confirmed", "Program", -35), ("Catering menu chosen", "Food", -30),
    ("Sponsors confirmed", "Budget", -25), ("Speaker travel booked", "Program", -14), ("Dietary requirements to caterer", "Food", -10),
    ("Final catering headcount", "Food", -7), ("Room assignments and signage final", "Venue", -5),
    ("Attendee logistics email", "Attendees", -4), ("Registration closes", "Attendees", -3),
    ("Run-of-show sign-off", "General", -2), ("Budget reconciliation", "Budget", 14),
]
STARTER_INITIATIVES = [
    ("Program and speakers", "Program", -7, ["Confirm speakers", "Book speaker travel", "Send speaker briefing"]),
    ("Venue and logistics", "Venue", -5, ["Sign venue contract", "Confirm AV", "Finalize signage"]),
    ("Food and catering", "Food", -7, ["Choose menu", "Collect dietary needs", "Send final headcount"]),
    ("Registration and comms", "Attendees", -3, ["Open registration", "Send reminders", "Send logistics email"]),
    ("Budget and sponsors", "Budget", 14, ["Set budget", "Secure sponsors", "Reconcile actuals"]),
]


def apply_starter_plan(event_id: str, today: date | None = None) -> dict:
    """Standard deadlines and workstreams relative to the event date. Deadlines already in the past are skipped
    (a new event shouldn't start life with fake overdue items)."""
    today = today or demo_today()
    event = db.get_event(event_id)
    tag = event_id.removeprefix("evt_")
    deadlines = [Deadline(id=f"dl_{tag}_{i:02d}", event_id=event_id, title=t, dimension=dim, due_date=event.date + timedelta(days=off),
                          source="template") for i, (t, dim, off) in enumerate(STARTER_DEADLINES, 1)
                 if event.date + timedelta(days=off) >= today]
    inits, tasks = [], []
    for i, (title, dim, target, items) in enumerate(STARTER_INITIATIVES, 1):
        iid = f"ini_{tag}_{i}"
        inits.append(Initiative(id=iid, event_id=event_id, title=title, dimension=dim, target_date=event.date + timedelta(days=target)))
        tasks += [Task(id=f"tsk_{tag}_{i}{j:02d}", event_id=event_id, title=t, dimension=dim, initiative_id=iid, source="template")
                  for j, t in enumerate(items, 1)]
    db.insert_records("deadlines", deadlines)
    db.insert_records("initiatives", inits)
    db.insert_records("tasks", tasks)
    return {"deadlines": len(deadlines), "initiatives": len(inits), "tasks": len(tasks)}
