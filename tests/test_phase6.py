"""Phase 6 test: accounts (create account, log in, sessions, password storage, lockout) and the organizing team
(roster, PMO, multi-person task assignment, workload, red flags, the Team assistant's scope). No LLM calls.

The new screens (login, create account, home hero and timeline, Team tab, schedule grid, charts) were checked in a
real browser with Playwright: test user login, a brand-new account, assigning, adding people, changing the PMO, and
one live Team-assistant request that drafted and applied a shared assignment.
"""

import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_tmp_dir = tempfile.TemporaryDirectory()
os.environ["STAGECRAFT_DB_PATH"] = str(Path(_tmp_dir.name) / "test_stagecraft.db")
os.environ["STAGECRAFT_TODAY"] = "2026-10-24"

from fastapi.testclient import TestClient  # noqa: E402

from backend import auth, changes, planning, team  # noqa: E402
from backend import database as db  # noqa: E402
from backend.agents.assistants import TABS, _field_guide  # noqa: E402
from backend.app import app  # noqa: E402
from backend.config import DB_PATH  # noqa: E402

YBVS = "evt_ybvs_2026"
E = f"/api/events/{YBVS}"


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def accounts(client: TestClient) -> None:
    header("1. Logging in and creating accounts")
    check("without a session every /api call is refused (401)", all(client.get(p).status_code == 401
          for p in ("/api/home", f"{E}/planning", "/api/admin/tokenomics", f"{E}/inbox", "/api/connectors/gmail/status")))
    check("pages, the attendee page, and its calendar stay public", client.get("/").status_code == 200
          and client.get(f"/live/{YBVS}").status_code == 200 and client.get(f"{E}/calendar.ics").status_code == 200)
    r = client.post("/api/auth/login", json={"email": "test@stagecraft.com", "password": "password"})
    cookie = r.headers.get("set-cookie", "")
    check(f"seeded test user logs in ({r.json()['user']['display_name']})", r.status_code == 200 and client.get("/api/home").status_code == 200)
    check("session cookie is HttpOnly and SameSite=Lax (JavaScript can't read it)", "HttpOnly" in cookie and "samesite=lax" in cookie.lower())
    client.post("/api/auth/logout")
    check("logging out ends the session", client.get("/api/home").status_code == 401)

    r = client.post("/api/auth/signup", json={"first_name": "Maya", "last_name": "Chen", "email": "Maya.Chen@Example.org",
                                              "password": "Summit-2026!", "confirm_password": "Summit-2026!"})
    check("a new account is created and logged in straight away", r.status_code == 200 and r.json()["user"]["email"] == "maya.chen@example.org"
          and client.get("/api/home").status_code == 200)
    client.post("/api/auth/logout")
    check("the new account logs back in (email is case-insensitive)",
          client.post("/api/auth/login", json={"email": "MAYA.CHEN@example.org", "password": "Summit-2026!"}).status_code == 200)
    client.post("/api/auth/logout")
    bad = {
        "duplicate email": {"first_name": "M", "last_name": "C", "email": "maya.chen@example.org", "password": "12345678"},
        "passwords don't match": {"first_name": "A", "last_name": "B", "email": "a@b.org", "password": "12345678", "confirm_password": "87654321"},
        "short password": {"first_name": "A", "last_name": "B", "email": "a@b.org", "password": "1234567"},
        "bad email": {"first_name": "A", "last_name": "B", "email": "not-an-email", "password": "12345678"},
        "missing name": {"first_name": " ", "last_name": "B", "email": "a@b.org", "password": "12345678"},
    }
    for label, body in bad.items():
        r = client.post("/api/auth/signup", json=body)
        check(f"signup refused: {label} -> {r.json().get('error')}", r.status_code == 400)
    wrong = client.post("/api/auth/login", json={"email": "maya.chen@example.org", "password": "nope-nope"}).json()["error"]
    unknown = client.post("/api/auth/login", json={"email": "ghost@example.org", "password": "nope-nope"}).json()["error"]
    check("wrong password and unknown email get the same message (no account fishing)", wrong == unknown == "Email or password is incorrect.")

    header("2. How passwords and sessions are stored")
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT email, password_hash FROM users").fetchall()
    check(f"{len(rows)} users; no password is stored in plain text", rows and all("Summit-2026!" not in h and "password" != h for _, h in rows))
    check("each hash is scrypt with OWASP's cost (N=2^17, r=8, p=1) and its own salt",
          all(re.fullmatch(r"scrypt\$131072\$8\$1\$[A-Za-z0-9+/=]{24}\$[A-Za-z0-9+/=]{44}", h) for _, h in rows)
          and len({h.split("$")[4] for _, h in rows}) == len(rows))
    h1, h2 = auth.hash_password("same password"), auth.hash_password("same password")
    check("the same password hashes differently every time (salted), and both verify",
          h1 != h2 and auth.verify_password("same password", h1) and auth.verify_password("same password", h2) and not auth.verify_password("Same password", h1))
    client.post("/api/auth/login", json={"email": "test@stagecraft.com", "password": "password"})
    token = client.cookies.get(auth.COOKIE)
    stored = [r[0] for r in con.execute("SELECT token_hash FROM auth_sessions")]
    check("session tokens are stored only as SHA-256 hashes", token not in stored and auth._token_hash(token) in stored)
    check("the users endpoint never returns a password hash", "password_hash" not in client.get("/api/auth/me").text)
    client.post("/api/auth/logout")

    header("3. Brute-force lockout")
    for _ in range(auth.MAX_FAILS):
        client.post("/api/auth/login", json={"email": "maya.chen@example.org", "password": "guess"})
    r = client.post("/api/auth/login", json={"email": "maya.chen@example.org", "password": "Summit-2026!"})
    check(f"after {auth.MAX_FAILS} wrong passwords even the right one is refused for {auth.LOCK_MINUTES} min (429)", r.status_code == 429)
    check("other accounts are unaffected", client.post("/api/auth/login", json={"email": "test@stagecraft.com", "password": "password"}).status_code == 200)


