# Stagecraft AI

**An agentic planner for conferences and summits** (MGT 409, AI Foundations for Managers, final project · Kalab Dagem).

**Submission files:** [`report.html`](report.html) (report) and [`slides.html`](slides.html) (slides) are single self-contained files with screenshots embedded; double-click to open. The demo script is in [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md).

Stagecraft keeps one event's whole plan in one place: program, venue, food, attendees, tasks and budget, and the inbox. AI agents watch it for you:

- They read incoming email, flag problems, and write a daily brief.
- Every tab has an assistant that can answer questions and draft changes. Nothing is saved until you click Confirm.
- When something breaks on the day (a speaker's flight is late, a projector dies, lunch runs over), a three-agent team works out what it affects, proposes two fixes that pass the venue's rules, and drafts the messages to send.

The app has three safety layers and cost controls, and logs every AI call with its tokens and cost.

## What you can do

| Screen | What it's for |
|---|---|
| **Your events** | Every event you're running: days to go, % planned, red flags, registrations, budget, unread email. |
| **Overview** | Today's AI brief (top priorities, risks, what changed), red flags, deadlines for the next 14 days, workstream progress, changes waiting for your OK, recent activity. |
| **Program / Venue / Food / Attendees / Tasks and budget** | The plan for that part of the event: schedule and speakers, rooms and vendors with live weather, catering and dietary counts, the attendee list (search, filters, waitlist), tasks and budget lines. Tick a deadline or task done directly. |
| **Inbox** | The Inbox Watcher sorts each email (needs a reply, action, FYI, noise, suspicious), pulls out the key facts, raises red flags, drafts changes and a reply. Phishing and prompt-injection emails are quarantined before any AI reads them. Paste an email, or import from Gmail. |
| **Assistant panel** (every tab) | Ask in plain English ("Which sponsors haven't paid?", "Add these 5 attendees", "Move the AI panel to Zhang"). Edits come back as change cards: **Confirm** or **Discard**. |
| **Resolve an issue** | Describe a day-of problem. You see the hand-off (safety check → Triage → Solver → Comms), two verified fixes with before → after moves, and drafted messages. Approve one and the schedule, the attendee page, and a calendar update (`.ics`) all update. |
| **AI usage and safety** | Spend and tokens per agent, the Cost-Saver switch, the cost-vs-quality comparison, projections at scale, guardrail limits, the attack test, the incident log, connections, and the evidence download. |
| **Attendee page** (`/live/<event_id>`) | A public, read-only schedule that shows "Updated" banners after a fix is approved. |

## How it works

Thirteen AI agents (seven of them tab assistants built from one template), each with a narrow job, structured output, and only the tools that job needs. They all run on `gpt-6-luna` through Portkey, using pydantic-ai.

| Agent | Job | Writes to the plan? |
|---|---|---|
| 7 **tab assistants** (Overview, Program, Venue, Food, Attendees, Tasks and budget, Inbox) | Answer from live data with read tools; draft changes with "propose" tools. Each tab can only touch its own kinds of records. | Only drafts. A person confirms, and the change is re-validated at that moment. |
| **Inbox Watcher** | Per email: category, urgency, key facts, red flags, drafted changes, reply draft. Runs 4 emails in parallel. | Flags yes (deduplicated); changes only as drafts; replies are never sent. |
| **Brief Writer** | Ranks today's priorities from an exact digest of the data (overdue items, flags, new email, recent changes). | No. Cached once per day. |
| **Triage → Solver → Comms** (Resolve an issue) | Triage finds the affected sessions, speakers, rooms, and catering. Solver proposes two fixes using an exact room/time search. Comms drafts messages, an attendee banner, and a volunteer checklist. | Only after the organizer approves, and only if the rule checker passes again. |
| **New-event setup** | Turns a sentence into rooms, speakers, a conflict-free schedule, catering, and a starter plan. | Yes, after validation. |

```
 email / pasted text / issue report
        │
        ▼
 Layer 1  injection firewall ──blocked──▶ quarantined, logged, $0
        │ clean
        ▼
 agents (read tools, draft tools) ──▶ drafted change ──▶ person: Confirm / Approve
        │                                                     │
 Layer 3  token, dollar, step limits per run          Layer 2  rule checker (capacity, double-booking,
          (stops loops and overspending)                       locked keynotes, closed rooms, real ids)
                                                              │
                                                              ▼
                                                        SQLite plan + activity log
```

How this maps to the course:

- **Multi-agent hand-off.** Triage passes a typed `DisruptionAssessment` to the Solver. The Solver passes a verified `ResolutionPlan` to Comms. Every step is traced with its tokens, cost and time (Resolve screen, and `crisis_resolution.runs[].trace` in the evidence).
- **Tokenomics.** Cost-Saver mode does two things:
  - It prunes context: agents get only the rooms, sessions and people the problem touches, not the whole event.
  - It orders prompts so the provider's prompt cache applies (cached input costs 10% of the normal price).

  The A/B benchmark runs the same issue with Cost-Saver off and on, bypassing the gateway cache, and checks that both reach the same verified fix. That answers "did cost cutting make it worse?" with evidence. Every call is metered; scale projections cover 1, 100 and 10,000 organizers.
- **Attacks, malfunction, overspending.** One layer covers each:
  - Layer 1 blocks prompt injection, data-exfiltration and hidden-text attacks before any model call.
  - Layer 2 blocks impossible or hallucinated schedule changes.
  - Layer 3 stops runaway loops and spending.

  The attack test fires one attack at each layer. Outbound drafts are scrubbed of phone numbers, card numbers and keys.

## Setup

You need **Python 3.12 or newer** (tested on 3.12 and 3.14; the `python3` that ships with macOS is 3.9, which is too old). Install takes about 30 seconds.

```bash
cd stagecraft                       # the cloned repo folder
python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

API key: the app looks for `.env` **in this folder first, then in the folder above**. If your `.env` with `PORTKEY_API_KEY` is already one level up (as in the grading setup), there is nothing to do. Otherwise:

```bash
cp .env.example .env                # then put your PORTKEY_API_KEY in it
```

- `backend/config.py` loads `./.env` first, then `../.env`. Values already set are never overwritten, and the key is never printed, logged, or sent to the browser.
- The model is `gpt-6-luna` via Portkey (the only model allowed on the course key). If only `OPENAI_API_KEY` is set, the app calls OpenAI directly.
- No large datasets: the demo data (2 events, 3 venues, about 640 fictional attendees) is generated into a local SQLite file on first run.
- The demo events are always dated 21 and 41 days after "today", so deadlines stay meaningful. Pin the date with `STAGECRAFT_TODAY=YYYY-MM-DD`.

## Run the app

```bash
python main.py              # then open http://127.0.0.1:8000   (PORT=8020 python main.py for another port)
```

Click **Yale Black Venture Summit (YBVS) 2026**, then try:
- **Inbox → Process 14 new emails**
- **Resolve an issue → Room 2400 AV Failure → Find a fix**
- **AI usage and safety** (top right)

The app works offline apart from the model calls and the weather (no CDNs; the weather falls back to a cache). The original dark console from the first build is still at `/classic`.

## Demo

[`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) is a timed 7-minute walkthrough with what to click and what to say. The short version:

