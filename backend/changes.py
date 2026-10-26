"""Confirm-before-save for every assistant edit.

Assistants never write to the database. Their write tools call `draft_*`, which validates the change right away
(so the assistant gets immediate, specific feedback) and stores it as a *pending change* with a human-readable
before/after diff. Nothing happens until the organizer confirms. `confirm_change` re-validates against the data as it
is *now* (refusing stale edits), applies it, and writes the activity log that the daily brief reads.

Schedule edits (session moves, room status, speaker status) go through the Layer 2 action lock
(`security.validate_proposed_resolution`) both when drafted and when confirmed.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ValidationError

from backend import database as db
from backend import notify, security
from backend import team as team_mod
from backend.config import demo_today

RECORD_TYPES: dict[str, tuple[str, str]] = {  # record_type -> (table, id prefix)
    "deadline": ("deadlines", "dl"),
    "task": ("tasks", "tsk"),
    "initiative": ("initiatives", "ini"),
    "attendee": ("attendees", "att"),
    "vendor": ("vendors", "vnd"),
    "budget_item": ("budget_items", "bud"),
    "flag": ("flags", "flg"),
    "member": ("team_members", "mem"),
}
MAX_BULK_ROWS = 200


class ChangeError(ValueError):
    """A drafted change is invalid. The message is written for the assistant (and the person) to act on."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _label(record: BaseModel) -> str:
    for attr in ("title", "name", "description", "meal_name"):
        if getattr(record, attr, None):
            return str(getattr(record, attr))
    return getattr(record, "id", "record")


def _disp(event_id: str, field: str, v: Any) -> Any:
    """What a change card shows for a field (assignee ids become names)."""
    return team_mod.names_for(event_id, v) if field == "assignees" else _show(v)


def _with_team(event_id: str, table: str, fields: dict) -> dict:
    if table not in ("tasks", "team_members"):
        return fields
    try:
        if table == "team_members":
            return team_mod.validate_member_fields(fields)
        return team_mod.normalize_task_fields(event_id, fields)
    except team_mod.TeamError as exc:
        raise ChangeError(str(exc)) from exc


def _show(v: Any) -> Any:
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)


def _pydantic_msg(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:4])


def _layer2(event_id: str, change: dict) -> None:
    result = security.validate_proposed_resolution(event_id, [change])
    if not result["is_safe"]:
        raise ChangeError("Blocked by the safety check: " + "; ".join(r for rej in result["rejections"] for r in rej["reasons"]))


# ---- Storage -------------------------------------------------------------------------------------


def _store(event_id: str, tab: str, kind: str, payload: dict, summary: str, diff: list[dict], reason: str) -> dict:
    change = {"id": _new_id("chg"), "event_id": event_id, "tab": tab, "kind": kind, "payload": payload, "summary": summary,
              "diff": diff, "reason": reason, "status": "Pending", "created_at": _now(), "decided_at": None, "error": None, "origin": None}
    with db.connect() as conn:
        conn.execute("INSERT INTO pending_changes (id, event_id, tab, kind, payload_json, summary, diff_json, reason, status, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (change["id"], event_id, tab, kind, json.dumps(payload, default=str), summary, json.dumps(diff, default=str),
                      reason, "Pending", change["created_at"]))
    return change


def _row_to_change(r) -> dict:
    return {"id": r["id"], "event_id": r["event_id"], "tab": r["tab"], "kind": r["kind"], "payload": json.loads(r["payload_json"]),
            "summary": r["summary"], "diff": json.loads(r["diff_json"]), "reason": r["reason"], "status": r["status"],
            "created_at": r["created_at"], "decided_at": r["decided_at"], "error": r["error"], "origin": r["origin"]}


def get_change(change_id: str) -> dict | None:
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM pending_changes WHERE id = ?", (change_id,)).fetchone()
    return _row_to_change(r) if r else None


def list_changes(event_id: str, tab: str | None = None, status: str | None = "Pending") -> list[dict]:
    sql, params = "SELECT * FROM pending_changes WHERE event_id = ?", [event_id]
    if tab:
        sql, params = sql + " AND tab = ?", params + [tab]
    if status:
        sql, params = sql + " AND status = ?", params + [status]
    with db.connect() as conn:
        return [_row_to_change(r) for r in conn.execute(sql + " ORDER BY created_at, rowid", params).fetchall()]