def teams(client: TestClient) -> None:
    header("4. The organizing team")
    t = client.get(f"{E}/planning").json()["team"]
    check(f"10 people (incl. the demo lead), PMO = {t['pmo_name']}, every open task has an owner", len(t["members"]) == 10 and t["pmo_name"] == "Chris Nguyen"
          and t["unassigned_open_tasks"] == 0)
    shared = [x for x in db.list_records("tasks", YBVS) if len(x.assignees) > 1]
    check(f"{len(shared)} demo tasks are shared by several people, with all names shown",
          len(shared) == 6 and all(x.owner.count(",") == len(x.assignees) - 1 for x in shared))
    r = client.post(f"{E}/records/member", json={"fields": {"name": "Dana Lee", "email": "dana@ybvs.example", "role": "Signage", "team": "Volunteers"}})
    dana = r.json()["record"]["id"]
    check("add a person", r.status_code == 200 and len(team.members(YBVS)) == 11)
    r = client.post(f"{E}/records/task", json={"fields": {"title": "Hang room signs", "dimension": "Venue", "priority": "High", "assignees": ["Dana Lee", "Malik"]}})
    tid = r.json()["record"]["id"]
    check(f"create a task for two people by name ('Malik' alone is enough) -> {r.json()['record']['owner']}",
          r.status_code == 200 and r.json()["record"]["owner"] == "Dana Lee, Malik Johnson")
    r = client.post(f"{E}/records/task/{tid}", json={"changes": {"assignees": ["Nobody Real"]}})
    check(f"unknown people are refused: {r.json()['error'][:48]}…", r.status_code == 400)
    r = client.post(f"{E}/team/pmo", json={"member_id": dana})
    check("change the PMO: exactly one PMO afterwards", r.json()["team"]["pmo_name"] == "Dana Lee" and sum(m["is_pmo"] for m in r.json()["team"]["members"]) == 1)
    client.post(f"{E}/team/pmo", json={"member_id": "mem_ybvs_2026_02"})
    r = client.delete(f"{E}/records/member/{dana}")
    task = db.get_record("tasks", tid)
    check("removing a person takes them off their tasks and out of the owner text", r.status_code == 200 and task.owner == "Malik Johnson" and len(task.assignees) == 1)
    check("activity log shows who did it", changes.list_activity(YBVS)[0]["actor"] == "Test Organizer")

    header("5. Team red flags and workload")
    client.post(f"{E}/records/task/{tid}", json={"changes": {"assignees": []}})
    titles = [f["title"] for f in planning.get_flags(YBVS)]
    check("a high-priority task with nobody on it raises a red flag", "Nobody owns: Hang room signs" in titles)
    for x in [x for x in db.list_records("tasks", YBVS) if x.status != "Done"][:8]:
        db.update_record("tasks", x.id, **team.normalize_task_fields(YBVS, {"assignees": ["Priya Raman"]}))
    w = {m["name"]: m for m in team.workload(YBVS)["members"]}
    check(f"anyone with more than {team.OVERLOADED_OPEN_TASKS} open tasks is marked stretched and flagged",
          w["Priya Raman"]["stretched"] and any(t.startswith("Priya Raman is stretched") for t in (f["title"] for f in planning.get_flags(YBVS))))
    with db.connect() as conn:
        conn.execute("UPDATE team_members SET is_pmo = 0 WHERE event_id = ?", (YBVS,))
    check("a team with no PMO is flagged", "No PMO on the team" in [f["title"] for f in planning.get_flags(YBVS)])

    header("6. Assistants and the Team tab")
    check("there is a Team assistant that may edit members (its own tools), tasks, deadlines, and workstreams",
          TABS["team"]["agent"] == "TeamAssistant" and TABS["team"]["records"] == {"task", "deadline", "initiative"}
          and {f.__name__ for f in TABS["team"]["extra_writes"]} == {"propose_add_member", "propose_update_member", "propose_remove_member"})
    check("no other agent is offered team-member edits (the Inbox Watcher once tried to edit a speaker as a member)",
          all(not ({f.__name__ for f in cfg["extra_writes"]} & {"propose_add_member", "propose_update_member", "propose_remove_member"})
              for k, cfg in TABS.items() if k != "team"))
    check("its field guide tells the model the allowed teams and the assignees field",
          "team=Leadership|" in _field_guide({"member"}) and "assignees" in _field_guide({"task"}))
    ch = changes.draft_update(YBVS, "team", "task", tid, {"assignees": ["Chris", "Hannah Okoro"]}, "share it")
    check(f"a drafted assignment shows names on the card: {ch['diff'][0]['after']}", ch["diff"][0]["after"] == "Chris Nguyen, Hannah Okoro")
    check("confirming applies it", changes.confirm_change(ch["id"], "Test Organizer")["status"] == "Applied"
          and db.get_record("tasks", tid).owner == "Chris Nguyen, Hannah Okoro")
    home = client.get("/api/home").json()["events"][0]
    check("the home page gets each event's team and next three weeks of deadlines", home["team"] and "upcoming" in home)
    for m in ("auth.js", "people.js", "team.js", "charts.js", "visuals.js"):
        check(f"{m} is served", client.get(f"/static/ui/{m}").status_code == 200)


if __name__ == "__main__":
    with TestClient(app) as client:
        db.reset_demo_db()
        accounts(client)
        teams(client)
    print("\nPASS - Phase 6 verified.")
