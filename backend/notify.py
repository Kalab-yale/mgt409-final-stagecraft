"""Notifications: what each signed-in person should know about.

Accounts are linked to the organizing team by email (an account whose email matches a team member *is* that member).
Stored notifications:
  - "assigned"  - a task was assigned to you (by someone else)
  - "flag"      - a new Critical/High red flag on an event you lead (from an email or an assistant)
  - "announcement" - a lead posted a team update (everyone on the event's team who has an account)
Computed live (always current, never stale):
  - "approvals" - changes waiting for your OK on events you lead
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from backend import database as db

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT, link TEXT, created_at TEXT NOT NULL, read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at);
CREATE TABLE IF NOT EXISTS announcements (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, author_user_id TEXT, author_name TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT NOT NULL, ai_drafted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_announcements_event ON announcements(event_id, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init() -> None:
    with db.connect() as conn:
        conn.executescript(SCHEMA)


# ---- who is who -----------------------------------------------------------------------------------


def member_for(event_id: str, user) -> object | None:
    """The team member this account is on an event (matched by email), or None."""
    if user is None:
        return None
    email = user.email.lower()
    return next((m for m in db.list_records("team_members", event_id) if m.email.lower() == email), None)


def access_for(event_id: str, user) -> dict:
    m = member_for(event_id, user)
    lead = bool(m and (m.access == "Lead" or m.is_pmo))
    return {"member_id": m.id if m else None, "name": m.name if m else None, "is_member": m is not None,
            "is_lead": lead, "can_approve_budget": lead}


def _users_for_emails(emails: set[str]) -> dict[str, str]:
    """email -> user id, for team emails that have an account."""
    if not emails:
        return {}
    with db.connect() as conn:
        rows = conn.execute(f"SELECT id, email FROM users WHERE lower(email) IN ({','.join('?' * len(emails))})",
                            tuple(e.lower() for e in emails)).fetchall()
    return {r["email"].lower(): r["id"] for r in rows}


def lead_users(event_id: str) -> list[str]:
    leads = {m.email.lower() for m in db.list_records("team_members", event_id) if m.email and (m.access == "Lead" or m.is_pmo)}
    return list(_users_for_emails(leads).values())


# ---- writing --------------------------------------------------------------------------------------


def _add(user_ids, event_id: str, kind: str, title: str, body: str = "", link: str = "", skip_user: str | None = None) -> int:
    rows = [(f"ntf_{uuid.uuid4().hex[:12]}", u, event_id, kind, title, body, link, _now()) for u in dict.fromkeys(user_ids) if u and u != skip_user]
    if rows:
        init()
        with db.connect() as conn:
            conn.executemany("INSERT INTO notifications (id, user_id, event_id, kind, title, body, link, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def assignment_changed(event_id: str, task, before: list[str], after: list[str], actor_user_id: str | None = None, actor: str = "") -> int:
    """Tell the people newly added to a task (not the person who made the change)."""
    added = [a for a in after if a not in (before or [])]
    if not added:
        return 0
    members = {m.id: m for m in db.list_records("team_members", event_id)}
    users = _users_for_emails({members[a].email.lower() for a in added if a in members and members[a].email})
    event = db.get_event(event_id)
    due = f" · due {task.due_date:%a %b %-d}" if getattr(task, "due_date", None) else ""
    return _add(users.values(), event_id, "assigned", f"Assigned to you: {task.title}",
                f"{event.name if event else ''}{due}{f' · by {actor}' if actor else ''}", f"#/event/{event_id}/team", skip_user=actor_user_id)


def flag_raised(event_id: str, flag) -> int:
    if flag.severity not in ("Critical", "High"):
        return 0
    event = db.get_event(event_id)
    return _add(lead_users(event_id), event_id, "flag", f"{flag.severity} red flag: {flag.title}",
                f"{event.name if event else ''} · {flag.detail[:140]}", f"#/event/{event_id}/overview")


def team_users(event_id: str) -> list[str]:
    emails = {m.email.lower() for m in db.list_records("team_members", event_id) if m.email}
    return list(_users_for_emails(emails).values())


def announce(event_id: str, title: str, body: str, author_user_id: str | None, author_name: str, ai_drafted: bool = False) -> dict:
    """Post a team update: stored on the event (the Overview feed) and sent to every teammate's bell. In-app only."""
    init()
    row = {"id": f"ann_{uuid.uuid4().hex[:10]}", "event_id": event_id, "author_user_id": author_user_id, "author_name": author_name,
           "title": title, "body": body, "ai_drafted": int(ai_drafted), "created_at": _now()}
    with db.connect() as conn:
        conn.execute(f"INSERT INTO announcements ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    event = db.get_event(event_id)
    row["notified"] = _add(team_users(event_id), event_id, "announcement", f"Team update: {title}",
                           f"{author_name} · {event.name if event else ''} · {body[:140]}", f"#/event/{event_id}/overview", skip_user=author_user_id)
    return row


def announcements(event_id: str, limit: int = 10) -> list[dict]:
    init()
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM announcements WHERE event_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                              (event_id, limit)).fetchall()]


