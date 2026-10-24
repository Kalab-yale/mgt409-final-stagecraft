"""Onboarding Architect: turn a plain-English brief (or a CSV) into a complete, conflict-free event in SQLite.

Two ways in:
- `generate_event_from_prompt()`  - LLM agent drafts a `GeneratedEventBlueprint`; deterministic checks
                                     force it to self-correct until the schedule is conflict-free; then save.
- `import_event_from_csv_or_template()` - no LLM: reuse an existing venue and load sessions from CSV text
                                     or clone another event's programme.

Both paths screen untrusted text with the Layer 1 firewall and save everything in one transaction.
"""

from __future__ import annotations

import csv
import io
import math
import re
import time
import uuid
from datetime import date
from functools import lru_cache
from typing import Optional

import httpx
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import Agent, ModelRetry, UsageLimits
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

from backend import database as db
from backend import planning, security, tokenomics, tools
from backend.config import get_agent_settings, get_pydantic_ai_model, get_thread_model
from backend.models import (
    CateringSLA,
    CrisisScenarioPreset,
    EventFullState,
    EventSummary,
    Room,
    Session,
    Speaker,
    Venue,
    minutes,
)

AGENT_NAME = "OnboardingAgent"
PLACEHOLDER_EVENT_ID = "NEW"
DAY_START, DAY_END = "07:00", "21:00"
# Building a whole event is a bigger job than one crisis run, so onboarding gets this multiple of the
# per-run token / iteration limits from Settings (Layer 3 still enforces it, mid-run).
ONBOARDING_BUDGET_MULTIPLIER = 3
MAX_OUTPUT_TOKENS = 12_000


# ---- Structured output -------------------------------------------------------------------------


class GeneratedEventBlueprint(BaseModel):
    """A complete, ready-to-save conference. IDs are short local placeholders; they are renamed on save."""

    event_name: str
    event_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", description="lowercase-hyphenated, e.g. 'climate-forum-2026'")
    event_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")
    attendee_count: int = Field(gt=0)
    description: str = Field(description="1-2 sentences")
    use_existing_venue_id: Optional[str] = Field(default=None, description="id from list_existing_venue_templates, or null")
    custom_venue: Optional[Venue] = Field(default=None, description="new venue with accurate latitude/longitude, or null")
    rooms: list[Room] = Field(default_factory=list, description="4-6 rooms for a custom venue; empty when reusing a venue")
    speakers: list[Speaker] = Field(description="6-10 fictional speakers")
    sessions: list[Session] = Field(description="6-10 conflict-free sessions")
    catering_slas: list[CateringSLA] = Field(description="1-2 catering windows")
    crisis_presets: list[CrisisScenarioPreset] = Field(description="exactly 3 tailored crisis scenarios")


# ---- Deterministic checks (shared by the LLM path and the CSV path) --------------------------------


def _schedule_issues(state: EventFullState) -> list[str]:
    """Reference, capacity, AV, hours, and double-booking problems in a (not yet saved) event."""
    issues = []
    rooms = {r.id: r for r in state.rooms}
    speakers = {sp.id for sp in state.speakers}
    for label, items in (("room", state.rooms), ("speaker", state.speakers), ("session", state.sessions)):
        ids = [i.id for i in items]
        if len(ids) != len(set(ids)):
            issues.append(f"Duplicate {label} ids: {sorted({i for i in ids if ids.count(i) > 1})}.")
    for s in state.sessions:
        if s.room_id not in rooms:
            issues.append(f"Session {s.id} uses unknown room_id '{s.room_id}'.")
            continue
        unknown = [i for i in s.speaker_ids if i not in speakers]
        if unknown:
            issues.append(f"Session {s.id} uses unknown speaker_ids {unknown}.")
        missing = tools._match_av(s.required_av, rooms[s.room_id].av_specs)["missing_av"]
        if missing:
            issues.append(f"Session {s.id} requires {missing} but room {s.room_id} only has {rooms[s.room_id].av_specs}.")
        if minutes(s.start_time) < minutes(DAY_START) or minutes(s.end_time) > minutes(DAY_END):
            issues.append(f"Session {s.id} ({s.start_time}-{s.end_time}) is outside {DAY_START}-{DAY_END}.")
    for c in state.catering_slas:
        if c.room_id not in rooms:
            issues.append(f"Catering {c.id} uses unknown room_id '{c.room_id}'.")
    if issues:
        return issues  # conflict detection needs valid references
    for conflict in tools._detect_conflicts(state):
        if conflict["type"] != "Conflict Flagged":
            issues.append(f"{conflict['type']}: {conflict['detail']}")
    return issues


