# Evidence summary

Generated 2026-10-24T05:50:59+00:00 by `scripts/rehearse_demo.py` from `stagecraft-evidence.json`. Model: gpt-6-luna ($0.1 / $0.01 cached / $0.5 output per million tokens).

## Cost per feature (measured, all logged runs)

| Feature | Uses | AI calls | Tokens | Avg cost per use | Avg time |
|---|---:|---:|---:|---:|---:|
| Daily brief | 8 | 8 | 19,336 | $0.0005 | 3.9s |
| Inbox Watcher | 65 | 65 | 609,200 | $0.0006 | 1.9s |
| New-event setup | 2 | 2 | 25,370 | $0.0025 | 11.8s |
| Resolve an issue (3 agents) | 17 | 51 | 547,808 | $0.0028 | 24.6s |
| Tab assistants | 21 | 21 | 106,695 | $0.0003 | 2.7s |

Estimated AI bill for one event per month: **$0.50** (ESTIMATE: measured average cost per use x assumed monthly usage; usage assumed: 25 Resolve an issue (3 agents), 600 Inbox Watcher, 30 Daily brief, 300 Tab assistants, 1 New-event setup).

## Did cost cutting make it worse? (same issue, Cost-Saver OFF vs ON)

| | Full context | Cost-Saver |
|---|---:|---:|
| Tokens | 56,384 | 24,028 |
| Cost | $0.0031 | $0.0023 |
| Time | 29.4s | 23.9s |
| Fix chosen | Option A: Direct move to Zhang Auditorium (same 14:45–15:45 slot) | Option A: Move AI Agents & Enterprise SaaS to Zhang Auditorium at the same time |
| Passed the rule checker | True | True |

Cost-Saver vs full context: 57.4% fewer tokens, 26.5% lower cost, 18.5% faster (time varies run to run). Quality: No quality loss: both modes produced the same verified, conflict-free resolution.

Scale (Resolve an issue, 25 runs per organizer per month):

| Organizers | Per month | Without Cost-Saver | Saved per year |
|---:|---:|---:|---:|
| 1 | $0.06 | $0.09 | $0.26 |
| 100 | $6.50 | $8.65 | $25.84 |
| 10,000 | $649.56 | $864.88 | $2,583.92 |

## Resolve an issue (multi-agent hand-off)

Runs by outcome: {'BLOCKED_BY_SECURITY_GUARDRAIL': 7, 'SUCCESS': 17}. Successful runs: average 24.8s, median 26.0s (vs about 45 minutes by hand, an assumption).

## Safety

Attack test: all 3 attacks stopped: Attack 1: Indirect Prompt Injection & Data Exfiltration -> Layer 1; Malfunction 2: Hallucinated Room Overflow + Locked Keynote Cancellation -> Layer 2; Overspend 3: Runaway Loop / Budget Spike -> Layer 3.

Incidents logged: 39 (Layer 3: 9, Layer 2: 9, Layer 1: 21).
Limits: $0.15 and 60,000 tokens per run, 4 steps per agent.

## Planner (YBVS, during the rehearsal)

Inbox: 14 emails, by category {'Not processed': 14}; drafted changes {} from {}; assistant questions by tab {}; daily briefs 0.
