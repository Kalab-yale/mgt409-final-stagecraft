// Event-day live mode: what's happening now and next in every room, check-in, catering cutoffs, urgent flags, and one
// big "Something broke" button into Resolve an issue. On the event date it follows the real clock; before that it runs a
// rehearsal you can scrub or play through, so the team can practise the day.
import { ring, SERIES, STATUS } from "./charts.js";
import { api, esc, fmtLong, fmtWhen, post, toast } from "./core.js";
import { icon } from "./icons.js";
import { startResolve } from "./workspace.js";

const toMin = (t) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));
const hhmm = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(Math.round(m % 60)).padStart(2, "0")}`;
const dur = (m) => (m >= 60 ? `${Math.floor(m / 60)} h ${Math.round(m % 60)} min` : `${Math.max(0, Math.round(m))} min`);
const L = { id: null, ev: null, plan: null, activity: [], now: 0, real: false, playing: false, timer: null, poll: null, q: "", app: null };

export async function showLive(app, crumb, eventId) {
  stopLive();
  Object.assign(L, { id: eventId, app, q: "", playing: false });
  app.innerHTML = `<div class="live"><div class="skeleton" style="width:300px;height:30px"></div></div>`;
  await load();
  if (!L.ev) return;
  crumb.innerHTML = `${icon("next")}<a href="#/event/${encodeURIComponent(eventId)}/overview" style="color:inherit">${esc(L.ev.event.name)}</a>${icon("next")}<strong>Live</strong>`;
  document.title = `Live · ${L.ev.event.name}`;
  L.real = L.plan.today === L.ev.event.date;
  const first = Math.min(...L.ev.sessions.map((s) => toMin(s.start_time)));
  L.now = L.real ? new Date().getHours() * 60 + new Date().getMinutes() : first + 15;  // rehearsal starts 15 min into the day
  draw();
  L.timer = setInterval(() => {
    if (L.real) { L.now = new Date().getHours() * 60 + new Date().getMinutes(); draw(); }
    else if (L.playing) { L.now = Math.min(L.now + 1, bounds()[1]); draw(); if (L.now >= bounds()[1]) L.playing = false; }
  }, L.real ? 30_000 : 1_000);  // rehearsal "play": one minute of event time per second
  L.poll = setInterval(async () => { await load(); draw(); }, 20_000);
  window.addEventListener("hashchange", stopLive, { once: true });
}

export function stopLive() { clearInterval(L.timer); clearInterval(L.poll); }

async function load() {
  const E = `/api/events/${encodeURIComponent(L.id)}`;
  try {
    const [ev, plan, act] = await Promise.all([api(E), api(`${E}/planning`), api(`${E}/activity?limit=8`)]);
    Object.assign(L, { ev, plan, activity: act.activity });
  } catch (err) {
    L.app.innerHTML = `<div class="page"><div class="card card-pad">Couldn't open live mode: ${esc(err.message)}</div></div>`;
  }
}

const bounds = () => [Math.min(...L.ev.sessions.map((s) => toMin(s.start_time))) - 30, Math.max(...L.ev.sessions.map((s) => toMin(s.end_time))) + 15];
const tracks = () => [...new Set(L.ev.sessions.map((s) => s.track))];
const trackColor = (t) => SERIES[tracks().indexOf(t) % SERIES.length];
const spkDot = (s) => (s === "Confirmed On-Site" ? "green" : s === "In Transit" ? "orange" : "red");

