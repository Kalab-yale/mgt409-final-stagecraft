"""Redesign Phase 3 test: Inbox Watcher, inbox flags and drafts, the Daily Brief, and the Inbox chat assistant.

Part A (no LLM): schema migration, quarantine, deterministic digest, flag API. Part B (live, about $0.01): the whole
demo inbox processed in parallel through the API, confirming an inbox-drafted change, a pasted email, the cached brief,
and the Inbox assistant. 'Today' is pinned; a throwaway database keeps the demo DB untouched.
"""

import os
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

from backend import changes, security, tokenomics  # noqa: E402
from backend import database as db  # noqa: E402
from backend.agents.brief_writer import build_digest  # noqa: E402
from backend.agents.inbox_watcher import process_email  # noqa: E402
from backend.app import app  # noqa: E402

YBVS = "evt_ybvs_2026"
E = f"/api/events/{YBVS}"


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def part_a(client: TestClient) -> None:
    header("A. Migration, quarantine, digest, flags (no LLM)")
    with sqlite3.connect(os.environ["STAGECRAFT_DB_PATH"]) as conn:  # make it look like a pre-Phase-3 database
        conn.execute("ALTER TABLE inbox_messages DROP COLUMN reply_draft")
        conn.execute("ALTER TABLE pending_changes DROP COLUMN origin")
    db.init_db()
    cols = {r[1] for r in sqlite3.connect(os.environ["STAGECRAFT_DB_PATH"]).execute("PRAGMA table_info(inbox_messages)")}
    check("init_db migrates an older database (missing columns added, old rows load with defaults)",
          "reply_draft" in cols and db.list_records("inbox_messages", YBVS)[0].reply_draft == "")

    r = process_email(YBVS, "msg_ybvs_2026_12")
    print(f"  phishing email -> {r['status']}, tokens {r['usage']['total_tokens']}, flag {r['flags']}")
    flag = db.get_record("flags", r["flags"][0])
    print(f"     flag: {flag.title} | {flag.detail[:120]}...")
    check("phishing email quarantined before any AI read it (0 tokens), flagged, incident logged",
          r["status"] == "Quarantined" and r["usage"]["total_tokens"] == 0 and flag.source == "inbox"
          and any(i.incident_id == r["incident_id"] for i in security.get_security_incidents(YBVS)))
    check("re-processing a handled email is skipped", process_email(YBVS, "msg_ybvs_2026_12").get("skipped"))

    d = build_digest(YBVS)
    print(f"  digest: overdue={d['deadlines_overdue']}\n          this week={d['deadlines_this_week']}\n          inbox={ {k: v if isinstance(v, int) else len(v) for k, v in d['inbox'].items()} }")
    check("digest has exact overdue/this-week deadlines and inbox counts", any("AV vendor quote" in x for x in d["deadlines_overdue"])
          and any("Speaker bios" in x for x in d["deadlines_this_week"]) and d["inbox"]["new_unprocessed"] == 13 and len(d["inbox"]["quarantined"]) == 1)

    check("rule-based flags can't be dismissed by hand (they clear when data is fixed)",
          client.post(f"{E}/flags/sys_overdue_dl_ybvs_2026_04", json={"status": "Dismissed"}).status_code == 400)
    res = client.post(f"{E}/flags/flg_ybvs_01", json={"status": "Resolved"})
    check("a stored flag can be resolved and is logged", res.status_code == 200 and res.json()["status"] == "Resolved"
          and not any(f["id"] == "flg_ybvs_01" for f in client.get(f"{E}/flags").json()["flags"]))


