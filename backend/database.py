"""SQLite persistence for Stagecraft AI.

Zero-setup: one file at backend/stagecraft.db. Every function opens its own short-lived
connection, so it is safe to call from FastAPI request handlers. Rows go in and come out
as validated Pydantic models; list fields are stored as JSON text.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import TypeVar

from pydantic import BaseModel

from backend.config import DB_PATH
from backend.models import (
    Attendee,
    BudgetItem,
    CateringSLA,
    CrisisScenarioPreset,
    Deadline,
    EventFullState,
    EventSummary,
    Flag,
    InboxMessage,
    Initiative,
    Room,
    RoomStatus,
    Session,
    SessionStatus,
    Speaker,
    SpeakerStatus,
    Task,
    Vendor,
    Venue,
)

M = TypeVar("M", bound=BaseModel)

# Table name -> model. Order matters: parents before children (seed insert order).
TABLES: dict[str, type[BaseModel]] = {
    "venues": Venue,
    "rooms": Room,
    "events": EventSummary,
    "speakers": Speaker,
    "sessions": Session,
    "catering_slas": CateringSLA,
    "crisis_presets": CrisisScenarioPreset,
    # planning layer
    "deadlines": Deadline,
    "initiatives": Initiative,
    "tasks": Task,
    "attendees": Attendee,
    "vendors": Vendor,
    "budget_items": BudgetItem,
    "inbox_messages": InboxMessage,
    "flags": Flag,
}
PLANNING_TABLES = ("deadlines", "initiatives", "tasks", "attendees", "vendors", "budget_items", "inbox_messages", "flags")
WORKSPACE_TABLES = ("chat_messages", "pending_changes", "activity_log", "daily_briefs")  # chat, drafts, change history, briefs

# Columns added after a table first shipped: (table, column, type). init_db adds any that an older database lacks.
MIGRATIONS = [
    ("inbox_messages", "category", "TEXT"), ("inbox_messages", "urgency", "TEXT"), ("inbox_messages", "key_facts", "TEXT"),
    ("inbox_messages", "reply_draft", "TEXT"), ("inbox_messages", "processed_at", "TEXT"), ("pending_changes", "origin", "TEXT"),
]
JSON_FIELDS = {"av_specs", "required_av", "speaker_ids", "key_facts"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL, state_or_country TEXT NOT NULL,
    latitude REAL NOT NULL, longitude REAL NOT NULL, airport_corridor TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY, venue_id TEXT NOT NULL REFERENCES venues(id), name TEXT NOT NULL,
    floor_or_zone TEXT, capacity INTEGER NOT NULL, av_specs TEXT, status TEXT NOT NULL,
    is_catering_hub INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL, date TEXT NOT NULL,
    venue_id TEXT NOT NULL REFERENCES venues(id), attendee_count INTEGER NOT NULL,
    status TEXT NOT NULL, description TEXT
);
CREATE TABLE IF NOT EXISTS speakers (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), name TEXT NOT NULL,
    title_org TEXT, vip_tier TEXT NOT NULL, travel_origin TEXT, arrival_buffer_mins INTEGER,
    status TEXT NOT NULL, email TEXT, dietary_or_av_notes TEXT, bio TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), title TEXT NOT NULL,
    track TEXT NOT NULL, room_id TEXT NOT NULL REFERENCES rooms(id), start_time TEXT NOT NULL,
    end_time TEXT NOT NULL, expected_attendance INTEGER, required_av TEXT, speaker_ids TEXT,
    is_locked_keynote INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catering_slas (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), meal_name TEXT NOT NULL,
    room_id TEXT NOT NULL REFERENCES rooms(id), scheduled_time TEXT NOT NULL,
    hard_cutoff_time TEXT NOT NULL, late_penalty_usd REAL, headcount INTEGER, vendor_name TEXT
);
CREATE TABLE IF NOT EXISTS crisis_presets (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), title TEXT NOT NULL,
    category TEXT NOT NULL, prompt_text TEXT NOT NULL, severity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deadlines (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), title TEXT NOT NULL, dimension TEXT NOT NULL,
    due_date TEXT NOT NULL, owner TEXT, done INTEGER NOT NULL DEFAULT 0, notes TEXT, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS initiatives (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), title TEXT NOT NULL, dimension TEXT NOT NULL,
    owner TEXT, target_date TEXT NOT NULL, description TEXT, blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), title TEXT NOT NULL, dimension TEXT NOT NULL,
    initiative_id TEXT, owner TEXT, due_date TEXT, status TEXT NOT NULL, priority TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendees (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), name TEXT NOT NULL, email TEXT NOT NULL,
    organization TEXT, attendee_type TEXT NOT NULL, rsvp_status TEXT NOT NULL, dietary TEXT NOT NULL, registered_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vendors (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), name TEXT NOT NULL, relationship TEXT NOT NULL,
    category TEXT NOT NULL, contact_name TEXT, contact_email TEXT, contract_status TEXT NOT NULL,
    contract_value_usd REAL NOT NULL DEFAULT 0, notes TEXT
);
CREATE TABLE IF NOT EXISTS budget_items (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), category TEXT NOT NULL, description TEXT NOT NULL,
    vendor_id TEXT, planned_usd REAL NOT NULL, actual_usd REAL NOT NULL DEFAULT 0, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    id TEXT PRIMARY KEY, event_id TEXT, source TEXT NOT NULL, external_id TEXT, from_name TEXT NOT NULL,
    from_email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL,
    status TEXT NOT NULL, dimension TEXT, ai_summary TEXT, category TEXT, urgency TEXT, key_facts TEXT,
    reply_draft TEXT, processed_at TEXT
);
CREATE TABLE IF NOT EXISTS flags (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id), title TEXT NOT NULL, detail TEXT,
    dimension TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL, source_ref TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, tab TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
    created_at TEXT NOT NULL, meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS pending_changes (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, tab TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
    summary TEXT NOT NULL, diff_json TEXT NOT NULL, reason TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
    decided_at TEXT, error TEXT, origin TEXT
);
CREATE TABLE IF NOT EXISTS daily_briefs (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, brief_date TEXT NOT NULL, created_at TEXT NOT NULL,
    content_json TEXT NOT NULL, run_id TEXT
);
CREATE TABLE IF NOT EXISTS activity_log (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
    summary TEXT NOT NULL, ref TEXT
);
CREATE TABLE IF NOT EXISTS resolution_history (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, run_id TEXT NOT NULL, committed_at TEXT NOT NULL,
    option_label TEXT NOT NULL, strategy_type TEXT NOT NULL, changes_json TEXT NOT NULL,
    public_banner TEXT, executive_brief TEXT, ics TEXT
);
"""