def blueprint_issues(bp: GeneratedEventBlueprint) -> list[str]:
    """Everything wrong with a blueprint, phrased so the agent can fix it."""
    issues = []
    try:
        date.fromisoformat(bp.event_date)
    except ValueError:
        issues.append(f"event_date '{bp.event_date}' is not a real date.")
    if bool(bp.use_existing_venue_id) == bool(bp.custom_venue):
        return issues + ["Set exactly one of use_existing_venue_id or custom_venue."]
    if bp.use_existing_venue_id:
        venue = db.get_venue(bp.use_existing_venue_id)
        if venue is None:
            return issues + [f"use_existing_venue_id '{bp.use_existing_venue_id}' does not exist; call list_existing_venue_templates."]
        rooms = db.get_rooms(venue.id)
    else:
        venue, rooms = bp.custom_venue, bp.rooms
        if not 4 <= len(rooms) <= 6:
            issues.append(f"Custom venue needs 4-6 rooms (got {len(rooms)}).")
    for label, items, lo, hi in (("speakers", bp.speakers, 6, 10), ("sessions", bp.sessions, 6, 10),
                                 ("catering_slas", bp.catering_slas, 1, 2), ("crisis_presets", bp.crisis_presets, 3, 3)):
        if not lo <= len(items) <= hi:
            issues.append(f"Need {lo}-{hi} {label} (got {len(items)})." if lo != hi else f"Need exactly {lo} {label} (got {len(items)}).")
    if issues:
        return issues
    state = EventFullState(
        event=EventSummary(id=PLACEHOLDER_EVENT_ID, slug=bp.event_slug, name=bp.event_name, date=bp.event_date,
                           venue_id=venue.id, attendee_count=bp.attendee_count),
        venue=venue, rooms=rooms, sessions=bp.sessions, speakers=bp.speakers,
        catering_slas=bp.catering_slas, crisis_presets=bp.crisis_presets,
    )
    return _schedule_issues(state)


# ---- Agent ---------------------------------------------------------------------------------------

INSTRUCTIONS = """You are the Onboarding Architect for Stagecraft AI, a conference logistics war-room.
Turn the organizer's brief into one complete GeneratedEventBlueprint.

1. Call list_existing_venue_templates first. If the brief names one of those venues, set use_existing_venue_id
   to its id, leave custom_venue null and rooms empty, and use that venue's room ids in sessions.
   Otherwise fill custom_venue with the real venue name, city, state_or_country, accurate latitude/longitude
   (4 decimals) and airport_corridor listing the main airports and rail, e.g. "SFO / OAK / SJC / BART & Caltrain",
   plus 4-6 rooms sized for the attendee count: one room that seats everyone for the keynote, breakout rooms, and
   one is_catering_hub room for meals. Room status "Operational".
2. IDs are short placeholders: custom venue "v1", rooms "r1".., speakers "s1".., sessions "x1".., catering "c1"..,
   crises "k1".. Use event_id "NEW" everywhere and venue_id "v1" (or the existing venue id) on rooms.
3. 6-10 FICTIONAL speakers (invented names, never real people), email like "first.last@<event_slug>.example",
   one-sentence bio, a realistic mix of vip_tier and travel_origin (e.g. "SFO - Flight", "Local", "LAX - Flight").
   status is "Confirmed On-Site" or "In Transit" (never "Delayed").
4. 6-10 sessions between 08:00 and 18:00 (HH:MM 24h). Hard rules: a room never hosts two overlapping sessions;
   a speaker is never in two overlapping sessions; expected_attendance <= room capacity; required_av lists only
   items in that room's av_specs. The opening keynote has is_locked_keynote true. Meals use track
   "Catering & Networking" in the catering hub. Every non-meal session has at least one speaker.
5. 1-2 catering_slas: hard_cutoff_time 20-45 min after scheduled_time, late_penalty_usd realistic for the headcount,
   a fictional vendor_name.
6. Exactly 3 crisis_presets about THIS event's sessions, rooms, and speakers (refer to them by name, not id):
   one "Flight/Transit Delay", one "AV/Room Failure", one "Catering/Overrun". prompt_text is a 2-3 sentence ops alert.
"""