def set_origin(change_ids: list[str], origin: str) -> None:
    """Tag drafts with where they came from (e.g. 'inbox:msg_123') so the UI can show them next to the email."""
    with db.connect() as conn:
        conn.executemany("UPDATE pending_changes SET origin = ? WHERE id = ?", [(origin, cid) for cid in change_ids])


def _set_status(change_id: str, status: str, error: str | None = None) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE pending_changes SET status = ?, decided_at = ?, error = ? WHERE id = ?", (status, _now(), error, change_id))


def log_activity(event_id: str, actor: str, action: str, summary: str, ref: str = "") -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO activity_log VALUES (?, ?, ?, ?, ?, ?, ?)", (_new_id("act"), event_id, _now(), actor, action, summary, ref))


def list_activity(event_id: str, since: str | None = None, limit: int = 100) -> list[dict]:
    sql, params = "SELECT * FROM activity_log WHERE event_id = ?", [event_id]
    if since:
        sql, params = sql + " AND at >= ?", params + [since]
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY at DESC, rowid DESC LIMIT ?", params + [limit]).fetchall()]


def add_chat_message(event_id: str, tab: str, role: str, content: str, meta: dict | None = None) -> dict:
    msg = {"id": _new_id("msg"), "event_id": event_id, "tab": tab, "role": role, "content": content, "created_at": _now(), "meta": meta or {}}
    with db.connect() as conn:
        conn.execute("INSERT INTO chat_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (msg["id"], event_id, tab, role, content, msg["created_at"], json.dumps(msg["meta"], default=str)))
    return msg


def chat_history(event_id: str, tab: str, limit: int = 50) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM chat_messages WHERE event_id = ? AND tab = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                            (event_id, tab, limit)).fetchall()
    return [dict(r) | {"meta": json.loads(r["meta_json"])} for r in reversed(rows)]


# ---- Drafting (validate now, apply later) -----------------------------------------------------------


def _table_for(record_type: str) -> tuple[str, str, type[BaseModel]]:
    if record_type not in RECORD_TYPES:
        raise ChangeError(f"Unknown record_type '{record_type}'. Use one of: {', '.join(RECORD_TYPES)}.")
    table, prefix = RECORD_TYPES[record_type]
    return table, prefix, db.TABLES[table]


def _existing(event_id: str, table: str, record_id: str) -> BaseModel:
    record = db.get_record(table, record_id)
    if record is None or record.event_id != event_id:
        raise ChangeError(f"No {table[:-1].replace('_', ' ')} with id '{record_id}' in this event. Look it up first.")
    return record


def _prepare_fields(table: str, fields: dict, event_id: str, dimension: str | None) -> dict:
    fields = {k: v for k, v in fields.items() if k not in ("id",)} | {"event_id": event_id}
    if table in ("deadlines", "tasks", "initiatives", "flags") and dimension and not fields.get("dimension"):
        fields["dimension"] = dimension
    if table in ("deadlines", "tasks", "flags"):
        fields.setdefault("source", "assistant")
    if table == "flags":
        fields.setdefault("created_at", _now())
    if table == "attendees":
        fields.setdefault("registered_on", demo_today().isoformat())
    return fields


def draft_update(event_id: str, tab: str, record_type: str, record_id: str, changes: dict, reason: str = "") -> dict:
    table, _, model = _table_for(record_type)
    current = _existing(event_id, table, record_id)
    unknown = set(changes) - set(model.model_fields)
    if unknown:
        raise ChangeError(f"Unknown field(s) {sorted(unknown)}. Valid fields: {', '.join(f for f in model.model_fields if f not in ('id', 'event_id'))}.")
    if {"id", "event_id"} & set(changes):
        raise ChangeError("id and event_id can't be changed.")
    changes = _with_team(event_id, table, changes)
    try:
        updated = model.model_validate(current.model_dump() | changes)
    except ValidationError as exc:
        raise ChangeError(f"Invalid value: {_pydantic_msg(exc)}") from exc
    before, after = current.model_dump(mode="json"), updated.model_dump(mode="json")
    changed = [k for k in changes if before[k] != after[k]]
    shown = [k for k in changed if not (k == "owner" and "assignees" in changed)]  # owner text follows assignees
    diff = [{"field": k, "before": _disp(event_id, k, before[k]), "after": _disp(event_id, k, after[k])} for k in shown]
    if not diff:
        raise ChangeError("No change: those values are already set.")
    summary = f"Update {record_type.replace('_', ' ')} '{_label(current)}': " + ", ".join(f"{d['field']} {d['before']} → {d['after']}" for d in diff)
    return _store(event_id, tab, "update_record", {"table": table, "record_id": record_id,
                                                   "changes": {k: after[k] for k in changed}}, summary, diff, reason)


