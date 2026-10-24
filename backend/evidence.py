"""Evidence bundle: every real number the final report needs, straight from SQLite. No secrets, no estimates
presented as measurements (estimates are labelled)."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone


from backend import changes, config, planning, security, tokenomics
from backend import database as db
from backend.benchmark import list_benchmarks
from backend.orchestrator import list_war_room_runs

MANUAL_BASELINE_MINUTES = 45  # assumption: an ops lead re-planning a room/time change and messaging everyone by hand
SCALE_TIERS = (1, 100, 10_000)

# Which product feature each agent belongs to (for cost by feature)
JOBS = {"TriageAgent": "Resolve an issue (3 agents)", "SolverAgent": "Resolve an issue (3 agents)", "CommsAgent": "Resolve an issue (3 agents)",
        "InboxWatcher": "Inbox Watcher", "BriefWriter": "Daily brief", "OnboardingAgent": "New-event setup"}
# Labelled assumption for the whole-app monthly estimate: one active event, one organizer
MONTHLY_USAGE_ASSUMED = {"Resolve an issue (3 agents)": 25, "Inbox Watcher": 600, "Daily brief": 30, "Tab assistants": 300, "New-event setup": 1}


def _job(agent: str) -> str:
    return JOBS.get(agent, "Tab assistants" if agent.endswith("Assistant") else "Other")


def cost_by_feature(workflow: list) -> dict:
    """Measured cost per AI call for each feature, plus a labelled estimate of one event's monthly AI bill."""
    calls: dict[str, list] = {}
    runs: dict[str, set] = {}
    for r in workflow:
        for c in r.agent_calls:
            calls.setdefault(_job(c.agent_name), []).append(c)
            runs.setdefault(_job(c.agent_name), set()).add(r.run_id)
    features = {}
    for job, cs in sorted(calls.items()):
        n_units = len(runs[job])  # one "use" = one run (a resolve run has 3 calls; everything else has 1)
        cost = sum(c.cost_usd for c in cs)
        features[job] = {"uses": n_units, "ai_calls": len(cs), "tokens": sum(c.total_tokens for c in cs),
                         "cached_input_tokens": sum(c.cached_input_tokens for c in cs), "cost_usd": cost,
                         "avg_cost_per_use_usd": cost / n_units if n_units else 0.0,
                         "avg_seconds_per_use": round(sum(c.latency_ms for c in cs) / n_units / 1000, 1) if n_units else 0.0}
    monthly = {job: {"uses_per_month": n, "cost_usd": n * features[job]["avg_cost_per_use_usd"]}
               for job, n in MONTHLY_USAGE_ASSUMED.items() if job in features}
    return {"measured": features,
            "monthly_estimate_one_event": {"label": "ESTIMATE: measured average cost per use x assumed monthly usage",
                                           "assumed_usage": MONTHLY_USAGE_ASSUMED, "by_feature": monthly,
                                           "total_usd": sum(v["cost_usd"] for v in monthly.values())}}


def workspace_summary(event_id: str) -> dict:
    """What the planner side of the app did for one event: assistants, drafted changes, inbox, flags, briefs."""
    with db.connect() as conn:
        chat = conn.execute("SELECT tab, role, COUNT(*) n FROM chat_messages WHERE event_id = ? GROUP BY tab, role", (event_id,)).fetchall()
        briefs = conn.execute("SELECT COUNT(*) FROM daily_briefs WHERE event_id = ?", (event_id,)).fetchone()[0]
        activity = conn.execute("SELECT actor, COUNT(*) n FROM activity_log WHERE event_id = ? GROUP BY actor", (event_id,)).fetchall()
    all_changes = changes.list_changes(event_id, status=None)
    inbox = db.list_records("inbox_messages", event_id)
    stored_flags = db.list_records("flags", event_id)
    return {
        "health": planning.event_health(event_id),
        "assistant_questions_by_tab": {r["tab"]: r["n"] for r in chat if r["role"] == "user"},
        "drafted_changes": {
            "total": len(all_changes),
            "by_status": dict(Counter(c["status"] for c in all_changes)),
            "by_origin": dict(Counter("email" if (c.get("origin") or "").startswith("inbox:") else "assistant" for c in all_changes)),
        },
        "inbox": {"emails": len(inbox), "by_source": dict(Counter(m.source for m in inbox)), "by_status": dict(Counter(m.status for m in inbox)),
                  "by_category": dict(Counter(m.category or "Not processed" for m in inbox))},
        "flags": {"open_now": len(planning.get_flags(event_id)), "stored_by_source": dict(Counter(f.source for f in stored_flags)),
                  "stored_by_status": dict(Counter(f.status for f in stored_flags))},
        "daily_briefs": briefs,
        "activity_by_actor": {r["actor"]: r["n"] for r in activity},
    }


