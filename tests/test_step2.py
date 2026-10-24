"""Step 2 test: config paths, SQLite init/seed, Pydantic validation, CRUD helpers. No LLM calls.

Runs against a throwaway database file so it never touches the demo DB (backend/stagecraft.db).
"""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # so `backend` imports from any cwd

_tmp_dir = tempfile.TemporaryDirectory()
os.environ["STAGECRAFT_DB_PATH"] = str(Path(_tmp_dir.name) / "test_stagecraft.db")

from pydantic import ValidationError  # noqa: E402

from backend import config, database as db  # noqa: E402
from backend.models import (  # noqa: E402
    CateringSLA,
    CrisisScenarioPreset,
    EventFullState,
    EventSummary,
    Room,
    Session,
    Speaker,
    Venue,
)

YBVS = "evt_ybvs_2026"
TECH_AI = "evt_techai_2026"


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def test_config_paths() -> None:
    print("\n1. Config (.env lookup from backend/)")
    check("REPO_ROOT is the folder that contains backend/", config.REPO_ROOT == REPO_ROOT)
    check("looks in ./.env then ../.env", config.ENV_PATHS == (REPO_ROOT / ".env", REPO_ROOT.parent / ".env"))
    found = ["./.env" if p.parent == REPO_ROOT else "../.env" for p in config.loaded_env_files()]
    print(f"       .env files found: {found}")
    check("an API key was loaded (value not shown)", config.get_llm_backend() in {"portkey", "openai"})
    check("model name configured", bool(config.MODEL_NAME))


def test_init_and_seed() -> None:
    print("\n2. init_db() creates and seeds SQLite")
    check("fresh DB file does not exist yet", not config.DB_PATH.exists())
    db.init_db()
    check("DB file created", config.DB_PATH.exists())
    venues_before = len(db.get_venues())
    db.init_db()  # second call must not duplicate the seed
    check("init_db() is idempotent (no duplicate seed)", len(db.get_venues()) == venues_before == 3)


def test_seed_counts_and_types() -> None:
    print("\n3. Seed data loads into validated Pydantic models")
    venues = db.get_venues()
    events = db.get_events()
    check("3 venues", len(venues) == 3 and all(isinstance(v, Venue) for v in venues))
    check("2 events", len(events) == 2 and all(isinstance(e, EventSummary) for e in events))

    ybvs = db.get_event_full_state(YBVS)
    check("full state is an EventFullState", isinstance(ybvs, EventFullState))
    check("YBVS is the Live War-Room at Evans Hall", ybvs.event.status == "Live War-Room" and ybvs.venue.id == "ven_evans")
    check("Evans Hall has 6 rooms (1 catering hub)", len(ybvs.rooms) == 6 and sum(r.is_catering_hub for r in ybvs.rooms) == 1)
    check("YBVS has 12 sessions", len(ybvs.sessions) == 12 and all(isinstance(s, Session) for s in ybvs.sessions))
    check("YBVS has 16 speakers", len(ybvs.speakers) == 16 and all(isinstance(s, Speaker) for s in ybvs.speakers))
    check("YBVS has catering SLAs", len(ybvs.catering_slas) >= 1 and all(isinstance(c, CateringSLA) for c in ybvs.catering_slas))
    check("YBVS has 4 crisis presets", len(ybvs.crisis_presets) == 4
          and all(isinstance(c, CrisisScenarioPreset) for c in ybvs.crisis_presets))
    check("all 4 crisis categories covered", {c.category for c in ybvs.crisis_presets} == {
        "Flight/Transit Delay", "AV/Room Failure", "Catering/Overrun", "Red-Team Prompt Injection"})
    lunch = next(c for c in ybvs.catering_slas if c.meal_name == "Hot Lunch")
    check("Hot Lunch: 12:30 -> 13:00 cutoff, $4,200 penalty",
          (lunch.scheduled_time, lunch.hard_cutoff_time, lunch.late_penalty_usd) == ("12:30", "13:00", 4200))
    check("sessions span 08:30 to 17:30", ybvs.sessions[0].start_time == "08:30" and max(s.end_time for s in ybvs.sessions) == "17:30")
    check("Opening Keynote is locked in Zhang Auditorium", any(
        s.is_locked_keynote and s.room_id == "rm_evans_zhang" and s.start_time == "09:00" for s in ybvs.sessions))

    room_ids = {r.id for r in ybvs.rooms}
    speaker_ids = {s.id for s in ybvs.speakers}
    check("every session points at a real room and real speakers", all(
        s.room_id in room_ids and set(s.speaker_ids) <= speaker_ids for s in ybvs.sessions))
    check("no session exceeds its room capacity", all(
        s.expected_attendance <= db.get_room(s.room_id).capacity for s in ybvs.sessions))

    tech = db.get_event_full_state(TECH_AI)
    check("Tech & AI Summit: Planning Mode, 6 sessions, 8 speakers",
          tech.event.status == "Planning Mode" and len(tech.sessions) == 6 and len(tech.speakers) == 8)


