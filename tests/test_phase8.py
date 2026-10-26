"""Phase 8 test: "My work" and notifications (accounts linked to the team by email, lead-only budget approvals) and
event-day live mode (served, check-in through the API it uses). No LLM calls.

Live mode itself is client-side (rehearsal clock, now/next, rooms, catering, check-in desk, Something broke -> Resolve);
it was checked in a real browser: scrubbing to 14:40, playing the day, checking someone in, and opening Resolve
pre-filled with what was on at that moment.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_tmp_dir = tempfile.TemporaryDirectory()
os.environ["STAGECRAFT_DB_PATH"] = str(Path(_tmp_dir.name) / "test_stagecraft.db")
os.environ["STAGECRAFT_TODAY"] = "2026-10-24"

from fastapi.testclient import TestClient  # noqa: E402

from backend import changes, notify  # noqa: E402
from backend import database as db  # noqa: E402
from backend.app import app  # noqa: E402
from backend.models import Flag  # noqa: E402

YBVS = "evt_ybvs_2026"
E = f"/api/events/{YBVS}"


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def task_id(title: str) -> str:
    return next(t.id for t in db.list_records("tasks", YBVS) if t.title == title)


def run(lead: TestClient) -> None:
    header("1. Accounts are linked to the organizing team by email")
    lead.post("/api/auth/login", json={"email": "test@stagecraft.com", "password": "password"})
    me = lead.get(f"{E}/planning").json()["me"]
    check(f"the demo account is '{me['name']}' on the YBVS team, a lead who can approve budget changes",
          me["is_member"] and me["is_lead"] and me["can_approve_budget"])
    sofia = TestClient(app)
    sofia.post("/api/auth/signup", json={"first_name": "Sofia", "last_name": "Martinez", "email": "sofia.martinez@ybvs.example", "password": "sofia-pass-1"})
    sm = sofia.get(f"{E}/planning").json()["me"]
    check("a new account whose email matches a team member becomes that member (not a lead)", sm["name"] == "Sofia Martinez" and not sm["is_lead"])
    outsider = TestClient(app)
    outsider.post("/api/auth/signup", json={"first_name": "Omar", "last_name": "Ali", "email": "omar@example.org", "password": "omar-pass-1"})
    check("an account that isn't on any team gets an empty My work with no memberships", outsider.get("/api/me/work").json()["memberships"] == [])

    header("2. My work")
    w = lead.get("/api/me/work").json()
    check(f"the lead's open tasks across events ({[t['title'] for t in w['tasks']]})",
          {"Sign off the run-of-show", "Track actuals against budget weekly"} <= {t["title"] for t in w["tasks"]})
    check("and the deadline they co-own", any(d["title"].startswith("Run-of-show sign-off") for d in w["deadlines"]))
    sw = sofia.get("/api/me/work").json()
    check(f"Sofia sees her own program tasks ({len(sw['tasks'])})", sw["tasks"] and all(t["event_name"] for t in sw["tasks"]))

    header("3. Notifications")
    tid = task_id("Send booth assignments")
    sofia.post(f"{E}/records/task/{tid}", json={"changes": {"assignees": ["Ade Bello", "Test Organizer"]}})
    n = lead.get("/api/notifications").json()
    check(f"assigning someone a task notifies them: '{n['items'][0]['title']}'", n["items"][0]["kind"] == "assigned" and "Send booth assignments" in n["items"][0]["title"]
          and "by Sofia Martinez" in n["items"][0]["body"])
    check("the person who made the change isn't notified", sofia.get("/api/notifications").json()["unread"] == 0)
    lead.post(f"{E}/records/task/{tid}", json={"changes": {"assignees": ["Ade Bello", "Test Organizer", "Sofia Martinez"]}})
    check("people already on the task aren't notified again; the newly added one is", sofia.get("/api/notifications").json()["unread"] == 1)
    notify.flag_raised(YBVS, Flag(id="flg_t", event_id=YBVS, title="Caterer can't make the date", detail="x", severity="Critical", dimension="Food",
                                  source="inbox", created_at="2026-10-24T09:00:00+00:00"))
    notify.flag_raised(YBVS, Flag(id="flg_u", event_id=YBVS, title="Minor typo", detail="x", severity="Low", dimension="Program",
                                  source="inbox", created_at="2026-10-24T09:00:00+00:00"))
    titles = [i["title"] for i in lead.get("/api/notifications").json()["items"]]
    check("Critical/High red flags notify the event's leads; low ones don't", any("Caterer can't make the date" in t for t in titles) and not any("Minor typo" in t for t in titles))
    check("non-leads don't get flag alerts", not any("Caterer" in i["title"] for i in sofia.get("/api/notifications").json()["items"]))
    first = lead.get("/api/notifications").json()["items"][0]["id"]
    before = lead.get("/api/notifications").json()["unread"]
    lead.post("/api/notifications/read", json={"ids": [first]})
    check("mark one read", lead.get("/api/notifications").json()["unread"] == before - 1)
    lead.post("/api/notifications/read", json={})
    check("mark all read", lead.get("/api/notifications").json()["unread"] == 0)

    header("4. Only leads approve budget changes")
    bid = db.list_records("budget_items", YBVS)[0].id
    ch = changes.draft_update(YBVS, "budget", "budget_item", bid, {"planned_usd": 9999}, "test")
    check("leads see 'changes waiting for your OK' live in their notifications",
          any(i["kind"] == "approvals" for i in lead.get("/api/notifications").json()["items"]))
    r = sofia.post(f"{E}/changes/{ch['id']}/confirm")
    check(f"a member can't approve a budget change (409): {r.json()['error'][:52]}…", r.status_code == 409 and changes.get_change(ch["id"])["status"] == "Pending")
    check("a member can't edit the budget directly (403)", sofia.post(f"{E}/records/budget_item/{bid}", json={"changes": {"planned_usd": 1}}).status_code == 403)
    check("a lead can", lead.post(f"{E}/changes/{ch['id']}/confirm").json()["change"]["status"] == "Applied")
    other = changes.draft_update(YBVS, "program", "task", task_id("Collect bios and headshots"), {"priority": "High"}, "test")
    check("non-budget changes are still open to everyone on the team", sofia.post(f"{E}/changes/{other['id']}/confirm").json()["change"]["status"] == "Applied")
    lead.post(f"{E}/records/member/mem_ybvs_2026_04", json={"changes": {"access": "Lead"}})
    check("a lead can make someone else a lead (Sofia can now approve)", sofia.get(f"{E}/planning").json()["me"]["can_approve_budget"])

    header("5. Live mode")
    for f in ("live.js", "notifications.js"):
        check(f"{f} served", lead.get(f"/static/ui/{f}").status_code == 200)
    att = next(a for a in db.list_records("attendees", YBVS) if a.rsvp_status == "Confirmed")
    r = lead.post(f"{E}/records/attendee/{att.id}", json={"changes": {"rsvp_status": "Checked In"}})
    check(f"check-in desk: {att.name} checked in", r.status_code == 200 and db.get_record("attendees", att.id).rsvp_status == "Checked In")
    st = lead.get(f"{E}/planning").json()["attendees"]["by_status"]
    check(f"the live check-in count updates ({st.get('Checked In')} checked in)", st.get("Checked In") == 1)
    ev = lead.get(E).json()
    check("live mode has what it needs: sessions with rooms and speakers, rooms, catering cutoffs",
          all(x["room"]["capacity"] for x in ev["sessions"]) and any(x["speakers"] for x in ev["sessions"]) and ev["rooms"]
          and all(c["hard_cutoff_time"] for c in ev["catering_slas"]))


if __name__ == "__main__":
    with TestClient(app) as client:
        db.reset_demo_db()
        run(client)
    print("\nPASS - Phase 8 verified.")
