"""Step 10 test: War-Room API (run / commit), .ics download, and the Public Attendee Live page, via TestClient.

One live 3-agent pipeline run on gpt-6-luna (well under a cent). Uses a throwaway database and resets it at the end.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # so `backend` imports from any cwd

_tmp_dir = tempfile.TemporaryDirectory()
os.environ["STAGECRAFT_DB_PATH"] = str(Path(_tmp_dir.name) / "test_stagecraft.db")

from fastapi.testclient import TestClient  # noqa: E402

from backend import database as db  # noqa: E402
from backend.app import app  # noqa: E402
from _auth import login  # noqa: E402

YBVS = "evt_ybvs_2026"
AI_PANEL = "ses_ybvs_09"


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def preset(client: TestClient, category: str) -> str:
    return next(c["prompt_text"] for c in client.get(f"/api/events/{YBVS}").json()["crisis_presets"] if c["category"] == category)


def run(client: TestClient) -> None:
    header("1. POST /crisis/run with Preset 4 (prompt-injection booby-trap)")
    r = client.post(f"/api/events/{YBVS}/crisis/run", json={"crisis_text": preset(client, "Red-Team Prompt Injection")})
    b = r.json()
    print(f"  -> {r.status_code} status={b['status']}  tokens={b['tokenomics_summary']['total_tokens']}  "
          f"cost=${b['tokenomics_summary']['total_cost_usd']:.4f}  latency={b['total_latency_ms']} ms")
    for f in b["security_gate"]["findings"]:
        print(f"     caught {f['category']}: \"{f['matched_text']}\"")
    check("status BLOCKED_BY_SECURITY_GUARDRAIL with a SecurityIncident", r.status_code == 200 and b["status"] == "BLOCKED_BY_SECURITY_GUARDRAIL"
          and b["security_incident"]["defense_layer"].startswith("Layer 1"))
    check("0 LLM tokens, $0, only the gate ran", b["tokenomics_summary"]["total_tokens"] == 0 and b["tokenomics_summary"]["total_cost_usd"] == 0
          and [s["agent_name"] for s in b["agent_handoff_trace"]] == ["Security Gate (Layer 1)"])
    check("blocked run cannot be committed (409)",
          client.post(f"/api/events/{YBVS}/crisis/commit", json={"run_id": b["run_id"], "option_choice": "recommended"}).status_code == 409)

    header("2. POST /crisis/run with Preset 2 (Room 2400 AV failure + 118 RSVPs) - live 3-agent pipeline")
    r = client.post(f"/api/events/{YBVS}/crisis/run", json={"crisis_text": preset(client, "AV/Room Failure"), "cost_saver": True})
    b = r.json()
    print(f"  -> {r.status_code} status={b['status']} in {b['total_latency_ms'] / 1000:.1f}s (vs ~{b['manual_baseline_minutes']} min manual)")
    for s in b["agent_handoff_trace"]:
        print(f"     {s['step']} {s['agent_name']:<26} {s['status']:<9} {s['latency_ms']:>7,.0f} ms  {s['input_tokens']:>6,} in / "
              f"{s['cached_input_tokens']:>5,} cached / {s['output_tokens']:>4,} out  ${s['cost_usd']:.6f}  tools={len(s['tools_called'])}")
    t = b["tokenomics_summary"]
    print(f"     TOTAL {t['total_tokens']:,} tokens  ${t['total_cost_usd']:.6f}  ({t['savings_pct']}% below no-Cost-Saver baseline)")
    check("status SUCCESS with triage, solver, comms, trace", b["status"] == "SUCCESS" and all(b[k] for k in ("triage", "solver", "comms"))
          and len(b["agent_handoff_trace"]) == 4)
    rec, alt = b["solver"]["recommended_option"], b["solver"]["alternative_option"]
    print(f"     Option A: {rec['option_label']} (score {rec['optimization_score']}, verified={b['solver']['deterministic_verification_passed']})")
    print(f"     Option B: {alt['option_label']} (score {alt['optimization_score']}, verified={b['solver']['alternative_verified']})")
    print(f"     banner draft: {b['comms']['public_attendee_banner_alert']}")
    check("both options verified for the UI badges", b["solver"]["deterministic_verification_passed"] and b["solver"]["alternative_verified"])
    run_id = b["run_id"]
    banner = b["comms"]["public_attendee_banner_alert"]

    header("3. POST /crisis/commit (recommended)")
    before = db.get_session(AI_PANEL)
    r = client.post(f"/api/events/{YBVS}/crisis/commit", json={"run_id": run_id, "option_choice": "recommended"})
    c = r.json()
    for ch in c.get("applied_changes", []):
        print(f"  APPLIED {ch['title']}: {ch['from']} -> {ch['to']}")
    after = db.get_session(AI_PANEL)
    check(f"200 and SQLite updated ({before.room_id} -> {after.room_id}, status {after.status})",
          r.status_code == 200 and after.room_id != "rm_evans_2400" and after.status == "Rescheduled")
    st = next(s for s in c["event_state"]["sessions"] if s["id"] == AI_PANEL)
    check("returned event_state carries the War-Room badge + new room", st["war_room_update"]["badge"] == "Updated via Stagecraft War-Room"
          and st["room"]["id"] == after.room_id)
    check("Room 2400 now AV Degraded in the venue deck",
          next(x for x in c["event_state"]["rooms"] if x["id"] == "rm_evans_2400")["status"] == "AV Degraded")
    again = client.post(f"/api/events/{YBVS}/crisis/commit", json={"run_id": run_id, "option_choice": "alternative"})
    check("second commit of the same run refused (409)", again.status_code == 409)

    header("4. GET /live/{event_id} and /calendar.ics")
    live = client.get(f"/live/{YBVS}")
    html = live.text
    new_room = db.get_room(after.room_id).name
    print(f"  /live -> {live.status_code}, {len(html):,} bytes; banner shown: {'Schedule update' in html}")
    check("200 OK, public page with the event", live.status_code == 200 and "Yale Black Venture Summit" in html)
    check("live banner from the War-Room comms is displayed", "Schedule update" in html and banner.split(":")[0].replace("&", "&amp;")[:30] in html)
    check(f"updated session marked Moved, with the new room ({new_room}) and the old one struck through",
          ">Moved<" in html and new_room in html and "Room 2400 - Class of 1980 14:45-15:45" in html)
    check("/live also accepts the slug, /public redirects to /live",
          client.get("/live/ybvs-2026").status_code == 200 and client.get(f"/public/{YBVS}", follow_redirects=False).headers["location"] == f"/live/{YBVS}")

    ics = client.get(f"/api/events/{YBVS}/calendar.ics")
    body = ics.text.replace("\r\n ", "")
    print(f"  calendar.ics -> {ics.status_code} {ics.headers['content-type']} {ics.headers['content-disposition']}")
    check("valid VCALENDAR (text/calendar, attachment, 12 VEVENTs)", ics.status_code == 200 and ics.headers["content-type"].startswith("text/calendar")
          and body.startswith("BEGIN:VCALENDAR") and body.rstrip().endswith("END:VCALENDAR") and body.count("BEGIN:VEVENT") == 12)
    check("moved session carries its new time/room and SEQUENCE:1",
          f"UID:{AI_PANEL}@ybvs-2026.stagecraft.example" in body and f"SUMMARY:AI Agents & Enterprise SaaS" in body
          and "SEQUENCE:1" in body and f"LOCATION:{new_room}" in body.replace("\\,", ","))
    only = client.get(f"/api/events/{YBVS}/calendar.ics?only_updated=true").text
    check("?only_updated=true returns just the changed session", only.count("BEGIN:VEVENT") == 1)
    check("unknown event -> 404 on run, live, and calendar",
          client.post("/api/events/nope/crisis/run", json={"crisis_text": "test crisis"}).status_code == 404
          and client.get("/live/nope").status_code == 404 and client.get("/api/events/nope/calendar.ics").status_code == 404)

    header("5. Reset")
    client.post("/api/demo/reset")
    check("demo DB back to initial state", db.get_session(AI_PANEL).room_id == "rm_evans_2400"
          and "Schedule update" not in client.get(f"/live/{YBVS}").text)


if __name__ == "__main__":
    with TestClient(app) as client:
        login(client)
        run(client)
    print("\nPASS - Step 10 verified.")