def draft_create(event_id: str, tab: str, record_type: str, fields: dict, reason: str = "", dimension: str | None = None) -> dict:
    return draft_bulk_create(event_id, tab, record_type, [fields], reason, dimension)


def draft_bulk_create(event_id: str, tab: str, record_type: str, rows: list[dict], reason: str = "", dimension: str | None = None) -> dict:
    table, _, model = _table_for(record_type)
    if not rows:
        raise ChangeError("Nothing to add.")
    if len(rows) > MAX_BULK_ROWS:
        raise ChangeError(f"At most {MAX_BULK_ROWS} rows per change; split the list.")
    prepared, errors = [], []
    for i, row in enumerate(rows, 1):
        fields = _prepare_fields(table, dict(row), event_id, dimension)
        try:
            fields = _with_team(event_id, table, fields)
            model.model_validate({"id": "draft"} | fields)
            prepared.append(fields)
        except ValidationError as exc:
            errors.append(f"row {i}: {_pydantic_msg(exc)}")
    if errors:
        raise ChangeError("Invalid rows - " + " | ".join(errors[:5]))
    names = [str(f.get("name") or f.get("title") or f.get("description")) for f in prepared]
    noun = record_type.replace("_", " ")
    summary = (f"Add {noun} '{names[0]}'" if len(prepared) == 1
               else f"Add {len(prepared)} {noun}s: {', '.join(names[:5])}{' and more' if len(names) > 5 else ''}")
    diff = [{"field": k, "before": None, "after": _disp(event_id, k, v)} for k, v in prepared[0].items() if k != "event_id"] if len(prepared) == 1 \
        else [{"field": "rows", "before": None, "after": len(prepared)}]
    return _store(event_id, tab, "create_records", {"table": table, "rows": prepared}, summary, diff, reason)


def draft_delete(event_id: str, tab: str, record_type: str, record_id: str, reason: str = "") -> dict:
    table, _, _ = _table_for(record_type)
    current = _existing(event_id, table, record_id)
    return _store(event_id, tab, "delete_record", {"table": table, "record_id": record_id},
                  f"Delete {record_type.replace('_', ' ')} '{_label(current)}'", [{"field": "record", "before": _label(current), "after": None}], reason)


def draft_session_move(event_id: str, tab: str, session_id: str, new_room_id: str = "", new_start_time: str = "", new_end_time: str = "",
                       expected_attendance: int | None = None, reason: str = "") -> dict:
    s = db.get_session(session_id)
    if s is None or s.event_id != event_id:
        raise ChangeError(f"No session '{session_id}' in this event.")
    room_id, start, end = new_room_id or s.room_id, new_start_time or s.start_time, new_end_time or s.end_time
    if (room_id, start, end) == (s.room_id, s.start_time, s.end_time) and expected_attendance in (None, s.expected_attendance):
        raise ChangeError("That's where the session already is.")
    _layer2(event_id, {"action": "move_session", "session_id": session_id, "room_id": room_id, "new_start_time": start,
                       "new_end_time": end, "expected_attendance": expected_attendance})
    old_room, new_room = db.get_room(s.room_id), db.get_room(room_id)
    diff = [{"field": "room", "before": old_room.name, "after": new_room.name}, {"field": "time", "before": f"{s.start_time}-{s.end_time}", "after": f"{start}-{end}"}]
    if expected_attendance and expected_attendance != s.expected_attendance:
        diff.append({"field": "expected_attendance", "before": s.expected_attendance, "after": expected_attendance})
    return _store(event_id, tab, "session_move", {"session_id": session_id, "room_id": room_id, "start_time": start, "end_time": end,
                                                  "expected_attendance": expected_attendance,
                                                  "before": {"room_id": s.room_id, "start_time": s.start_time, "end_time": s.end_time}},
                  f"Move '{s.title}' from {old_room.name} {s.start_time}-{s.end_time} to {new_room.name} {start}-{end}", diff, reason)


