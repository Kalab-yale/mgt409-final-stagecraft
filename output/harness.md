# Stagecraft AI: harness

The working reference for how Stagecraft is put together: what runs where, how a request is checked, and how to verify each part. Start with **Auth**, since every other part of the app now sits behind it.

- **Front end:** plain ES modules, no build step, in `frontend/static/ui/`. One page, `frontend/templates/app.html`, with hash routes.
- **Back end:** FastAPI (`backend/app.py`) with PydanticAI agents (`backend/agents/`).
- **Data:** SQLite, `backend/stagecraft.db`, created and seeded on first run.
- **Model:** `gpt-6-luna` through the course Portkey key, read from `.env` (this folder first, then the folder above).

```
 Browser (one page, hash routes)                          FastAPI  backend/app.py
 ┌─────────────────────────────────────┐                  ┌──────────────────────────────────────────────────────┐
 │ #/login  #/signup     (auth.js)     │ POST /api/auth/* │ auth.py: scrypt hashes, sessions, lockout            │
 │ #/  home: hero, 3-week timeline     │ ───────────────▶ │                                                      │
 │ #/event/{id}/{tab}    (workspace.js)│  cookie:         │ middleware require_login ─▶ 401 if no valid session  │
 │   overview team program venue food │  stagecraft_     │   (except /api/auth/* and /api/events/*/calendar.ics)│
 │   attendees budget inbox resolve    │  session         │                                                      │
 │ #/settings  AI usage and safety     │  (HttpOnly)      │ /api/home, /api/events/*  planning.py, team.py       │
 │ /live/{id}  public attendee page    │ ◀─────────────── │ /api/events/*/assistants  agents/assistants.py (8)   │
 └─────────────────────────────────────┘                  │ /api/events/*/crisis      orchestrator.py (3 agents) │
                                                          │ /api/admin/*              tokenomics, security       │
                                                          └──────────────────────────────────────────────────────┘
```

---

## 1. Auth: accounts, passwords, sessions

**Files:**
- `backend/auth.py`: all the logic.
- `backend/app.py`: the four endpoints and the `require_login` middleware.
- `frontend/static/ui/auth.js`: the screens and the user menu.
- `tests/test_phase6.py`: the checks.

### 1.1 What a person does

| Screen | Fields | What happens |
|---|---|---|
| **Create account** (`#/signup`) | first name, last name, email, password, confirm password | The browser checks that the names are filled in, the password is 8+ characters, and the two passwords match (a strength meter is shown). The server checks all of it again, creates the user, starts a session, and opens Home. |
| **Log in** (`#/login`) | email, password | Correct details start a session and open Home. Any mistake shows the same message: "Email or password is incorrect." |
| **User menu** (initials, top right) | — | Shows the person's name and email, plus **Log out**, which deletes the session and returns to the login screen. |
| Any other page while logged out | — | The app asks `/api/auth/me`, gets a 401, and goes to `#/login`. Any API call that returns 401 later (an expired session) does the same. |

**Test account** (always present, demo only): `test@stagecraft.com` / `password`. `auth.init_auth()` creates it on startup if it's missing. `reset_demo_db()` resets events but never touches users or sessions.

### 1.2 Endpoints

| Method and path | Body | Result |
|---|---|---|
| `POST /api/auth/signup` | `first_name, last_name, email, password, confirm_password?` | 200 `{user}` and sets the cookie. 400 `{error}` for a duplicate email, mismatched passwords, a short password, a bad email, or a missing name. |
| `POST /api/auth/login` | `email, password` | 200 `{user}` and sets the cookie. 401 for wrong details (generic message). 429 when the email is locked. |
| `POST /api/auth/logout` | — | Deletes the session row and clears the cookie. |
| `GET /api/auth/me` | — | 200 `{user}`, or 401. |

`user` is `{id, first_name, last_name, email, created_at, display_name}`. No endpoint ever returns `password_hash`.

### 1.3 How passwords are stored

| Table `users` | |
|---|---|
| `id` | `usr_` + 12 random hex characters |
| `first_name`, `last_name` | trimmed, at most 60 characters each |
| `email` | trimmed and lower-cased; `UNIQUE COLLATE NOCASE` |
| `password_hash` | `scrypt$131072$8$1$<salt b64>$<hash b64>` |
| `created_at`, `last_login_at` | UTC ISO timestamps |