1. **Your events**: open *Yale Black Venture Summit 2026*.
2. **Inbox**: *Process 14 new emails*. Show a reply draft, a drafted change, and the quarantined phishing email.
3. **Overview**: the daily brief and the red flags. Click *Ask the assistant* on a flag, then confirm a drafted change.
4. **Resolve an issue**: run *Room 2400 AV Failure + RSVP Surge*, compare the two fixes, approve one, and open the attendee page. Then run the *Red-Team Booby-Trap* preset: blocked, $0.
5. **AI usage and safety**: the cost-vs-quality comparison, the scale table, and the attack test.

Reset the demo data at any time (AI usage and security logs are kept):

```bash
python -c "from backend.database import reset_demo_db; reset_demo_db()"
```

### Gmail (optional, read-only)

Stagecraft is an MCP client of the course Gmail server (`../mcp_servers/mcp_gmail_server.py`). It only calls `search_emails` and `read_email`, and it never sends. Imported emails go through the same firewall and Inbox Watcher as the demo inbox.

To connect:

1. Put a Google OAuth *Desktop app* client at `../mcp_servers/gmail_credentials.json`.
2. Run the login command shown under *AI usage and safety → Connections* once in a terminal.
3. Use **Import from Gmail** on an event's Inbox tab.

Without this, the demo inbox is used. Override the paths with `GMAIL_MCP_SERVER` and `GMAIL_MCP_PYTHON`.

## Tests

```bash
python tests/run_all.py --offline   # 8 suites with no model calls: about 1 minute, $0
python tests/run_all.py             # all 17 suites with live agent runs: 2-10 minutes (faster when the gateway cache has seen the prompts), under $0.10
```