def list_existing_venue_templates() -> list[dict]:
    """Venues already in Stagecraft that an event can reuse, with their rooms (id, name, capacity, AV)."""
    return [
        {"venue_id": v.id, "name": v.name, "city": v.city, "state_or_country": v.state_or_country,
         "rooms": [{"room_id": r.id, "name": r.name, "capacity": r.capacity, "av_specs": r.av_specs,
                    "is_catering_hub": r.is_catering_hub} for r in db.get_rooms(v.id)]}
        for v in db.get_venues()
    ]


@lru_cache(maxsize=1)
def build_onboarding_agent() -> Agent[None, GeneratedEventBlueprint]:
    agent = Agent(
        get_pydantic_ai_model(),
        name=AGENT_NAME,
        instructions=INSTRUCTIONS,
        output_type=GeneratedEventBlueprint,
        model_settings=get_agent_settings(max_tokens=MAX_OUTPUT_TOKENS),
        retries=3,
    )
    agent.tool_plain(list_existing_venue_templates)

    @agent.output_validator
    def _must_be_conflict_free(bp: GeneratedEventBlueprint) -> GeneratedEventBlueprint:
        issues = blueprint_issues(bp)
        if issues:
            raise ModelRetry("The blueprint failed validation. Fix every problem and return the full blueprint again:\n- "
                             + "\n- ".join(issues))
        return bp

    return agent


# ---- Saving ----------------------------------------------------------------------------------------


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "event"


def _unique_slug(slug: str) -> str:
    base, n, candidate = _slugify(slug), 2, _slugify(slug)
    while db.slug_exists(candidate):
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _safe_email(email: str, name: str, slug: str) -> str:
    """Generated contacts must use the reserved .example domain so a demo can never email a real person."""
    if email.endswith(".example"):
        return email
    local = email.split("@")[0] if "@" in email else ".".join(_slugify(name).split("-"))
    return f"{local or 'speaker'}@{slug}.example"


def _summary(state: EventFullState) -> dict:
    rooms = {r.id: r.name for r in state.rooms}
    return {
        "event": state.event.model_dump(mode="json"),
        "venue": state.venue.model_dump(mode="json"),
        "counts": {"rooms": len(state.rooms), "sessions": len(state.sessions), "speakers": len(state.speakers),
                   "catering_slas": len(state.catering_slas), "crisis_presets": len(state.crisis_presets)},
        "schedule": [f"{s.start_time}-{s.end_time} | {s.title} | {rooms.get(s.room_id, s.room_id)} | {s.expected_attendance}"
                     + (" | LOCKED" if s.is_locked_keynote else "") for s in state.sessions],
    }


def save_blueprint(bp: GeneratedEventBlueprint) -> EventFullState:
    """Rename placeholder ids to unique ones, attach everything to the new event, and save in one transaction."""
    tag = uuid.uuid4().hex[:6]
    event_id, slug = f"evt_{tag}", _unique_slug(bp.event_slug)

    new_venue, new_rooms = None, []
    if bp.use_existing_venue_id:
        venue_id = bp.use_existing_venue_id
        room_map = {r.id: r.id for r in db.get_rooms(venue_id)}
    else:
        venue_id = f"ven_{tag}"
        new_venue = bp.custom_venue.model_copy(update={"id": venue_id})
        room_map = {r.id: f"rm_{tag}_{i}" for i, r in enumerate(bp.rooms, 1)}
        new_rooms = [r.model_copy(update={"id": room_map[r.id], "venue_id": venue_id, "status": "Operational"}) for r in bp.rooms]

    speaker_map = {sp.id: f"spk_{tag}_{i:02d}" for i, sp in enumerate(bp.speakers, 1)}
    speakers = [sp.model_copy(update={"id": speaker_map[sp.id], "event_id": event_id,
                                      "email": _safe_email(sp.email, sp.name, slug)}) for sp in bp.speakers]
    ordered = sorted(bp.sessions, key=lambda s: (s.start_time, s.title))
    sessions = [s.model_copy(update={"id": f"ses_{tag}_{i:02d}", "event_id": event_id, "room_id": room_map[s.room_id],
                                     "speaker_ids": [speaker_map[x] for x in s.speaker_ids], "status": "Scheduled"})
                for i, s in enumerate(ordered, 1)]
    catering = [c.model_copy(update={"id": f"cat_{tag}_{i}", "event_id": event_id, "room_id": room_map[c.room_id]})
                for i, c in enumerate(bp.catering_slas, 1)]
    crises = [k.model_copy(update={"id": f"crisis_{tag}_{i}", "event_id": event_id}) for i, k in enumerate(bp.crisis_presets, 1)]
    event = EventSummary(id=event_id, slug=slug, name=bp.event_name, date=bp.event_date, venue_id=venue_id,
                         attendee_count=bp.attendee_count, status="Planning Mode", description=bp.description)
    return db.save_event_bundle(event, speakers, sessions, catering, crises, venue=new_venue, rooms=new_rooms)


