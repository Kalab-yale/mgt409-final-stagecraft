"""Redesign Phase 4 test: the design A workspace is served (offline-safe assets), the classic console still works,
and a person's direct edits are validated, applied immediately, and logged. No LLM calls.

The UI itself was verified interactively in the browser (home, all seven tabs, inbox processing, change cards,
assistant drawer, prefill from red flags, new-event dialog). This test covers what the UI depends on.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_tmp_dir = tempfile.TemporaryDirectory()
os.environ["STAGECRAFT_DB_PATH"] = str(Path(_tmp_dir.name) / "test_stagecraft.db")
os.environ["STAGECRAFT_TODAY"] = "2026-10-24"

from fastapi.testclient import TestClient  # noqa: E402

from backend import changes, planning  # noqa: E402
from backend import database as db  # noqa: E402
from backend.app import app  # noqa: E402

YBVS = "evt_ybvs_2026"
E = f"/api/events/{YBVS}"
MODULES = ("main.js", "core.js", "icons.js", "workspace.js", "chat.js", "modals.js", "resolve.js", "settings.js")


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def run(client: TestClient) -> None:
    header("1. The workspace shell and its assets")
    r = client.get("/")
    print(f"  GET / -> {r.status_code}, {len(r.text):,} bytes")
    check("GET / serves the design A shell (light top bar, module entry point)", r.status_code == 200 and 'class="topbar"' in r.text
          and 'type="module" src="/static/ui/main.js' in r.text and "Stagecraft" in r.text)
    check("no external CDNs: works offline (no Tailwind, font, or icon CDN)",
          not re.search(r"https?://(cdn\.|unpkg|fonts\.googleapis)", r.text))
    css = client.get("/static/ui/app.css")
    check(f"stylesheet served ({len(css.content):,} bytes) and has no remote imports", css.status_code == 200 and "@import" not in css.text)
    for m in MODULES:
        a = client.get(f"/static/ui/{m}")
        check(f"module {m} served with Cache-Control: no-cache ({len(a.content):,} bytes)",
              a.status_code == 200 and a.headers.get("cache-control") == "no-cache" and len(a.content) > 1000)
    imports = set(re.findall(r'from "\./([a-z]+\.js)"', "".join(client.get(f"/static/ui/{m}").text for m in MODULES)))
    check(f"every module import resolves ({sorted(imports)})", imports <= set(MODULES))
    check("the original console is still reachable at /classic", client.get("/classic").status_code == 200)
    check("the attendee live page still works", client.get(f"/live/{YBVS}").status_code == 200)

    header("2. Direct edits by a person: applied right away, validated, logged")
    before = planning.event_health(YBVS)["overdue_deadlines"]
    r = client.post(f"{E}/records/deadline/dl_ybvs_2026_04", json={"changes": {"done": True}})
    print(f"  tick 'AV vendor quote approved' done -> {r.status_code}, overdue {before} -> {r.json()['health']['overdue_deadlines']}")
    check("ticking an overdue deadline done applies at once and returns fresh health", r.status_code == 200
          and r.json()["record"]["done"] is True and r.json()["health"]["overdue_deadlines"] == before - 1)
    check("the edit is in the activity log as the organizer's", changes.list_activity(YBVS)[0]["actor"] == "organizer"
          and "AV vendor quote approved" in changes.list_activity(YBVS)[0]["summary"])
    r = client.post(f"{E}/records/task/tsk_ybvs_2026_202", json={"changes": {"status": "Done"}})
    check("ticking the blocked AV task done clears the 'Blocked task' flag",
          r.status_code == 200 and not any(f["title"].startswith("Blocked task") for f in planning.get_flags(YBVS)))
    bad = client.post(f"{E}/records/task/tsk_ybvs_2026_201", json={"changes": {"status": "Someday"}})
    check(f"invalid value rejected (400): {bad.json().get('error', '')[:70]}", bad.status_code == 400)
    check("unknown record type -> 404", client.post(f"{E}/records/spaceship/x", json={"changes": {}}).status_code == 404)
    check("a record from another event can't be edited through this event (404)",
          client.post(f"{E}/records/task/tsk_techai_2026_101", json={"changes": {"status": "Done"}}).status_code == 404)
    check("id/event_id can't be changed (400)",
          client.post(f"{E}/records/task/tsk_ybvs_2026_201", json={"changes": {"event_id": "evt_techai_2026"}}).status_code == 400)


if __name__ == "__main__":
    with TestClient(app) as client:
        db.reset_demo_db()
        run(client)
    print("\nPASS - Phase 4 verified.")
