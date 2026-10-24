"""Step 3 test: deterministic operations tools + live Open-Meteo corridor monitor. No LLM calls.

Runs against a throwaway database file so it never touches the demo DB (backend/stagecraft.db).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # so `backend` imports from any cwd

_tmp_dir = tempfile.TemporaryDirectory()
os.environ["STAGECRAFT_DB_PATH"] = str(Path(_tmp_dir.name) / "test_stagecraft.db")

from backend import database as db  # noqa: E402
from backend import tools  # noqa: E402

YBVS = "evt_ybvs_2026"
TECH_AI = "evt_techai_2026"
AI_PANEL = "ses_ybvs_09"  # "AI Agents & Enterprise SaaS", Room 2400, 14:45-15:45


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def show_check(r: dict) -> None:
    m = r["metrics"]
    print(f"    -> {r['session_title']} into {r['target_room_name']} {r['new_start_time']}-{r['new_end_time']}")
    print(f"       is_valid={r['is_valid']}  score={r['score']}  seat_buffer={m['seat_buffer']}  "
          f"av_match={m['av_match_pct']}%  swap_with={r['requires_two_way_swap_with_session_id']}")
    for v in r["violations"]:
        print(f"       VIOLATION: {v}")
    for w in r["warnings"]:
        print(f"       warning:   {w}")


def test_inspection() -> None:
    header("0. Inspection tools")
    overview = tools.get_event_overview_tool(YBVS)
    print(f"  {overview['event']['name']} @ {overview['venue']['name']} "
          f"({overview['venue']['latitude']}, {overview['venue']['longitude']})")
    print(f"  counts={overview['counts']}  window={overview['schedule_window']}")
    print(f"  speakers not on site: {[s['name'] for s in overview['speakers_not_on_site']]}")
    check("overview counts 12 sessions / 16 speakers", overview["counts"]["sessions"] == 12 and overview["counts"]["speakers"] == 16)
    check("seed schedule has no active conflicts", overview["active_conflicts"] == [])

    by_name = tools.find_affected_sessions_tool(YBVS, query_keyword="whitfield")
    print(f"  keyword 'whitfield' -> {[s['title'] for s in by_name]}")
    check("keyword search finds both of Jordan Whitfield's sessions", {s["id"] for s in by_name} == {"ses_ybvs_08", "ses_ybvs_11"})
    in_window = tools.find_affected_sessions_tool(YBVS, room_id="rm_evans_2400", time_window_start="14:00", time_window_end="16:00")
    check("room + time-window search finds the AI panel", [s["id"] for s in in_window] == [AI_PANEL])
    check("session detail includes speaker tiers and room capacity",
          in_window[0]["room"]["capacity"] == 127 and in_window[0]["speakers"][0]["vip_tier"] == "Tier 2 - Featured")
    check("unknown event returns an error, not a crash", "error" in tools.get_event_overview_tool("nope"))


def test_hard_constraints() -> None:
    header("1. check_hard_constraints_tool")
    print("  a) 118-person AI panel into the 60-seat Room 2230 - Nooyi")
    r = tools.check_hard_constraints_tool(YBVS, AI_PANEL, "rm_evans_2230", "14:45", "15:45", expected_attendance_override=118)
    show_check(r)
    check("move is rejected", r["is_valid"] is False and r["score"] == 0)
    check("exact 58-seat overflow deficit reported", r["metrics"]["seat_buffer"] == -58
          and any("overflow deficit of 58 seats" in v for v in r["violations"]))
    check("missing non-portable AV (Dual Projection, Lecture Capture) caught", set(r["metrics"]["missing_av"]) == {"Dual Projection", "Lecture Capture"})

    print("  b) Same panel into Zhang Auditorium (free at 14:45)")
    r = tools.check_hard_constraints_tool(YBVS, AI_PANEL, "rm_evans_zhang", "14:45", "15:45", expected_attendance_override=118)
    show_check(r)
    check("Zhang is valid with AV substitutions", r["is_valid"] and r["metrics"]["av_match_pct"] == 100 and r["score"] > 0)

    print("  c) Panel into Room 4200 at 14:45 (occupied by Founder Pitch Showcase)")
    r = tools.check_hard_constraints_tool(YBVS, AI_PANEL, "rm_evans_4200", "14:45", "15:45")
    show_check(r)
    check("double-booking detected and swap partner identified", r["requires_two_way_swap_with_session_id"] == "ses_ybvs_10")

    print("  d) Jordan Whitfield's keynote pushed to 16:00 (Jordan also on the 16:00 LP/GP panel)")
    r = tools.check_hard_constraints_tool(YBVS, "ses_ybvs_08", "rm_evans_zhang", "16:00", "16:45")
    show_check(r)
    check("speaker double-booking detected", r["metrics"]["speaker_conflict_free"] is False)

    print("  e) Jordan's keynote at 15:00 with live ETA 14:30 (90-min buffer)")
    r = tools.check_hard_constraints_tool(YBVS, "ses_ybvs_08", "rm_evans_zhang", "15:00", "15:45",
                                          speaker_eta_overrides={"spk_ybvs_03": "14:30"})
    show_check(r)
    check("transit buffer violation (stage-ready 16:00 > 15:00)", any("stage-ready 16:00" in v for v in r["violations"]))

    print("  f) Attempt to move the locked Opening Keynote")
    r = tools.check_hard_constraints_tool(YBVS, "ses_ybvs_02", "rm_evans_2400", "09:00", "10:15")
    show_check(r)
    check("locked keynote protected", any(v.startswith("Locked keynote") for v in r["violations"]))


def test_swap_solver() -> None:
    header("2. propose_optimal_swaps_tool - Room 2400 AV failure + RSVP surge to 118")
    db.update_room_status("rm_evans_2400", "AV Degraded")
    options = tools.propose_optimal_swaps_tool(
        YBVS, AI_PANEL, reason="Room 2400 projector matrix failure + RSVP surge 95 -> 118",
        expected_attendance_override=118,
    )
    for o in options:
        print(f"\n  #{o['rank']} {o['option_type']}  score={o['score']}  seat_buffer={o['seat_buffer']}  "
              f"av_match={o['av_match_pct']}%  speaker_conflict_free={o['speaker_conflict_free']}  "
              f"catering_impact=${o['catering_impact_usd']:,.0f}")
        for mv in o["moves"]:
            print(f"     move: {mv['title']}: {mv['from_room']} {mv['from_time']} -> {mv['to_room']} {mv['to_time']}")
        print(f"     why:  {o['explanation']}")
    print()
    check("solver returns 1-3 feasible options", 1 <= len(options) <= 3)
    check("options ranked by score", [o["score"] for o in options] == sorted((o["score"] for o in options), reverse=True))
    check("no option keeps the panel in the failed Room 2400", all(o["moves"][0]["to_room_id"] != "rm_evans_2400" for o in options))
    check("every option seats all 118 with full AV and no speaker conflicts",
          all(o["seat_buffer"] >= 0 and o["av_match_pct"] == 100 and o["speaker_conflict_free"] for o in options))
    best = options[0]["moves"][0]
    check("top option is Zhang Auditorium at the original 14:45 slot", best["to_room"] == "Zhang Auditorium" and best["to_time"] == "14:45-15:45")
    start, end = best["to_time"].split("-")
    recheck = tools.check_hard_constraints_tool(YBVS, AI_PANEL, best["to_room_id"], start, end, expected_attendance_override=118)
    check("top option independently re-verified by check_hard_constraints_tool", recheck["is_valid"])

    db.reset_demo_db()

    print("\n  Two-way swap: Flight & Amtrak Domino - Jordan Whitfield lands late (ETA 14:00 + 90-min buffer)")
    print("  Afternoon Keynote (320 ppl) only fits Zhang; Jordan is also on the 16:00-17:00 LP/GP panel.")
    db.update_speaker_status("spk_ybvs_03", "Delayed")
    swaps = tools.propose_optimal_swaps_tool(
        YBVS, "ses_ybvs_08", min_start_time="15:30", reason="Keynote GP delayed ~2h on NE corridor",
        speaker_eta_overrides={"spk_ybvs_03": "14:00"},
    )
    for o in swaps:
        print(f"\n  #{o['rank']} {o['option_type']}  score={o['score']}  catering_impact=${o['catering_impact_usd']:,.0f}")
        for mv in o["moves"]:
            print(f"     move: {mv['title']}: {mv['from_room']} {mv['from_time']} -> {mv['to_room']} {mv['to_time']}")
        print(f"     why:  {o['explanation']}")
    print()
    swap = next((o for o in swaps if o["option_type"] == "Two-Way Swap"), None)
    check("solver finds a Two-Way Swap", swap is not None)
    check("swap trades the keynote with Closing Remarks (non-locked) in Zhang",
          swap["swap_partner_session_id"] == "ses_ybvs_12" and swap["moves"][0]["to_time"] == "17:00-17:45")
    check("both sides of the swap are conflict-free", swap["speaker_conflict_free"] and swap["seat_buffer"] >= 0
          and swap["partner_seat_buffer"] >= 0)
    db.reset_demo_db()


def test_catering() -> None:
    header("3. calculate_catering_sla_impact_tool - Hot Lunch 12:30, cutoff 13:00, $4,200")
    results = {}
    for shift in (15, 25, 35):
        r = tools.calculate_catering_sla_impact_tool(YBVS, shift)
        results[shift] = r
        lunch = r["slas_checked"][0]
        print(f"\n  +{shift} min: service {lunch['projected_service_time']}  slack {lunch['slack_mins']:+d}  "
              f"risk={lunch['risk']}  penalty=${r['total_penalty_usd']:,.0f}")
        print(f"     {r['summary']}")
        if r["mitigation_plan"]:
            for lever in r["mitigation_plan"]["levers"]:
                print(f"     lever: {lever['action']} (-{lever['minutes_saved']} min)")
    print()
    check("only the Hot Lunch SLA falls in 12:00-13:30", [s["meal_name"] for s in results[15]["slas_checked"]] == ["Hot Lunch"])
    check("15-min delay: SAFE, $0 penalty, no plan needed",
          results[15]["total_penalty_usd"] == 0 and results[15]["slas_checked"][0]["risk"] == "SAFE" and results[15]["mitigation_plan"] is None)
    check("25-min delay: AT RISK (5 mins slack), $0 penalty", results[25]["slas_checked"][0]["risk"] == "AT RISK"
          and results[25]["total_penalty_usd"] == 0)
    plan = results[35]["mitigation_plan"]
    check("35-min delay: BREACH, 5 mins over, $4,200 penalty",
          results[35]["total_penalty_usd"] == 4200 and results[35]["slas_checked"][0]["minutes_over_cutoff"] == 5)
    check("35-min plan recovers >= 15 mins and avoids the $4,200", plan["total_minutes_saved"] >= 15 and plan["penalty_avoided_usd"] == 4200)


def test_weather() -> None:
    header("4. check_live_venue_weather_and_transit_tool - live Open-Meteo")
    for event_id in (YBVS, TECH_AI):
        r = tools.check_live_venue_weather_and_transit_tool(event_id)
        print(f"\n  {r['venue']['name']} ({r['venue']['city']})  source={r['source']}")
        print("  " + json.dumps(r["conditions"], indent=2).replace("\n", "\n  "))
        print(f"  corridor: {r['corridor_nodes']}")
        print(f"  RISK: {r['transit_delay_risk']} ({r['risk_points']} pts)  factors: {r['risk_factors']}")
        print(f"  advisory: {r['advisory']}")
        check(f"{r['venue']['city']}: valid risk level and source",
              r["transit_delay_risk"] in {"LOW", "MODERATE", "HIGH"} and r["source"] in {"open-meteo-live", "fallback_cache"})
        if r["source"] != "open-meteo-live":
            print(f"  NOTE: live call failed ({r.get('fallback_reason')}); used fallback cache.")

    print("\n  Offline fallback (STAGECRAFT_OFFLINE=1 simulates a grading machine with no internet)")
    os.environ["STAGECRAFT_OFFLINE"] = "1"
    try:
        r = tools.check_live_venue_weather_and_transit_tool(YBVS)
    finally:
        del os.environ["STAGECRAFT_OFFLINE"]
    print(f"  source={r['source']}  risk={r['transit_delay_risk']}  reason={r['fallback_reason']}")
    check("offline mode returns fallback_cache without crashing", r["source"] == "fallback_cache" and r["transit_delay_risk"] in {"LOW", "MODERATE", "HIGH"})


if __name__ == "__main__":
    db.reset_demo_db()
    test_inspection()
    test_hard_constraints()
    test_swap_solver()
    test_catering()
    test_weather()
    print("\nPASS - Step 3 verified.")