# ---- Entry point: natural-language onboarding --------------------------------------------------------


def generate_event_from_prompt(user_prompt: str, run_id: Optional[str] = None) -> dict:
    """Screen the brief (Layer 1) -> run the Onboarding Architect under a token/iteration budget (Layer 3)
    -> meter usage -> save the conflict-free event to SQLite. Returns the saved event summary."""
    screen = security.inspect_inbound_input("onboarding", user_prompt, source="onboarding_prompt")
    if not screen["allowed"]:
        return {"ok": False, "blocked": True, "event_id": None, "security": screen,
                "error": "Onboarding prompt blocked by the Layer 1 firewall; no model call was made."}

    cfg = security.get_guardrail_config()
    limits = UsageLimits(request_limit=cfg.max_agent_loop_iterations * ONBOARDING_BUDGET_MULTIPLIER // 2,
                         total_tokens_limit=cfg.max_tokens_per_run * ONBOARDING_BUDGET_MULTIPLIER)
    run_id = run_id or tokenomics.start_workflow_run("onboarding", "Onboarding: new event from prompt")
    started = time.perf_counter()
    try:
        result = build_onboarding_agent().run_sync(user_prompt, usage_limits=limits, model=get_thread_model())
    except UsageLimitExceeded as exc:
        incident = security.log_incident("onboarding", security.L3, "HIGH", user_prompt, f"Onboarding run exceeded its budget: {exc}",
                                         "Run halted; nothing saved; organizer notified")
        return {"ok": False, "blocked": True, "event_id": None, "run_id": run_id, "incident_id": incident.incident_id,
                "error": f"Budget circuit breaker tripped: {exc}"}
    except UnexpectedModelBehavior as exc:
        incident = security.log_incident("onboarding", security.L2, "HIGH", user_prompt,
                                         f"Agent could not produce a valid, conflict-free blueprint: {exc}",
                                         "Blueprint discarded; nothing saved")
        return {"ok": False, "blocked": False, "event_id": None, "run_id": run_id, "incident_id": incident.incident_id,
                "error": f"Onboarding agent failed validation: {exc}"}
    latency_ms = (time.perf_counter() - started) * 1000

    metric = tokenomics.record_agent_usage(run_id, AGENT_NAME, result, latency_ms,
                                           cost_saver_enabled=tokenomics.get_cost_saver_mode())
    state = save_blueprint(result.output)
    planning.apply_starter_plan(state.event.id)  # standard deadlines + workstreams for the dashboard
    tokenomics.set_run_event_id(run_id, state.event.id)
    usage = result.usage() if callable(result.usage) else result.usage
    return {
        "ok": True, "event_id": state.event.id, "run_id": run_id, **_summary(state),
        "usage": {"requests": usage.requests, "input_tokens": metric.input_tokens, "output_tokens": metric.output_tokens,
                  "total_tokens": metric.total_tokens, "cost_usd": metric.cost_usd, "latency_ms": metric.latency_ms},
        "security": {"allowed": True, "alert": screen["alert"]},
    }


# ---- Entry point: CSV / venue-template import (no LLM) -------------------------------------------------

CSV_COLUMNS = ["title", "track", "room_name", "start_time", "end_time", "expected_attendance", "speaker_name"]


def _match_room(name: str, rooms: list[Room]) -> Room | None:
    """Exact name match first, then a unique partial match ('2400' -> 'Room 2400 - Class of 1980')."""
    key = name.strip().lower()
    exact = [r for r in rooms if r.name.lower() == key]
    partial = [r for r in rooms if key and key in r.name.lower()]
    return exact[0] if exact else partial[0] if len(partial) == 1 else None


