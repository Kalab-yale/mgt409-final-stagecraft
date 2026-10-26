"""The organizing team: members, the PMO, who owns which tasks, and how loaded each person is.

Tasks store `assignees` as team-member ids (a task can belong to several people). People and assistants may refer to
members by name or id; `normalize_task_fields` resolves either and keeps the task's `owner` text (shown everywhere)
in sync with the assignees. Exactly one member per event can be the PMO.
"""

from __future__ import annotations

from datetime import date

from backend import database as db
from backend.config import demo_today

OVERLOADED_OPEN_TASKS = 7  # more open tasks than this -> flagged as stretched


class TeamError(ValueError):
    pass


TEAM_ORDER = ["Leadership", "Program", "Venue and logistics", "Food", "Registration", "Sponsorship and budget", "Marketing", "Volunteers"]


def members(event_id: str) -> list:
    return sorted(db.list_records("team_members", event_id),
                  key=lambda m: (not m.is_pmo, TEAM_ORDER.index(m.team) if m.team in TEAM_ORDER else 99, m.name))


def names_for(event_id: str, ids) -> str:
    by_id = {m.id: m.name for m in db.list_records("team_members", event_id)}
    return ", ".join(by_id.get(i, i) for i in ids or []) or "(nobody)"


def resolve(event_id: str, values) -> tuple[list[str], list[str]]:
    """Names or ids (a list, or a comma-separated string) -> (ids, names). Unknown people raise TeamError."""
    if isinstance(values, str):
        values = [v for v in (x.strip() for x in values.replace(" and ", ",").split(",")) if v]
    team = members(event_id)
    by_id = {m.id: m for m in team}
    by_name = {m.name.lower(): m for m in team}
    first = {}
    for m in team:
        first.setdefault(m.name.split()[0].lower(), []).append(m)
    ids, unknown = [], []
    for v in values or []:
        key = str(v).strip()
        m = by_id.get(key) or by_name.get(key.lower())
        if m is None and len(first.get(key.lower(), [])) == 1:  # "Chris" is enough when only one Chris
            m = first[key.lower()][0]
        if m is None:
            unknown.append(key)
        elif m.id not in ids:
            ids.append(m.id)
    if unknown:
        raise TeamError(f"Not on the team: {', '.join(unknown)}. Team: {', '.join(m.name for m in team) or 'nobody yet'}. "
                        "Add them to the team first.")
    return ids, [by_id[i].name for i in ids]


def validate_member_fields(fields: dict) -> dict:
    """Team-member names and roles end up in text the agents read, so they get the same rules as account names."""
    from backend.auth import NAME_RE, AuthError, screen_text  # local import: auth imports database

    fields = dict(fields)
    if "name" in fields:
        name = " ".join(str(fields["name"]).split())
        if not name or len(name) > 80 or not NAME_RE.match(name):
            raise TeamError("Names can use letters, spaces, apostrophes, hyphens, and periods (up to 80 characters).")
        fields["name"] = name
    try:
        for key in ("name", "role"):
            if fields.get(key):
                screen_text(str(fields[key]), f"team member {key}")
    except AuthError as exc:
        raise TeamError(str(exc)) from exc
    return fields


def normalize_task_fields(event_id: str, fields: dict) -> dict:
    """For a task create/update: resolve `assignees` (or a bare `owner`) and set `owner` to the names."""
    fields = dict(fields)
    if "assignees" in fields:
        ids, names = resolve(event_id, fields["assignees"])
        fields["assignees"], fields["owner"] = ids, ", ".join(names)
    elif fields.get("owner"):
        try:
            ids, names = resolve(event_id, fields["owner"])
            fields["assignees"], fields["owner"] = ids, ", ".join(names)
        except TeamError:
            pass  # free-text owner (e.g. an outside contact) stays as text
    return fields


def set_pmo(event_id: str, member_id: str) -> None:
    m = db.get_record("team_members", member_id)
    if m is None or m.event_id != event_id:
        raise TeamError(f"Unknown team member '{member_id}'.")
    with db.connect() as conn:
        conn.execute("UPDATE team_members SET is_pmo = (id = ?) WHERE event_id = ?", (member_id, event_id))


def after_member_saved(event_id: str, member_id: str) -> None:
    """Keep the one-PMO rule and refresh the names on that person's tasks (if they were renamed)."""
    m = db.get_record("team_members", member_id)
    if m is None:
        return
    if m.is_pmo:
        set_pmo(event_id, member_id)
    _refresh_owner_text(event_id)


