"""Cost vs. quality A/B benchmark (rubric: "Did any of your cost cutting make the app perform worse? Show evidence.").

Runs the SAME crisis through the full war-room desk twice - Cost-Saver OFF (full context dump) and ON (compact,
cache-friendly context) - and compares real token counts, latency, and dollars, plus resolution quality:
both runs must succeed, both recommended plans must pass the deterministic Layer 2 lock, and we report whether
they chose the identical room/time moves. Nothing is committed to the schedule. Results are stored for the
evidence export.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from backend import database as db
from backend import security, tokenomics
from backend.agents.common import FRESH_RESPONSES
from backend.config import MODEL_NAME
from backend.orchestrator import list_war_room_runs, run_crisis_war_room_pipeline

BENCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmarks (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, event_id TEXT NOT NULL, scenario TEXT NOT NULL, result_json TEXT NOT NULL
);
"""
DEFAULT_PRESET_CATEGORY = "AV/Room Failure"
BASELINE_BUDGET_MULTIPLIER = 3  # lets the unoptimized baseline finish so the comparison is apples-to-apples


def _side(run: dict) -> dict:
    t = run["tokenomics_summary"]
    solver = run.get("solver") or {}
    rec = solver.get("recommended_option") or {}
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "cost_saver_enabled": run["cost_saver_enabled"],
        "input_tokens": t["total_input_tokens"],
        "cached_input_tokens": t["total_cached_tokens"],
        "output_tokens": t["total_output_tokens"],
        "total_tokens": t["total_tokens"],
        "cost_usd": t["total_cost_usd"],
        "latency_ms": run["total_latency_ms"],
        "per_agent": t["per_agent"],
        "recommended_option": rec.get("option_label"),
        "optimization_score": rec.get("optimization_score"),
        "layer2_verified": bool(solver.get("deterministic_verification_passed")),
        "moves": sorted((m["session_id"], m["new_room_id"], m["new_start_time"], m["new_end_time"])
                        for m in rec.get("proposed_changes", [])),
        "cost_breakdown_usd": _cost_breakdown(t),
        "notifications": len((run.get("comms") or {}).get("speaker_and_moderator_messages", [])),
        "volunteer_tasks": len((run.get("comms") or {}).get("volunteer_action_checklist", [])),
    }


def _cost_breakdown(t: dict) -> dict:
    """Where the dollars go: uncached input vs cached input (90% off) vs output."""
    p = tokenomics.PRICING.get(MODEL_NAME, tokenomics.DEFAULT_PRICING)
    uncached = max(0, t["total_input_tokens"] - t["total_cached_tokens"])
    return {"uncached_input": uncached * p["input"] / 1e6, "cached_input": t["total_cached_tokens"] * p["cached_input"] / 1e6,
            "output": t["total_output_tokens"] * p["output"] / 1e6}


def _pct(new: float, old: float) -> float:
    return round(100 * (1 - new / old), 1) if old else 0.0


def _compare(event_id: str, scenario: str, off: dict, on: dict, source: str) -> dict:
    a, b = _side(off), _side(on)
    both_ok = a["status"] == b["status"] == "SUCCESS"
    same_plan = both_ok and a["moves"] == b["moves"]
    parity = both_ok and a["layer2_verified"] and b["layer2_verified"]
    budget = security.get_guardrail_config().max_tokens_per_run
    return {
        "event_id": event_id,
        "scenario": scenario,
        "source": source,
        "production_token_budget": budget,
        "baseline_exceeds_production_budget": a["total_tokens"] > budget,
        "optimized_budget_used_pct": round(100 * b["total_tokens"] / budget, 1),
        "unoptimized": a,
        "optimized": b,
        "savings": {
            "input_tokens_pct": _pct(b["input_tokens"], a["input_tokens"]),
            "total_tokens_pct": _pct(b["total_tokens"], a["total_tokens"]),
            "cost_pct": _pct(b["cost_usd"], a["cost_usd"]),
            "cost_usd_saved_per_run": a["cost_usd"] - b["cost_usd"],
            "latency_pct": _pct(b["latency_ms"], a["latency_ms"]),
        },
        "quality": {
            "both_succeeded": both_ok,
            "both_layer2_verified": parity,
            "identical_resolution": same_plan,
            "constraint_satisfaction": "100% preserved" if parity else "DEGRADED - see runs",
            "verdict": ("No quality loss: both modes produced the same verified, conflict-free resolution." if same_plan and parity
                        else "No constraint loss: both plans verified conflict-free, but they chose different valid moves." if parity
                        else "Quality check failed in at least one mode - inspect the runs."),
        },
    }


def _save(result: dict) -> dict:
    result = result | {"id": f"bench_{uuid.uuid4().hex[:8]}", "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with db.connect() as conn:
        conn.executescript(BENCH_SCHEMA)
        conn.execute("INSERT INTO benchmarks VALUES (?, ?, ?, ?, ?)",
                     (result["id"], result["created_at"], result["event_id"], result["scenario"], json.dumps(result)))
    return result


def list_benchmarks(limit: int = 20) -> list[dict]:
    with db.connect() as conn:
        conn.executescript(BENCH_SCHEMA)
        rows = conn.execute("SELECT result_json FROM benchmarks ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    return [json.loads(r["result_json"]) for r in rows]


def run_cost_quality_benchmark(event_id: str, crisis_text: Optional[str] = None, use_cached: bool = True) -> dict:
    """A/B the war-room desk on one crisis. With use_cached, reuse the latest SUCCESS pair of runs (one per mode)
    for the same scenario if they exist; otherwise run both modes live (~2 pipeline runs, well under a cent)."""
    if crisis_text is None:
        preset = next((c for c in db.get_crisis_presets(event_id) if c.category == DEFAULT_PRESET_CATEGORY), None)
        if preset is None:
            presets = db.get_crisis_presets(event_id)
            preset = next((c for c in presets if c.category != "Red-Team Prompt Injection"), None)
        if preset is None:
            raise ValueError("No crisis text given and this event has no usable crisis preset.")
        crisis_text = preset.prompt_text

    if use_cached:
        runs = [r for r in list_war_room_runs(event_id, limit=200) if r["status"] == "SUCCESS" and r["crisis_text"].strip() == crisis_text.strip()]
        off = next((r for r in runs if not r["cost_saver_enabled"]), None)
        on = next((r for r in runs if r["cost_saver_enabled"]), None)
        if off and on:  # note: these may include gateway-cache replays; use_cached=False forces a fresh A/B
            return _save(_compare(event_id, on["scenario_title"], off, on, source="cached telemetry from earlier live runs"))

    token = FRESH_RESPONSES.set(True)  # bypass the gateway response cache: measure, don't replay
    try:
        budget = security.BUDGET_MULTIPLIER.set(BASELINE_BUDGET_MULTIPLIER)  # baseline only: let it finish
        try:
            off = run_crisis_war_room_pipeline(event_id, crisis_text, cost_saver=False)
        finally:
            security.BUDGET_MULTIPLIER.reset(budget)
        on = run_crisis_war_room_pipeline(event_id, crisis_text, cost_saver=True)  # normal production budget
    finally:
        FRESH_RESPONSES.reset(token)
    return _save(_compare(event_id, on["scenario_title"], off, on, source="live A/B run (gateway cache bypassed)"))