- **Algorithm:** **scrypt** from Python's standard library (`hashlib.scrypt`), so there's no extra dependency.
  - It is *memory-hard*: each guess needs about 128 MB of RAM. That makes GPU and ASIC cracking expensive, which plain SHA-256 or MD5 would not.
  - Cost: **N = 2^17, r = 8, p = 1**, OWASP's recommended setting. One hash takes about 0.27 s on a laptop. A person logging in doesn't notice that, but an attacker trying millions of passwords does.
- **Salt:** a fresh random 16-byte salt for every password (`secrets.token_bytes`). The same password never produces the same hash, so precomputed rainbow tables don't help. The test checks this.
- **Format includes the cost**, so it can be raised later. Old hashes still verify and are re-hashed at the new cost on the user's next successful login (`_needs_rehash`).
- **Comparison is constant-time** (`hmac.compare_digest`), so response timing doesn't leak how many bytes matched.
- **No user enumeration:** a login for an email that doesn't exist still runs one scrypt against a dummy hash, so it takes as long as a wrong password. Both return the same message.
- **Never stored or logged:** the plain password exists only in memory during the request. It is not written to the activity log, the incident log, or any file. `test_phase6` checks that no stored hash contains the password.

What a stolen database gives an attacker: salted, memory-hard hashes. To recover even a weak password they would have to brute-force each user separately, at about a quarter of a second of CPU and 128 MB of memory per guess, per user. AI tooling doesn't change that arithmetic.

### 1.4 Sessions

| Table `auth_sessions` | |
|---|---|
| `token_hash` | **SHA-256** of the session token (the raw token is never stored) |
| `user_id` | → `users.id` (`ON DELETE CASCADE`) |
| `created_at`, `expires_at` | 7-day lifetime; expired rows are swept on each new login |

- **Token:** `secrets.token_urlsafe(32)`, 256 random bits.
- **Cookie** `stagecraft_session`:
  - `HttpOnly`: JavaScript can't read it, so a script injected into the page can't steal it.
  - `SameSite=Lax`: other websites can't make your browser POST to Stagecraft with it.
  - `Path=/`, `Max-Age` of 7 days.
  - `Secure` is set automatically when the app is served over HTTPS.
- A stolen database can't be used to log in, because it holds only hashes of tokens.
- **Logout** deletes the row on the server, so the old cookie is useless even if someone copied it.

### 1.5 What requires a login

`require_login` (middleware in `app.py`) runs before every request:

- **Every `/api/...` path needs a valid session**, otherwise it returns `401 {"detail": "Please log in."}`. The signed-in user is placed on `request.state.user`.
- **Public on purpose:**
  - `/api/auth/login|signup|logout|me`.
  - `/api/events/{id}/calendar.ics`, which attendees download from the public page.
  - Pages: `/`, the login screen, `/live/{id}` (the public attendee schedule), `/classic`, and static files.
- The signed-in person's name is now recorded in the activity log for:
  - direct edits;
  - confirming or discarding an assistant's change ("… confirmed by Test Organizer");
  - closing flags;
  - approving a Resolve-an-issue fix.

### 1.6 Brute-force and abuse protection

**Per account:**
- Every failed login inserts a row into `login_failures (email, at)`.
- With **5 failures in 15 minutes** for an email, logins for that email are refused with **429 "Too many failed attempts. Try again in 15 minutes."**, even with the right password.
- A successful login clears the counter. Other accounts are unaffected.

**Per device:** `auth.RateLimiter` keeps an in-memory sliding window per client IP:
- **20 login attempts a minute.**
- **10 sign-ups or sensitive account changes** (email, password, delete) **per 10 minutes.**
- Past the limit the server returns **429 "Too many attempts from this device. Try again in N seconds."**
- This stops someone trying thousands of different emails, and keeps the deliberately slow password hashing from being used to overload the server.

**Known limits:**
- Signup reveals whether an email already has an account.
- Someone could deliberately lock out a known email for 15 minutes; the per-device cap limits how fast.
- There's no email verification or password reset, because both need the app to send email.
- The limiter lives in one process's memory, so a multi-server deployment would need a shared store.

### 1.6b Names are screened

- First and last names may contain only letters (any language), spaces, apostrophes, hyphens, and periods (`auth.NAME_RE`). So `Robert'); DROP TABLE users;--`, `<img onerror=…>`, and `Agent47` are refused; `Anne-Marie`, `O'Neil`, `José`, and `Nguyễn` are fine.
- Names also pass the **Layer 1 injection firewall**, because names can appear in text the AI agents read (team names show up in the activity log the Daily Brief uses). "Ignore previous instructions" as a name is blocked and logged as a security incident.
- Team-member names and roles get the same rules, whether a person or an assistant enters them (`team.validate_member_fields`).
- Defense in depth: SQL is always parameterized, and the UI escapes every value, so even text that got past these rules would display as plain text.