function draw() {
  const { ev, plan } = L, now = L.now;
  const sessions = [...ev.sessions].sort((a, b) => toMin(a.start_time) - toMin(b.start_time));
  const live = sessions.filter((s) => toMin(s.start_time) <= now && now < toMin(s.end_time));
  const next = sessions.filter((s) => toMin(s.start_time) > now && toMin(s.start_time) - now <= 90);
  const st = plan.attendees.by_status;
  const checked = st["Checked In"] || 0, expected = (st.Confirmed || 0) + (st.Registered || 0) + checked;
  const offsite = ev.speakers.filter((s) => s.status !== "Confirmed On-Site");
  const issues = ev.rooms.filter((r) => r.status !== "Operational");
  const meals = [...ev.catering_slas].sort((a, b) => toMin(a.scheduled_time) - toMin(b.scheduled_time));
  const nextMeal = meals.find((m) => toMin(m.hard_cutoff_time) >= now);
  const urgent = plan.flags.filter((f) => f.severity === "Critical" || f.severity === "High");
  const [lo, hi] = bounds();

  L.app.innerHTML = `
    <div class="live">
      <header class="live-bar">
        <div class="row" style="gap:14px;min-width:0">
          <span class="live-pill ${L.real ? "on" : ""}">${icon("radio")} ${L.real ? "Live" : "Rehearsal"}</span>
          <div style="min-width:0"><div class="live-title ellipsis">${esc(ev.event.name)}</div>
            <div class="tiny muted">${esc(fmtLong(ev.event.date))} · ${esc(ev.venue.name)}${L.real ? "" : " · practising the day before it happens"}</div></div>
        </div>
        <div class="live-clock num" aria-live="polite">${hhmm(now)}</div>
        <div class="row" style="gap:8px">
          <button class="btn danger-solid" id="broke">${icon("alert")} Something broke</button>
          <a class="btn sm" href="#/event/${encodeURIComponent(L.id)}/overview">Exit live mode</a>
        </div>
      </header>
      ${L.real ? "" : `
      <div class="scrub card">
        <button class="icon-btn" data-step="-15" aria-label="Back 15 minutes">${icon("back")}</button>
        <button class="btn sm" id="play">${icon(L.playing ? "pause" : "play")} ${L.playing ? "Pause" : "Play the day"}</button>
        <button class="icon-btn" data-step="15" aria-label="Forward 15 minutes">${icon("next")}</button>
        <input type="range" id="scrub" min="${lo}" max="${hi}" step="5" value="${now}" aria-label="Time of day">
        <span class="tiny muted num" style="width:92px;text-align:right">${hhmm(lo)} – ${hhmm(hi)}</span>
      </div>`}

      <div class="live-kpis">
        <div class="card kpi live-checkin">${ring(Math.round((100 * checked) / Math.max(1, expected)), { size: 74, stroke: 8, label: `${checked}`, sub: "checked in", color: SERIES[2], tipLabel: `${checked} of ${expected} expected` })}
          <div><div class="stat-label">Check-in</div><div class="small">${checked} of ${expected} expected</div></div></div>
        <div class="card kpi"><div class="stat-label">On now</div><div class="v num">${live.length}</div><div class="s">${live.length ? live.map((s) => esc(s.room.name.split(" - ")[0])).join(", ") : "between sessions"}</div></div>
        <div class="card kpi"><div class="stat-label">Speakers not on site</div><div class="v num" style="color:${offsite.length ? "var(--orange)" : "inherit"}">${offsite.length}</div>
          <div class="s">${offsite.slice(0, 2).map((s) => esc(s.name)).join(", ") || "everyone is here"}</div></div>
        <div class="card kpi"><div class="stat-label">Room problems</div><div class="v num" style="color:${issues.length ? "var(--red)" : "inherit"}">${issues.length}</div>
          <div class="s">${issues.map((r) => `${esc(r.name.split(" - ")[0])}: ${esc(r.status)}`).join(", ") || "all rooms operational"}</div></div>
        <div class="card kpi"><div class="stat-label">Next meal</div><div class="v num">${nextMeal ? esc(nextMeal.scheduled_time) : "–"}</div>
          <div class="s">${nextMeal ? `${esc(nextMeal.meal_name)} · ${toMin(nextMeal.scheduled_time) > now ? `in ${dur(toMin(nextMeal.scheduled_time) - now)}` : "serving now"}` : "all meals served"}</div></div>
      </div>

      <div class="live-grid">
        <div class="stack" style="gap:18px;min-width:0">
          <section class="card"><div class="card-head"><h3>${icon("radio")} On now</h3><span class="muted small">${live.length} session${live.length === 1 ? "" : "s"}</span></div>
            <div class="card-body now-list">${live.map((s) => nowCard(s, now)).join("") || `<p class="muted small">Nothing in session at ${hhmm(now)}. ${next[0] ? `Next: ${esc(next[0].title)} at ${esc(next[0].start_time)}.` : ""}</p>`}</div></section>
          <section class="card"><div class="card-head"><h3>${icon("clock")} Up next</h3><span class="muted small">within 90 minutes</span></div>
            <div class="card-body">${next.map((s) => nextRow(s, now)).join("") || `<p class="muted small">Nothing else starts in the next 90 minutes.</p>`}</div></section>
          <section class="card"><div class="card-head"><h3>${icon("venue")} Rooms</h3></div>
            <div class="card-body room-board">${ev.rooms.map((r) => roomTile(r, sessions, now)).join("")}</div></section>
        </div>
        <div class="stack" style="gap:18px;min-width:0">
          <section class="card"><div class="card-head"><h3>${icon("attendees")} Check-in desk</h3></div>
            <div class="card-body"><label class="sr-only" for="ci-q">Find an attendee</label>
              <input class="input" id="ci-q" placeholder="Type a name or organization" value="${esc(L.q)}" autocomplete="off">
              <div id="ci-results" class="ci-results">${checkinResults()}</div></div></section>
          <section class="card"><div class="card-head"><h3>${icon("food")} Catering</h3></div>
            <div class="card-body">${meals.map((m) => mealRow(m, now)).join("")}</div></section>
          <section class="card"><div class="card-head"><h3>${icon("flag")} Urgent</h3><span class="muted small">${urgent.length}</span></div>
            <div class="card-body">${urgent.slice(0, 5).map((f) => `<div class="list-row"><span class="sev ${esc(f.severity)}"></span><div class="grow"><div style="font-weight:500">${esc(f.title)}</div>
              <div class="tiny muted">${esc(f.detail || "")}</div></div></div>`).join("") || `<p class="muted small">${icon("check")} No urgent flags.</p>`}</div></section>
          <section class="card"><div class="card-head"><h3>${icon("list")} Just happened</h3></div>
            <div class="card-body">${L.activity.slice(0, 6).map((a) => `<div class="list-row"><div class="grow small">${esc(a.summary)}</div><span class="tiny faint" style="white-space:nowrap">${esc(fmtWhen(a.at))}</span></div>`).join("")
              || `<p class="muted small">Check-ins, fixes, and changes show up here.</p>`}</div></section>
        </div>
      </div>
    </div>`;
  bind(live, next);
}