def draft_room_status(event_id: str, tab: str, room_id: str, status: str, reason: str = "") -> dict:
    room = db.get_room(room_id)
    if room is None or room.venue_id != db.get_event(event_id).venue_id:
        raise ChangeError(f"No room '{room_id}' at this event's venue.")
    if room.status == status:
        raise ChangeError(f"{room.name} is already {status}.")
    _layer2(event_id, {"action": "update_room_status", "room_id": room_id, "status": status})
    return _store(event_id, tab, "room_status", {"room_id": room_id, "status": status, "before": room.status},
                  f"Set {room.name} to {status}", [{"field": "status", "before": room.status, "after": status}], reason)


def draft_speaker_status(event_id: str, tab: str, speaker_id: str, status: str, reason: str = "") -> dict:
    sp = db.get_speaker(speaker_id)
    if sp is None or sp.event_id != event_id:
        raise ChangeError(f"No speaker '{speaker_id}' in this event.")
    if sp.status == status:
        raise ChangeError(f"{sp.name} is already {status}.")
    _layer2(event_id, {"action": "update_speaker_status", "speaker_id": speaker_id, "status": status})
    return _store(event_id, tab, "speaker_status", {"speaker_id": speaker_id, "status": status, "before": sp.status},
                  f"Set {sp.name} to {status}", [{"field": "status", "before": sp.status, "after": status}], reason)


def draft_catering_update(event_id: str, tab: str, sla_id: str, changes: dict, reason: str = "") -> dict:
    from backend.models import CateringSLA

    sla = next((c for c in db.get_catering_slas(event_id) if c.id == sla_id), None)
    if sla is None:
        raise ChangeError(f"No catering service '{sla_id}' in this event.")
    if {"id", "event_id"} & set(changes) or set(changes) - set(CateringSLA.model_fields):
        raise ChangeError(f"Editable fields: {', '.join(f for f in CateringSLA.model_fields if f not in ('id', 'event_id'))}.")
    try:
        updated = CateringSLA.model_validate(sla.model_dump() | changes)
    except ValidationError as exc:
        raise ChangeError(f"Invalid value: {_pydantic_msg(exc)}") from exc
    before, after = sla.model_dump(), updated.model_dump()
    diff = [{"field": k, "before": _show(before[k]), "after": _show(after[k])} for k in changes if before[k] != after[k]]
    if not diff:
        raise ChangeError("No change: those values are already set.")
    return _store(event_id, tab, "catering_update", {"sla_id": sla_id, "changes": {d["field"]: after[d["field"]] for d in diff}},
                  f"Update catering '{sla.meal_name}': " + ", ".join(f"{d['field']} {d['before']} → {d['after']}" for d in diff), diff, reason)


# ---- Confirm / reject ----------------------------------------------------------------------------------


def touches_budget(change: dict) -> bool:
    """Money changes (budget lines, vendor contract values) need a lead's OK."""
    p = change.get("payload") or {}
    if p.get("table") == "budget_items":
        return True
    return p.get("table") == "vendors" and any(d.get("field") == "contract_value_usd" for d in change.get("diff") or [])


