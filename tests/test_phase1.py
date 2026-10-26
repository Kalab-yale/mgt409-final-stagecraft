"""Redesign Phase 1 test: planning data layer (deadlines, initiatives, tasks, attendees, budget, vendors, inbox,
flags), relative demo dates, deterministic red-flag rules, health summary, starter plan, and read APIs. No LLM calls.

'Today' is pinned with STAGECRAFT_TODAY so results are reproducible; a throwaway database keeps the demo DB untouched.
"""

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_tmp_dir = tempfile.TemporaryDirectory()
os.environ["STAGECRAFT_DB_PATH"] = str(Path(_tmp_dir.name) / "test_stagecraft.db")
os.environ["STAGECRAFT_TODAY"] = "2026-10-24"

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from backend import database as db  # noqa: E402
from backend import planning  # noqa: E402
from backend.agents.onboarding_agent import import_event_from_csv_or_template  # noqa: E402
from backend.app import app  # noqa: E402
from _auth import login  # noqa: E402

YBVS, TECH_AI = "evt_ybvs_2026", "evt_techai_2026"
TODAY = date(2026, 10, 24)


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def flag_titles(event_id: str) -> list[str]:
    return [f["title"] for f in planning.get_flags(event_id)]


def test_seed_and_dates() -> None:
    header("1. Seed: rich planning data, dated relative to 'today'")
    counts = {t: len(db.list_records(t, YBVS)) for t in db.PLANNING_TABLES}
    print(f"  YBVS: {counts}")
    check("YBVS seeded: 10 team members (incl. the demo lead), 17 deadlines, 6 initiatives, 27 tasks, 362 attendees, 8 vendors/sponsors, 12 budget lines, 14 emails",
          counts == {"team_members": 10, "deadlines": 17, "initiatives": 6, "tasks": 27, "attendees": 362, "vendors": 8, "budget_items": 12,
                     "inbox_messages": 14, "flags": 1})
    check("Tech & AI Summit seeded too", all(len(db.list_records(t, TECH_AI)) > 0 for t in ("deadlines", "initiatives", "attendees", "inbox_messages")))
    check("YBVS is 21 days out, Tech & AI 41 days out", db.get_event(YBVS).date == TODAY + timedelta(days=21)
          and db.get_event(TECH_AI).date == TODAY + timedelta(days=41))
    inbox = db.list_records("inbox_messages", YBVS)
    check("inbox timestamps are within the last ~3 days", all(m.received_at[:10] >= str(TODAY - timedelta(days=4)) for m in inbox))
    check("all seeded contacts use the reserved .example domain",
          all(a.email.endswith(".example") for a in db.list_records("attendees", YBVS))
          and all(v.contact_email.endswith(".example") for v in db.list_records("vendors", YBVS)))

    os.environ["STAGECRAFT_TODAY"] = "2027-03-01"
    db.reset_demo_db()
    later = db.get_event(YBVS).date
    av = next(d for d in db.list_records("deadlines", YBVS) if d.title == "AV vendor quote approved")
    check(f"re-seeding on another day keeps the demo 21 days out ({later})", later == date(2027, 3, 22) and av.due_date == later - timedelta(days=23))
    os.environ["STAGECRAFT_TODAY"] = "2026-10-24"
    db.reset_demo_db()


def test_statuses_and_progress() -> None:
    header("2. Deadline status and initiative progress")
    statuses = {d.title: planning.deadline_status(d) for d in db.list_records("deadlines", YBVS)}
    for t, s in statuses.items():
        if s != "Upcoming":
            print(f"     {s:<9} {t}")
    check("exactly one overdue deadline (AV quote approval, 2 days late)",
          [t for t, s in statuses.items() if s == "Overdue"] == ["AV vendor quote approved"])
    check("bios/headshots due tomorrow -> 'Due soon'", statuses["Speaker bios and headshots collected"] == "Due soon")
    tasks = db.list_records("tasks", YBVS)
    prog = {i.title: planning.initiative_progress(i, tasks) for i in db.list_records("initiatives", YBVS)}
    for t, p in prog.items():
        print(f"     {p['progress_pct']:>3}%  {p['status']:<9} {t}  ({p['tasks_done']}/{p['tasks_total']})")
    check("speaker workstream 1/5 done = 20%", prog["Speaker lineup and travel"]["progress_pct"] == 20)
    check("venue workstream is Blocked (AV quote task blocked)", prog["Venue, AV, and room readiness"]["status"] == "Blocked")