function nowCard(s, now) {
  const a = toMin(s.start_time), b = toMin(s.end_time), pct = Math.round((100 * (now - a)) / (b - a));
  const full = s.expected_attendance >= s.room.capacity * 0.95;
  const warn = [s.room.status !== "Operational" ? `${s.room.name.split(" - ")[0]} is ${s.room.status}` : "",
                ...s.speakers.filter((p) => p.status !== "Confirmed On-Site").map((p) => `${p.name} is ${p.status.toLowerCase()}`)].filter(Boolean);
  return `<div class="now-card" style="--c:${trackColor(s.track)}">
    <div class="row-between"><span class="small" style="font-weight:600">${esc(s.room.name)}</span><span class="tiny muted num">${esc(s.start_time)}–${esc(s.end_time)} · ends in ${dur(b - now)}</span></div>
    <div class="now-title">${s.is_locked_keynote ? icon("shield") + " " : ""}${esc(s.title)}</div>
    <div class="now-bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" aria-label="Session progress"><span style="width:${pct}%"></span></div>
    <div class="row wrap" style="gap:6px;margin-top:8px">${s.speakers.map((p) => `<span class="chip"><span class="dot ${spkDot(p.status)}"></span>${esc(p.name)}</span>`).join("")}
      <span class="chip ${full ? "chip-warn" : ""}">${icon("attendees")} ${s.expected_attendance}/${s.room.capacity}</span></div>
    ${warn.length ? `<div class="now-warn">${icon("alert")} ${warn.map(esc).join(" · ")}</div>` : ""}
  </div>`;
}