### 1.6c Browser protections (every response, `security_headers` middleware)

| Header | What it stops |
|---|---|
| `Content-Security-Policy: default-src 'self'; script-src 'self'; … frame-ancestors 'none'` | Scripts from anywhere else, inline script injection, and the page being embedded elsewhere |
| `X-Frame-Options: DENY` | Clickjacking: the login page can't be framed by another site (older browsers) |
| `X-Content-Type-Options: nosniff` | Browsers guessing a file's type and running it as script |
| `Referrer-Policy: strict-origin-when-cross-origin` | Leaking page addresses to other sites |
| `Permissions-Policy: camera=(), microphone=(), geolocation=()` | Device access the app never needs |

- The app page has no inline scripts. Even the theme boot is a file, `theme-boot.js`, so the strict policy doesn't break anything.
- The first-build console at `/classic` loads CDN scripts, so it gets a looser policy, but it still can't be framed.
- **API docs are off** (`/docs`, `/redoc`, and `/openapi.json` return 404), so the server doesn't publish a map of every endpoint.

### 1.6d Security audit (October 2026)

A disposable copy of the app was attacked:
- 6 SQL injection strings against login.
- HTML/script and SQL in signup names, then rendered in a real browser.
- 10,000-character and wrongly typed inputs.
- A forged session cookie.
- 20 rapid logins with different emails.
- A sweep of every route without a session.

Results:
- **Held:** SQL injection, rendering, malformed input, forged cookies, and the route sweep.
- **Found and fixed (sections 1.6, 1.6b, 1.6c):** no per-device limit, missing browser headers, public API docs, and names accepting anything.
- **Pre-login data:** none is loaded. The "AV contract not signed / Panel moved / 9 emails" cards on the login page are fixed decorative text in `auth.js`.

### 1.7 Account settings (`#/account/{profile|security|appearance|danger}`, `frontend/static/ui/account.js`)

Opened from the profile menu. The menu has your name and email, Account settings, Password and sessions, AI usage and safety, a Light/Dark/System theme switch, and Log out.

| Section | What you can do | Server rule |
|---|---|---|
| **Profile** | Change your first and last name (live avatar preview); change your email | Names follow 1.6b. **Email needs your current password**, and can't be one another account uses |
| **Security** | Change password (strength meter, confirm); see every device where you're logged in (browser and OS, signed in, last active, "This device"); sign out one, or everywhere else | **Changing the password needs the current one, signs out every other device, and issues this browser a fresh session token** |
| **Appearance** | Theme: Light, Dark, or System (follows the computer); accent: Blue, Indigo, Green, or Pink | Saved on the account (`users.theme`, `users.accent`), so it follows you to other browsers; only known values are accepted |
| **Delete account** | Remove your login and every session | **Needs your current password and the word DELETE.** The demo test account can't be deleted. Events and the team roster stay, because they belong to the organizing team |

Endpoints:
- `POST /api/account/profile|email|password|preferences|delete`.
- `GET /api/account/sessions`.
- `POST /api/account/sessions/revoke-others`.
- `DELETE /api/account/sessions/{id}`.

All of them need a session, and email, password, and delete count against the "sensitive" rate limit. Each session row stores `id`, `user_agent`, and `last_seen_at` (updated at most every 5 minutes) so the device list can show where you're logged in.

### 1.8 Verify it yourself

```bash
python tests/test_phase6.py                       # 42 checks: accounts, storage, lockout, team
python tests/test_phase7.py                       # 41 checks: headers, rate limits, names, account settings, themes
```

Or by hand, with the app running:

```bash
sqlite3 backend/stagecraft.db "select email, substr(password_hash,1,32)||'…' from users;"   # hashes only
sqlite3 backend/stagecraft.db "select substr(token_hash,1,16)||'…', expires_at from auth_sessions;"
```

Browser check (done with Playwright, typing and clicking):
1. Logged out, any page redirects to `#/login`.
2. A wrong password shows the generic error.
3. The test user logs in and the cookie is `HttpOnly`.
4. Log out works.
5. Create an account with mismatched passwords: refused.
6. Matched passwords: the new account is logged in.
7. Log out, then log back in with the email typed in capitals: works.
8. The workspace opens for the new user.

---

## 2. Organizing team, PMO, and assignments