def _apply(change: dict, actor: str = "", actor_user_id: str | None = None) -> None:
    p, eid = change["payload"], change["event_id"]
    kind = change["kind"]
    if kind == "update_record":
        current = db.get_record(p["table"], p["record_id"])
        if current is None:
            raise ChangeError("The record no longer exists.")
        now = current.model_dump(mode="json")
        stale = [d["field"] for d in change["diff"] if _disp(eid, d["field"], now.get(d["field"])) != d["before"]]
        if stale:
            raise ChangeError(f"Someone changed {', '.join(stale)} after this was drafted. Ask the assistant again.")
        updated = db.update_record(p["table"], p["record_id"], **p["changes"])
        if p["table"] == "team_members":
            team_mod.after_member_saved(eid, p["record_id"])
        if p["table"] == "tasks" and "assignees" in p["changes"]:
            notify.assignment_changed(eid, updated, current.assignees, updated.assignees, actor_user_id, actor)
    elif kind == "create_records":
        prefix = next(pre for t, pre in RECORD_TYPES.values() if t == p["table"])
        models = [db.TABLES[p["table"]].model_validate({"id": _new_id(prefix)} | row) for row in p["rows"]]
        db.insert_records(p["table"], models)
        if p["table"] == "team_members":
            for m in models:
                team_mod.after_member_saved(eid, m.id)
        for m in models:
            if p["table"] == "tasks" and m.assignees:
                notify.assignment_changed(eid, m, [], m.assignees, actor_user_id, actor)
            elif p["table"] == "flags":
                notify.flag_raised(eid, m)
    elif kind == "delete_record":
        if p["table"] == "team_members" and db.get_record("team_members", p["record_id"]):
            team_mod.remove_member(eid, p["record_id"])
        elif not db.delete_record(p["table"], p["record_id"]):
            raise ChangeError("The record no longer exists.")
    elif kind == "session_move":
        s = db.get_session(p["session_id"])
        if (s.room_id, s.start_time, s.end_time) != tuple(p["before"].values()):
            raise ChangeError("The session moved after this was drafted. Ask the assistant again.")
        _layer2(eid, {"action": "move_session", "session_id": p["session_id"], "room_id": p["room_id"], "new_start_time": p["start_time"],
                      "new_end_time": p["end_time"], "expected_attendance": p["expected_attendance"]})
        db.apply_resolution([{"session_id": p["session_id"], "room_id": p["room_id"], "start_time": p["start_time"], "end_time": p["end_time"]}],
                            {}, {p["session_id"]: p["expected_attendance"]} if p["expected_attendance"] else {})
    elif kind == "room_status":
        _layer2(eid, {"action": "update_room_status", "room_id": p["room_id"], "status": p["status"]})
        db.update_room_status(p["room_id"], p["status"])
    elif kind == "speaker_status":
        db.update_speaker_status(p["speaker_id"], p["status"])
    elif kind == "catering_update":
        db.update_catering_sla(p["sla_id"], **p["changes"])
    else:
        raise ChangeError(f"Unknown change kind '{kind}'.")


def confirm_change(change_id: str, actor: str = "organizer", actor_user_id: str | None = None, can_approve_budget: bool = True) -> dict:
    change = get_change(change_id)
    if change is None:
        raise KeyError(f"Unknown change '{change_id}'.")
    if change["status"] != "Pending":
        raise ChangeError(f"This change is already {change['status'].lower()}.")
    if touches_budget(change) and not can_approve_budget:
        raise ChangeError("Only the PMO or an event lead can approve budget changes. It stays waiting for a lead's OK.")
    try:
        _apply(change, actor, actor_user_id)
    except (ChangeError, ValidationError, ValueError, KeyError) as exc:
        msg = _pydantic_msg(exc) if isinstance(exc, ValidationError) else str(exc)
        _set_status(change_id, "Failed", msg)
        add_chat_message(change["event_id"], change["tab"], "system", f"Couldn't apply: {change['summary']}. {msg}", {"change_id": change_id})
        return get_change(change_id)
    _set_status(change_id, "Applied")
    log_activity(change["event_id"], f"{change['tab']} assistant, confirmed by {actor}", change["kind"], change["summary"], change_id)
    add_chat_message(change["event_id"], change["tab"], "system", f"Applied: {change['summary']}", {"change_id": change_id})
    return get_change(change_id)


def reject_change(change_id: str, actor: str = "organizer") -> dict:
    change = get_change(change_id)
    if change is None:
        raise KeyError(f"Unknown change '{change_id}'.")
    if change["status"] != "Pending":
        raise ChangeError(f"This change is already {change['status'].lower()}.")
    _set_status(change_id, "Rejected")
    add_chat_message(change["event_id"], change["tab"], "system", f"Discarded: {change['summary']}", {"change_id": change_id})
    return get_change(change_id)