function nextRow(s, now) {
  const late = s.speakers.filter((p) => p.status !== "Confirmed On-Site");
  return `<div class="list-row next-row"><div class="next-in num"><b>${dur(toMin(s.start_time) - now)}</b><span>${esc(s.start_time)}</span></div>
    <div class="grow"><div style="font-weight:500">${esc(s.title)}</div><div class="tiny muted">${esc(s.room.name)} · ${s.expected_attendance}/${s.room.capacity} seats</div>
      ${late.length || s.room.status !== "Operational" ? `<div class="tiny red-text" style="margin-top:2px">${icon("alert")} ${[...late.map((p) => `${esc(p.name)} ${esc(p.status.toLowerCase())}`), s.room.status !== "Operational" ? `room ${esc(s.room.status)}` : ""].filter(Boolean).join(" · ")}</div>` : ""}</div></div>`;
}

function roomTile(r, sessions, now) {
  const here = sessions.filter((s) => s.room.id === r.id);
  const cur = here.find((s) => toMin(s.start_time) <= now && now < toMin(s.end_time));
  const nxt = here.find((s) => toMin(s.start_time) > now);
  const tone = r.status !== "Operational" ? "bad" : cur ? "busy" : "free";
  return `<div class="room-tile ${tone}"><div class="row-between"><b class="small ellipsis">${esc(r.name.split(" - ")[0])}</b>
      <span class="pill tiny ${tone === "bad" ? "red" : tone === "busy" ? "blue" : "green"}">${tone === "bad" ? esc(r.status) : tone === "busy" ? "In use" : "Free"}</span></div>
    <div class="tiny muted ellipsis" style="margin-top:4px">${cur ? `${esc(cur.title)} · until ${esc(cur.end_time)}` : nxt ? `Next: ${esc(nxt.start_time)} ${esc(nxt.title)}` : "Nothing else today"}</div></div>`;
}

function mealRow(m, now) {
  const a = toMin(m.scheduled_time), cut = toMin(m.hard_cutoff_time);
  const state = now < a ? `in ${dur(a - now)}` : now <= cut ? `serving · cutoff in ${dur(cut - now)}` : "done";
  const tone = now < a ? "" : now <= cut ? "orange" : "green";
  return `<div class="list-row"><div class="grow"><div style="font-weight:500">${esc(m.meal_name)}</div>
    <div class="tiny muted">${esc(m.scheduled_time)}–${esc(m.hard_cutoff_time)} · ${m.headcount} people · $${Number(m.late_penalty_usd).toLocaleString()} if late</div></div>
    <span class="pill ${tone}">${esc(state)}</span></div>`;
}

function checkinResults() {
  const q = L.q.trim().toLowerCase();
  if (q.length < 2) return `<p class="tiny muted">Search the ${L.plan.attendees.total} people on the list, then check them in.</p>`;
  const rows = L.plan.attendees.rows.filter((a) => `${a.name} ${a.organization} ${a.email}`.toLowerCase().includes(q)).slice(0, 6);
  return rows.map((a) => `<div class="list-row ci-row"><div class="grow"><div style="font-weight:500">${esc(a.name)}</div><div class="tiny muted">${esc(a.organization)} · ${esc(a.attendee_type)}${a.dietary !== "None" ? ` · ${esc(a.dietary)}` : ""}</div></div>
    ${a.rsvp_status === "Checked In" ? `<span class="pill green">${icon("check")} Checked in</span>`
      : a.rsvp_status === "Cancelled" || a.rsvp_status === "Waitlist" ? `<span class="pill orange">${esc(a.rsvp_status)}</span>`
      : `<button class="btn primary sm" data-checkin="${esc(a.id)}">Check in</button>`}</div>`).join("") || `<p class="tiny muted">No one matches "${esc(L.q)}".</p>`;
}