def remove_member(event_id: str, member_id: str) -> int:
    """Delete a member and take them off their tasks. Returns how many tasks were updated."""
    touched = 0
    names = {m.id: m.name for m in db.list_records("team_members", event_id)}
    for t in db.list_records("tasks", event_id):
        if member_id in t.assignees:
            rest = [a for a in t.assignees if a != member_id]
            db.update_record("tasks", t.id, assignees=rest, owner=", ".join(names[a] for a in rest if a in names))
            touched += 1
    db.delete_record("team_members", member_id)
    return touched


def _refresh_owner_text(event_id: str) -> None:
    names = {m.id: m.name for m in db.list_records("team_members", event_id)}
    for t in db.list_records("tasks", event_id):
        if t.assignees:
            text = ", ".join(names[a] for a in t.assignees if a in names)
            if text != t.owner:
                db.update_record("tasks", t.id, owner=text)


def add_creator(event_id: str, user) -> object | None:
    """Whoever creates an event runs it: they join its team as a Lead and the PMO (or are promoted if already listed)."""
    if user is None:
        return None
    existing = next((m for m in db.list_records("team_members", event_id) if m.email.lower() == user.email.lower()), None)
    if existing:
        db.update_record("team_members", existing.id, access="Lead", is_pmo=True)
        member_id = existing.id
    else:
        member_id = db.create_record("team_members", "mem", event_id=event_id, name=user.display_name, email=user.email,
                                     role="Event lead (creator)", team="Leadership", is_pmo=True, access="Lead", source="user").id
    set_pmo(event_id, member_id)
    return db.get_record("team_members", member_id)


# Which teams usually own each task area (used to suggest an owner for unassigned tasks).
AREA_TEAMS = {"Program": ["Program"], "Venue": ["Venue and logistics"], "Food": ["Food"], "Attendees": ["Registration", "Marketing"],
              "Budget": ["Sponsorship and budget"], "General": ["Leadership", "Volunteers"]}


def suggest_owners(tasks, members_out: list[dict]) -> dict:
    """For each open, unassigned task: the member on that area's team with the fewest open tasks (not stretched).
    Falls back to the least-loaded non-PMO member. Plain code, no AI: the reason is shown next to the suggestion."""
    load = {m["id"]: m["open_tasks"] for m in members_out}
    out = {}
    for t in tasks:
        if t.status == "Done" or t.assignees:
            continue
        teams = AREA_TEAMS.get(t.dimension, [])
        pool = [m for m in members_out if m["team"] in teams and not m["stretched"]]
        why = "on the {team} team"
        if not pool:
            pool = [m for m in members_out if not m["is_pmo"] and not m["stretched"]] or members_out
            why = "lightest load on the team"
        if not pool:
            continue
        best = min(pool, key=lambda m: (load[m["id"]], m["name"]))
        load[best["id"]] += 1  # spread several suggestions across people instead of piling them on one
        out[t.id] = {"member_id": best["id"], "name": best["name"],
                     "reason": f"{why.format(team=best['team'])} · {best['open_tasks']} open task{'s' if best['open_tasks'] != 1 else ''}"}
    return out


def workload(event_id: str, today: date | None = None) -> dict:
    """Per member: open, overdue, blocked, high-priority, and done tasks, plus deadlines and workstreams they lead."""
    today = today or demo_today()
    team = members(event_id)
    tasks = db.list_records("tasks", event_id)
    deadlines = db.list_records("deadlines", event_id)
    inits = db.list_records("initiatives", event_id)
    out = []
    for m in team:
        mine = [t for t in tasks if m.id in t.assignees]
        open_ = [t for t in mine if t.status != "Done"]
        out.append(m.model_dump() | {
            "open_tasks": len(open_), "done_tasks": len(mine) - len(open_),
            "overdue_tasks": sum(1 for t in open_ if t.due_date and t.due_date < today),
            "blocked_tasks": sum(1 for t in open_ if t.status == "Blocked"),
            "high_priority_open": sum(1 for t in open_ if t.priority == "High"),
            "shared_tasks": sum(1 for t in mine if len(t.assignees) > 1),
            "deadlines_owned": sum(1 for d in deadlines if not d.done and m.name in d.owner),
            "workstreams_led": [i.title for i in inits if m.name in i.owner],
            "stretched": len(open_) > OVERLOADED_OPEN_TASKS,
        })
    unassigned = [t for t in tasks if t.status != "Done" and not t.assignees]
    pmo = next((m for m in team if m.is_pmo), None)
    return {"members": out, "pmo_id": pmo.id if pmo else None, "pmo_name": pmo.name if pmo else None,
            "unassigned_open_tasks": len(unassigned), "teams": sorted({m.team for m in team}),
            "suggestions": suggest_owners(sorted(unassigned, key=lambda t: (t.due_date or date.max, t.id)), out)}