def part_b(client: TestClient) -> None:
    header("B. Live: the whole inbox, drafts, a pasted email, the brief, the Inbox assistant")
    s = client.post(f"{E}/inbox/process", json={}).json()
    print(f"  processed {s['processed']} emails: {s['needs_reply']} need a reply, {s['flags_raised']} flags, "
          f"{s['changes_drafted']} drafts, {s['total_tokens']:,} tokens, ${s['cost_usd']:.4f}")
    by_subject = {r["subject"]: r for r in s["results"]}
    for r in s["results"]:
        print(f"   {r.get('status'):<12} {str(r.get('dimension')):<10} {str(r.get('category')):<14} {r['subject'][:40]:<40} "
              f"flags={len(r.get('flags', []))} drafts={len(r.get('changes', []))}")
    check("13 remaining emails processed in parallel with no errors", s["processed"] == 13 and s["errors"] == [])
    check("nothing left unprocessed", all(m.status != "New" for m in db.list_records("inbox_messages", YBVS)))

    cater = by_subject["Final headcount deadline moved up"]
    av = by_subject["Revised AV quote: $9,800"]
    cancel = by_subject["Can't make the Seed-to-Series A panel"]
    news = by_subject["10 trends shaping conferences this fall"]
    allergy = by_subject["Severe nut allergy"]
    check("caterer email -> Food, urgent, flagged", cater["dimension"] == "Food" and cater["urgency"] in ("Critical", "High") and cater["flags"])
    check("AV quote email -> flag about the $9,800 / +$2,300 increase", any("9,800" in f or "2,300" in f for f in av["flags"]))
    check("speaker cancellation -> Program flag + a drafted task to find a replacement",
          cancel["dimension"] == "Program" and cancel["flags"] and any("task" in c.lower() for c in cancel["changes"]))
    check("newsletter -> Noise, done, no flags, no drafts", news["category"] == "Noise" and news["status"] == "Done"
          and not news["flags"] and not news["changes"])
    check("allergy email -> needs a reply, with a reply draft", allergy["status"] == "Needs Reply" and len(allergy["reply_draft"]) > 40)
    print(f"     reply to Leila: {allergy['reply_draft'][:220]}...")

    inbox = client.get(f"{E}/inbox").json()
    cater_msg = next(m for m in inbox["messages"] if m["subject"] == "Final headcount deadline moved up")
    dl_drafts = [c for c in cater_msg["changes"] if c["kind"] == "update_record" and c["payload"]["table"] == "deadlines"]
    print(f"  drafts attached to the caterer email: {[c['summary'][:90] for c in cater_msg['changes']]}")
    if dl_drafts:
        r = client.post(f"{E}/changes/{dl_drafts[0]['id']}/confirm").json()
        new_due = db.get_record("deadlines", dl_drafts[0]["payload"]["record_id"]).due_date
        check(f"confirming the inbox-drafted deadline change applies it (now due {new_due})", r["change"]["status"] == "Applied")
    else:
        check("caterer email produced a draft or flag the organizer can act on", bool(cater_msg["changes"] or cater_msg["flags"]))
    check("GET /inbox groups drafts and flags under their email", sum(len(m["changes"]) for m in inbox["messages"]) == s["changes_drafted"]
          and any(m["flags"] for m in inbox["messages"]))

    pasted = client.post(f"{E}/inbox/paste", json={
        "from_name": "Marcus Ellison-Grant", "from_email": "marcus@harborbridge-vc.example", "subject": "Step-free access to the stage",
        "body": "Hi team, I'll be using a wheelchair on summit day after a knee operation. Is the Zhang Auditorium stage step-free, "
                "and could someone meet me at the Whitney Ave entrance at 8:30? Thanks, Marcus"}).json()
    print(f"  pasted email -> {pasted['status']} | {pasted['dimension']} | {pasted['urgency']} | flags {pasted['flags']}")
    print(f"     reply: {pasted['reply_draft'][:200]}...")
    check("pasted email processed immediately, with a reply draft", pasted["status"] == "Needs Reply" and pasted["reply_draft"])

    b = client.get(f"{E}/brief").json()
    br = b["brief"]
    print(f"\n  DAILY BRIEF ({b['brief_date']}, {b['usage']['total_tokens']:,} tokens, ${b['usage']['cost_usd']:.4f})")
    print(f"  {br['headline']}")
    for p in br["top_priorities"]:
        print(f"   [{p['urgency']}] {p['title']}: {p['next_step']}")
    for r in br["risks"]:
        print(f"   risk: {r[:140]}")
    check("brief has a headline and 3-5 priorities, generated fresh", not b["cached"] and 3 <= len(br["top_priorities"]) <= 5 and br["headline"])
    check("the AV problem is surfaced as a priority", any("AV" in p["title"] or "AV" in p["why"] for p in br["top_priorities"]))
    check("digest ships with the brief (exact numbers for the UI)", b["digest"]["event"]["days_to_go"] == 21 and "flags" in b["digest"])
    again = client.get(f"{E}/brief").json()
    check("second request today returns the cached brief (no new model call)", again["cached"] and again["id"] == b["id"])
    fresh = client.get(f"{E}/brief?regenerate=true").json()
    check("regenerate=true writes a new one", not fresh["cached"] and fresh["id"] != b["id"])

    r = client.post(f"{E}/assistants/inbox/chat", json={"message": "Which emails still need a reply from us? Just list the senders."}).json()
    print(f"\n  [inbox] Assistant: {r['reply']}\n          tools: {r['tool_calls']}")
    needs = [m.from_name for m in db.list_records("inbox_messages", YBVS) if m.status == "Needs Reply"]
    check("Inbox assistant answers from the triaged inbox", sum(n.split()[0] in r["reply"] for n in needs) >= min(3, len(needs)))

    dash = tokenomics.get_tokenomics_dashboard_summary()["by_agent"]
    print(f"  metered: { {k: dash[k]['calls'] for k in ('InboxWatcher', 'BriefWriter', 'InboxAssistant') if k in dash} }")
    check("Watcher, BriefWriter, and Inbox assistant are all metered", {"InboxWatcher", "BriefWriter", "InboxAssistant"} <= set(dash))


if __name__ == "__main__":
    with TestClient(app) as client:
        db.reset_demo_db()
        part_a(client)
        part_b(client)
    print("\nPASS - Phase 3 verified.")