function bind(live, next) {
  const $ = (s) => L.app.querySelector(s);
  $("#play")?.addEventListener("click", () => { L.playing = !L.playing; draw(); });
  L.app.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => { const [lo, hi] = bounds(); L.now = Math.max(lo, Math.min(hi, L.now + Number(b.dataset.step))); draw(); }));
  $("#scrub")?.addEventListener("input", (e) => { L.now = Number(e.target.value); L.playing = false; draw(); document.getElementById("scrub")?.focus(); });
  const q = $("#ci-q");
  q.addEventListener("input", () => { L.q = q.value; $("#ci-results").innerHTML = checkinResults(); bindCheckins(); });
  bindCheckins();
  $("#broke").addEventListener("click", () => brokeMenu($("#broke"), live, next));
}

function bindCheckins() {
  L.app.querySelectorAll("[data-checkin]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      await post(`/api/events/${encodeURIComponent(L.id)}/records/attendee/${b.dataset.checkin}`, { changes: { rsvp_status: "Checked In" } });
      const a = L.plan.attendees.rows.find((x) => x.id === b.dataset.checkin);
      if (a) { a.rsvp_status = "Checked In"; L.plan.attendees.by_status["Checked In"] = (L.plan.attendees.by_status["Checked In"] || 0) + 1; }
      toast(`${a?.name || "Attendee"} checked in`);
      const keep = L.q; draw(); L.q = keep;
      document.getElementById("ci-q")?.focus();
    } catch (err) { toast(err.message, "bad"); b.disabled = false; }
  }));
}

// "Something broke": pick what kind of problem, and Resolve opens with the current context already written in.
function brokeMenu(anchor, live, next) {
  document.querySelector(".broke-pop")?.remove();
  const at = hhmm(L.now);
  const ctx = [live.length ? `On now: ${live.map((s) => `${s.title} (${s.room.name}, until ${s.end_time})`).join("; ")}.` : "",
               next[0] ? `Next: ${next[0].title} at ${next[0].start_time} in ${next[0].room.name}.` : ""].filter(Boolean).join(" ");
  const opts = [
    ["user", "A speaker is late", `It's ${at}. A speaker is running late: [who, and their new arrival time]. ${ctx}`],
    ["venue", "A room or AV problem", `It's ${at}. [Room] has a problem: [projector / mic / power / closed]. ${ctx}`],
    ["food", "Catering is running late", `It's ${at}. Catering is running [N] minutes late for [meal]. ${ctx}`],
    ["clock", "A session is running over", `It's ${at}. [Session] is running [N] minutes over. ${ctx}`],
    ["sparkles", "Something else", `It's ${at}. ${ctx} What happened: `],
  ];
  const pop = document.createElement("div");
  pop.className = "menu broke-pop";
  pop.innerHTML = `<div class="menu-head"><b>What broke?</b><div class="tiny muted">The three agents will propose a fix. Nothing changes until you approve.</div></div>
    ${opts.map(([ic, label], i) => `<button data-o="${i}">${icon(ic)} ${label}</button>`).join("")}`;
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  Object.assign(pop.style, { position: "fixed", top: `${r.bottom + 8}px`, left: `${Math.min(r.left, innerWidth - 300)}px`, width: "290px" });
  const close = () => { pop.remove(); document.removeEventListener("mousedown", out); };
  const out = (e) => { if (!pop.contains(e.target) && e.target !== anchor) close(); };
  setTimeout(() => document.addEventListener("mousedown", out), 0);
  pop.querySelectorAll("[data-o]").forEach((b) => b.addEventListener("click", () => {
    const [, label, text] = opts[Number(b.dataset.o)];
    close();
    stopLive();
    startResolve(L.id, { text, label: `live mode at ${at}: ${label}` });
  }));
}
