"""Step 4 test: real token metering, Cost-Saver context pruning (A/B with live calls), and scale projections.

Makes 5 small live calls to gpt-6-luna (well under $0.01 total). Uses a throwaway database file so the
demo DB (backend/stagecraft.db) and its token logs are never touched.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # so `backend` imports from any cwd

_tmp_dir = tempfile.TemporaryDirectory()
os.environ["STAGECRAFT_DB_PATH"] = str(Path(_tmp_dir.name) / "test_stagecraft.db")

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.models.openai import OpenAIChatModelSettings  # noqa: E402

from backend import database as db  # noqa: E402
from backend import tokenomics as tk  # noqa: E402
from backend.config import MODEL_NAME, get_pydantic_ai_model  # noqa: E402

YBVS = "evt_ybvs_2026"
AI_PANEL = "ses_ybvs_09"
CRISIS = ("Room 2400's projector matrix failed 25 minutes before 'AI Agents & Enterprise SaaS' (ses_ybvs_09, 14:45-15:45), "
          "and RSVPs surged from 95 to 118.")
# Reasoning off: otherwise hidden reasoning tokens can eat the whole output budget.
SETTINGS = OpenAIChatModelSettings(max_tokens=300, openai_reasoning_effort="none")


def check(label: str, condition: bool) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    assert condition, label


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def timed_run(agent: Agent, prompt: str):
    started = time.perf_counter()
    result = agent.run_sync(prompt)
    return result, (time.perf_counter() - started) * 1000


def test_real_usage_extraction() -> None:
    header("1. record_agent_usage() on a real pydantic-ai call")
    run_id = tk.start_workflow_run(YBVS, "Metering smoke test", cost_saver_enabled=True)
    agent = Agent(get_pydantic_ai_model(), instructions="Follow the user's instruction exactly.", model_settings=SETTINGS)
    result, latency = timed_run(agent, "Reply with: STAGECRAFT_READY")
    metric = tk.record_agent_usage(run_id, "OnboardingAgent", result, latency, cost_saver_enabled=True)

    usage = result.usage() if callable(result.usage) else result.usage
    print(f"  reply: {result.output!r}")
    print(f"  pydantic-ai usage: input={usage.input_tokens} cached={usage.cache_read_tokens} output={usage.output_tokens} "
          f"reported_cost={getattr(usage, 'cost', None)}")
    print(f"  AgentCallMetric:   {metric.model_dump()}")
    check("input/output tokens match result.usage exactly",
          (metric.input_tokens, metric.output_tokens) == (usage.input_tokens, usage.output_tokens) and metric.input_tokens > 0)
    expected = tk.price_tokens(usage.input_tokens, usage.cache_read_tokens, usage.output_tokens)
    check(f"cost = tokens x {MODEL_NAME} price table (${metric.cost_usd:.8f})", abs(metric.cost_usd - expected) < 1e-12)
    reported = getattr(usage, "cost", None)
    if reported:
        check(f"matches the cost pydantic-ai reports (${float(reported):.8f})", abs(metric.cost_usd - float(reported)) < 1e-9)
    check("row persisted and rolled up into its WorkflowRunMetric", tk.get_workflow_run_metric(run_id).agent_calls[0].call_id == metric.call_id)


def test_context_sizes() -> None:
    header("2. build_compact_context(): full dump vs Cost-Saver (YBVS 2026)")
    for label, ids in (("Room 2400 crisis (relevant: ses_ybvs_09)", [AI_PANEL]), ("Whole-day context (no focus)", None)):
        c = tk.compare_context_sizes(YBVS, ids)
        print(f"  {label}")
        print(f"     full dump : {c['full_chars']:>6} chars  ~{c['full_tokens_est']:>5} tokens")
        print(f"     compact   : {c['compact_chars']:>6} chars  ~{c['compact_tokens_est']:>5} tokens")
        print(f"     reduction : {c['reduction_pct']}%  (~{c['tokens_pruned_est']} tokens pruned)")
        check(f"{label}: >= 45% smaller", c["reduction_pct"] >= 45)
    compact = tk.build_compact_context(YBVS, [AI_PANEL], cost_saver=True)
    full = tk.build_compact_context(YBVS, [AI_PANEL], cost_saver=False)
    check("compact keeps every room + an occupancy map (Zhang's free slots stay visible)",
          all(r.id in compact for r in db.get_rooms("ven_evans")) and "rm_evans_zhang: 09:00-10:15, 13:45-14:30, 17:00-17:30" in compact)
    check("compact drops speaker bios; full dump keeps them", "Leads a 140-person team" in full and "Leads a 140-person team" not in compact)
    check("neither context contains crisis-preset (red-team) text", "Ignore all scheduling constraints" not in full + compact)
    check("static header comes first (cacheable prefix)", compact.startswith(tk.COMPACT_HEADER))


def run_desk(cost_saver: bool) -> tuple[str, str]:
    """Mini two-agent desk on the same crisis, with full or compact context."""
    context = tk.build_compact_context(YBVS, [AI_PANEL], cost_saver=cost_saver)
    pruned = tk.compare_context_sizes(YBVS, [AI_PANEL])["tokens_pruned_est"] if cost_saver else 0
    run_id = tk.start_workflow_run(YBVS, "Room 2400 AV Failure + RSVP Surge", cost_saver_enabled=cost_saver)
    model = get_pydantic_ai_model()
    triage = Agent(model, name="TriageAgent", model_settings=SETTINGS,
                   instructions="You are the TriageAgent in a conference war-room. In 2 short bullets: what broke and who is affected.")
    solver = Agent(model, name="SolverAgent", model_settings=SETTINGS,
                   instructions="You are the SolverAgent. Using the event context, name the single best room and time for the "
                   "disrupted session (seats >= attendance, room Operational, AV covers required_av; laser projection covers "
                   "projection and a broadcast rig covers lecture capture; the room must be free). One sentence.")
    prompt = f"{context}\n\nCRISIS: {CRISIS}"
    for name, agent in (("TriageAgent", triage), ("SolverAgent", solver)):
        result, latency = timed_run(agent, prompt)
        tk.record_agent_usage(run_id, name, result, latency, cost_saver_enabled=cost_saver, context_tokens_pruned=pruned)
        if name == "SolverAgent":
            answer = result.output
    return run_id, answer


def test_ab_runs() -> None:
    header("3. A/B: same crisis through Triage + Solver, Cost-Saver OFF vs ON (live calls)")
    off_id, off_answer = run_desk(cost_saver=False)
    on_id, on_answer = run_desk(cost_saver=True)
    off, on = tk.get_workflow_run_metric(off_id), tk.get_workflow_run_metric(on_id)
    for label, m, answer in (("OFF (full dump)", off, off_answer), ("ON  (compact)  ", on, on_answer)):
        print(f"  Cost-Saver {label}: input={m.total_input_tokens:>5} cached={m.total_cached_tokens:>4} "
              f"output={m.total_output_tokens:>4}  cost=${m.total_cost_usd:.6f}  latency={m.total_latency_ms:,.0f} ms  "
              f"baseline=${m.baseline_unoptimized_cost_usd:.6f}  savings={m.savings_pct}%")
        print(f"     SolverAgent: {answer}")
    real_cut = 100 * (1 - on.total_input_tokens / off.total_input_tokens)
    print(f"  Measured input-token reduction (real API counts): {real_cut:.1f}%")
    check("Cost-Saver ON uses >= 45% fewer real input tokens", real_cut >= 45)
    check("Cost-Saver ON is cheaper per run", on.total_cost_usd < off.total_cost_usd)
    check("both modes reach the same answer (Zhang Auditorium)", "zhang" in off_answer.lower() and "zhang" in on_answer.lower())


def fmt(usd: float) -> str:
    return f"${usd:,.2f}" if usd >= 1 else f"${usd:.5f}"


def test_dashboard() -> None:
    header("4. get_tokenomics_dashboard_summary()")
    s = tk.get_tokenomics_dashboard_summary()
    p = s["pricing_per_1m_tokens"]
    print(f"  Pricing ({p['model']}, per 1M tokens): input ${p['input']:.2f} | cached ${p['cached_input']:.2f} | output ${p['output']:.2f}")
    print(f"  Logged: {s['totals']['runs']} runs, {s['totals']['agent_calls']} agent calls, "
          f"{s['totals']['tokens']:,} tokens, {fmt(s['totals']['cost_usd'])}")
    print("\n  Per agent:")
    for name, a in s["by_agent"].items():
        print(f"     {name:<16} calls={a['calls']}  avg_tokens={a['avg_tokens']:>5}  avg_cost={fmt(a['avg_cost_usd'])}  "
              f"avg_latency={a['avg_latency_ms']:,.0f} ms")
    pr, pu = s["per_run"], s["per_user_month"]
    rows = [("Per crisis run", pr["optimized_usd"], pr["unoptimized_usd"]),
            (f"Per user / month ({pu['runs']} runs)", pu["optimized_usd"], pu["unoptimized_usd"])]
    for tier in s["scale"]:
        rows.append((f"{tier['users']:,} users / month", tier["monthly_optimized_usd"], tier["monthly_unoptimized_usd"]))
        rows.append((f"{tier['users']:,} users / year", tier["annual_optimized_usd"], tier["annual_unoptimized_usd"]))
    print(f"\n  {'':<30} {'Cost-Saver ON':>15} {'Cost-Saver OFF':>15} {'Savings':>13}")
    print("  " + "-" * 75)
    for label, on, off in rows:
        print(f"  {label:<30} {fmt(on):>15} {fmt(off):>15} {fmt(off - on):>13}")
    print(f"\n  Avg tokens per run: ON {pr['avg_tokens_optimized']:,} vs OFF {pr['avg_tokens_unoptimized']:,}  "
          f"-> {pr['savings_pct']}% cheaper per run ({pr['unoptimized_source']})")
    print(f"  Assumptions: {s['assumptions']}")
    big = s["scale"][-1]
    check("projections scale linearly (10,000 users = 100x 100 users)",
          abs(big["monthly_optimized_usd"] - 100 * s["scale"][0]["monthly_optimized_usd"]) < 1e-9)
    check("Cost-Saver shows positive savings at scale", big["annual_savings_usd"] > 0)

    tk.set_cost_saver_mode(False)
    check("toggle: COST_SAVER_MODE can be switched off", tk.get_cost_saver_mode() is False
          and tk.build_compact_context(YBVS).startswith("## STAGECRAFT EVENT CONTEXT (full)"))
    tk.set_cost_saver_mode(True)


if __name__ == "__main__":
    db.reset_demo_db()
    tk.reset_token_logs()
    check("Cost-Saver defaults to ON", tk.get_cost_saver_mode() is True)
    test_real_usage_extraction()
    tk.reset_token_logs()  # keep the 32-token ping out of the crisis-run averages
    test_context_sizes()
    test_ab_runs()
    test_dashboard()
    print("\nPASS - Step 4 verified.")