# ---- Low-level helpers -------------------------------------------------------


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _to_row(model: BaseModel) -> dict:
    data = model.model_dump(mode="json")
    return {k: json.dumps(v) if k in JSON_FIELDS else v for k, v in data.items()}


def _from_row(model_cls: type[M], row: sqlite3.Row) -> M:
    # NULL -> field default (columns added by a migration are NULL on older rows)
    data = {k: json.loads(row[k]) if k in JSON_FIELDS else row[k] for k in row.keys() if row[k] is not None}
    return model_cls.model_validate(data)


def _insert(conn: sqlite3.Connection, table: str, model: BaseModel) -> None:
    row = _to_row(model)
    cols = ", ".join(row)
    marks = ", ".join(f":{c}" for c in row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", row)


def _select(table: str, where: str = "", params: tuple = (), order: str = "") -> list:
    model_cls = TABLES[table]
    sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "") + (f" ORDER BY {order}" if order else "")
    with connect() as conn:
        return [_from_row(model_cls, r) for r in conn.execute(sql, params).fetchall()]


def _select_one(table: str, record_id: str):
    rows = _select(table, "id = ?", (record_id,))
    return rows[0] if rows else None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ---- Setup / reset -------------------------------------------------------------


def init_db() -> None:
    """Create tables if missing and load the demo seed if the database is empty. A database created before the
    planning layer existed gets its demo planning data backfilled once (dated around the existing event dates)."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, kind in MIGRATIONS:
            if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        if conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 0:
            _load_seed(conn)
        elif conn.execute("SELECT COUNT(*) FROM deadlines").fetchone()[0] == 0:
            _backfill_planning(conn)


def _backfill_planning(conn: sqlite3.Connection) -> None:
    from backend.config import demo_today
    from backend.seed_planning import TECH_AI, YBVS, build_planning_seed

    dates = {r["id"]: date.fromisoformat(r["date"]) for r in conn.execute("SELECT id, date FROM events WHERE id IN (?, ?)", (YBVS, TECH_AI))}
    if len(dates) < 2:
        return
    for table, records in build_planning_seed(dates, demo_today()).items():
        for record in records:
            _insert(conn, table, record)


def reset_demo_db() -> None:
    """Drop everything and reload the clean seed, so every demo/test starts identical."""
    with connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in ["resolution_history", *WORKSPACE_TABLES, *reversed(list(TABLES))]:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        _load_seed(conn)


def _load_seed(conn: sqlite3.Connection) -> None:
    from backend.seed import build_seed_data  # local import keeps seed data out of module load

    seed = build_seed_data()
    for table in TABLES:  # dict order = foreign-key safe insert order
        for record in seed.get(table, []):
            _insert(conn, table, record)


# ---- Venues & rooms -------------------------------------------------------------


def get_venues() -> list[Venue]:
    return _select("venues", order="name")


def get_venue(venue_id: str) -> Venue | None:
    return _select_one("venues", venue_id)


def create_venue(
    name: str,
    city: str,
    state_or_country: str,
    latitude: float,
    longitude: float,
    airport_corridor: str = "",
    notes: str = "",
    venue_id: str | None = None,
) -> Venue:
    venue = Venue(
        id=venue_id or _new_id("ven"), name=name, city=city, state_or_country=state_or_country,
        latitude=latitude, longitude=longitude, airport_corridor=airport_corridor, notes=notes,
    )
    with connect() as conn:
        _insert(conn, "venues", venue)
    return venue


def get_rooms(venue_id: str) -> list[Room]:
    return _select("rooms", "venue_id = ?", (venue_id,), order="capacity DESC")


def get_room(room_id: str) -> Room | None:
    return _select_one("rooms", room_id)


def create_room(
    venue_id: str,
    name: str,
    floor_or_zone: str,
    capacity: int,
    av_specs: list[str] | None = None,
    is_catering_hub: bool = False,
    room_id: str | None = None,
) -> Room:
    room = Room(
        id=room_id or _new_id("rm"), venue_id=venue_id, name=name, floor_or_zone=floor_or_zone,
        capacity=capacity, av_specs=av_specs or [], is_catering_hub=is_catering_hub,
    )
    with connect() as conn:
        _insert(conn, "rooms", room)
    return room


def update_room_status(room_id: str, status: RoomStatus) -> Room:
    room = get_room(room_id)
    if room is None:
        raise KeyError(f"Unknown room: {room_id}")
    updated = Room.model_validate(room.model_dump() | {"status": status})  # rejects unknown statuses
    with connect() as conn:
        conn.execute("UPDATE rooms SET status = ? WHERE id = ?", (status, room_id))
    return updated


# ---- Events ---------------------------------------------------------------------


def get_events() -> list[EventSummary]:
    return _select("events", order="date")


def get_event(event_id: str) -> EventSummary | None:
    return _select_one("events", event_id)


def create_event(
    name: str,
    slug: str,
    event_date: date | str,
    venue_id: str,
    attendee_count: int,
    status: str = "Planning Mode",
    description: str = "",
    event_id: str | None = None,
) -> EventSummary:
    if get_venue(venue_id) is None:
        raise KeyError(f"Unknown venue: {venue_id}")
    event = EventSummary(
        id=event_id or _new_id("evt"), slug=slug, name=name, date=event_date, venue_id=venue_id,
        attendee_count=attendee_count, status=status, description=description,
    )
    with connect() as conn:
        _insert(conn, "events", event)
    return event


def get_event_full_state(event_id: str) -> EventFullState:
    """One call that returns the event plus its venue, rooms, sessions, speakers, catering and crises."""
    event = get_event(event_id)
    if event is None:
        raise KeyError(f"Unknown event: {event_id}")
    return EventFullState(
        event=event,
        venue=get_venue(event.venue_id),
        rooms=get_rooms(event.venue_id),
        sessions=get_sessions(event_id),
        speakers=get_speakers(event_id),
        catering_slas=get_catering_slas(event_id),
        crisis_presets=get_crisis_presets(event_id),
    )


def slug_exists(slug: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM events WHERE slug = ?", (slug,)).fetchone() is not None


def save_event_bundle(
    event: EventSummary,
    speakers: list[Speaker],
    sessions: list[Session],
    catering_slas: list[CateringSLA],
    crisis_presets: list[CrisisScenarioPreset],
    venue: Venue | None = None,
    rooms: list[Room] | None = None,
) -> EventFullState:
    """Insert a whole new event (optionally with a new venue + rooms) in ONE transaction: all or nothing."""
    with connect() as conn:
        if venue is not None:
            _insert(conn, "venues", venue)
        for room in rooms or []:
            _insert(conn, "rooms", room)
        _insert(conn, "events", event)
        for table, records in (("speakers", speakers), ("sessions", sessions),
                               ("catering_slas", catering_slas), ("crisis_presets", crisis_presets)):
            for record in records:
                _insert(conn, table, record)
    return get_event_full_state(event.id)


def delete_event(event_id: str, delete_unused_custom_venue: bool = True) -> dict:
    """Remove an event and everything under it. A custom (non-seed) venue no other event uses goes too."""
    from backend.seed import VENUES as SEED_VENUES

    event = get_event(event_id)
    if event is None:
        raise KeyError(f"Unknown event: {event_id}")
    venue_removed = False
    with connect() as conn:
        for table in ("resolution_history", *WORKSPACE_TABLES, *PLANNING_TABLES, "crisis_presets", "catering_slas", "sessions", "speakers"):
            conn.execute(f"DELETE FROM {table} WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        still_used = conn.execute("SELECT 1 FROM events WHERE venue_id = ?", (event.venue_id,)).fetchone()
        if delete_unused_custom_venue and not still_used and event.venue_id not in {v.id for v in SEED_VENUES}:
            conn.execute("DELETE FROM rooms WHERE venue_id = ?", (event.venue_id,))
            conn.execute("DELETE FROM venues WHERE id = ?", (event.venue_id,))
            venue_removed = True
    return {"deleted_event_id": event_id, "venue_removed": venue_removed}



def delete_venue_if_unused(venue_id: str) -> bool:
    """Remove a custom venue and its rooms when no event uses it (seed venues are never removed)."""
    from backend.seed import VENUES as SEED_VENUES

    if venue_id in {v.id for v in SEED_VENUES}:
        return False
    with connect() as conn:
        if conn.execute("SELECT 1 FROM events WHERE venue_id = ?", (venue_id,)).fetchone():
            return False
        conn.execute("DELETE FROM rooms WHERE venue_id = ?", (venue_id,))
        conn.execute("DELETE FROM venues WHERE id = ?", (venue_id,))
    return True

# ---- Sessions, speakers, catering, crises ----------------------------------------


def get_sessions(event_id: str) -> list[Session]:
    return _select("sessions", "event_id = ?", (event_id,), order="start_time, title")


def get_session(session_id: str) -> Session | None:
    return _select_one("sessions", session_id)


def update_session_schedule(
    session_id: str,
    room_id: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    status: SessionStatus = "Rescheduled",
) -> Session:
    """Move a session to a new room and/or time. Validates the result before saving."""
    session = get_session(session_id)
    if session is None:
        raise KeyError(f"Unknown session: {session_id}")
    if room_id is not None and get_room(room_id) is None:
        raise KeyError(f"Unknown room: {room_id}")
    changes = {"room_id": room_id, "start_time": start_time, "end_time": end_time, "status": status}
    updated = Session.model_validate(session.model_dump() | {k: v for k, v in changes.items() if v is not None})
    with connect() as conn:
        conn.execute(
            "UPDATE sessions SET room_id = ?, start_time = ?, end_time = ?, status = ? WHERE id = ?",
            (updated.room_id, updated.start_time, updated.end_time, updated.status, session_id),
        )
    return updated


def get_speakers(event_id: str) -> list[Speaker]:
    return _select("speakers", "event_id = ?", (event_id,), order="vip_tier, name")


def get_speaker(speaker_id: str) -> Speaker | None:
    return _select_one("speakers", speaker_id)


def update_speaker_status(speaker_id: str, status: SpeakerStatus) -> Speaker:
    speaker = get_speaker(speaker_id)
    if speaker is None:
        raise KeyError(f"Unknown speaker: {speaker_id}")
    updated = Speaker.model_validate(speaker.model_dump() | {"status": status})
    with connect() as conn:
        conn.execute("UPDATE speakers SET status = ? WHERE id = ?", (status, speaker_id))
    return updated


def get_catering_slas(event_id: str) -> list[CateringSLA]:
    return _select("catering_slas", "event_id = ?", (event_id,), order="scheduled_time")


def get_crisis_presets(event_id: str) -> list[CrisisScenarioPreset]:
    return _select("crisis_presets", "event_id = ?", (event_id,), order="id")


# ---- Committed resolutions (war-room history) ------------------------------------


def apply_resolution(moves: list[dict], room_statuses: dict[str, str], attendance: dict[str, int]) -> None:
    """Write an already-verified plan in ONE transaction. Callers must run the Layer 2 check first.
    moves: [{"session_id", "room_id", "start_time", "end_time"}]."""
    with connect() as conn:
        for m in moves:
            conn.execute("UPDATE sessions SET room_id = ?, start_time = ?, end_time = ?, status = 'Rescheduled' WHERE id = ?",
                         (m["room_id"], m["start_time"], m["end_time"], m["session_id"]))
        for session_id, n in attendance.items():
            conn.execute("UPDATE sessions SET expected_attendance = ? WHERE id = ?", (n, session_id))
        for room_id, status in room_statuses.items():
            conn.execute("UPDATE rooms SET status = ? WHERE id = ?", (status, room_id))


def record_resolution(event_id: str, run_id: str, option_label: str, strategy_type: str, changes: list[dict],
                      public_banner: str = "", executive_brief: str = "", ics: str = "") -> str:
    from datetime import datetime, timezone

    history_id = _new_id("res")
    with connect() as conn:
        conn.execute("INSERT INTO resolution_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (history_id, event_id, run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      option_label, strategy_type, json.dumps(changes), public_banner, executive_brief, ics))
    return history_id


def get_resolution_history(event_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM resolution_history WHERE event_id = ? ORDER BY committed_at DESC, rowid DESC",
                            (event_id,)).fetchall()
    return [dict(r) | {"changes": json.loads(r["changes_json"])} for r in rows]


def run_already_committed(run_id: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM resolution_history WHERE run_id = ?", (run_id,)).fetchone() is not None


def get_session_update_badges(event_id: str) -> dict[str, dict]:
    """session_id -> latest war-room change, for the 'Updated via Stagecraft War-Room' badge."""
    badges: dict[str, dict] = {}
    for entry in reversed(get_resolution_history(event_id)):  # oldest first, so the newest change wins
        for c in entry["changes"]:
            badges[c["session_id"]] = {"badge": "Updated via Stagecraft War-Room", "committed_at": entry["committed_at"],
                                       "run_id": entry["run_id"], "from": c["from"], "to": c["to"]}
    return badges


# ---- Planning layer: one validated CRUD API for every planning table ---------------------------------


def list_records(table: str, event_id: str, order: str = "") -> list:
    if table not in PLANNING_TABLES:
        raise KeyError(f"Not a planning table: {table}")
    return _select(table, "event_id = ?", (event_id,), order=order)


def get_record(table: str, record_id: str):
    if table not in PLANNING_TABLES:
        raise KeyError(f"Not a planning table: {table}")
    return _select_one(table, record_id)


def insert_records(table: str, records: list[BaseModel]) -> None:
    if table not in PLANNING_TABLES:
        raise KeyError(f"Not a planning table: {table}")
    with connect() as conn:
        for record in records:
            _insert(conn, table, record)


def create_record(table: str, prefix: str, **fields) -> BaseModel:
    """Validate and insert one planning record (id generated). Raises pydantic.ValidationError on bad input."""
    record = TABLES[table].model_validate({"id": _new_id(prefix)} | fields)
    insert_records(table, [record])
    return record


def update_record(table: str, record_id: str, **changes) -> BaseModel:
    """Validate the merged record before writing, so a bad status or date never reaches the database."""
    current = get_record(table, record_id)
    if current is None:
        raise KeyError(f"Unknown {table[:-1]} id: {record_id}")
    blocked = {"id", "event_id"} & set(changes)
    if blocked:
        raise ValueError(f"Cannot change {sorted(blocked)}")
    updated = TABLES[table].model_validate(current.model_dump() | changes)
    row = _to_row(updated)
    sets = ", ".join(f"{k} = :{k}" for k in row if k != "id")
    with connect() as conn:
        conn.execute(f"UPDATE {table} SET {sets} WHERE id = :id", row)
    return updated


def delete_record(table: str, record_id: str) -> bool:
    if table not in PLANNING_TABLES:
        raise KeyError(f"Not a planning table: {table}")
    with connect() as conn:
        return conn.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,)).rowcount > 0


def update_catering_sla(sla_id: str, **changes) -> CateringSLA:
    """Validated update of a catering SLA (headcount, times, penalty, vendor)."""
    current = _select_one("catering_slas", sla_id)
    if current is None:
        raise KeyError(f"Unknown catering SLA: {sla_id}")
    if {"id", "event_id"} & set(changes):
        raise ValueError("Cannot change id or event_id")
    updated = CateringSLA.model_validate(current.model_dump() | changes)
    row = _to_row(updated)
    with connect() as conn:
        conn.execute(f"UPDATE catering_slas SET {', '.join(f'{k} = :{k}' for k in row if k != 'id')} WHERE id = :id", row)
    return updated

