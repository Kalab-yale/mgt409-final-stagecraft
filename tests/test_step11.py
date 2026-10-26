"""Step 11 test: Settings / Tokenomics / Guardrails admin API via TestClient.

Runs ONE live A/B benchmark (2 full pipeline runs on gpt-6-luna, well under a cent); the second benchmark call
reuses that telemetry. Uses a throwaway database so the demo DB is never touched.
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

from backend.app import app  # noqa: E402
from _auth import login  # noqa: E402

YBVS = "evt_ybvs_2026"


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def money(x: float) -> str:
    return f"${x:,.2f}" if x >= 1 else f"${x:.5f}"


def run(client: TestClient) -> None:
    header("1. GET /api/admin/tokenomics (fresh workspace)")
    t = client.get("/api/admin/tokenomics").json()
    tiers = [x["users"] for x in t["summary"]["scale"]]
    print(f"  cost_saver_enabled={t['cost_saver_enabled']}  pricing={t['summary']['pricing_per_1m_tokens']}  tiers={tiers}")
    check("tokenomics payload with pricing and 1 / 100 / 10,000 user tiers", tiers == [1, 100, 10_000] and "per_run" in t["summary"])

    header("2. Cost-Saver toggle + live A/B benchmark")
    off = client.post("/api/admin/tokenomics/toggle-cost-saver", json={}).json()
    on = client.post("/api/admin/tokenomics/toggle-cost-saver", json={"enabled": True}).json()
    check(f"toggle flips ({off['cost_saver_enabled']}) then sets explicitly ({on['cost_saver_enabled']})",
          off["cost_saver_enabled"] is False and on["cost_saver_enabled"] is True)
    check("/api/events reports the saved mode", client.get("/api/events").json()["cost_saver_mode"] is True)

    b = client.post("/api/admin/tokenomics/benchmark", json={"event_id": YBVS, "use_cached": True}).json()
    a, o = b["unoptimized"], b["optimized"]
    print(f"\n  A/B on '{b['scenario']}' ({b['source']})")
    print(f"  {'':<22}{'COST-SAVER OFF':>16}{'COST-SAVER ON':>16}")
    for label, k, f in (("input tokens", "input_tokens", "{:,}"), ("cached input", "cached_input_tokens", "{:,}"),
                        ("output tokens", "output_tokens", "{:,}"), ("total tokens", "total_tokens", "{:,}"),
                        ("cost per run", "cost_usd", "${:.6f}"), ("latency (s)", "latency_ms", None)):
        x, y = a[k], o[k]
        fx, fy = (f"{x / 1000:.1f}", f"{y / 1000:.1f}") if f is None else (f.format(x), f.format(y))
        print(f"  {label:<22}{fx:>16}{fy:>16}")
    print(f"  {'status':<22}{a['status']:>16}{o['status']:>16}")
    print(f"  {'Layer 2 verified':<22}{str(a['layer2_verified']):>16}{str(o['layer2_verified']):>16}")
    print(f"  recommended OFF: {a['recommended_option']}")
    print(f"  recommended ON : {o['recommended_option']}")
    print(f"  savings: {b['savings']}")
    print(f"  quality: {b['quality']['verdict']}")
    check("live A/B ran both modes to SUCCESS", b["source"].startswith("live A/B run") and a["status"] == o["status"] == "SUCCESS")
    print(f"  cost breakdown OFF: { {k: round(v, 6) for k, v in a['cost_breakdown_usd'].items()} }")
    print(f"  cost breakdown ON : { {k: round(v, 6) for k, v in o['cost_breakdown_usd'].items()} }")
    print(f"  baseline exceeds the {b['production_token_budget']:,}-token production budget: {b['baseline_exceeds_production_budget']}"
          f" · Cost-Saver uses {b['optimized_budget_used_pct']}% of it")
    check("Cost-Saver cuts input and total tokens", b["savings"]["total_tokens_pct"] > 0 and b["savings"]["input_tokens_pct"] > 0)
    print(f"  dollar delta per run: {b['savings']['cost_pct']}% (measured, not asserted: prompt caching already discounts repeated context)")
    check("quality parity: both plans Layer-2 verified (100% constraint satisfaction)", b["quality"]["both_layer2_verified"])
    print(f"  identical resolution in both modes: {b['quality']['identical_resolution']}")

    cached = client.post("/api/admin/tokenomics/benchmark", json={"event_id": YBVS, "use_cached": True}).json()
    check("second benchmark reuses cached telemetry (no new LLM spend)", cached["source"].startswith("cached")
          and cached["optimized"]["run_id"] == o["run_id"])

    t = client.get("/api/admin/tokenomics").json()
    s = t["summary"]
    print("\n  Scale projection (25 runs / organizer / month):")
    print(f"  {'':<26}{'UNOPTIMIZED':>14}{'COST-SAVER':>14}{'SAVED/YR':>14}")
    print(f"  {'single crisis run':<26}{money(s['per_run']['unoptimized_usd']):>14}{money(s['per_run']['optimized_usd']):>14}{'':>14}")
    for x in s["scale"]:
        print(f"  {str(x['users']) + ' organizer(s) / month':<26}{money(x['monthly_unoptimized_usd']):>14}"
              f"{money(x['monthly_optimized_usd']):>14}{money(x['annual_savings_usd']):>14}")
    agents = sorted({c["agent_name"] for c in t["agent_calls"]})
    print(f"  per-agent rows: {len(t['agent_calls'])} ({', '.join(agents)})")
    check("dashboard now has measured optimized AND unoptimized runs", s["optimized"]["runs"] >= 1 and s["unoptimized"]["runs"] >= 1
          and s["per_run"]["unoptimized_source"] == "measured Cost-Saver OFF runs")
    check("per-agent telemetry rows for Triage, Solver, Comms", {"TriageAgent", "SolverAgent", "CommsAgent"} <= set(agents)
          and all(c["input_tokens"] > 0 and c["latency_ms"] > 0 for c in t["agent_calls"]))
    check("latest_benchmark exposed for the KPI cards", t["latest_benchmark"]["id"] == cached["id"])

    header("3. Red-team self-test + GET /api/admin/security")
    r = client.post("/api/admin/security/red-team-test", json={"event_id": YBVS}).json()
    for x in r["results"]:
        print(f"  {'INTERCEPTED' if x['defense_fired'] else 'MISSED':<12} {x['test']}  [{x['severity']}]  logged={x['logged_to_audit']}")
        print(f"     {x['details'][0]}")
    check("all 3 layers fired and logged", r["all_passed"] and len(r["results"]) == 3)
    sec = client.get("/api/admin/security").json()
    print(f"  incidents by layer: {sec['counts']}")
    check("incidents visible in GET /api/admin/security for every layer", all(n >= 1 for n in sec["counts"].values())
          and {i["incident_id"] for i in sec["incidents"]} >= {x["incident_id"] for x in r["results"]})
    check("guardrail config exposed (60,000 tokens, $0.15, 4 loops)", sec["config"]["max_tokens_per_run"] == 60_000
          and sec["config"]["max_cost_per_run_usd"] == 0.15 and sec["config"]["max_agent_loop_iterations"] == 4)
    upd = client.post("/api/admin/security/update-config", json={"max_cost_per_run_usd": 0.25, "max_agent_loop_iterations": 5})
    bad = client.post("/api/admin/security/update-config", json={"max_tokens_per_run": -5})
    unknown = client.post("/api/admin/security/update-config", json={"disable_everything": True})
    check("update-config saves valid values, rejects bad ones (400)", upd.status_code == 200 and upd.json()["config"]["max_cost_per_run_usd"] == 0.25
          and bad.status_code == 400 and unknown.status_code == 400)
    client.post("/api/admin/security/update-config", json={"max_cost_per_run_usd": 0.15, "max_agent_loop_iterations": 4})

    header("4. System status, venue creator, evidence export")
    sysd = client.get("/api/admin/system").json()
    print(f"  {sysd['model']} via {sysd['provider']} · key loaded={sysd['api_key_loaded']} · env files {sysd['env_files_found']}")
    check("system status reports provider + env lookup without exposing the key",
          sysd["api_key_loaded"] and "sk-" not in str(sysd) and os.environ.get("PORTKEY_API_KEY", "x" * 12) not in str(sysd))
    v = client.post("/api/admin/venues", json={"name": "Hynes Convention Center", "city": "Boston", "state_or_country": "Massachusetts",
                                                "airport_corridor": "BOS / South Station",
                                                "rooms": [{"name": "Ballroom A", "capacity": 500, "av_specs": ["LED Video Wall"]},
                                                          {"name": "Atrium", "capacity": 400, "av_specs": ["Ambient PA"], "is_catering_hub": True}]})
    if v.status_code == 200:
        print(f"  venue created at ({v.json()['latitude']}, {v.json()['longitude']}) with {len(v.json()['rooms'])} rooms")
        check("venue creator geocodes the city and adds rooms", abs(v.json()["latitude"] - 42.36) < 0.2 and len(v.json()["rooms"]) == 2)
    else:
        print(f"  venue creator -> {v.status_code} (offline geocoding?)")

    ev = client.get("/api/admin/export-evidence")
    e = ev.json()
    cr = e["crisis_resolution"]
    print(f"  export -> {ev.status_code}, {len(ev.content):,} bytes, keys={list(e)}")
    print(f"  runs={len(e['tokenomics']['workflow_runs'])} benchmarks={len(e['benchmarks'])} incidents={e['security']['incident_counts']['total']} "
          f"resolved={cr['successful_runs']} avg={cr['avg_resolution_seconds']}s speedup≈{cr['speedup_vs_manual_x']}x vs {cr['manual_baseline_minutes_assumed']} min manual")
    check("evidence bundle complete (system, tokenomics, benchmarks, security, crisis_resolution)",
          ev.status_code == 200 and {"system", "tokenomics", "benchmarks", "security", "crisis_resolution"} <= set(e)
          and len(e["benchmarks"]) >= 2 and e["security"]["incident_counts"]["total"] >= 3 and cr["successful_runs"] >= 2)
    check("download header set", "stagecraft-evidence.json" in ev.headers.get("content-disposition", ""))
    check("no API key anywhere in the evidence", os.environ.get("PORTKEY_API_KEY", "x" * 12) not in ev.text)


if __name__ == "__main__":
    with TestClient(app) as client:
        login(client)
        run(client)
    print("\nPASS - Step 11 verified.")