# ---- reading --------------------------------------------------------------------------------------


def for_user(user, limit: int = 30) -> dict:
    init()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                              (user.id, limit)).fetchall()]
        pending = conn.execute("SELECT event_id, COUNT(*) n FROM pending_changes WHERE status = 'Pending' GROUP BY event_id").fetchall()
    live = []
    for r in pending:
        if access_for(r["event_id"], user)["is_lead"]:
            ev = db.get_event(r["event_id"])
            live.append({"id": f"approvals:{r['event_id']}", "kind": "approvals", "event_id": r["event_id"], "created_at": _now(), "read_at": None,
                         "title": f"{r['n']} change{'s' if r['n'] != 1 else ''} waiting for your OK", "body": ev.name if ev else "",
                         "link": f"#/event/{r['event_id']}/overview"})
    unread = sum(1 for r in rows if not r["read_at"]) + len(live)
    return {"items": live + rows, "unread": unread}


def mark_read(user, ids: list[str] | None = None) -> int:
    init()
    with db.connect() as conn:
        if ids:
            q = f"UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL AND id IN ({','.join('?' * len(ids))})"
            return conn.execute(q, (_now(), user.id, *ids)).rowcount
        return conn.execute("UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL", (_now(), user.id)).rowcount


# ---- "My work" ------------------------------------------------------------------------------------


def my_work(user) -> dict:
    """Across every event: my open tasks, the deadlines I own, and approvals waiting for me."""
    from backend.config import demo_today
    from backend.planning import deadline_status

    today = demo_today()
    tasks, deadlines, approvals = [], [], []
    for e in sorted(db.get_events(), key=lambda e: e.date):
        acc = access_for(e.id, user)
        if not acc["is_member"]:
            continue
        for t in db.list_records("tasks", e.id):
            if t.status != "Done" and acc["member_id"] in t.assignees:
                tasks.append({"id": t.id, "event_id": e.id, "event_name": e.name, "title": t.title, "status": t.status, "priority": t.priority,
                              "due_date": t.due_date.isoformat() if t.due_date else None, "shared_with": len(t.assignees) - 1,
                              "overdue": bool(t.due_date and t.due_date < today)})
        for d in db.list_records("deadlines", e.id):
            if not d.done and acc["name"] in (d.owner or ""):
                deadlines.append({"id": d.id, "event_id": e.id, "event_name": e.name, "title": d.title, "due_date": d.due_date.isoformat(),
                                  "status": deadline_status(d, today)})
        if acc["is_lead"]:
            with db.connect() as conn:
                n = conn.execute("SELECT COUNT(*) FROM pending_changes WHERE event_id = ? AND status = 'Pending'", (e.id,)).fetchone()[0]
            if n:
                approvals.append({"event_id": e.id, "event_name": e.name, "count": n})
    key = lambda x: (x["due_date"] or "9999-12-31")  # noqa: E731
    return {"tasks": sorted(tasks, key=key), "deadlines": sorted(deadlines, key=key), "approvals": approvals,
            "memberships": [{"event_id": e.id, "event_name": e.name, **access_for(e.id, user)} for e in db.get_events() if member_for(e.id, user)]}
