"""Tokenomics: meter every agent call, price it, prune context, and project costs at scale.

Three jobs:
1. Metering   - `record_agent_usage()` pulls real token counts out of a pydantic-ai result, prices them,
                and logs one `AgentCallMetric` row per agent call, grouped into a `WorkflowRunMetric`.
2. Cost-Saver - `COST_SAVER_MODE` switches agents between a full verbose JSON dump of the event
                (baseline) and `build_compact_context()` (pruned, cache-friendly).
3. Economics  - `get_tokenomics_dashboard_summary()` averages logged runs and projects cost per user and
                at 100 / 10,000 users.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from backend import database as db
from backend.config import MODEL_NAME
from backend.models import Session, minutes

# ---- Pricing (USD per 1M tokens) -------------------------------------------------------------
# gpt-6-luna list prices from the course's Lecture 11 config (they match the cost pydantic-ai reports
# for our Portkey calls). Cached input is billed at 10% of the input rate, as on OpenAI.
PRICING: dict[str, dict[str, float]] = {
    "gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "output": 0.50},
    "gpt-6-astra": {"input": 10.00, "cached_input": 1.00, "output": 50.00},
}
DEFAULT_PRICING = PRICING["gpt-6-luna"]

# ---- Scale assumptions -----------------------------------------------------------------------
RUNS_PER_USER_PER_MONTH = 25
CRISIS_AGENTS = {"TriageAgent", "SolverAgent", "CommsAgent"}
SCALE_TIERS = (100, 10_000)
CHARS_PER_TOKEN = 4  # rough English/JSON average, used only for pre-flight context estimates

# ---- Cost-Saver toggle -----------------------------------------------------------------------
COST_SAVER_MODE: bool = os.environ.get("STAGECRAFT_COST_SAVER", "1") != "0"


def get_cost_saver_mode() -> bool:
    return COST_SAVER_MODE


def set_cost_saver_mode(enabled: bool, persist: bool = True) -> bool:
    """Flip Cost-Saver on/off for the whole app (the API/Settings UI calls this). Saved in SQLite so it survives restarts."""
    global COST_SAVER_MODE
    COST_SAVER_MODE = bool(enabled)
    if persist:
        init_token_tables()
        with db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('cost_saver_mode', ?)", ("1" if COST_SAVER_MODE else "0",))
    return COST_SAVER_MODE


def load_cost_saver_mode() -> bool:
    """Restore the saved Cost-Saver setting (called on server startup); keeps the env default if never set."""
    init_token_tables()
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'cost_saver_mode'").fetchone()
    return set_cost_saver_mode(row["value"] == "1", persist=False) if row else COST_SAVER_MODE


# ---- Schemas ---------------------------------------------------------------------------------


class AgentCallMetric(BaseModel):
    call_id: str
    run_id: str
    timestamp: str
    agent_name: str
    model_name: str
    input_tokens: int = Field(ge=0)          # all prompt tokens, cached ones included
    cached_input_tokens: int = Field(ge=0)   # subset of input_tokens served from the provider's prompt cache
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    cost_usd: float = Field(ge=0)
    cost_saver_enabled: bool
    context_tokens_pruned: int = Field(default=0, ge=0)  # est. tokens Cost-Saver removed vs the full dump


class WorkflowRunMetric(BaseModel):
    run_id: str
    event_id: str
    scenario_title: str
    started_at: str
    cost_saver_enabled: bool
    total_input_tokens: int
    total_cached_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    total_latency_ms: float
    baseline_unoptimized_cost_usd: float  # same run with no caching and the full context dump
    savings_pct: float
    agent_calls: list[AgentCallMetric]


# ---- Persistence (same SQLite file as the event data) ----------------------------------------

TOKEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, scenario_title TEXT NOT NULL,
    started_at TEXT NOT NULL, cost_saver_enabled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_call_metrics (
    call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(run_id), timestamp TEXT NOT NULL,
    agent_name TEXT NOT NULL, model_name TEXT NOT NULL, input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
    latency_ms REAL NOT NULL, cost_usd REAL NOT NULL, cost_saver_enabled INTEGER NOT NULL,
    context_tokens_pruned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calls_run ON agent_call_metrics(run_id);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def init_token_tables() -> None:
    with db.connect() as conn:
        conn.executescript(TOKEN_SCHEMA)


def reset_token_logs() -> None:
    """Delete all metering rows (demo/test helper). Event data is untouched."""
    init_token_tables()
    with db.connect() as conn:
        conn.execute("DELETE FROM agent_call_metrics")
        conn.execute("DELETE FROM workflow_runs")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def start_workflow_run(event_id: str, scenario_title: str, cost_saver_enabled: bool | None = None) -> str:
    """Open a run (one crisis / schedule request handled by the agent desk). Returns its run_id."""
    init_token_tables()
    run_id = f"run_{uuid.uuid4().hex[:10]}"
    saver = COST_SAVER_MODE if cost_saver_enabled is None else cost_saver_enabled
    with db.connect() as conn:
        conn.execute("INSERT INTO workflow_runs VALUES (?, ?, ?, ?, ?)", (run_id, event_id, scenario_title, _now(), int(saver)))
    return run_id


def set_run_event_id(run_id: str, event_id: str) -> None:
    """Re-tag a run once its event exists (onboarding starts before the event has an id)."""
    with db.connect() as conn:
        conn.execute("UPDATE workflow_runs SET event_id = ? WHERE run_id = ?", (event_id, run_id))


# ---- Pricing & usage extraction --------------------------------------------------------------


def price_tokens(input_tokens: int, cached_input_tokens: int, output_tokens: int, model_name: str = MODEL_NAME) -> float:
    """Exact dollar cost: uncached input + cached input (discounted) + output."""
    p = PRICING.get(model_name, DEFAULT_PRICING)
    uncached = max(0, input_tokens - cached_input_tokens)
    return (uncached * p["input"] + cached_input_tokens * p["cached_input"] + output_tokens * p["output"]) / 1_000_000


def extract_usage(result: Any) -> dict[str, int]:
    """Token counts from a pydantic-ai run result (or a RunUsage). Handles 1.x and 2.x field names."""
    usage = getattr(result, "usage", result)
    usage = usage() if callable(usage) else usage

    def first(*names: str) -> int:
        for n in names:
            v = getattr(usage, n, None)
            if v:
                return int(v)
        return 0

    details = getattr(usage, "details", None) or {}
    cached = first("cache_read_tokens") or int(details.get("cached_tokens", 0) or details.get("cache_read_tokens", 0) or 0)
    return {
        "input_tokens": first("input_tokens", "request_tokens"),
        "output_tokens": first("output_tokens", "response_tokens"),
        "cached_input_tokens": cached,
        "requests": first("requests"),
    }


def record_agent_usage(
    run_id: str,
    agent_name: str,
    result: Any,
    latency_ms: float,
    cost_saver_enabled: bool = False,
    model_name: str = MODEL_NAME,
    context_tokens_pruned: int = 0,
) -> AgentCallMetric:
    """Meter one agent call: extract real tokens, price them, and persist the row.

    Cached tokens are exactly what the provider reports (never assumed). With Cost-Saver on, pass
    `context_tokens_pruned` (from `compare_context_sizes`) so the run's no-optimisation baseline is auditable.
    """
    init_token_tables()
    u = extract_usage(result)
    metric = AgentCallMetric(
        call_id=f"call_{uuid.uuid4().hex[:10]}",
        run_id=run_id,
        timestamp=_now(),
        agent_name=agent_name,
        model_name=model_name,
        input_tokens=u["input_tokens"],
        cached_input_tokens=u["cached_input_tokens"],
        output_tokens=u["output_tokens"],
        total_tokens=u["input_tokens"] + u["output_tokens"],
        latency_ms=round(latency_ms, 1),
        cost_usd=price_tokens(u["input_tokens"], u["cached_input_tokens"], u["output_tokens"], model_name),
        cost_saver_enabled=cost_saver_enabled,
        context_tokens_pruned=context_tokens_pruned if cost_saver_enabled else 0,
    )
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone() is None:
            raise KeyError(f"Unknown run_id '{run_id}'. Call start_workflow_run() first.")
        row = metric.model_dump()
        conn.execute(
            f"INSERT INTO agent_call_metrics ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(int(v) if isinstance(v, bool) else v for v in row.values()),
        )
    return metric


def _baseline_cost(call: AgentCallMetric) -> float:
    """What this call would have cost with the full context dump and no prompt caching."""
    return price_tokens(call.input_tokens + call.context_tokens_pruned, 0, call.output_tokens, call.model_name)


def get_workflow_run_metric(run_id: str) -> WorkflowRunMetric:
    init_token_tables()
    with db.connect() as conn:
        run = conn.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(f"Unknown run_id '{run_id}'.")
        rows = conn.execute("SELECT * FROM agent_call_metrics WHERE run_id = ? ORDER BY timestamp", (run_id,)).fetchall()
    calls = [AgentCallMetric.model_validate(dict(r)) for r in rows]
    cost = sum(c.cost_usd for c in calls)
    baseline = sum(_baseline_cost(c) for c in calls)
    return WorkflowRunMetric(
        run_id=run_id,
        event_id=run["event_id"],
        scenario_title=run["scenario_title"],
        started_at=run["started_at"],
        cost_saver_enabled=bool(run["cost_saver_enabled"]),
        total_input_tokens=sum(c.input_tokens for c in calls),
        total_cached_tokens=sum(c.cached_input_tokens for c in calls),
        total_output_tokens=sum(c.output_tokens for c in calls),
        total_cost_usd=cost,
        total_latency_ms=round(sum(c.latency_ms for c in calls), 1),
        baseline_unoptimized_cost_usd=baseline,
        savings_pct=round(100 * (1 - cost / baseline), 1) if baseline else 0.0,
        agent_calls=calls,
    )


def list_workflow_runs(limit: int | None = None) -> list[WorkflowRunMetric]:
    init_token_tables()
    sql = "SELECT run_id FROM workflow_runs ORDER BY started_at DESC" + (f" LIMIT {int(limit)}" if limit else "")
    with db.connect() as conn:
        run_ids = [r["run_id"] for r in conn.execute(sql).fetchall()]
    return [get_workflow_run_metric(r) for r in run_ids]


# ---- Context building: full dump vs Cost-Saver compact --------------------------------------

COMPACT_HEADER = """## STAGECRAFT EVENT CONTEXT (compact v1)
Times are HH:MM 24h local. Lists are pipe-delimited rows with these columns:
ROOMS:    id | name | capacity | status | catering_hub | av_specs
SESSIONS: id | start-end | room_id | attendance | track | locked | status | speaker_ids | required_av
ROOM OCCUPANCY: room_id: busy start-end windows (covers sessions not listed in full)
SPEAKERS: id | name | tier | status | travel_origin | arrival_buffer_mins | notes
CATERING: id | meal | room_id | scheduled | hard_cutoff | penalty_usd | headcount | vendor
"""


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def _neighbourhood(sessions: list[Session], relevant_ids: set[str]) -> list[Session]:
    """Relevant sessions plus anything that shares their room, a speaker, or an overlapping time slot,
    so the agents still see every constraint the solver could trip over."""
    focus = [s for s in sessions if s.id in relevant_ids]
    keep = []
    for s in sessions:
        if s.id in relevant_ids or any(
            s.room_id == f.room_id
            or set(s.speaker_ids) & set(f.speaker_ids)
            or (minutes(s.start_time) < minutes(f.end_time) and minutes(f.start_time) < minutes(s.end_time))
            for f in focus
        ):
            keep.append(s)
    return keep


def build_compact_context(
    event_id: str,
    relevant_session_ids: list[str] | None = None,
    cost_saver: bool | None = None,
) -> str:
    """The event context an agent receives.

    cost_saver=False -> full verbose JSON dump: every room, session, speaker (with bio), and catering SLA.
    cost_saver=True  -> static header + venue/rooms first (identical across calls, so the provider can cache the
                        prefix), then only the sessions near the disruption and their speakers, without bios.
    Crisis preset text is never included: a crisis arrives as the user message, so red-team text can't hide here.
    """
    saver = COST_SAVER_MODE if cost_saver is None else cost_saver
    state = db.get_event_full_state(event_id)

    if not saver:
        full = state.model_dump(mode="json", exclude={"crisis_presets"})
        return "## STAGECRAFT EVENT CONTEXT (full)\n" + json.dumps(full, indent=2)

    ev, v = state.event, state.venue
    lines = [COMPACT_HEADER, f"EVENT: {ev.id} | {ev.name} | {ev.date} | {ev.attendee_count} attendees | {ev.status}",
             f"VENUE: {v.id} | {v.name} | {v.city}, {v.state_or_country} | corridor {v.airport_corridor}", "ROOMS:"]
    lines += [f"{r.id} | {r.name} | {r.capacity} | {r.status} | {'Y' if r.is_catering_hub else 'N'} | {', '.join(r.av_specs)}"
              for r in state.rooms]

    # ---- dynamic part (changes per crisis) goes after the cacheable prefix ----
    sessions = _neighbourhood(state.sessions, set(relevant_session_ids)) if relevant_session_ids else state.sessions
    speaker_ids = {sid for s in sessions for sid in s.speaker_ids}
    scope = f"{len(sessions)} of {len(state.sessions)} (disruption neighbourhood)" if relevant_session_ids else "all"
    lines.append(f"SESSIONS [{scope}]:")
    lines += [f"{s.id} | {s.start_time}-{s.end_time} | {s.room_id} | {s.expected_attendance} | {s.track} | "
              f"{'LOCKED' if s.is_locked_keynote else '-'} | {s.status} | {','.join(s.speaker_ids) or '-'} | "
              f"{', '.join(s.required_av)}" for s in sessions]
    if relevant_session_ids:  # pruned sessions still block rooms: keep a one-line busy map per room
        lines.append("ROOM OCCUPANCY [all sessions]:")
        lines += [f"{r.id}: " + (", ".join(f"{s.start_time}-{s.end_time}" for s in state.sessions if s.room_id == r.id) or "free")
                  for r in state.rooms]
    lines.append("SPEAKERS:")
    lines += [f"{sp.id} | {sp.name} | {sp.vip_tier} | {sp.status} | {sp.travel_origin} | {sp.arrival_buffer_mins} | "
              f"{sp.dietary_or_av_notes or '-'}" for sp in state.speakers if sp.id in speaker_ids]
    lines.append("CATERING:")
    lines += [f"{c.id} | {c.meal_name} | {c.room_id} | {c.scheduled_time} | {c.hard_cutoff_time} | {c.late_penalty_usd:.0f} | "
              f"{c.headcount} | {c.vendor_name}" for c in state.catering_slas]
    return "\n".join(lines)


def compare_context_sizes(event_id: str, relevant_session_ids: list[str] | None = None) -> dict:
    """Side-by-side size of the full dump vs the compact context (estimated tokens)."""
    full = build_compact_context(event_id, relevant_session_ids, cost_saver=False)
    compact = build_compact_context(event_id, relevant_session_ids, cost_saver=True)
    full_t, compact_t = estimate_tokens(full), estimate_tokens(compact)
    return {
        "full_chars": len(full), "compact_chars": len(compact),
        "full_tokens_est": full_t, "compact_tokens_est": compact_t,
        "tokens_pruned_est": full_t - compact_t,
        "reduction_pct": round(100 * (1 - compact_t / full_t), 1),
    }


# ---- Dashboard & scale projections ---------------------------------------------------------


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _mode_stats(runs: list[WorkflowRunMetric]) -> dict:
    return {
        "runs": len(runs),
        "avg_input_tokens": round(_avg([r.total_input_tokens for r in runs])),
        "avg_cached_tokens": round(_avg([r.total_cached_tokens for r in runs])),
        "avg_output_tokens": round(_avg([r.total_output_tokens for r in runs])),
        "avg_total_tokens": round(_avg([r.total_input_tokens + r.total_output_tokens for r in runs])),
        "avg_cost_usd": _avg([r.total_cost_usd for r in runs]),
        "avg_latency_ms": round(_avg([r.total_latency_ms for r in runs]), 1),
        "avg_agent_calls": round(_avg([len(r.agent_calls) for r in runs]), 1),
    }


def get_tokenomics_dashboard_summary(
    runs_per_user_per_month: int = RUNS_PER_USER_PER_MONTH,
    scale_tiers: tuple[int, ...] = SCALE_TIERS,
) -> dict:
    """Real averages from every logged run, plus per-user and 100 / 10,000-user projections.

    Unoptimised per-run cost uses measured Cost-Saver-OFF runs when they exist; otherwise it falls back to the
    estimated baseline of the optimised runs (full context, no caching)."""
    runs = list_workflow_runs()
    # Per-run averages and projections use real crisis runs only (Triage/Solver/Comms ran). Blocked runs (0 tokens),
    # onboarding, and tab-assistant chats are different jobs; totals and per-agent rows still include everything.
    crisis_runs = [r for r in runs if any(c.agent_name in CRISIS_AGENTS for c in r.agent_calls)]
    optimized = [r for r in crisis_runs if r.cost_saver_enabled]
    unoptimized = [r for r in crisis_runs if not r.cost_saver_enabled]
    opt, unopt = _mode_stats(optimized), _mode_stats(unoptimized)

    per_run_opt = opt["avg_cost_usd"] if optimized else _avg([r.total_cost_usd for r in unoptimized])
    if unoptimized:
        per_run_unopt, unopt_source = unopt["avg_cost_usd"], "measured Cost-Saver OFF runs"
    else:
        per_run_unopt, unopt_source = _avg([r.baseline_unoptimized_cost_usd for r in optimized]), "estimated baseline"
    savings_pct = round(100 * (1 - per_run_opt / per_run_unopt), 1) if per_run_unopt else 0.0

    per_user_opt = per_run_opt * runs_per_user_per_month
    per_user_unopt = per_run_unopt * runs_per_user_per_month
    scale = []
    for users in scale_tiers:
        m_opt, m_unopt = per_user_opt * users, per_user_unopt * users
        scale.append({
            "users": users,
            "runs_per_month": users * runs_per_user_per_month,
            "monthly_optimized_usd": m_opt,
            "monthly_unoptimized_usd": m_unopt,
            "monthly_savings_usd": m_unopt - m_opt,
            "annual_optimized_usd": m_opt * 12,
            "annual_unoptimized_usd": m_unopt * 12,
            "annual_savings_usd": (m_unopt - m_opt) * 12,
        })

    by_agent: dict[str, list[AgentCallMetric]] = {}
    for r in runs:
        for c in r.agent_calls:
            by_agent.setdefault(c.agent_name, []).append(c)

    return {
        "cost_saver_mode": COST_SAVER_MODE,
        "pricing_per_1m_tokens": PRICING.get(MODEL_NAME, DEFAULT_PRICING) | {"model": MODEL_NAME},
        "totals": {
            "runs": len(runs),
            "agent_calls": sum(len(r.agent_calls) for r in runs),
            "tokens": sum(r.total_input_tokens + r.total_output_tokens for r in runs),
            "cost_usd": sum(r.total_cost_usd for r in runs),
        },
        "optimized": opt,
        "unoptimized": unopt,
        "per_run": {
            "optimized_usd": per_run_opt,
            "unoptimized_usd": per_run_unopt,
            "unoptimized_source": unopt_source,
            "savings_pct": savings_pct,
            "avg_tokens_optimized": opt["avg_total_tokens"],
            "avg_tokens_unoptimized": unopt["avg_total_tokens"] if unoptimized else None,
        },
        "per_user_month": {
            "runs": runs_per_user_per_month,
            "optimized_usd": per_user_opt,
            "unoptimized_usd": per_user_unopt,
            "savings_usd": per_user_unopt - per_user_opt,
        },
        "scale": scale,
        "by_agent": {
            name: {"calls": len(cs), "avg_tokens": round(_avg([c.total_tokens for c in cs])),
                   "avg_cost_usd": _avg([c.cost_usd for c in cs]), "avg_latency_ms": round(_avg([c.latency_ms for c in cs]), 1)}
            for name, cs in sorted(by_agent.items())
        },
        "assumptions": [
            f"{runs_per_user_per_month} crisis/schedule runs per active organizer per month",
            f"Prices: {MODEL_NAME} per 1M tokens (input / cached input / output) from PRICING",
            f"Unoptimized per-run cost from {unopt_source}",
            f"Averages use {len(crisis_runs)} completed crisis runs (blocked and onboarding runs excluded)",
        ],
    }