def test_crud_and_reset() -> None:
    print("\n4. CRUD helpers, validation, and reset")
    room = db.update_room_status("rm_evans_2400", "AV Degraded")
    check("update_room_status persists", db.get_room("rm_evans_2400").status == "AV Degraded" and isinstance(room, Room))

    moved = db.update_session_schedule("ses_ybvs_09", room_id="rm_evans_zhang", start_time="14:45", end_time="15:45")
    check("update_session_schedule moves room and marks Rescheduled",
          moved.room_id == "rm_evans_zhang" and db.get_session("ses_ybvs_09").status == "Rescheduled")

    try:
        db.update_session_schedule("ses_ybvs_09", start_time="16:00", end_time="15:00")
        rejected = False
    except ValidationError:
        rejected = True
    check("end-before-start schedule is rejected", rejected and db.get_session("ses_ybvs_09").start_time == "14:45")

    venue = db.create_venue("Lagos Continental Expo Hall", "Lagos", "Nigeria", 6.4281, 3.4219, "LOS Murtala Muhammed")
    db.create_room(venue.id, "Hall 1", "Ground", 800, ["LED Video Wall"])
    event = db.create_event("Africa Founders Forum 2027", "aff-2027", "2027-03-10", venue.id, 600)
    check("create_venue / create_room / create_event work",
          len(db.get_venues()) == 4 and len(db.get_rooms(venue.id)) == 1 and db.get_event(event.id).name == event.name)

    db.reset_demo_db()
    check("reset_demo_db() restores the clean seed", len(db.get_venues()) == 3 and len(db.get_events()) == 2
          and db.get_room("rm_evans_2400").status == "Operational"
          and db.get_session("ses_ybvs_09").room_id == "rm_evans_2400")


def print_summary() -> None:
    print("\n" + "=" * 78 + "\nSEED SUMMARY\n" + "=" * 78)
    print("\nVENUES")
    for v in db.get_venues():
        rooms = db.get_rooms(v.id)
        print(f"  {v.name}  ({v.city}, {v.state_or_country})  [{v.latitude}, {v.longitude}]")
        print(f"    corridor: {v.airport_corridor}")
        for r in rooms:
            hub = "  [catering hub]" if r.is_catering_hub else ""
            print(f"    - {r.name:<40} cap {r.capacity:>4}  {r.floor_or_zone}{hub}")

    for e in db.get_events():
        state = db.get_event_full_state(e.id)
        rooms = {r.id: r.name for r in state.rooms}
        print(f"\nEVENT: {e.name}  |  {e.date}  |  {state.venue.name}  |  {e.attendee_count} attendees  |  {e.status}")
        print(f"  Sessions ({len(state.sessions)}):")
        for s in state.sessions:
            lock = " [LOCKED]" if s.is_locked_keynote else ""
            print(f"    {s.start_time}-{s.end_time}  {s.title[:52]:<52} {rooms[s.room_id][:24]:<24} {s.expected_attendance:>3}{lock}")
        print(f"  Speakers ({len(state.speakers)}):")
        for sp in state.speakers:
            print(f"    {sp.vip_tier:<18} {sp.name:<22} {sp.travel_origin:<26} {sp.status}")
        print(f"  Catering SLAs ({len(state.catering_slas)}):")
        for c in state.catering_slas:
            print(f"    {c.meal_name:<28} {c.scheduled_time} (cutoff {c.hard_cutoff_time})  ${c.late_penalty_usd:,.0f} penalty  {c.vendor_name}")
        print(f"  Crisis presets ({len(state.crisis_presets)}):")
        for c in state.crisis_presets:
            print(f"    [{c.severity:<8}] {c.category:<26} {c.title}")


if __name__ == "__main__":
    test_config_paths()
    test_init_and_seed()
    test_seed_counts_and_types()
    test_crud_and_reset()
    print_summary()
    print("\nPASS - Step 2 verified.")
