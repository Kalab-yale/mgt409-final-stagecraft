"""Shared plumbing for the crisis-pipeline agents: budget-guarded runs (Layer 3) + token metering.

Every agent in one crisis run shares that run's token budget from Settings, so a runaway Triage
leaves less room for the Solver - exactly like a real per-incident spending cap.
"""

from __future__ import annotations

import re
import time
from contextvars import ContextVar
from typing import Any, Callable

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded

from backend import database as db
from backend import security, tokenomics
from backend.config import get_thread_model

# When True (set by the A/B benchmark), every model call bypasses the Portkey response cache so token counts and
# latency are freshly measured instead of replayed from an earlier identical request.
FRESH_RESPONSES: ContextVar[bool] = ContextVar("fresh_responses", default=False)
PORTKEY_FORCE_REFRESH = {"x-portkey-cache-force-refresh": "true"}


class BudgetExceeded(RuntimeError):
    """Layer 3 tripped: the run hit its token / iteration limit."""

    def __init__(self, message: str, incident_id: str | None = None):
        super().__init__(message)
        self.incident_id = incident_id


class CrisisInputBlocked(RuntimeError):
    """Layer 1 blocked the crisis text before any agent saw it."""

    def __init__(self, screen: dict):
        super().__init__(screen.get("alert", "Blocked by Layer 1"))
        self.screen = screen


def run_tokens_so_far(run_id: str) -> int:
    m = tokenomics.get_workflow_run_metric(run_id)
    return m.total_input_tokens + m.total_output_tokens


def guarded_run(
    agent: Agent,
    prompt: str,
    *,
    deps: Any,
    run_id: str,
    event_id: str,
    agent_name: str,
    cost_saver: bool,
    context_tokens_pruned: int = 0,
):
    """Run one agent under the run's remaining budget, then meter it. Returns (result, AgentCallMetric)."""
    cfg = security.get_guardrail_config()
    used = run_tokens_so_far(run_id)
    remaining = int(cfg.max_tokens_per_run * security.BUDGET_MULTIPLIER.get()) - used
    if remaining <= 0:
        guard = security.check_budget_and_loop_guard(event_id, used, tokenomics.get_workflow_run_metric(run_id).total_cost_usd, 0)
        raise BudgetExceeded(f"{agent_name} not started: run already used {used:,} of {cfg.max_tokens_per_run:,} tokens.",
                             guard["incident_id"])
    # One model request per tool round, plus the final answer.
    limits = UsageLimits(request_limit=cfg.max_agent_loop_iterations + 1, total_tokens_limit=remaining)
    started = time.perf_counter()
    try:
        fresh = {"extra_headers": PORTKEY_FORCE_REFRESH} if FRESH_RESPONSES.get() else None  # merged with the agent's settings
        result = agent.run_sync(prompt, deps=deps, usage_limits=limits, model_settings=fresh, model=get_thread_model())
    except UsageLimitExceeded as exc:
        incident = security.log_incident(
            event_id, security.L3, "HIGH", prompt[:500],
            f"{agent_name} exceeded the run budget ({remaining:,} tokens / {cfg.max_agent_loop_iterations + 1} requests left): {exc}",
            "Agent run halted mid-loop; partial plan discarded; organizer notified",
        )
        raise BudgetExceeded(f"{agent_name} halted by the budget circuit breaker: {exc}", incident.incident_id) from exc
    latency_ms = (time.perf_counter() - started) * 1000
    metric = tokenomics.record_agent_usage(run_id, agent_name, result, latency_ms, cost_saver_enabled=cost_saver,
                                           context_tokens_pruned=context_tokens_pruned)
    return result, metric


def id_humanizer(event_id: str) -> Callable[[str], str]:
    """Returns a function that swaps internal ids (ses_..., rm_..., spk_...) in display text for real names."""
    state = db.get_event_full_state(event_id)
    names = {s.id: f"'{s.title}'" for s in state.sessions} | {r.id: r.name for r in state.rooms} | {sp.id: sp.name for sp in state.speakers}
    if not names:
        return lambda text: text
    pattern = re.compile("|".join(re.escape(k) for k in sorted(names, key=len, reverse=True)))
    return lambda text: pattern.sub(lambda m: names[m.group(0)], text) if text else text