def import_event_from_csv_or_template(
    event_name: str,
    event_date: str,
    attendee_count: int,
    venue_id: str,
    csv_text: Optional[str] = None,
    template_event_id: Optional[str] = None,
    event_slug: Optional[str] = None,
    description: str = "",
) -> dict:
    """Create an event at an existing venue (the template) without the LLM.

    Sessions come from `csv_text` (columns: title,track,room_name,start_time,end_time,expected_attendance,
    speaker_name - several speakers separated by ';') or are cloned from `template_event_id`. Everything is
    validated (capacity, double-booking, speaker overlaps) before anything is saved."""
    venue = db.get_venue(venue_id)
    if venue is None:
        return {"ok": False, "errors": [f"Unknown venue template '{venue_id}'."]}
    rooms = db.get_rooms(venue_id)
    tag = uuid.uuid4().hex[:6]
    event_id = f"evt_{tag}"
    slug = _unique_slug(event_slug or event_name)
    errors: list[str] = []
    speakers: list[Speaker] = []
    sessions: list[Session] = []
    catering: list[CateringSLA] = []
    crises: list[CrisisScenarioPreset] = []

    if csv_text:
        screen = security.inspect_inbound_input(venue_id, csv_text, source="csv_import")
        if not screen["allowed"]:
            return {"ok": False, "blocked": True, "security": screen, "errors": [screen["alert"]]}
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
        missing_cols = [c for c in CSV_COLUMNS if c not in (reader.fieldnames or [])]
        if missing_cols:
            return {"ok": False, "errors": [f"CSV is missing columns: {missing_cols}. Expected: {','.join(CSV_COLUMNS)}"]}
        speaker_ids: dict[str, str] = {}
        keynote_locked = False
        for line, row in enumerate(reader, start=2):
            room = _match_room(row["room_name"] or "", rooms)
            if room is None:
                errors.append(f"Line {line}: room '{row['room_name']}' is not at {venue.name} "
                              f"(rooms: {', '.join(r.name for r in rooms)}).")
                continue
            names = [n.strip() for n in (row["speaker_name"] or "").split(";") if n.strip()]
            for n in names:
                if n not in speaker_ids:
                    speaker_ids[n] = f"spk_{tag}_{len(speaker_ids) + 1:02d}"
            is_keynote = (row["track"] or "").strip() == "Keynote"
            try:
                sessions.append(Session(
                    id=f"ses_{tag}_{len(sessions) + 1:02d}", event_id=event_id, title=row["title"].strip(),
                    track=(row["track"] or "").strip(), room_id=room.id, start_time=(row["start_time"] or "").strip(),
                    end_time=(row["end_time"] or "").strip(), expected_attendance=int(row["expected_attendance"]),
                    speaker_ids=[speaker_ids[n] for n in names], is_locked_keynote=is_keynote and not keynote_locked,
                ))
                keynote_locked = keynote_locked or is_keynote
            except (ValidationError, ValueError, TypeError) as exc:
                msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
                errors.append(f"Line {line}: {msg}")
        keynote_speakers = {sid for s in sessions if s.track == "Keynote" for sid in s.speaker_ids}
        speakers = [Speaker(id=sid, event_id=event_id, name=name, title_org="Speaker",
                            vip_tier="Tier 1 - Keynote" if sid in keynote_speakers else "Tier 3 - Panelist",
                            travel_origin="Local", arrival_buffer_mins=15, status="Confirmed On-Site",
                            email=_safe_email("", name, slug)) for name, sid in speaker_ids.items()]
    elif template_event_id:
        template = db.get_event(template_event_id)
        if template is None or template.venue_id != venue_id:
            return {"ok": False, "errors": [f"Template event '{template_event_id}' not found at venue {venue_id}."]}
        src = db.get_event_full_state(template_event_id)
        spk_map = {sp.id: f"spk_{tag}_{i:02d}" for i, sp in enumerate(src.speakers, 1)}
        speakers = [sp.model_copy(update={"id": spk_map[sp.id], "event_id": event_id, "status": "Confirmed On-Site"}) for sp in src.speakers]
        sessions = [s.model_copy(update={"id": f"ses_{tag}_{i:02d}", "event_id": event_id, "status": "Scheduled",
                                         "speaker_ids": [spk_map[x] for x in s.speaker_ids]}) for i, s in enumerate(src.sessions, 1)]
        catering = [c.model_copy(update={"id": f"cat_{tag}_{i}", "event_id": event_id}) for i, c in enumerate(src.catering_slas, 1)]
        crises = [k.model_copy(update={"id": f"crisis_{tag}_{i}", "event_id": event_id}) for i, k in enumerate(src.crisis_presets, 1)]
    else:
        return {"ok": False, "errors": ["Provide csv_text or template_event_id."]}

    try:
        event = EventSummary(id=event_id, slug=slug, name=event_name, date=event_date, venue_id=venue_id,
                             attendee_count=attendee_count, status="Planning Mode", description=description)
    except ValidationError as exc:
        errors.append(f"Event: {exc.errors()[0]['loc'][0]} {exc.errors()[0]['msg']}")
    if not errors:
        errors = _schedule_issues(EventFullState(event=event, venue=venue, rooms=rooms, sessions=sessions,
                                                 speakers=speakers, catering_slas=catering, crisis_presets=crises))
    if errors:
        return {"ok": False, "event_id": None, "errors": errors}

    state = db.save_event_bundle(event, speakers, sessions, catering, crises)
    planning.apply_starter_plan(state.event.id)  # standard deadlines + workstreams for the dashboard
    return {"ok": True, "event_id": state.event.id, **_summary(state), "errors": []}