def test_flags_and_health() -> None:
    header("3. Red flags (deterministic rules) and event health")
    flags = planning.get_flags(YBVS)
    for f in flags:
        print(f"     {f['severity']:<8} {f['dimension_label']:<22} {f['title']}")
    titles = [f["title"] for f in flags]
    check("overdue AV approval, unsigned AV + shuttle contracts, social ads over budget, blocked task, due-soon bios, stored keynote flag", all(
        t in titles for t in ("Overdue: AV vendor quote approved", "AV and Production contract not signed: Elm City AV Productions",
                              "Transport contract not signed: Shoreline Shuttle Co.", "Over budget: Social media ads",
                              "Blocked task: Approve AV vendor quote", "Due in 1 day: Speaker bios and headshots collected",
                              "Afternoon keynote slides not received")))
    check("sorted most severe first", [planning.SEVERITY_RANK[f["severity"]] for f in flags] == sorted(planning.SEVERITY_RANK[f["severity"]] for f in flags))

    h = planning.event_health(YBVS)
    print(f"  health: {h['days_to_go']} days · {h['phase']} · {h['progress_pct']}% · flags {h['flags']} · regs {h['registrations']} · "
          f"budget {h['budget']} · {h['inbox_new']} new emails")
    check("health: 21 days, Planning phase, 318/350 active registrations, $61,400 planned / $31,460 spent, $25k sponsorship signed",
          h["days_to_go"] == 21 and h["phase"] == "Planning" and h["registrations"]["active"] == 318 and h["registrations"]["cap"] == 350
          and h["budget"]["planned_usd"] == 61400 and h["budget"]["spent_usd"] == 31460 and h["budget"]["sponsorship_signed_usd"] == 25000)

    print("  fixing the data clears the flags:")
    db.update_record("deadlines", "dl_ybvs_2026_04", done=True)
    db.update_record("vendors", "ven_ybvs_av", contract_status="Signed")
    db.update_record("budget_items", "bud_ybvs_09", planned_usd=2000)
    after = flag_titles(YBVS)
    check("AV approval done + contract signed + ads budget raised -> those three flags disappear",
          not any(t in after for t in ("Overdue: AV vendor quote approved", "AV and Production contract not signed: Elm City AV Productions",
                                       "Over budget: Social media ads")))
    db.reset_demo_db()

    os.environ["STAGECRAFT_TODAY"] = str(db.get_event(YBVS).date - timedelta(days=2))
    late = planning.event_health(YBVS)
    escalated = [f for f in planning.get_flags(YBVS) if f["title"].startswith("Overdue")]
    print(f"  two days before the event: phase={late['phase']}, overdue={late['overdue_deadlines']}, "
          f"severities={sorted({f['severity'] for f in escalated})}")
    check("in the final week the phase changes and overdue items escalate to Critical",
          late["phase"] == "Final week" and late["overdue_deadlines"] > 1 and {f["severity"] for f in escalated} == {"Critical"})
    os.environ["STAGECRAFT_TODAY"] = "2026-10-24"


def test_crud_and_starter_plan() -> None:
    header("4. Validated CRUD and the starter plan for new events")
    t = db.create_record("tasks", "tsk", event_id=YBVS, title="Order lanyards", dimension="General", priority="Low")
    check("create_record validates and saves", db.get_record("tasks", t.id).title == "Order lanyards")
    for label, fn in (("bad status", lambda: db.update_record("tasks", t.id, status="Maybe")),
                      ("bad dimension", lambda: db.create_record("deadlines", "dl", event_id=YBVS, title="x", dimension="Parking", due_date="2026-11-01")),
                      ("changing event_id", lambda: db.update_record("tasks", t.id, event_id=TECH_AI))):
        try:
            fn()
            ok = False
        except (ValidationError, ValueError):
            ok = True
        check(f"rejected: {label}", ok)
    check("delete_record", db.delete_record("tasks", t.id) and db.get_record("tasks", t.id) is None)

    csv = ("title,track,room_name,start_time,end_time,expected_attendance,speaker_name\n"
           "Opening Keynote,Keynote,Zhang Auditorium,09:00,10:00,300,Dana Whitlock\n"
           "Lunch,Catering & Networking,Beinecke Terrace,12:00,13:00,300,")
    new_date = TODAY + timedelta(days=30)
    r = import_event_from_csv_or_template("Healthcare AI Day", str(new_date), 300, "ven_evans", csv_text=csv)
    dls = db.list_records("deadlines", r["event_id"])
    print(f"  new event on {new_date}: {len(dls)} deadlines, {len(db.list_records('initiatives', r['event_id']))} initiatives, "
          f"{len(db.list_records('tasks', r['event_id']))} tasks")
    check("new events get a starter plan", len(dls) > 0 and len(db.list_records("initiatives", r["event_id"])) == 5)
    check("starter plan skips deadlines already in the past (no fake overdue items)",
          all(d.due_date >= TODAY for d in dls) and not any(d.title == "Venue contract signed" for d in dls))
    db.delete_event(r["event_id"])
    check("delete_event removes the event's planning rows", all(db.list_records(t, r["event_id"]) == [] for t in db.PLANNING_TABLES))


def test_api(client: TestClient) -> None:
    header("5. Read APIs")
    home = client.get("/api/home").json()
    for e in home["events"]:
        h = e["health"]
        print(f"  {e['name']:<40} {h['days_to_go']:>3} days · {h['progress_pct']:>3}% · {h['flags']['total']} flags · next: {h['next_deadlines'][0]['title']}")
    check("GET /api/home -> one card per event with health", len(home["events"]) == 2 and all("health" in e for e in home["events"]))
    p = client.get(f"/api/events/{YBVS}/planning").json()
    check("GET /api/events/{id}/planning -> deadlines, initiatives, attendees, budget, vendors, inbox, flags",
          {"health", "flags", "deadlines", "initiatives", "attendees", "vendors", "budget", "inbox"} <= set(p))
    print(f"  dietary (active registrations): {p['attendees']['dietary_active']}")
    check("attendee summaries computed (dietary counts sum to active registrations)",
          sum(p["attendees"]["dietary_active"].values()) == p["health"]["registrations"]["active"])
    check("inbox newest first", p["inbox"][0]["received_at"] >= p["inbox"][-1]["received_at"])
    check("unknown event -> 404", client.get("/api/events/nope/planning").status_code == 404)


if __name__ == "__main__":
    with TestClient(app) as client:
        login(client)
        db.reset_demo_db()
        test_seed_and_dates()
        test_statuses_and_progress()
        test_flags_and_health()
        test_crud_and_starter_plan()
        test_api(client)
    print("\nPASS - Phase 1 verified.")