def system_status() -> dict:
    """Provider / env / data-source status for the Settings console (never includes key values)."""
    try:
        backend = config.get_llm_backend()
    except RuntimeError:
        backend = "missing"
    env_files = ["./.env" if p.parent == config.REPO_ROOT else "../.env" for p in config.loaded_env_files()]
    return {
        "model": config.MODEL_NAME,
        "provider": {"portkey": "Portkey gateway (OpenAI-compatible)", "openai": "OpenAI direct", "missing": "NO API KEY FOUND"}[backend],
        "api_key_loaded": backend != "missing",
        "env_files_found": env_files,
        "env_lookup_order": ["./.env (repo root)", "../.env (grading location)"],
        "pricing_per_1m_tokens": tokenomics.PRICING.get(config.MODEL_NAME, tokenomics.DEFAULT_PRICING),
        "cost_saver_mode": tokenomics.get_cost_saver_mode(),
        "weather_source": "Open-Meteo (free, no key) with offline fallback cache",
        "database": str(config.DB_PATH.name),
    }


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*")  # also catches truncated ones ("name@gmail")


def redact_real_emails(bundle: dict) -> dict:
    """The evidence may be published: keep the fictional demo addresses (*.example), redact any real one."""
    text = json.dumps(bundle, default=str)
    text = _EMAIL.sub(lambda m: m.group(0) if m.group(0).lower().endswith(".example") else "[email redacted]", text)
    return json.loads(text)


def build_evidence_bundle() -> dict:
    return redact_real_emails(_build_evidence_bundle())


def _build_evidence_bundle() -> dict:
    runs = list_war_room_runs(limit=500)
    success = [r for r in runs if r["status"] == "SUCCESS"]
    latencies = sorted(r["total_latency_ms"] for r in success)
    avg_ms = sum(latencies) / len(latencies) if latencies else 0.0
    incidents = security.get_security_incidents(limit=1000)
    workflow = tokenomics.list_workflow_runs()

    from backend.connectors import gmail  # local import: keeps the MCP client optional for the rest of this module

    gm = gmail.status()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "how_to_read": {
            "multi_agent_handoff": "crisis_resolution.runs[].trace (gate -> Triage -> Solver -> Comms) and committed_resolutions",
            "tokenomics": "tokenomics.dashboard, tokenomics.cost_by_feature, benchmarks[0] (Cost-Saver OFF vs ON, same issue, quality check)",
            "safety": "security.incidents (3 layers), security.guardrail_config",
            "planner": "workspace (per event: assistants, drafted changes, inbox, flags, briefs)",
        },
        "system": system_status(),
        "connectors": {"gmail": {k: gm[k] for k in ("available", "signed_in", "mode", "note")}},
        "tokenomics": {
            "dashboard": tokenomics.get_tokenomics_dashboard_summary(scale_tiers=SCALE_TIERS),
            "cost_by_feature": cost_by_feature(workflow),
            "workflow_runs": [r.model_dump() for r in workflow],
        },
        "workspace": {e.id: {"name": e.name} | workspace_summary(e.id) for e in db.get_events()},
        "benchmarks": list_benchmarks(limit=50),
        "security": {
            "guardrail_config": security.get_guardrail_config().model_dump(),
            "incident_counts": {
                "by_layer": dict(Counter(i.defense_layer for i in incidents)),
                "by_severity": dict(Counter(i.severity for i in incidents)),
                "total": len(incidents),
            },
            "incidents": [i.model_dump() for i in incidents],
        },
        "crisis_resolution": {
            "runs_by_status": dict(Counter(r["status"] for r in runs)),
            "successful_runs": len(success),
            "avg_resolution_seconds": round(avg_ms / 1000, 1),
            "median_resolution_seconds": round(latencies[len(latencies) // 2] / 1000, 1) if latencies else 0.0,
            "manual_baseline_minutes_assumed": MANUAL_BASELINE_MINUTES,
            "speedup_vs_manual_x": round(MANUAL_BASELINE_MINUTES * 60_000 / avg_ms, 1) if avg_ms else None,
            "runs": [{
                "run_id": r["run_id"], "event_id": r["event_id"], "scenario": r["scenario_title"], "status": r["status"],
                "cost_saver": r["cost_saver_enabled"], "started_at": r["started_at"], "latency_ms": r["total_latency_ms"],
                "tokens": r["tokenomics_summary"].get("total_tokens", 0), "cost_usd": r["tokenomics_summary"].get("total_cost_usd", 0.0),
                "recommended": ((r.get("solver") or {}).get("recommended_option") or {}).get("option_label"),
                "layer2_verified": (r.get("solver") or {}).get("deterministic_verification_passed"),
                "trace": [{k: s[k] for k in ("step", "agent_name", "status", "latency_ms", "total_tokens", "cost_usd")}
                          for s in r["agent_handoff_trace"]],
            } for r in runs],
            "committed_resolutions": {e.id: [{k: v for k, v in h.items() if k not in ("ics", "changes_json")}
                                             for h in db.get_resolution_history(e.id)] for e in db.get_events()},
        },
    }