# ---- CSV import at a brand-new city (geocoded venue, rooms inferred from the CSV) ------------------------

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


def geocode_city(city: str) -> dict | None:
    """Free, no-key Open-Meteo geocoder: 'Boston, MA' -> name, region, country, latitude, longitude."""
    name, _, qualifier = (part.strip() for part in city.partition(","))
    try:
        resp = httpx.get(GEOCODE_URL, params={"name": name, "count": 10}, timeout=4.0)
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except (httpx.HTTPError, ValueError):
        return None
    if not results:
        return None
    q = qualifier.lower()
    match = next((r for r in results if q and q in f"{r.get('admin1', '')} {r.get('country', '')} {r.get('country_code', '')}".lower()), results[0])
    return {"name": match["name"], "region": match.get("admin1", ""), "country": match.get("country", ""),
            "latitude": round(match["latitude"], 4), "longitude": round(match["longitude"], 4)}


def import_event_from_csv_custom_city(
    event_name: str,
    event_date: str,
    attendee_count: int,
    city: str,
    venue_name: str,
    csv_text: str,
    airport_corridor: str = "",
    event_slug: Optional[str] = None,
    description: str = "",
) -> dict:
    """CSV import when the venue isn't in Stagecraft yet: geocode the city (so live weather works), create the
    venue with one room per distinct `room_name` (capacity = peak attendance + 15%, rounded up to 10), then run
    the normal CSV import. If the import fails, the new venue is removed again."""
    screen = security.inspect_inbound_input("onboarding", f"{venue_name}\n{city}\n{csv_text}", source="csv_import")
    if not screen["allowed"]:
        return {"ok": False, "blocked": True, "security": screen, "errors": [screen["alert"]]}
    geo = geocode_city(city)
    if geo is None:
        return {"ok": False, "errors": [f"Could not find '{city}' (offline, or unknown city). Pick an existing venue template instead."]}
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    missing_cols = [c for c in CSV_COLUMNS if c not in (reader.fieldnames or [])]
    if missing_cols:
        return {"ok": False, "errors": [f"CSV is missing columns: {missing_cols}. Expected: {','.join(CSV_COLUMNS)}"]}
    peaks: dict[str, int] = {}
    hubs: set[str] = set()
    for row in reader:
        room = (row["room_name"] or "").strip()
        if not room:
            continue
        try:
            peaks[room] = max(peaks.get(room, 0), int(row["expected_attendance"]))
        except (TypeError, ValueError):
            peaks.setdefault(room, 50)
        if (row["track"] or "").strip() == "Catering & Networking":
            hubs.add(room)
    if not peaks:
        return {"ok": False, "errors": ["CSV has no rows with a room_name."]}

    region = ", ".join(x for x in (geo["region"], geo["country"]) if x)
    venue = db.create_venue(venue_name or f"{geo['name']} Event Venue", geo["name"], region, geo["latitude"], geo["longitude"],
                            airport_corridor=airport_corridor, notes="Created from CSV import; rooms inferred from the schedule.")
    for room, peak in peaks.items():
        db.create_room(venue.id, room, "Main level", int(math.ceil(peak * 1.15 / 10) * 10) or 10,
                       av_specs=["Ambient PA"] if room in hubs else ["Standard Projection", "Handheld Mics"],
                       is_catering_hub=room in hubs)
    result = import_event_from_csv_or_template(event_name, event_date, attendee_count, venue.id, csv_text=csv_text,
                                               event_slug=event_slug, description=description)
    if not result["ok"]:
        db.delete_venue_if_unused(venue.id)
    else:
        result["geocoded"] = geo
    return result