| Suite | Covers |
|---|---|
| `test_step1`–`11` | Config and model ping, data layer, scheduling tools, tokenomics, the 3 safety layers, new-event setup, Triage → Solver → Comms, the API, resolve run/commit, usage and safety API with the live A/B benchmark |
| `test_phase1`–`5` | Planning data and red-flag rules, tab assistants with confirm-before-save, Inbox Watcher and Daily Brief, the workspace UI, the Gmail MCP connector (against a stand-in server) and resolve approval |
| `test_final` | Submission readiness: every dependency listed, no key in any file, evidence complete and consistent, report and slides self-contained with the required sections, the report quotes the evidence, every demo page loads |

## Evidence

```bash
python scripts/rehearse_demo.py   # runs the demo story once (about 3 minutes, about $0.04), then restores the clean demo data
```

This writes [`docs/evidence/stagecraft-evidence.json`](docs/evidence/stagecraft-evidence.json), which contains every AI call, the A/B benchmark, cost by feature, the incident log, hand-off traces and the planner summary. It also writes [`docs/evidence/summary.md`](docs/evidence/summary.md) with the headline numbers. Real email addresses are redacted; the fictional `.example` ones are kept. The same JSON downloads from *AI usage and safety → Connections*. The screenshot runs are in `docs/evidence/screenshot_runs.json`.

The report and slides are written in `docs/src/` and built into the self-contained root files with:

```bash
python scripts/build_docs.py   # embeds docs/screenshots/*.jpg; --repo URL to change the GitHub link
```

## Project structure

```
.
├── main.py                       # python main.py -> server
├── backend/
│   ├── app.py                    # FastAPI: pages + JSON API
│   ├── config.py                 # .env loading, model factory, demo date
│   ├── models.py                 # Pydantic schemas (event, rooms, sessions, deadlines, tasks, attendees, budget, email, flags)
│   ├── database.py               # SQLite: tables, migrations, queries, demo reset
│   ├── seed.py, seed_planning.py # 3 venues, 2 events, ~640 attendees, budgets, vendors, 18 demo emails (all fictional)
│   ├── planning.py               # deadline status, progress, red-flag rules, event health, starter plan
│   ├── changes.py                # confirm-before-save: drafts, re-validation, activity log, chat history
│   ├── tools.py                  # constraint checker, exact room/time solver, catering math, weather risk
│   ├── tokenomics.py             # per-call metering, Cost-Saver context pruning, scale projections
│   ├── security.py               # the 3 safety layers, outbound scrubber, incident log, attack test
│   ├── orchestrator.py           # safety check -> Triage -> Solver -> Comms; approve; .ics; run history
│   ├── benchmark.py              # Cost-Saver OFF vs ON on the same issue, with a quality check
│   ├── evidence.py               # system status and the evidence bundle
│   ├── connectors/gmail.py       # MCP client of the course Gmail server (read-only)
│   └── agents/                   # assistants, inbox_watcher, brief_writer, triage, solver, comms, onboarding, common (guarded runs)
├── frontend/
│   ├── templates/                # app.html (workspace), public_live.html (attendee page), classic.html (first build)
│   └── static/ui/                # main.js, workspace.js, chat.js, resolve.js, settings.js, modals.js, core.js, icons.js, app.css
├── report.html, slides.html      # submission files (built, self-contained)
├── scripts/                      # rehearse_demo.py (demo rehearsal + evidence), build_docs.py (report/slides)
├── docs/                         # src/ (report + slides source), screenshots/, evidence/, DEMO_SCRIPT.md
└── tests/                        # run_all.py, 17 suites, fake_gmail_mcp.py (stand-in Gmail MCP server)
```

## Demo data

| Venue | City | Rooms |
|---|---|---|
| Yale SOM – Edward P. Evans Hall | New Haven, CT | 6 (including Zhang Auditorium and a catering hub) |
| Pier 27 Metro Convention Center | New York, NY | 5 |
| Skylight International Convention Center | Addis Ababa, Ethiopia | 4 |

- **Yale Black Venture Summit (YBVS) 2026**, at Evans Hall, 21 days out:
  - 12 sessions and 16 speakers
  - 17 deadlines and 6 workstreams
  - 362 attendees
  - 8 vendors and sponsors, and 12 budget lines
  - 14 emails, including a phishing/injection email
  - 4 day-of presets, including a red-team injection
- **Yale SOM Tech & AI Summit 2026**, at Pier 27, 41 days out: 6 sessions, 9 deadlines, 280 attendees, 4 emails.

All people, organisations, and emails are fictional (`.example` domains).