**Files:**
- Backend: `backend/team.py`, the `TeamMember` model and `Task.assignees` (`models.py`), the `team_members` table (`database.py`), and the team red flags (`planning.py`).
- Front end: `frontend/static/ui/team.js` (the Team tab) and `people.js` (avatars and the assign picker).

- **Roster:**
  - Each event has its own team members: name, email, role, team, and `is_pmo`.
  - Teams: Leadership, Program, Venue and logistics, Food, Registration, Sponsorship and budget, Marketing, Volunteers.
  - Members don't need an account.
- **PMO:** at most one per event. `team.set_pmo` clears the others in one statement, and creating or editing a member with `is_pmo` does the same.
- **Multi-person tasks:**
  - `Task.assignees` is a list of member ids.
  - People and assistants may use names: "Malik" is enough when only one Malik exists.
  - `team.normalize_task_fields` resolves the names and keeps `Task.owner` (the text shown everywhere) as "Name, Name".
  - Unknown names are refused with the list of team members.
- **Removing a person** takes them off every task and out of the owner text.
- **Workload** (`team.workload`): per person, the open, overdue, blocked, high-priority and shared tasks, the deadlines they own, and the workstreams they lead. More than 7 open tasks counts as *stretched*.
- **Red flags** (computed live):
  - "Nobody owns: …" for a high-priority open task with no one assigned.
  - "No PMO on the team".
  - "… is stretched thin".
- **Ways to assign:**
  - **Team tab:** the Assign picker per task, *New task* with several owners, *Add person*, *Change PMO*, and filters (All open, Unassigned, Shared, Mine, by person).
  - **Any tab:** the avatar stack on a task row opens the same picker.
  - **Team assistant** (8th assistant): "Who is overloaded?", "Share the shuttle task between Malik and Dana". It drafts a change card ("Owners: Malik Johnson → Malik Johnson, Dana Lee") that waits for **Confirm**.
