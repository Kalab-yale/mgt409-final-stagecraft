"""Deterministic operations tools for the Stagecraft agents.

No LLM calls happen here. The agents decide *what* to try; these functions do the
operations math (capacity, AV, double-booking, speaker buffers, catering penalties,
weather risk) so every answer is exact and repeatable. Each tool returns plain
JSON-friendly dicts so an agent can read the result directly. Unknown ids come back
as {"error": ...} instead of raising, so an agent can recover and retry.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

import httpx

from backend import database as db
from backend.models import EventFullState, Room, Session, minutes

# ---- Shared constants & helpers ----------------------------------------------------------

SLOT_STEP_MINS = 15          # solver tries start times on a 15-minute grid
VENUE_OPEN, VENUE_CLOSE = "08:00", "18:00"
MIN_TRANSITION_MINS = 5      # room turnover we never compress below
CATERING_SAFETY_BUFFER_MINS = 10
CATERING_TRACK = "Catering & Networking"

# Room AV that can stand in for a required item (e.g. a laser projector covers "Dual Projection").
AV_EQUIVALENTS: dict[str, set[str]] = {
    "Dual Projection": {"Dual Laser Projection", "LED Video Wall"},
    "Standard Projection": {"Dual Projection", "Dual Laser Projection", "LED Video Wall"},
    "Lecture Capture": {"Live Broadcast Rig"},
    "Handheld Mics": {"Stage Mic Array", "Tiered Seating Mics"},
    "Lapel Mic": {"Stage Mic Array", "Tiered Seating Mics", "Handheld Mics"},
    "Ambient PA": {"Stage Mic Array", "Live Broadcast Rig"},
}
# Gear the AV crew can wheel in from the portable backup kit (a warning, not a blocker).
PORTABLE_AV = {"Standard Projection", "Lapel Mic", "Handheld Mics", "Whiteboard", "Ambient PA", "Confidence Monitor"}

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _hhmm(total_mins: int) -> str:
    return f"{total_mins // 60:02d}:{total_mins % 60:02d}"


def _overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    return minutes(start_a) < minutes(end_b) and minutes(start_b) < minutes(end_a)


def _load(event_id: str) -> EventFullState | None:
    try:
        return db.get_event_full_state(event_id)
    except KeyError:
        return None


def _with_room_status(state: EventFullState, room_status_overrides: dict[str, str] | None) -> EventFullState:
    """What-if view: apply reported room statuses (e.g. a projector just failed) without writing to the DB."""
    if not room_status_overrides:
        return state
    rooms = [r.model_copy(update={"status": room_status_overrides[r.id]}) if r.id in room_status_overrides else r
             for r in state.rooms]
    return state.model_copy(update={"rooms": rooms})


def _match_av(required: Iterable[str], room_av: Iterable[str]) -> dict:
    """How well a room's AV covers a session's needs, including substitutions and portable gear."""
    required, have = list(required), set(room_av)
    matched, substitutions, portable, missing = [], {}, [], []
    for item in required:
        if item in have:
            matched.append(item)
        elif sub := next((s for s in sorted(AV_EQUIVALENTS.get(item, ())) if s in have), None):
            matched.append(item)
            substitutions[item] = sub
        elif item in PORTABLE_AV:
            portable.append(item)
        else:
            missing.append(item)
    pct = round(100 * len(matched) / len(required)) if required else 100
    return {"av_match_pct": pct, "substitutions": substitutions, "portable_backup_needed": portable, "missing_av": missing}


def _session_detail(state: EventFullState, s: Session) -> dict:
    rooms = {r.id: r for r in state.rooms}
    speakers = {sp.id: sp for sp in state.speakers}
    room = rooms.get(s.room_id)
    av = _match_av(s.required_av, room.av_specs if room else [])
    return {
        "id": s.id,
        "title": s.title,
        "track": s.track,
        "status": s.status,
        "start_time": s.start_time,
        "end_time": s.end_time,
        "is_locked_keynote": s.is_locked_keynote,
        "expected_attendance": s.expected_attendance,
        "room": {"id": s.room_id, "name": room.name, "capacity": room.capacity, "status": room.status,
                 "floor_or_zone": room.floor_or_zone} if room else {"id": s.room_id, "name": "UNKNOWN"},
        "seat_margin": room.capacity - s.expected_attendance if room else None,
        "required_av": s.required_av,
        "room_av_gaps": av["missing_av"] + av["portable_backup_needed"],
        "speakers": [
            {"id": sp.id, "name": sp.name, "vip_tier": sp.vip_tier, "status": sp.status,
             "travel_origin": sp.travel_origin, "arrival_buffer_mins": sp.arrival_buffer_mins}
            for sp in (speakers.get(i) for i in s.speaker_ids) if sp
        ],
    }


def _detect_conflicts(state: EventFullState) -> list[dict]:
    """Problems visible in the current schedule right now."""
    rooms = {r.id: r for r in state.rooms}
    speakers = {sp.id: sp for sp in state.speakers}
    conflicts: list[dict] = []
    sessions = state.sessions
    for i, a in enumerate(sessions):
        room = rooms.get(a.room_id)
        if room and room.status != "Operational":
            conflicts.append({"type": "Room Not Operational", "session_ids": [a.id],
                              "detail": f"'{a.title}' ({a.start_time}) is in {room.name}, which is {room.status}."})
        if room and a.expected_attendance > room.capacity:
            conflicts.append({"type": "Over Capacity", "session_ids": [a.id],
                              "detail": f"'{a.title}' expects {a.expected_attendance} but {room.name} seats {room.capacity}."})
        for sp_id in a.speaker_ids:
            sp = speakers.get(sp_id)
            if sp and sp.status == "Delayed":
                conflicts.append({"type": "Speaker Delayed", "session_ids": [a.id],
                                  "detail": f"{sp.name} ({sp.vip_tier}) is Delayed for '{a.title}' at {a.start_time}."})
        if a.status == "Conflict Flagged":
            conflicts.append({"type": "Conflict Flagged", "session_ids": [a.id], "detail": f"'{a.title}' is flagged."})
        for b in sessions[i + 1:]:
            if not _overlaps(a.start_time, a.end_time, b.start_time, b.end_time):
                continue
            if a.room_id == b.room_id:
                conflicts.append({"type": "Room Double-Booked", "session_ids": [a.id, b.id],
                                  "detail": f"'{a.title}' and '{b.title}' overlap in {rooms[a.room_id].name}."})
            for sp_id in set(a.speaker_ids) & set(b.speaker_ids):
                conflicts.append({"type": "Speaker Double-Booked", "session_ids": [a.id, b.id],
                                  "detail": f"{speakers[sp_id].name} is in '{a.title}' and '{b.title}' at the same time."})
    return conflicts


# ---- 1. Schedule & venue inspection --------------------------------------------------------


def get_event_overview_tool(event_id: str) -> dict:
    """Event + venue metadata, headline counts, degraded rooms, travelling speakers, and live conflicts."""
    state = _load(event_id)
    if state is None:
        return {"error": f"Unknown event_id '{event_id}'."}
    return {
        "event": state.event.model_dump(mode="json"),
        "venue": state.venue.model_dump(mode="json"),
        "counts": {
            "rooms": len(state.rooms),
            "sessions": len(state.sessions),
            "speakers": len(state.speakers),
            "catering_slas": len(state.catering_slas),
            "crisis_presets": len(state.crisis_presets),
        },
        "schedule_window": {"first_start": min(s.start_time for s in state.sessions),
                            "last_end": max(s.end_time for s in state.sessions)} if state.sessions else None,
        "degraded_rooms": [{"id": r.id, "name": r.name, "status": r.status} for r in state.rooms if r.status != "Operational"],
        "speakers_not_on_site": [
            {"id": sp.id, "name": sp.name, "vip_tier": sp.vip_tier, "status": sp.status, "travel_origin": sp.travel_origin}
            for sp in state.speakers if sp.status != "Confirmed On-Site"
        ],
        "active_conflicts": _detect_conflicts(state),
    }


def find_affected_sessions_tool(
    event_id: str,
    query_keyword: str = "",
    room_id: str = "",
    speaker_id: str = "",
    time_window_start: str = "",
    time_window_end: str = "",
) -> list[dict]:
    """Sessions matching ALL given filters. Keyword searches title, track, room name, and speaker names/orgs."""
    state = _load(event_id)
    if state is None:
        return [{"error": f"Unknown event_id '{event_id}'."}]
    for t in (time_window_start, time_window_end):
        if t and not _HHMM_RE.match(t):
            return [{"error": f"Time '{t}' must be HH:MM (24-hour)."}]
    rooms = {r.id: r for r in state.rooms}
    speakers = {sp.id: sp for sp in state.speakers}
    kw = query_keyword.strip().lower()
    win_start = time_window_start or "00:00"
    win_end = time_window_end or "23:59"

    results = []
    for s in state.sessions:
        if room_id and s.room_id != room_id:
            continue
        if speaker_id and speaker_id not in s.speaker_ids:
            continue
        if (time_window_start or time_window_end) and not _overlaps(s.start_time, s.end_time, win_start, win_end):
            continue
        if kw:
            haystack = [s.title, s.track, rooms[s.room_id].name if s.room_id in rooms else ""]
            haystack += [f"{speakers[i].name} {speakers[i].title_org}" for i in s.speaker_ids if i in speakers]
            if not any(kw in h.lower() for h in haystack):
                continue
        results.append(_session_detail(state, s))
    return results


# ---- 2. Constraint checker & swap solver ---------------------------------------------------


def _evaluate(
    state: EventFullState,
    session: Session,
    room: Room,
    start: str,
    end: str,
    *,
    attendance: int | None = None,
    ignore_ids: frozenset[str] = frozenset(),
    extra_sessions: Iterable[Session] = (),
    speaker_etas: dict[str, str] | None = None,
) -> dict:
    """Core hard-constraint check for one placement. `extra_sessions` are other sessions at their *new*
    positions (used by two-way swaps); `ignore_ids` are sessions whose old positions no longer count."""
    violations: list[str] = []
    warnings: list[str] = []
    speakers = {sp.id: sp for sp in state.speakers}
    attendance = session.expected_attendance if attendance is None else attendance
    others = [s for s in state.sessions if s.id != session.id and s.id not in ignore_ids] + list(extra_sessions)

    # Locked keynote protection
    moved = (room.id, start, end) != (session.room_id, session.start_time, session.end_time)
    if session.is_locked_keynote and moved:
        violations.append(f"Locked keynote: '{session.title}' is locked to its {session.start_time}-{session.end_time} "
                          "slot and cannot be moved or cancelled.")

    # Capacity
    seat_buffer = room.capacity - attendance
    if seat_buffer < 0:
        violations.append(f"Capacity: {attendance} expected vs {room.capacity} seats in {room.name} "
                          f"(overflow deficit of {-seat_buffer} seats).")
    elif seat_buffer < max(5, round(0.05 * attendance)):
        warnings.append(f"Tight fit: only {seat_buffer} spare seats in {room.name}.")

    # Room status
    if room.status != "Operational":
        violations.append(f"Room status: {room.name} is '{room.status}', not Operational.")

    # AV hardware
    av = _match_av(session.required_av, room.av_specs)
    if av["missing_av"]:
        violations.append(f"AV: {room.name} lacks {', '.join(av['missing_av'])} (not available in the portable kit).")
    if av["portable_backup_needed"]:
        warnings.append(f"AV: bring portable backup for {', '.join(av['portable_backup_needed'])}.")

    # Room double-booking
    occupants = [o for o in others if o.room_id == room.id and _overlaps(start, end, o.start_time, o.end_time)]
    for o in occupants:
        violations.append(f"Room double-booked: '{o.title}' ({o.id}) holds {room.name} {o.start_time}-{o.end_time}.")
        if o.is_locked_keynote:
            violations.append(f"'{o.title}' is a locked keynote and cannot be displaced.")
    swap_partner = next((o.id for o in occupants if not o.is_locked_keynote), None)

    # Speaker overlap & transit buffer
    speaker_conflict_free = True
    for sp_id in session.speaker_ids:
        sp = speakers.get(sp_id)
        if sp is None:
            continue
        for o in others:
            if sp_id in o.speaker_ids and _overlaps(start, end, o.start_time, o.end_time):
                speaker_conflict_free = False
                violations.append(f"Speaker double-booked: {sp.name} is also in '{o.title}' {o.start_time}-{o.end_time}.")
        eta = (speaker_etas or {}).get(sp_id)
        if eta:
            ready = minutes(eta) + sp.arrival_buffer_mins
            if ready > minutes(start):
                speaker_conflict_free = False
                violations.append(f"Transit buffer: {sp.name} arrives {eta} + {sp.arrival_buffer_mins}-min buffer = "
                                  f"stage-ready {_hhmm(ready)}, after the {start} start.")
        elif sp.status == "Delayed":
            speaker_conflict_free = False
            violations.append(f"Transit: {sp.name} is Delayed with no confirmed ETA.")
        elif sp.status == "In Transit":
            warnings.append(f"Transit: {sp.name} is still In Transit ({sp.travel_origin}); confirm arrival "
                            f"{sp.arrival_buffer_mins} mins before {start}.")

    # Venue hours
    if minutes(start) < minutes(VENUE_OPEN) or minutes(end) > minutes(VENUE_CLOSE):
        warnings.append(f"Outside standard venue hours ({VENUE_OPEN}-{VENUE_CLOSE}); confirm staffing.")

    # Catering exposure: does the new slot newly pull attendees into a meal's service window?
    catering_impact = 0.0
    if session.track != CATERING_TRACK:
        for sla in state.catering_slas:
            now_overlaps = _overlaps(start, end, sla.scheduled_time, sla.hard_cutoff_time)
            was_overlapping = _overlaps(session.start_time, session.end_time, sla.scheduled_time, sla.hard_cutoff_time)
            if now_overlaps and not was_overlapping:
                catering_impact += sla.late_penalty_usd
                warnings.append(f"Catering: overlaps {sla.meal_name} service ({sla.scheduled_time}-{sla.hard_cutoff_time}); "
                                f"${sla.late_penalty_usd:,.0f} SLA exposure.")

    time_shift = minutes(start) - minutes(session.start_time)
    if violations:
        score = 0
    else:
        s = 100.0
        utilisation = attendance / room.capacity
        if utilisation < 0.5:
            s -= (0.5 - utilisation) * 40           # oversized room feels empty and wastes a big space
        s -= (100 - av["av_match_pct"]) * 0.3       # relying on portable gear
        s -= min(20, abs(time_shift) / 6)            # every hour moved costs 10 points
        s -= 5 if room.id != session.room_id else 0  # attendees must find a new room
        s -= 3 * len(warnings)
        s -= min(30, catering_impact / 200)
        score = max(1, round(s))

    return {
        "is_valid": not violations,
        "requires_two_way_swap_with_session_id": swap_partner,
        "violations": violations,
        "warnings": warnings,
        "score": score,
        "metrics": {
            "expected_attendance": attendance,
            "room_capacity": room.capacity,
            "seat_buffer": seat_buffer,
            "av_match_pct": av["av_match_pct"],
            "av_substitutions": av["substitutions"],
            "portable_backup_needed": av["portable_backup_needed"],
            "missing_av": av["missing_av"],
            "speaker_conflict_free": speaker_conflict_free,
            "catering_impact_usd": catering_impact,
            "time_shift_mins": time_shift,
        },
    }


def check_hard_constraints_tool(
    event_id: str,
    session_id: str,
    target_room_id: str,
    new_start_time: str,
    new_end_time: str,
    expected_attendance_override: int | None = None,
    speaker_eta_overrides: dict[str, str] | None = None,
    room_status_overrides: dict[str, str] | None = None,
) -> dict:
    """Would moving `session_id` to (target_room_id, new_start_time-new_end_time) break any hard constraint?

    Optional overrides model live changes not yet saved: an RSVP surge (`expected_attendance_override`),
    known speaker arrival times (`speaker_eta_overrides`, speaker_id -> "HH:MM"), or reported room failures
    (`room_status_overrides`, room_id -> "AV Degraded" / "Closed").
    """
    state = _load(event_id)
    if state is None:
        return {"error": f"Unknown event_id '{event_id}'."}
    state = _with_room_status(state, room_status_overrides)
    session = next((s for s in state.sessions if s.id == session_id), None)
    room = next((r for r in state.rooms if r.id == target_room_id), None)
    if session is None:
        return {"error": f"Session '{session_id}' is not part of event '{event_id}'."}
    if room is None:
        return {"error": f"Room '{target_room_id}' is not at this event's venue ({state.venue.name})."}
    for t in (new_start_time, new_end_time):
        if not _HHMM_RE.match(t):
            return {"error": f"Time '{t}' must be HH:MM (24-hour)."}
    if minutes(new_end_time) <= minutes(new_start_time):
        return {"error": f"new_end_time {new_end_time} must be after new_start_time {new_start_time}."}

    result = _evaluate(state, session, room, new_start_time, new_end_time,
                       attendance=expected_attendance_override, speaker_etas=speaker_eta_overrides)
    return {"session_id": session.id, "session_title": session.title, "target_room_id": room.id,
            "target_room_name": room.name, "new_start_time": new_start_time, "new_end_time": new_end_time, **result}


def _explain(title: str, room: Room, start: str, end: str, ev: dict, same_time: bool) -> str:
    m = ev["metrics"]
    av_note = f"AV {m['av_match_pct']}% covered"
    if m["av_substitutions"]:
        av_note += " (" + "; ".join(f"{k} via {v}" for k, v in m["av_substitutions"].items()) + ")"
    if m["portable_backup_needed"]:
        av_note += f", portable kit for {', '.join(m['portable_backup_needed'])}"
    when = "same time slot" if same_time else f"shifted {m['time_shift_mins']:+d} min"
    return (f"'{title}' -> {room.name} {start}-{end} ({when}): {room.capacity} seats for {m['expected_attendance']} "
            f"expected (+{m['seat_buffer']} buffer); {av_note}; "
            f"{'no speaker conflicts' if m['speaker_conflict_free'] else 'speaker conflict'}; "
            f"${m['catering_impact_usd']:,.0f} catering impact.")


def _move(s: Session, room: Room, start: str, end: str, rooms: dict[str, Room]) -> dict:
    return {"session_id": s.id, "title": s.title,
            "from_room": rooms[s.room_id].name, "from_time": f"{s.start_time}-{s.end_time}",
            "to_room_id": room.id, "to_room": room.name, "to_time": f"{start}-{end}"}


def propose_optimal_swaps_tool(
    event_id: str,
    disrupted_session_id: str,
    min_start_time: str = "",
    reason: str = "",
    expected_attendance_override: int | None = None,
    speaker_eta_overrides: dict[str, str] | None = None,
    room_status_overrides: dict[str, str] | None = None,
) -> list[dict]:
    """Search every room x 15-minute start time (Direct Moves) and every non-locked partner session
    (Two-Way Swaps) for the disrupted session. Returns the top 3 feasible options, best first.
    An empty list means no feasible option exists under the hard constraints."""
    state = _load(event_id)
    if state is None:
        return [{"error": f"Unknown event_id '{event_id}'."}]
    state = _with_room_status(state, room_status_overrides)
    a = next((s for s in state.sessions if s.id == disrupted_session_id), None)
    if a is None:
        return [{"error": f"Session '{disrupted_session_id}' is not part of event '{event_id}'."}]
    if min_start_time and not _HHMM_RE.match(min_start_time):
        return [{"error": f"min_start_time '{min_start_time}' must be HH:MM (24-hour)."}]

    rooms = {r.id: r for r in state.rooms}
    duration = minutes(a.end_time) - minutes(a.start_time)
    earliest = minutes(min_start_time) if min_start_time else minutes(a.start_time)
    latest_end = max(minutes(VENUE_CLOSE), max(minutes(s.end_time) for s in state.sessions))
    is_catering = a.track == CATERING_TRACK
    common = {"attendance": expected_attendance_override, "speaker_etas": speaker_eta_overrides}
    options: list[dict] = []

    # Direct moves: any open room, any slot on the grid
    for room in state.rooms:
        if room.is_catering_hub != is_catering:
            continue
        for start_m in range(earliest, latest_end - duration + 1, SLOT_STEP_MINS):
            start, end = _hhmm(start_m), _hhmm(start_m + duration)
            if (room.id, start) == (a.room_id, a.start_time):
                continue  # staying put is not a resolution
            ev = _evaluate(state, a, room, start, end, **common)
            if not ev["is_valid"]:
                continue
            m = ev["metrics"]
            options.append({
                "option_type": "Direct Move",
                "key": f"room:{room.id}",
                "score": ev["score"],
                "moves": [_move(a, room, start, end, rooms)],
                "seat_buffer": m["seat_buffer"],
                "av_match_pct": m["av_match_pct"],
                "speaker_conflict_free": m["speaker_conflict_free"],
                "catering_impact_usd": m["catering_impact_usd"],
                "time_shift_mins": m["time_shift_mins"],
                "warnings": ev["warnings"],
                "explanation": _explain(a.title, room, start, end, ev, start == a.start_time),
            })

    # Two-way swaps: trade slots with a non-locked session; BOTH must pass every hard constraint
    for b in state.sessions:
        if b.id == a.id or b.is_locked_keynote or (b.track == CATERING_TRACK) != is_catering:
            continue
        b_duration = minutes(b.end_time) - minutes(b.start_time)
        a_room, a_start = rooms[b.room_id], b.start_time
        a_end = _hhmm(minutes(a_start) + duration)
        b_room, b_start = rooms[a.room_id], a.start_time
        b_end = _hhmm(minutes(b_start) + b_duration)
        if minutes(a_start) < earliest or minutes(a_end) > latest_end or minutes(b_end) > latest_end:
            continue
        a_moved = a.model_copy(update={"room_id": a_room.id, "start_time": a_start, "end_time": a_end})
        b_moved = b.model_copy(update={"room_id": b_room.id, "start_time": b_start, "end_time": b_end})
        pair = frozenset({a.id, b.id})
        ev_a = _evaluate(state, a, a_room, a_start, a_end, ignore_ids=pair, extra_sessions=[b_moved], **common)
        ev_b = _evaluate(state, b, b_room, b_start, b_end, ignore_ids=pair, extra_sessions=[a_moved],
                         speaker_etas=speaker_eta_overrides)
        if not (ev_a["is_valid"] and ev_b["is_valid"]):
            continue
        ma, mb = ev_a["metrics"], ev_b["metrics"]
        options.append({
            "option_type": "Two-Way Swap",
            "key": f"swap:{b.id}",
            "score": max(1, round((ev_a["score"] + ev_b["score"]) / 2 - 10)),  # -10: disrupts a second session
            "moves": [_move(a, a_room, a_start, a_end, rooms), _move(b, b_room, b_start, b_end, rooms)],
            "swap_partner_session_id": b.id,
            "seat_buffer": ma["seat_buffer"],
            "partner_seat_buffer": mb["seat_buffer"],
            "av_match_pct": ma["av_match_pct"],
            "partner_av_match_pct": mb["av_match_pct"],
            "speaker_conflict_free": ma["speaker_conflict_free"] and mb["speaker_conflict_free"],
            "catering_impact_usd": ma["catering_impact_usd"] + mb["catering_impact_usd"],
            "time_shift_mins": ma["time_shift_mins"],
            "warnings": ev_a["warnings"] + ev_b["warnings"],
            "explanation": _explain(a.title, a_room, a_start, a_end, ev_a, a_start == a.start_time)
            + " Swap: " + _explain(b.title, b_room, b_start, b_end, ev_b, b_start == b.start_time),
        })

    # Rank: best score, then smallest time shift. Prefer variety (one per room/partner) before repeats.
    options.sort(key=lambda o: (-o["score"], abs(o["time_shift_mins"]), o["moves"][0]["to_room"]))
    seen, diverse, repeats = set(), [], []
    for o in options:
        (repeats if o["key"] in seen else diverse).append(o)
        seen.add(o["key"])
    top = (diverse + repeats)[:3]
    for rank, o in enumerate(top, start=1):
        o.pop("key")
        o["rank"] = rank
        o["reason"] = reason
    return top


# ---- 3. Catering SLA & penalty calculator --------------------------------------------------


def _catering_levers(state: EventFullState, meal_time: str) -> list[dict]:
    """Ways to claw back minutes before a meal: transitions, then keynote Q&A, then the last breakout."""
    before = sorted((s for s in state.sessions if s.track != CATERING_TRACK and minutes(s.end_time) <= minutes(meal_time)),
                    key=lambda s: s.start_time)
    levers: list[dict] = []
    # 1) Compress transition breaks between blocks (never below the room-turnover minimum)
    blocks = sorted({(s.start_time, s.end_time) for s in before})
    latest_end = None
    for start, end in blocks:
        if latest_end and minutes(start) > minutes(latest_end):
            gap = minutes(start) - minutes(latest_end)
            if gap - MIN_TRANSITION_MINS > 0:
                levers.append({"action": f"Compress the {latest_end}-{start} transition break from {gap} to "
                               f"{MIN_TRANSITION_MINS} mins", "minutes_saved": gap - MIN_TRANSITION_MINS})
        latest_end = end if latest_end is None or minutes(end) > minutes(latest_end) else latest_end
    # 2) Trim keynote Q&A
    for s in before:
        if s.track == "Keynote" and minutes(s.end_time) - minutes(s.start_time) >= 45:
            levers.append({"action": f"Trim audience Q&A in '{s.title}' by 10 mins", "minutes_saved": 10})
    # 3) Shorten the last pre-meal breakout block
    if before:
        last_end = max(s.end_time for s in before)
        last_block = [s for s in before if s.end_time == last_end and s.track != "Keynote"]
        if last_block:
            names = " + ".join(f"'{s.title}'" for s in last_block)
            levers.append({"action": f"End {names} 10 mins early (wrap at {_hhmm(minutes(last_end) - 10)})",
                           "minutes_saved": 10})
    levers.sort(key=lambda l: -l["minutes_saved"])  # biggest wins first; stable, so ties keep the order above
    return levers


def calculate_catering_sla_impact_tool(
    event_id: str,
    proposed_schedule_shift_mins: int,
    affected_time_window: str = "12:00-13:30",
) -> dict:
    """If the programme slides by N minutes, which meals inside `affected_time_window` miss their hard cutoff,
    what does it cost, and what is the cheapest way to win the minutes back?"""
    state = _load(event_id)
    if state is None:
        return {"error": f"Unknown event_id '{event_id}'."}
    try:
        win_start, win_end = (t.strip() for t in affected_time_window.split("-"))
        assert _HHMM_RE.match(win_start) and _HHMM_RE.match(win_end)
    except (ValueError, AssertionError):
        return {"error": "affected_time_window must look like 'HH:MM-HH:MM'."}

    shift = proposed_schedule_shift_mins
    rooms = {r.id: r.name for r in state.rooms}
    slas, total_penalty, worst_over, worst_short = [], 0.0, 0, 0
    for sla in state.catering_slas:
        if not minutes(win_start) <= minutes(sla.scheduled_time) <= minutes(win_end):
            continue
        projected = minutes(sla.scheduled_time) + shift
        slack = minutes(sla.hard_cutoff_time) - projected
        breached = slack < 0
        risk = "BREACH" if breached else "AT RISK" if slack < CATERING_SAFETY_BUFFER_MINS else "SAFE"
        penalty = sla.late_penalty_usd if breached else 0.0
        total_penalty += penalty
        worst_over = max(worst_over, -slack)
        worst_short = max(worst_short, CATERING_SAFETY_BUFFER_MINS - slack)
        slas.append({
            "sla_id": sla.id, "meal_name": sla.meal_name, "room": rooms.get(sla.room_id, sla.room_id),
            "vendor_name": sla.vendor_name, "headcount": sla.headcount,
            "scheduled_time": sla.scheduled_time, "hard_cutoff_time": sla.hard_cutoff_time,
            "projected_service_time": _hhmm(projected), "slack_mins": slack,
            "minutes_over_cutoff": max(0, -slack), "risk": risk,
            "late_penalty_usd": sla.late_penalty_usd, "penalty_incurred_usd": penalty,
        })

    # Mitigation: recover enough to clear the cutoff AND restore a safety buffer.
    plan = None
    if worst_short > 0 and slas:
        target = worst_short
        chosen, saved = [], 0
        for lever in _catering_levers(state, min(s["scheduled_time"] for s in slas)):
            if saved >= target:
                break
            chosen.append(lever)
            saved += lever["minutes_saved"]
        at_stake = sum(s["late_penalty_usd"] for s in slas if s["risk"] != "SAFE")
        plan = {
            "minutes_to_clear_cutoff": worst_over,
            "target_recovery_mins": target,
            "levers": chosen,
            "total_minutes_saved": saved,
            "penalty_avoided_usd": at_stake if saved >= worst_over else 0.0,
            "residual_risk": "None - cutoff cleared with safety buffer" if saved >= target
            else "Partial - escalate to vendor for a late-service extension",
        }

    breached = [s for s in slas if s["risk"] == "BREACH"]
    if breached:
        summary = (f"A {shift}-min slide breaches {', '.join(s['meal_name'] for s in breached)}: "
                   f"${total_penalty:,.0f} penalty unless {worst_over}+ mins are recovered.")
    elif any(s["risk"] == "AT RISK" for s in slas):
        summary = f"A {shift}-min slide stays inside the cutoff but leaves under {CATERING_SAFETY_BUFFER_MINS} mins of slack."
    else:
        summary = f"A {shift}-min slide is absorbed by the catering slack; no penalty."
    if plan and plan["levers"]:
        summary += f" Plan: {' + '.join(l['action'] for l in plan['levers'])} (saves {plan['total_minutes_saved']} mins)."

    return {
        "event_id": event_id,
        "proposed_schedule_shift_mins": shift,
        "affected_time_window": f"{win_start}-{win_end}",
        "slas_checked": slas,
        "total_penalty_usd": total_penalty,
        "mitigation_plan": plan,
        "summary": summary,
    }


# ---- 4. Live weather & transit corridor monitor --------------------------------------------

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT_SECS = 4.0
WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Heavy freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers", 81: "Heavy rain showers",
    82: "Violent rain showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Severe thunderstorm with hail",
}
# Used only when the network is unavailable: a typical late-autumn Northeast day, clearly labelled.
FALLBACK_TELEMETRY = {
    "observed_at": "cached", "temperature_c": 9.0, "precipitation_mm": 0.3, "wind_speed_kmh": 22.0,
    "weather_code": 61, "max_precip_probability_pct": 45, "min_visibility_m": 9000.0,
}
EXTRA_BUFFER_BY_RISK = {"LOW": 0, "MODERATE": 30, "HIGH": 90}


def _fetch_open_meteo(lat: float, lon: float) -> dict:
    if os.environ.get("STAGECRAFT_OFFLINE") == "1":
        raise httpx.ConnectError("STAGECRAFT_OFFLINE=1 (offline mode forced)")
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
        "hourly": "precipitation_probability,visibility",
        "forecast_days": 1, "timezone": "auto",
    }
    resp = httpx.get(OPEN_METEO_URL, params=params, timeout=HTTP_TIMEOUT_SECS)
    resp.raise_for_status()
    data = resp.json()
    cur, hourly = data["current"], data["hourly"]
    # Next 6 hours starting from the current local hour
    times = hourly["time"]
    idx = next((i for i, t in enumerate(times) if t[:13] == cur["time"][:13]), 0)
    probs = [p for p in hourly["precipitation_probability"][idx:idx + 6] if p is not None]
    vis = [v for v in hourly["visibility"][idx:idx + 6] if v is not None]
    return {
        "observed_at": f"{cur['time']} ({data.get('timezone', 'local')})",
        "temperature_c": cur["temperature_2m"],
        "precipitation_mm": cur["precipitation"],
        "wind_speed_kmh": cur["wind_speed_10m"],
        "weather_code": cur["weather_code"],
        "max_precip_probability_pct": max(probs) if probs else 0,
        "min_visibility_m": min(vis) if vis else None,
    }


def _transit_risk(t: dict) -> tuple[str, int, list[str]]:
    """Score corridor delay risk from wind, precipitation, visibility, and weather type."""
    points, factors = 0, []
    wind, precip = t["wind_speed_kmh"], t["precipitation_mm"]
    prob, vis, code = t["max_precip_probability_pct"], t["min_visibility_m"], t["weather_code"]
    if wind >= 50:
        points += 3; factors.append(f"Severe wind {wind} km/h: ground stops / speed restrictions likely")
    elif wind >= 30:
        points += 2; factors.append(f"Strong wind {wind} km/h: crosswind delays at airports")
    elif wind >= 20:
        points += 1; factors.append(f"Breezy {wind} km/h")
    if precip >= 5:
        points += 3; factors.append(f"Heavy precipitation {precip} mm now")
    elif precip >= 1:
        points += 2; factors.append(f"Steady precipitation {precip} mm now")
    elif precip > 0:
        points += 1; factors.append(f"Light precipitation {precip} mm now")
    if prob >= 70:
        points += 2; factors.append(f"{prob}% precipitation chance in next 6h")
    elif prob >= 40:
        points += 1; factors.append(f"{prob}% precipitation chance in next 6h")
    if vis is not None and vis < 1000:
        points += 3; factors.append(f"Low visibility {vis:.0f} m: reduced arrival rates")
    elif vis is not None and vis < 5000:
        points += 1; factors.append(f"Reduced visibility {vis:.0f} m")
    if code in (56, 57, 66, 67, 95, 96, 99):
        points += 3; factors.append(f"Hazard: {WMO_CODES.get(code, code)}")
    elif code in (45, 48, 71, 73, 75, 77, 85, 86):
        points += 2; factors.append(f"Hazard: {WMO_CODES.get(code, code)}")
    level = "HIGH" if points >= 5 else "MODERATE" if points >= 2 else "LOW"
    return level, points, factors or ["No adverse weather signals"]


def check_live_venue_weather_and_transit_tool(event_id: str) -> dict:
    """Live Open-Meteo conditions at the venue -> transit corridor delay risk. Never raises on network failure:
    falls back to clearly-labelled cached telemetry (`source: fallback_cache`)."""
    state = _load(event_id)
    if state is None:
        return {"error": f"Unknown event_id '{event_id}'."}
    venue = state.venue
    try:
        telemetry, source, error = _fetch_open_meteo(venue.latitude, venue.longitude), "open-meteo-live", None
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        telemetry, source, error = dict(FALLBACK_TELEMETRY), "fallback_cache", f"{type(exc).__name__}: {exc}"

    level, points, factors = _transit_risk(telemetry)
    travelling = [
        {"id": sp.id, "name": sp.name, "vip_tier": sp.vip_tier, "travel_origin": sp.travel_origin, "status": sp.status}
        for sp in state.speakers if sp.status != "Confirmed On-Site"
    ]
    extra = EXTRA_BUFFER_BY_RISK[level]
    result = {
        "event_id": event_id,
        "venue": {"name": venue.name, "city": venue.city, "latitude": venue.latitude, "longitude": venue.longitude},
        "airport_corridor": venue.airport_corridor,
        "corridor_nodes": [n.strip() for n in venue.airport_corridor.split("/") if n.strip()],
        "source": source,
        "conditions": {**telemetry, "description": WMO_CODES.get(telemetry["weather_code"], "Unknown")},
        "transit_delay_risk": level,
        "risk_points": points,
        "risk_factors": factors,
        "recommended_extra_buffer_mins": extra,
        "speakers_in_transit": travelling,
        "advisory": (f"{level} corridor delay risk on {venue.airport_corridor}. "
                     + (f"Add {extra} mins to arrival buffers for the {len(travelling)} speaker(s) still travelling."
                        if extra else "Standard arrival buffers are sufficient.")),
    }
    if error:
        result["fallback_reason"] = error
    return result