- **Direct-edit endpoints** (validated, applied at once, logged under the signed-in person's name):
  - `POST /api/events/{id}/records/{member|task|deadline}`: create.
  - `POST …/records/{type}/{rid}`: edit.
  - `DELETE …/records/{member|task|deadline}/{rid}`: delete.
  - `POST /api/events/{id}/team/pmo`: change the PMO.

---

## 2b. My work, notifications, and who may approve money

**Files:** `backend/notify.py`, `frontend/static/ui/notifications.js` (the bell), and the My work card in `frontend/static/ui/main.js`.

- **Accounts ↔ team.**
  - An account whose email matches a team member *is* that member on that event (`notify.access_for`).
  - The demo account `test@stagecraft.com` is seeded as "Test Organizer", an event lead on both events, so the demo has real content.
  - Anyone else creates an account with the email the PMO put on the roster.
- **Roles.** Team members are *Lead* or *Member*; the PMO counts as a lead.
  - **Only leads approve money changes:** budget lines, and vendor contract values (`changes.touches_budget`).
  - A member's Confirm on such a card is refused (409), and the card shows "Needs a lead's OK".
  - A member's direct budget edit is refused (403).
  - Everything else is open to anyone signed in. Leads can make others leads on the Team tab.
- **My work** (`GET /api/me/work`, Home): across every event,
  - your open tasks (with who you share them with), sorted by due date, which you can tick off right there;
  - the deadlines you own;
  - "N changes waiting for your OK" for events you lead.

  Accounts not on any team get a short "ask your PMO to add you" note instead.
- **Notifications** (`GET /api/notifications`, `POST /api/notifications/read`): a bell in the top bar with an unread count. It refreshes every minute and on every page change.
  - **"Assigned to you: …"** goes to people newly added to a task: direct edits, the Assign picker, or confirmed assistant drafts. It says who did it, and never notifies the person who made the change or people already on the task.
  - **"Critical/High red flag: …"** goes to the event's leads when an email or an assistant raises one; Medium and Low don't notify.
  - **"N changes waiting for your OK"** is computed live for leads, so it's never stale.
  - Stored in `notifications (id, user_id, event_id, kind, title, body, link, created_at, read_at)`.
  - Clicking one opens the right tab and marks it read; you can also mark all read.

## 2b1. Team updates, status reports, meeting notes, feedback (`backend/agents/team_writers.py`, `frontend/static/ui/updates.js`)

- **The creator runs the event.** `POST /api/events/generate-ai` and `/api/events/import-csv` add the signed-in person to the new event's team as PMO and Lead (`team.add_creator`; promoted if their email is already on the roster). Before this, a new event had no team, so nobody could approve its budget changes.
- **Team updates** (`GET|POST /api/events/{id}/announcements`, Overview card).
  - Only leads/PMO post (403 otherwise). The text passes the injection firewall.
  - Stored in `announcements`; every team member with an account gets an `announcement` notification (not the author). In-app only; nothing is emailed.
  - **Draft with AI** (`POST .../announcements/draft`, leads only): the AnnouncementWriter phrases the same exact digest the Daily Brief uses, plus an optional note. The lead edits, then posts. Posts drafted with AI are labelled.
- **Status report** (`POST /api/events/{id}/status-report`, audience `chair` | `sponsors` | `som_events`). The StatusReporter returns typed sections; code assembles the email and signs it with the sender's name. For sponsors, code removes budget, inbox, flags, and pending drafts from the digest before the model sees it. Copy, or open in the mail app (`mailto:`); never sent by Stagecraft.
- **Meeting notes to tasks** (`POST /api/events/{id}/team/notes-to-tasks`, Team tab and Overview). Notes (≤5,000 chars) pass the firewall, then NotesToTasks extracts up to 12 actions given today's date, the roster, and existing open tasks. Code resolves each owner against the roster (unknown names are reported, never invented), skips duplicates, and stores each as a normal change card (`origin = meeting_notes`) to Confirm or Discard.
- **Owner suggestions** (`team.suggest_owners`, in `planning.team.suggestions`). Plain code: for each open unassigned task, the least-loaded, non-stretched member of the area's team (Program→Program, Venue→Venue and logistics, Food→Food, Attendees→Registration/Marketing, Budget→Sponsorship and budget, General→Leadership/Volunteers), else the least-loaded non-PMO. Suggestions spread across people. Shown as a one-click "Assign Tomás" button and at the top of the Assign picker.
- **Post-event feedback** (`POST /api/events/{id}/feedback/summarize`, `GET .../feedback`). One response per line (≤300). Each line is screened on its own; injected lines are dropped and counted. The FeedbackSummarizer returns sentiment counts, themes with counts, praise, complaints, quotes, and actions. Code checks that the counts add up and removes any quote that isn't verbatim in the responses. Stored in `feedback_summaries`.
- **Verify:** `python tests/test_phase10.py` (live, about 7 model calls, under $0.01).

## 2b2. Connect Gmail in the app (`backend/connectors/google_gmail.py`, `frontend/static/ui/gmail.js`)

**For organizers:** go to *Inbox → Import from Gmail*. The dialog has three states:
1. **Not set up:** the one-time Google steps, with links.
2. **Connect your Gmail:** promises (read-only; you choose; only imported emails are read by AI; yours alone), then **Continue with Google**.
3. **Connected:** From/To dates, keywords, sender → **Search** → a preview list with "looks related" badges and checkboxes → **Import N selected**.

Already-imported emails are marked and can't be ticked. *Account settings → Connected accounts* shows the inbox and has Disconnect.

**How the sign-in works:**
- **The client file.** Google requires a registered OAuth client for any app that reads Gmail. A *Desktop app* client JSON is read from `GMAIL_OAUTH_CLIENT_FILE`, `./gmail_credentials.json` (gitignored), or `../mcp_servers/gmail_credentials.json`.
- **Starting the sign-in.** `POST /api/gmail/connect` stores a **single-use state**, bound to the signed-in user and expiring in 10 minutes, with a **PKCE verifier**. It returns Google's consent URL with scope **`gmail.readonly` only** and `access_type=offline`.
- **Coming back.** Google redirects to **`http://127.0.0.1:<port>/`**: a loopback address, which Desktop clients accept, the same pattern Google's own Python library uses. `index()` spots `?state=&code=` and calls `finish_connect`, which checks that the state exists, is fresh, and belongs to this session, then exchanges the code with the PKCE verifier and reads the address from the Gmail profile.
- **Storing tokens.** Tokens are stored per user in `gmail_connections`, **encrypted with Fernet**, using a key from `STAGECRAFT_SECRET_KEY` or `backend/.secret_key` (created with 0600 permissions, gitignored). They never go to the browser. Expired access tokens refresh automatically.
- **Disconnect** deletes the row and revokes the token at Google.
- **Only on this computer.** Starting a sign-in from a non-loopback address is refused, because the app runs locally.

**Preview** (`POST /api/events/{id}/gmail/preview`):
- Dates become `after:` and `before:` (Gmail's `before` is exclusive, so a day is added), plus `from:` and keywords.
- It reads only headers and Google's snippet: **no AI, no cost**.
- **"Looks related"** is plain code (`event_signals` + `relatedness`):
  - the sender is a known vendor, sponsor, speaker, teammate, or attendee;
  - or the sender shares a domain with a vendor (not shared domains like gmail.com or yale.edu);
  - or the subject or snippet mentions the event's name, acronym, venue, or a vendor.
- Related emails are pre-ticked.

**Import** (`POST /api/events/{id}/gmail/import`, at most 25 at a time):
- Fetches the full message and prefers the plain-text part (HTML is stripped).
- Stores it as `InboxMessage(source="gmail", external_id=<id>)`, skipping duplicates.
- Runs the Inbox Watcher on **only those emails**; each passes the injection firewall first.
- Nothing is ever sent.

Tested offline against a fake Google (`tests/test_phase9.py`, 28 checks), and in a real browser with Google's consent screen simulated.

The older path, the course Gmail MCP server (`backend/connectors/gmail.py`, `/api/events/{id}/gmail/sync`), still works for developers.

## 2c. Event-day live mode (`#/event/{id}/live`, `frontend/static/ui/live.js`)

Opened from the **Live mode** button in an event's header.

**The clock:**
- On the event date it follows the real clock and refreshes the data every 20 seconds.
- Before that it runs a **rehearsal**: drag the time slider, step 15 minutes, or *Play the day* at one minute per second. The team can practise the day and see what the screen will show at, say, 14:40.

**Panels:**
- **On now:** each running session with a progress bar, time left, seats used, and speaker status. Warnings appear if a speaker isn't on site or the room isn't operational.
- **Up next:** what starts within 90 minutes, with countdowns and the same warnings.
- **Rooms:** free, in use, or a problem, plus what's next in each.
- **Check-in desk:** search the attendee list and check people in. This is a direct, logged edit to `rsvp_status = Checked In`, with a live counter.
- **Catering:** each meal's service window, cutoff, headcount, and late penalty; *in N min*, *serving*, or *done*.
- **Urgent** red flags, and **Just happened** (the activity log).

**Something broke:** pick the kind of problem (late speaker, room or AV, catering, overrun, other). Resolve an issue opens pre-filled with the time and what is on and up next; you fill in the specifics, and the three agents take it from there.

## 3. Design notes (visuals)

- **Home:**
  - A greeting hero ("Good afternoon, Test.") with cross-event chips: urgent flags, overdue items, due this week, unread emails.
  - A three-week timeline with one dot per deadline, colored per event, event days marked, and overdue items in the margin.
  - Event cards with generated stage-lights cover art (city and days to go), a progress ring, budget and registration figures, the next three deadlines, and team avatars with the PMO.
  - A "Plan a new event" tile.
- **Overview:** *Road to event day*, a timeline of each workstream from its first task to its target date, filled by progress and colored by status, with lines for today and event day.
- **Program:** a **Grid** view, with rooms across and time down in 5-minute rows, sessions colored by track, locked keynotes marked, and seats used. The **List** view is still available.
- **Attendees:** a seats ring, an RSVP status bar, and bars for attendee type and dietary needs.
- **Budget:** planned vs spent by category, with over-budget categories called out.
- **Team:** a PMO spotlight card, a workload bar chart (overdue, blocked, on track), and member cards.
- **Dark mode and accents:**
  - Every colour in `app.css` is a theme variable (surfaces, borders, grid lines, tints).
  - `:root[data-theme="dark"]` swaps them, and `data-accent` picks one of four accent colours.
  - `theme-boot.js` runs in `<head>` before the CSS paints, so dark mode never flashes white.
  - `theme.js` applies a change instantly and re-renders the screen, because charts bake their colours into the page.
  - Charts switch to the palette's dark steps. Both sets passed the colour-vision check on their surface (light `#ffffff`, dark `#1c1c1e`).
- **Chart rules** (`charts.js`):
  - Categorical colors come in a fixed order, validated for color-vision deficiency on adjacent pairs.
  - Status colors (critical, serious, good) are used only for states.
  - Bars have 2px gaps and a 4px rounded data end.
  - There is a legend for two or more series, and every value is also written as text.
  - One shared tooltip appears on hover and on keyboard focus.

---

## 4. Specs and how to run

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt   # Python 3.12+
python main.py                        # http://127.0.0.1:8000, log in as test@stagecraft.com / password
python tests/run_all.py --offline     # 12 suites, no model calls
python tests/run_all.py               # all 22 suites
```
