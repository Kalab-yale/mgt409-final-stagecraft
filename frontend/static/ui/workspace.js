// The event workspace: header, tabs, the seven tab screens, and the assistant panel beside them.
import { bindChangeActions, changeCard, mountChat, prefillChat, setApprover } from "./chat.js";
import { api, busy, daysLabel, deadlineTone, DIM_TAB, esc, fmtDate, fmtLong, fmtWhen, money, pct, post, sevTone, statusTone, TABS, toast } from "./core.js";
import { icon } from "./icons.js";
import { openGmail, reopenIfReturning } from "./gmail.js";
import { openPasteEmail } from "./modals.js";
import { avatarStack, openAssign } from "./people.js";
import { renderResolve } from "./resolve.js";
import { renderTeam } from "./team.js";
import { mountTeamUpdates, openFeedback, openMeetingNotes, openStatusReport } from "./updates.js";
import { attendeeCharts, budgetChart, roadmap, scheduleGrid } from "./visuals.js";

const ws = { id: null, tab: "overview", ev: null, plan: null, changes: [], activity: [], inbox: null, brief: null, weather: null,
             mailFilter: "Needs Reply", mailId: null, schedView: "grid", att: { q: "", status: "", type: "", dietary: "", page: 0 }, prompt: "", nav: 0,
             resolveFrom: null };  // what "Resolve with the agents" was clicked on (flag or email)
let els = {};
let mountedChat = "";  // "eventId:tab" the panel currently shows; remount only when it changes
const E = () => `/api/events/${encodeURIComponent(ws.id)}`;

export async function showWorkspace(app, crumb, eventId, tab) {
  if (!TABS.some((t) => t.id === tab) && tab !== "resolve") tab = "overview";
  const nav = ++ws.nav;  // a newer navigation wins if this one is still loading
  const fresh = ws.id !== eventId || !document.getElementById("ws");
  ws.tab = tab;
  if (fresh) {
    Object.assign(ws, { id: eventId, ev: null, plan: null, inbox: null, brief: null, weather: null, mailId: null, changes: [], activity: [] });
    app.innerHTML = `<div class="page"><div class="skeleton" style="width:320px;height:30px"></div><div class="skeleton" style="width:200px;margin-top:12px"></div></div>`;
    try {
      await loadCore();
    } catch (err) {
      app.innerHTML = `<div class="page"><div class="card card-pad">Couldn't open this event: ${esc(err.message)} · <a href="#/">Back to your events</a></div></div>`;
      return;
    }
    if (nav !== ws.nav) return;
    renderFrame(app);
  }
  crumb.innerHTML = `${icon("next")}<strong>${esc(ws.ev.event.name)}</strong>`;
  document.title = `${ws.ev.event.name} · Stagecraft`;
  renderHeader();
  document.getElementById("ws").classList.toggle("no-chat", tab === "resolve");
  await renderTab();
  if (nav !== ws.nav || tab === "resolve") return;
  const key = `${ws.id}:${tab}`;
  if (mountedChat !== key) {
    mountChat(els.chat, ws.id, tab, refresh);
    mountedChat = key;
  }
  if (ws.prompt) {
    prefillChat(ws.prompt);
    els.chat.classList.add("open");
  }
  ws.prompt = "";
}

async function loadCore() {
  const [ev, plan, ch, act] = await Promise.all([api(E()), api(`${E()}/planning`), api(`${E()}/changes`), api(`${E()}/activity?limit=12`)]);
  Object.assign(ws, { ev, plan, changes: ch.changes, activity: act.activity });
  setApprover(plan.me);
}

// Reload data after any change and redraw the current tab (the chat panel redraws itself).
export async function refresh() {
  await loadCore();
  if (ws.tab === "inbox") ws.inbox = await api(`${E()}/inbox`);
  renderHeader();
  await renderTab();
}

function renderFrame(app) {
  app.innerHTML = `
    <div class="ws" id="ws">
      <section class="ws-main" id="ws-main">
        <div class="ws-head" id="ws-head"></div>
        <div class="ws-body" id="ws-body" role="tabpanel"></div>
      </section>
      <aside class="chat" id="chat" aria-label="Assistant"></aside>
    </div>`;
  mountedChat = "";
  els = { head: app.querySelector("#ws-head"), body: app.querySelector("#ws-body"), chat: app.querySelector("#chat"), main: app.querySelector("#ws-main") };
}

function renderHeader() {
  const e = ws.ev.event, v = ws.ev.venue, h = ws.plan.health;
  const urgent = h.flags.Critical + h.flags.High;
  const inboxCount = ws.plan.inbox.filter((m) => m.status === "New" || m.status === "Needs Reply").length;
  const badge = { overview: urgent, inbox: inboxCount, team: ws.plan.team?.unassigned_open_tasks || 0 };
  els.head.innerHTML = `
    <div class="ws-title">
      <div class="grow">
        <h1>${esc(e.name)}</h1>
        <div class="ws-sub">
          <span>${icon("calendar")} ${esc(fmtLong(e.date))} · ${h.days_to_go >= 0 ? esc(daysLabel(h.days_to_go)) : "past"}</span>
          <span>${icon("pin")} ${esc(v.name)}, ${esc(v.city)}</span>
          <span>${icon("attendees")} ${h.registrations.active} of ${h.registrations.cap} registered</span>
        </div>
      </div>
      <div class="row">
        <a class="btn sm" href="#/event/${encodeURIComponent(e.id)}/live" title="Event-day view: now, next, check-in, and a Something broke button">${icon("radio")} Live mode</a>
        <a class="btn sm" href="/live/${encodeURIComponent(e.id)}" target="_blank" rel="noopener">${icon("external")} Attendee page</a>
        <button class="btn sm ${ws.tab === "resolve" ? "active-soft" : ""}" id="resolve-open" title="Day-of problem? Three agents propose a verified fix">${icon("alert")} Resolve an issue</button>
        ${ws.tab === "resolve" ? "" : `<button class="btn primary sm chat-toggle" id="chat-open">${icon("chat")} Assistant</button>`}
      </div>
    </div>
    <nav class="tabs" role="tablist" aria-label="Event areas">
      ${TABS.map((t) => `<button class="tab ${t.id === ws.tab ? "active" : ""}" role="tab" aria-selected="${t.id === ws.tab}" data-tab="${t.id}">
        ${icon(t.icon)} ${esc(t.label)}${badge[t.id] ? ` <span class="count ${t.id === "inbox" ? "gray" : ""}">${badge[t.id]}</span>` : ""}</button>`).join("")}
    </nav>`;
  els.head.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => go(b.dataset.tab)));
  els.head.querySelector("#chat-open")?.addEventListener("click", () => els.chat.classList.toggle("open"));
  els.head.querySelector("#resolve-open").addEventListener("click", () => { ws.resolveFrom = null; go("resolve"); });
}

function go(tab, prompt = "") {
  ws.prompt = prompt;
  const target = `#/event/${encodeURIComponent(ws.id)}/${tab}`;
  if (location.hash === target) showWorkspace(document.getElementById("app"), document.getElementById("crumb"), ws.id, tab);
  else location.hash = target;
}

async function renderTab() {
  const r = { overview: overviewTab, team: teamTab, program: programTab, venue: venueTab, food: foodTab, attendees: attendeesTab, budget: budgetTab, inbox: inboxTab, resolve: resolveTab }[ws.tab];
  await r();
}

// ================= shared pieces =================
const flagsFor = (dim) => ws.plan.flags.filter((f) => !dim || f.dimension === dim);
const deadlinesFor = (dim) => ws.plan.deadlines.filter((d) => !dim || d.dimension === dim);
const allTasks = () => ws.plan.initiatives.flatMap((i) => i.tasks).concat(ws.plan.tasks_without_initiative);
const SOURCE = { system: "Rule", inbox: "From email", assistant: "Assistant", user: "You", seed: "You" };
const RESOLVABLE = new Set(["Program", "Venue", "Food"]);  // schedule, rooms, catering: what the three-agent desk can re-plan
// Day-of disruptions only: schedule conflicts, speakers not on site, and flags raised from emails or assistants in those areas
// (not paperwork such as unsigned contracts or overdue deadlines).
const canResolve = (f) => RESOLVABLE.has(f.dimension) && (/^sys_(schedule|travel)_/.test(f.id) || !f.id.startsWith("sys_"));

function resolveFrom(from) {
  ws.resolveFrom = from;
  go("resolve");
}

// Live mode's "Something broke" opens Resolve an issue with the moment's context already written in.
export function startResolve(eventId, from) {
  ws.resolveFrom = from;
  location.hash = `#/event/${encodeURIComponent(eventId)}/resolve`;
}

function flagsCard(dim, { limit = 6, title = "Red flags" } = {}) {
  const flags = flagsFor(dim);
  if (!flags.length) return dim ? "" : `<div class="card card-pad">${icon("check")} No red flags right now.</div>`;
  const rows = flags.map((f, i) => `
    <div class="list-row flag-row" ${i >= limit ? 'data-more hidden' : ""}>
      <span class="sev ${f.severity}" aria-hidden="true"></span>
      <div class="grow">
        <div class="row wrap"><span class="title">${esc(f.title)}</span><span class="pill ${sevTone(f.severity)}">${esc(f.severity)}</span>
          <span class="tiny faint">${esc(SOURCE[f.source] || f.source)}${dim ? "" : ` · ${esc(f.dimension_label)}`}</span></div>
        ${f.detail ? `<p class="small muted" style="margin-top:2px">${esc(f.detail)}</p>` : ""}
        <div class="row wrap" style="margin-top:4px;gap:2px 6px">
          <button class="btn plain sm" data-ask="${esc(DIM_TAB[f.dimension])}" data-prompt="${esc(`Help me resolve this: ${f.title}. ${f.detail || ""}`)}">${icon("sparkles")} Ask the assistant</button>
          ${canResolve(f) ? `<button class="btn plain sm" data-resolve-flag="${esc(f.id)}">${icon("alert")} Resolve with the agents</button>` : ""}
          ${f.source !== "system" ? `<button class="btn plain sm" data-flag="${esc(f.id)}" data-status="Resolved">Mark resolved</button>
            <button class="btn plain sm" data-flag="${esc(f.id)}" data-status="Dismissed" style="color:var(--text-2)">Dismiss</button>` : ""}
        </div>
      </div>
    </div>`).join("");
  return `<div class="card"><div class="card-head"><h3>${icon("flag")} ${title}</h3><span class="muted small">${flags.length} open</span></div>
    <div class="card-body">${rows}${flags.length > limit ? `<button class="btn plain sm" data-show-more>Show all ${flags.length}</button>` : ""}</div></div>`;
}

function deadlinesCard(dim, { title = "Deadlines", days = null } = {}) {
  let list = deadlinesFor(dim);
  const today = ws.plan.today;
  const open = list.filter((d) => d.status !== "Done" && (days === null || d.status === "Overdue"
    || (new Date(d.due_date) - new Date(today)) / 864e5 <= days));
  const done = list.filter((d) => d.status === "Done");
  const row = (d) => `
    <div class="list-row">
      <input type="checkbox" class="checkbox" data-deadline="${esc(d.id)}" ${d.status === "Done" ? "checked" : ""} aria-label="Mark ${esc(d.title)} done">
      <div class="grow"><div style="${d.status === "Done" ? "text-decoration:line-through;color:var(--text-3)" : ""}">${esc(d.title)}</div>
        <div class="tiny muted">${esc(d.owner || "Unassigned")}${d.notes ? ` · ${esc(d.notes)}` : ""}</div></div>
      <div style="text-align:right"><span class="pill ${deadlineTone(d.status)}">${d.status === "Done" ? "Done" : esc(fmtDate(d.due_date))}</span>
        ${d.status === "Overdue" ? `<div class="tiny" style="color:var(--red);margin-top:2px">Overdue</div>` : d.status === "Due soon" ? `<div class="tiny" style="color:var(--orange);margin-top:2px">${esc(daysLabel(Math.round((new Date(d.due_date) - new Date(today)) / 864e5)))}</div>` : ""}</div>
    </div>`;
  return `<div class="card"><div class="card-head"><h3>${icon("calendar")} ${title}</h3><span class="muted small">${open.length} open</span></div>
    <div class="card-body">${open.map(row).join("") || `<p class="muted small">Nothing due${days ? ` in the next ${days} days` : ""}.</p>`}
    ${done.length && days === null ? `<details style="margin-top:8px"><summary class="small muted">${done.length} done</summary>${done.map(row).join("")}</details>` : ""}</div></div>`;
}

function tasksCard(dim, title = "Tasks") {
  const tasks = allTasks().filter((t) => !dim || t.dimension === dim);
  if (!tasks.length) return "";
  const open = tasks.filter((t) => t.status !== "Done");
  return `<div class="card"><div class="card-head"><h3>${icon("list")} ${title}</h3><span class="muted small">${tasks.length - open.length} of ${tasks.length} done</span></div>
    <div class="card-body">${tasks.map(taskRow).join("")}</div></div>`;
}

function taskRow(t) {
  const byId = Object.fromEntries((ws.plan.team?.members || []).map((m) => [m.id, m]));
  return `<div class="list-row">
    <input type="checkbox" class="checkbox" data-task="${esc(t.id)}" ${t.status === "Done" ? "checked" : ""} aria-label="Mark ${esc(t.title)} done">
    <div class="grow"><div style="${t.status === "Done" ? "text-decoration:line-through;color:var(--text-3)" : ""}">${esc(t.title)}</div>
      <div class="tiny muted">${esc(t.owner || "Unassigned")}${t.due_date ? ` · due ${esc(fmtDate(t.due_date))}` : ""}</div></div>
    ${t.status === "Blocked" ? `<span class="pill red">Blocked</span>` : t.status === "In Progress" ? `<span class="pill blue">In progress</span>` : ""}
    ${t.status !== "Done" ? `<button class="assign-btn" data-assign-task="${esc(t.id)}" aria-label="Assign ${esc(t.title)}" title="Change owners">${avatarStack(t.assignees, byId, { max: 3 })}</button>` : ""}
  </div>`;
}

// ================= Team =================
async function teamTab() {
  renderTeam({ id: ws.id, plan: ws.plan, root: els.body, refresh, bind: bindCommon });
}

function kpi(label, value, sub = "", color = "") {
  return `<div class="card kpi"><div class="stat-label">${label}</div><div class="v num" ${color ? `style="color:${color}"` : ""}>${value}</div><div class="s">${sub}</div></div>`;
}

function bindCommon(root) {
  root.querySelectorAll("[data-show-more]").forEach((b) => b.addEventListener("click", () => {
    b.closest(".card").querySelectorAll("[data-more]").forEach((r) => r.removeAttribute("hidden"));
    b.remove();
  }));
  root.querySelectorAll("[data-ask]").forEach((b) => b.addEventListener("click", () => go(b.dataset.ask, b.dataset.prompt)));
  root.querySelectorAll("[data-resolve-flag]").forEach((b) => b.addEventListener("click", () => {
    const f = ws.plan.flags.find((x) => x.id === b.dataset.resolveFlag);
    resolveFrom({ text: `${f.title}. ${f.detail || ""}`.trim(), label: `the red flag “${f.title}”`, flag_id: f.id.startsWith("sys_") ? null : f.id });
  }));
  root.querySelectorAll("[data-flag]").forEach((b) => b.addEventListener("click", async () => {
    busy(b, true);
    try { await post(`${E()}/flags/${b.dataset.flag}`, { status: b.dataset.status }); toast(`Flag ${b.dataset.status.toLowerCase()}`); await refresh(); }
    catch (err) { toast(err.message, "bad"); busy(b, false); }
  }));
  const edit = async (type, id, changes) => {
    try { await post(`${E()}/records/${type}/${id}`, { changes }); await refresh(); }
    catch (err) { toast(err.message, "bad"); }
  };
  root.querySelectorAll("[data-assign-task]").forEach((b) => b.addEventListener("click", () => {
    const task = allTasks().find((t) => t.id === b.dataset.assignTask);
    openAssign(b, { eventId: ws.id, task, members: ws.plan.team.members, onSaved: refresh });
  }));
  root.querySelectorAll("[data-deadline]").forEach((c) => c.addEventListener("change", () => edit("deadline", c.dataset.deadline, { done: c.checked })));
  root.querySelectorAll("[data-task]").forEach((c) => c.addEventListener("change", () => edit("task", c.dataset.task, { status: c.checked ? "Done" : "To Do" })));
  bindChangeActions(root, ws.id, refresh);
}

function paint(html) {
  els.body.innerHTML = html;
  bindCommon(els.body);
}

// ================= Resolve an issue =================
async function resolveTab() {
  const from = ws.resolveFrom;
  ws.resolveFrom = null;
  // Keep the resolve screen's own state on refresh: reload data and the header only.
  await renderResolve(els.body, { id: ws.id, get ev() { return ws.ev; }, from, refresh: async () => { await loadCore(); renderHeader(); } });
}

// ================= Overview =================
async function overviewTab() {
  const h = ws.plan.health, p = ws.plan;
  const urgent = h.flags.Critical + h.flags.High;
  const pending = ws.changes;
  paint(`
    <div class="card" id="brief-card">${briefHtml()}</div>
    <div class="kpis">
      ${kpi("Days to go", h.days_to_go >= 0 ? h.days_to_go : "-", esc(h.phase))}
      ${kpi("Planned", `${h.progress_pct}%`, `${p.initiatives.length} workstreams`)}
      ${kpi("Red flags", h.flags.total, urgent ? `${urgent} need action soon` : "none urgent", urgent ? "var(--red)" : "")}
      ${kpi("Registered", `${h.registrations.active}`, `of ${h.registrations.cap} · ${h.registrations.waitlist} waitlisted`)}
      ${kpi("Budget spent", money(h.budget.spent_usd), `of ${money(h.budget.planned_usd)} planned`)}
    </div>
    <div class="grid-2">
      <div class="card" id="updates-card"><div class="card-head"><h3>${icon("bell")} Team updates</h3></div></div>
      <div class="card"><div class="card-head"><h3>${icon("send")} Share and wrap up</h3><span class="tiny muted">AI drafts, you edit</span></div>
        <div class="card-body tool-list">
          <button class="tool" id="tool-report">${icon("mail")}<span><b>Write a status report</b><span class="small muted">For the chair, sponsors, or the SOM events office, from live data</span></span>${icon("next")}</button>
          <button class="tool" id="tool-notes">${icon("clipboard")}<span><b>Meeting notes to tasks</b><span class="small muted">Paste notes; get tasks with owners and dates to confirm</span></span>${icon("next")}</button>
          <button class="tool" id="tool-feedback">${icon("chat")}<span><b>Summarize post-event feedback</b><span class="small muted">Themes, sentiment, quotes, and fixes for next year</span></span>${icon("next")}</button>
        </div></div>
    </div>
    ${pending.length ? `<div class="card"><div class="card-head"><h3>${icon("clipboard")} Waiting for your review</h3><span class="muted small">${pending.length} drafted change${pending.length === 1 ? "" : "s"}</span></div>
      <div class="card-body"><div class="grid-2">${pending.map((c) => changeCard(c)).join("")}</div></div></div>` : ""}
    <div class="grid-2">
      ${flagsCard(null)}
      ${deadlinesCard(null, { title: "Coming up (next 14 days)", days: 14 })}
    </div>
    ${roadmap(ws.plan, ws.ev.event.date)}
    <div class="card"><div class="card-head"><h3>${icon("clock")} Recent activity</h3></div><div class="card-body">
      ${ws.activity.length ? ws.activity.slice(0, 8).map((a) => `<div class="list-row"><div class="grow small">${esc(a.summary)}</div><span class="tiny faint" style="white-space:nowrap">${esc(a.actor)} · ${esc(fmtWhen(a.at))}</span></div>`).join("")
        : `<p class="muted small">Changes you confirm, emails the Inbox Watcher handles, and briefs show up here.</p>`}
    </div></div>`);
  mountTeamUpdates(document.getElementById("updates-card"), ws.id);
  els.body.querySelector("#tool-report").addEventListener("click", () => openStatusReport(ws.id));
  els.body.querySelector("#tool-feedback").addEventListener("click", () => openFeedback(ws.id));
  els.body.querySelector("#tool-notes").addEventListener("click", () => openMeetingNotes(ws.id, refresh));
  if (!ws.brief) loadBrief(false);
  else bindBrief();
}

function briefHtml() {
  const b = ws.brief;
  if (!b) return `<div class="card-head"><h3>${icon("sparkles")} Today's brief</h3><span class="muted small">Writing…</span></div>
    <div class="card-body stack"><div class="skeleton" style="width:80%"></div><div class="skeleton" style="width:65%"></div><div class="skeleton" style="width:72%"></div></div>`;
  if (!b.brief) return `<div class="card-head"><h3>${icon("sparkles")} Today's brief</h3><button class="btn sm" data-brief-refresh>${icon("refresh")} Try again</button></div>
    <div class="card-body"><p class="muted">The brief couldn't be written just now (${esc(b.error || "")}). Everything below is live data.</p></div>`;
  const br = b.brief;
  return `
    <div class="card-head"><h3>${icon("sparkles")} Today's brief</h3>
      <div class="row"><span class="tiny faint">${esc(fmtWhen(b.generated_at))} · $${Number(b.usage.cost_usd).toFixed(4)}</span>
      <button class="btn sm" data-brief-refresh title="Write a fresh brief from the latest data">${icon("refresh")} Refresh</button></div></div>
    <div class="card-body">
      <p class="brief-headline">${esc(br.headline)}</p>
      <div style="margin-top:12px">${br.top_priorities.map((p, i) => `
        <div class="prio ${p.urgency.toLowerCase()}"><span class="n">${i + 1}</span><div>
          <div style="font-weight:600">${esc(p.title)}</div>
          <p class="small muted">${esc(p.why)}</p>
          <button class="btn plain sm" style="padding:0;height:auto;margin-top:4px" data-ask="${esc(DIM_TAB[p.dimension])}" data-prompt="${esc(p.next_step)}">${icon("next")} ${esc(p.next_step)}</button>
        </div></div>`).join("")}</div>
      ${br.risks.length ? `<details style="margin-top:10px"><summary class="small" style="cursor:pointer;font-weight:500">Risks to keep an eye on (${br.risks.length})</summary>
        <ul class="small muted" style="margin:8px 0 0;padding-left:18px">${br.risks.map((r) => `<li style="margin-bottom:4px">${esc(r)}</li>`).join("")}</ul></details>` : ""}
      ${br.since_yesterday.length ? `<details><summary class="small" style="cursor:pointer;font-weight:500">Since yesterday (${br.since_yesterday.length})</summary>
        <ul class="small muted" style="margin:8px 0 0;padding-left:18px">${br.since_yesterday.map((r) => `<li style="margin-bottom:4px">${esc(r)}</li>`).join("")}</ul></details>` : ""}
    </div>`;
}

async function loadBrief(regenerate) {
  if (regenerate) { ws.brief = null; const c = document.getElementById("brief-card"); if (c) c.innerHTML = briefHtml(); }
  try {
    ws.brief = await api(`${E()}/brief${regenerate ? "?regenerate=true" : ""}`);
  } catch (err) {
    ws.brief = { brief: null, error: err.message };
  }
  const card = document.getElementById("brief-card");
  if (card && ws.tab === "overview") { card.innerHTML = briefHtml(); bindBrief(); }
}

function bindBrief() {
  const card = document.getElementById("brief-card");
  if (!card) return;
  card.querySelectorAll("[data-ask]").forEach((b) => b.addEventListener("click", () => go(b.dataset.ask, b.dataset.prompt)));
  card.querySelector("[data-brief-refresh]")?.addEventListener("click", () => loadBrief(true));
}

// ================= Program =================
async function programTab() {
  const st = ws.ev;
  const speakers = Object.fromEntries(st.speakers.map((s) => [s.id, s]));
  const statusDot = (s) => (s === "Confirmed On-Site" ? "green" : s === "In Transit" ? "orange" : "red");
  paint(`
    ${flagsCard("Program", { title: "Program red flags" })}
    <div class="card"><div class="card-head"><h3>${icon("program")} Schedule</h3>
        <div class="row"><span class="muted small">${st.sessions.length} sessions · ${esc(fmtDate(st.event.date))}</span>
          <div class="segmented" role="tablist" aria-label="Schedule view">${[["grid", "Grid"], ["list", "List"]].map(([k, l]) => `<button class="${ws.schedView === k ? "active" : ""}" data-sv="${k}">${l}</button>`).join("")}</div></div></div>
      <div class="card-body">${ws.schedView === "grid" ? scheduleGrid(st) : st.sessions.map((s) => `
        <div class="session">
          <div class="time">${esc(s.start_time)}<div class="tiny muted" style="font-weight:400">to ${esc(s.end_time)}</div></div>
          <div class="grow">
            <div style="font-weight:500">${esc(s.title)}</div>
            <div class="row wrap" style="margin-top:6px"><span class="pill">${esc(s.track)}</span>
              ${s.is_locked_keynote ? `<span class="pill blue">${icon("shield")} Locked keynote</span>` : ""}
              ${s.war_room_update || s.status === "Rescheduled" ? `<span class="pill green">Rescheduled</span>` : ""}
              ${s.speakers.map((sp) => `<span class="chip" title="${esc(sp.status)} · ${esc(sp.travel_origin)}"><span class="dot ${statusDot(sp.status)}"></span>${esc(sp.name)}</span>`).join("")}</div>
          </div>
          <div class="small">
            <div>${esc(s.room.name)}</div>
            <div class="row" style="margin-top:4px"><div class="bar grow ${s.utilization_pct > 100 ? "red" : s.utilization_pct >= 95 ? "orange" : "green"}"><span style="width:${Math.min(100, s.utilization_pct)}%"></span></div>
              <span class="tiny muted num">${s.expected_attendance}/${s.room.capacity}</span></div>
            ${s.room.status !== "Operational" ? `<span class="pill red" style="margin-top:4px">${esc(s.room.status)}</span>` : ""}
          </div>
        </div>`).join("")}</div></div>
    <div class="grid-2">
      <div class="card"><div class="card-head"><h3>${icon("user")} Speakers</h3><span class="muted small">${st.speakers.filter((s) => s.status !== "Confirmed On-Site").length} still travelling</span></div>
        <div class="card-body">${st.speakers.map((s) => `
          <div class="list-row"><span class="dot ${statusDot(s.status)}" style="margin-top:7px"></span>
            <div class="grow"><div style="font-weight:500">${esc(s.name)}</div><div class="tiny muted">${esc(s.title_org)}</div></div>
            <div style="text-align:right"><div class="small">${esc(s.status)}</div><div class="tiny muted">${esc(s.travel_origin)}</div></div></div>`).join("")}</div></div>
      <div class="stack">${deadlinesCard("Program")}${tasksCard("Program")}</div>
    </div>`);
  els.body.querySelectorAll("[data-sv]").forEach((b) => b.addEventListener("click", () => { ws.schedView = b.dataset.sv; programTab(); }));
}

// ================= Venue =================
async function venueTab() {
  const st = ws.ev;
  const vendors = ws.plan.vendors.filter((v) => v.relationship === "Vendor" && v.category !== "Catering");
  paint(`
    ${flagsCard("Venue", { title: "Venue red flags" })}
    <div class="grid-3">${st.rooms.map((r) => `
      <div class="card room-card">
        <div class="row-between" style="align-items:flex-start"><div><div style="font-weight:600">${esc(r.name)}</div><div class="tiny muted">${esc(r.floor_or_zone)}${r.is_catering_hub ? " · catering" : ""}</div></div>
          <span class="pill ${r.status === "Operational" ? "green" : "red"}">${esc(r.status)}</span></div>
        <div class="row" style="margin:12px 0 4px"><span class="cap num">${r.capacity}</span><span class="small muted">seats · ${r.session_count} session${r.session_count === 1 ? "" : "s"}</span></div>
        <div class="row"><div class="bar grow ${r.utilization_pct > 100 ? "red" : r.utilization_pct >= 95 ? "orange" : "green"}"><span style="width:${Math.min(100, r.utilization_pct)}%"></span></div>
          <span class="tiny muted num">peak ${r.utilization_pct}%</span></div>
        <div class="row wrap" style="margin-top:10px">${r.av_specs.map((a) => `<span class="chip">${esc(a)}</span>`).join("")}</div>
      </div>`).join("")}</div>
    <div class="grid-2">
      <div class="stack">
        <div class="card" id="weather-card"><div class="card-head"><h3>${icon("cloud")} Weather and travel</h3></div><div class="card-body">${weatherHtml()}</div></div>
        <div class="card"><div class="card-head"><h3>${icon("venue")} Venue vendors</h3></div><div class="card-body">${vendors.map(vendorRow).join("") || `<p class="muted small">No vendors yet.</p>`}</div></div>
      </div>
      <div class="stack">${deadlinesCard("Venue")}${tasksCard("Venue")}</div>
    </div>`);
  if (!ws.weather) {  // live call; load in the background so the tab and the assistant don't wait for it
    api(`${E()}/weather`).catch(() => ({ error: true })).then((w) => {
      ws.weather = w;
      const c = document.querySelector("#weather-card .card-body");
      if (c && ws.tab === "venue") c.innerHTML = weatherHtml();
    });
  }
}

function weatherHtml() {
  const w = ws.weather;
  if (!w) return `<div class="skeleton" style="width:70%"></div>`;
  if (w.error) return `<p class="muted small">Weather is unavailable right now.</p>`;
  const tone = { LOW: "green", MODERATE: "orange", HIGH: "red" }[w.transit_delay_risk];
  return `<div class="row-between"><div><div class="stat-big">${Math.round(w.conditions.temperature_c)}°C</div><div class="small muted">${esc(w.conditions.description)} in ${esc(w.venue.city)}</div></div>
    <span class="pill ${tone}">${esc(w.transit_delay_risk.toLowerCase())} travel risk</span></div>
    <p class="small muted" style="margin-top:10px">${esc(w.advisory)}</p>
    <p class="tiny faint" style="margin-top:6px">${esc(w.airport_corridor)} · ${w.source === "open-meteo-live" ? "live from Open-Meteo" : "offline estimate"}</p>`;
}

function vendorRow(v) {
  const tone = { Signed: "green", Paid: "green", Invoiced: "blue", Negotiating: "orange", Prospect: "orange" }[v.contract_status];
  return `<div class="list-row"><div class="grow"><div style="font-weight:500">${esc(v.name)}</div>
    <div class="tiny muted">${esc(v.category)} · ${esc(v.contact_name)} · <a href="mailto:${esc(v.contact_email)}">${esc(v.contact_email)}</a></div>
    ${v.notes ? `<div class="tiny muted">${esc(v.notes)}</div>` : ""}</div>
    <div style="text-align:right"><span class="pill ${tone}">${esc(v.contract_status)}</span><div class="small num" style="margin-top:3px">${money(v.contract_value_usd)}</div></div></div>`;
}

// ================= Food =================
async function foodTab() {
  const active = ws.plan.health.registrations.active;
  const diet = Object.entries(ws.plan.attendees.dietary_active).filter(([k]) => k !== "None");
  const maxDiet = Math.max(1, ...diet.map(([, n]) => n));
  const caterers = ws.plan.vendors.filter((v) => v.category === "Catering");
  paint(`
    ${flagsCard("Food", { title: "Food red flags" })}
    <div class="grid-2">
      <div class="card"><div class="card-head"><h3>${icon("food")} Meals</h3></div><div class="card-body">
        ${ws.ev.catering_slas.map((c) => `
          <div class="list-row"><div class="grow"><div style="font-weight:500">${esc(c.meal_name)}</div>
            <div class="small muted">${esc(c.scheduled_time)} · must be served by ${esc(c.hard_cutoff_time)} · ${esc(c.room_name)}</div>
            <div class="tiny muted">${esc(c.vendor_name)} · late penalty ${money(c.late_penalty_usd)}</div></div>
            <div style="text-align:right"><div class="stat-big num" style="font-size:18px">${c.headcount}</div><div class="tiny muted">headcount</div>
              ${c.headcount < active ? `<span class="pill orange" style="margin-top:3px">${active - c.headcount} short</span>` : ""}</div></div>`).join("")}
      </div></div>
      <div class="card"><div class="card-head"><h3>${icon("attendees")} Dietary needs</h3><span class="muted small">${active} registered</span></div><div class="card-body">
        ${diet.map(([k, n]) => `<div class="list-row" style="align-items:center"><span style="width:110px">${esc(k)}</span>
          <div class="bar grow"><span style="width:${pct(n, maxDiet)}%"></span></div><span class="num small" style="width:34px;text-align:right">${n}</span></div>`).join("")}
        <p class="tiny muted" style="margin-top:8px">${ws.plan.attendees.dietary_active.None || 0} with no dietary needs.</p>
      </div></div>
    </div>
    <div class="grid-2">
      <div class="card"><div class="card-head"><h3>${icon("venue")} Caterer</h3></div><div class="card-body">${caterers.map(vendorRow).join("") || `<p class="muted small">No caterer yet.</p>`}</div></div>
      <div class="stack">${deadlinesCard("Food")}${tasksCard("Food")}</div>
    </div>`);
}

// ================= Attendees =================
async function attendeesTab() {
  const a = ws.plan.attendees, h = ws.plan.health;
  const s = a.by_status, t = a.by_type;
  paint(`
    ${flagsCard("Attendees", { title: "Attendee red flags" })}
    ${attendeeCharts(ws.plan)}
    <div class="card"><div class="card-head"><h3>${icon("attendees")} People</h3><span class="muted small" id="att-count"></span></div>
      <div class="card-body">
        <div class="row wrap" style="margin-bottom:10px">
          <div class="row grow" style="min-width:220px;position:relative">${icon("search")}<label class="sr-only" for="att-q">Search attendees</label>
            <input class="input" id="att-q" placeholder="Search name, email, or organization" value="${esc(ws.att.q)}"></div>
          ${["status", "type", "dietary"].map((k) => {
            const opts = { status: Object.keys(s), type: Object.keys(t), dietary: Object.keys(a.dietary_active) }[k];
            return `<label class="sr-only" for="att-${k}">Filter by ${k}</label><select class="select" style="width:auto" id="att-${k}" data-f="${k}">
              <option value="">${{ status: "Any RSVP", type: "Any type", dietary: "Any diet" }[k]}</option>
              ${opts.map((o) => `<option ${ws.att[k] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
          }).join("")}
        </div>
        <div class="table-wrap" id="att-table"></div>
      </div></div>
    <div class="grid-2">${deadlinesCard("Attendees")}${tasksCard("Attendees") || "<div></div>"}</div>`);
  const draw = () => drawAttendees();
  els.body.querySelector("#att-q").addEventListener("input", (e) => { ws.att.q = e.target.value; ws.att.page = 0; draw(); });
  els.body.querySelectorAll("[data-f]").forEach((sel) => sel.addEventListener("change", () => { ws.att[sel.dataset.f] = sel.value; ws.att.page = 0; draw(); }));
  draw();
}

function drawAttendees() {
  const f = ws.att, q = f.q.toLowerCase().trim(), PER = 50;
  const rows = ws.plan.attendees.rows.filter((a) => (!q || `${a.name} ${a.email} ${a.organization}`.toLowerCase().includes(q))
    && (!f.status || a.rsvp_status === f.status) && (!f.type || a.attendee_type === f.type) && (!f.dietary || a.dietary === f.dietary));
  const pages = Math.max(1, Math.ceil(rows.length / PER));
  f.page = Math.min(f.page, pages - 1);
  const shown = rows.slice(f.page * PER, (f.page + 1) * PER);
  const tone = { Confirmed: "green", Registered: "blue", Waitlist: "orange", Cancelled: "red", "Checked In": "green" };
  document.getElementById("att-count").textContent = `${rows.length} of ${ws.plan.attendees.total}`;
  document.getElementById("att-table").innerHTML = `
    <table class="table"><thead><tr><th>Name</th><th>Organization</th><th>Type</th><th>RSVP</th><th>Dietary</th><th>Registered</th></tr></thead><tbody>
    ${shown.map((a) => `<tr><td><div style="font-weight:500">${esc(a.name)}</div><div class="tiny muted">${esc(a.email)}</div></td><td>${esc(a.organization)}</td>
      <td>${esc(a.attendee_type)}</td><td><span class="pill ${tone[a.rsvp_status] || ""}">${esc(a.rsvp_status)}</span></td>
      <td>${a.dietary === "None" ? `<span class="faint">-</span>` : esc(a.dietary)}</td><td class="small muted">${esc(fmtDate(a.registered_on))}</td></tr>`).join("")
      || `<tr><td colspan="6" class="empty">No one matches these filters.</td></tr>`}</tbody></table>
    ${pages > 1 ? `<div class="row-between" style="margin-top:10px"><button class="btn sm" data-pg="-1" ${f.page === 0 ? "disabled" : ""}>${icon("back")} Previous</button>
      <span class="small muted">Page ${f.page + 1} of ${pages}</span><button class="btn sm" data-pg="1" ${f.page >= pages - 1 ? "disabled" : ""}>Next ${icon("next")}</button></div>` : ""}`;
  document.querySelectorAll("#att-table [data-pg]").forEach((b) => b.addEventListener("click", () => { f.page += Number(b.dataset.pg); drawAttendees(); }));
}

// ================= Tasks and budget =================
async function budgetTab() {
  const p = ws.plan, h = p.health;
  const items = p.budget.items;
  const over = items.filter((b) => b.actual_usd > b.planned_usd);
  const sponsors = p.vendors.filter((v) => v.relationship === "Sponsor");
  const vendors = p.vendors.filter((v) => v.relationship === "Vendor");
  paint(`
    ${flagsCard("Budget", { title: "Budget and task red flags" })}
    <div class="kpis">
      ${kpi("Planned", money(h.budget.planned_usd), `${items.length} lines`)}
      ${kpi("Spent or committed", money(h.budget.spent_usd), `${pct(h.budget.spent_usd, h.budget.planned_usd)}% of plan`)}
      ${kpi("Remaining", money(h.budget.planned_usd - h.budget.spent_usd), "")}
      ${kpi("Sponsorship signed", money(h.budget.sponsorship_signed_usd), `${sponsors.length} sponsors`)}
      ${kpi("Over budget", over.length, over.length ? over.map((b) => esc(b.description)).join(", ") : "all lines within plan", over.length ? "var(--red)" : "")}
    </div>
    ${budgetChart(p)}
    <div class="card"><div class="card-head"><h3>${icon("list")} Workstreams and tasks</h3><span class="muted small">${h.progress_pct}% overall</span></div>
      <div class="card-body">${p.initiatives.map((i) => `
        <details class="list-row" style="display:block" ${i.status === "Blocked" || i.status === "At risk" ? "open" : ""}>
          <summary style="cursor:pointer;list-style:none"><div class="row-between"><span style="font-weight:500">${esc(i.title)}</span>
            <span class="row"><span class="small muted num">${i.tasks_done}/${i.tasks_total}</span><span class="pill ${statusTone(i.status)}">${esc(i.status)}</span></span></div>
            <div class="bar" style="margin-top:6px"><span style="width:${i.progress_pct}%"></span></div>
            <div class="tiny muted" style="margin-top:3px">${esc(i.owner || "Unassigned")} · ${esc(i.description)}</div></summary>
          <div style="padding-left:6px;margin-top:6px">${i.tasks.map(taskRow).join("")}</div>
        </details>`).join("")}</div></div>
    <div class="card"><div class="card-head"><h3>${icon("dollar")} Budget</h3></div><div class="card-body table-wrap">
      <table class="table"><thead><tr><th>Line</th><th>Category</th><th class="r">Planned</th><th class="r">Spent</th><th>Status</th></tr></thead><tbody>
      ${items.map((b) => `<tr><td>${esc(b.description)}</td><td class="muted">${esc(b.category)}</td><td class="r num">${money(b.planned_usd)}</td>
        <td class="r num" style="${b.actual_usd > b.planned_usd ? "color:var(--red);font-weight:600" : ""}">${money(b.actual_usd)}</td>
        <td><span class="pill ${b.status === "Paid" ? "green" : b.status === "Committed" ? "blue" : ""}">${esc(b.status)}</span></td></tr>`).join("")}
      <tr><td style="font-weight:600">Total</td><td></td><td class="r num" style="font-weight:600">${money(h.budget.planned_usd)}</td><td class="r num" style="font-weight:600">${money(h.budget.spent_usd)}</td><td></td></tr>
      </tbody></table></div></div>
    <div class="grid-2">
      <div class="card"><div class="card-head"><h3>${icon("dollar")} Sponsors</h3></div><div class="card-body">${sponsors.map(vendorRow).join("") || `<p class="muted small">No sponsors yet.</p>`}</div></div>
      <div class="card"><div class="card-head"><h3>${icon("venue")} Vendors</h3></div><div class="card-body">${vendors.map(vendorRow).join("")}</div></div>
    </div>
    ${deadlinesCard("Budget")}`);
}

// ================= Inbox =================
const MAIL_FILTERS = [["Needs Reply", "Needs reply"], ["Triaged", "To act on"], ["New", "Unread"], ["Quarantined", "Quarantined"], ["Done", "Done"], ["All", "All"]];

async function inboxTab() {
  if (!ws.inbox) {
    els.body.innerHTML = `<div class="card card-pad"><div class="skeleton" style="width:40%"></div></div>`;
    ws.inbox = await api(`${E()}/inbox`);
  }
  const counts = ws.inbox.counts;
  const msgs = ws.inbox.messages.filter((m) => ws.mailFilter === "All" || m.status === ws.mailFilter);
  if (!ws.mailId || !msgs.some((m) => m.id === ws.mailId)) ws.mailId = msgs[0]?.id || null;
  const icons = { "Reply needed": "mail", "Action needed": "alert", FYI: "inbox", Noise: "inbox", Suspicious: "shield" };
  paint(`
    <div class="row-between wrap">
      <div class="segmented" role="tablist" aria-label="Filter emails">${MAIL_FILTERS.map(([k, label]) => `
        <button class="${ws.mailFilter === k ? "active" : ""}" data-mf="${k}">${label}${k !== "All" && counts[k] ? ` ${counts[k]}` : ""}</button>`).join("")}</div>
      <div class="row">
        ${counts.New ? `<button class="btn primary sm" id="process-mail">${icon("sparkles")} Process ${counts.New} new email${counts.New === 1 ? "" : "s"}</button>` : ""}
        <button class="btn sm" id="gmail-import">${icon("mail")} Import from Gmail</button>
        <button class="btn sm" id="paste-mail">${icon("plus")} Paste an email</button>
      </div>
    </div>
    <div class="inbox">
      <div class="card mail-list" role="list">${msgs.map((m) => `
        <button class="mail-item ${m.id === ws.mailId ? "active" : ""}" data-mail="${esc(m.id)}" role="listitem">
          <div class="row-between"><span class="from">${esc(m.from_name)}</span><span class="tiny faint">${esc(fmtWhen(m.received_at))}</span></div>
          <div class="subj">${m.urgency === "Critical" || m.urgency === "High" ? `<span class="dot ${m.urgency === "Critical" ? "red" : "orange"}" style="margin-right:6px"></span>` : ""}${esc(m.subject)}</div>
          <div class="sum">${esc(m.ai_summary || m.body.slice(0, 140))}</div>
        </button>`).join("") || `<div class="empty small">No emails here.</div>`}</div>
      <div id="mail-detail">${mailDetail(ws.inbox.messages.find((m) => m.id === ws.mailId), icons)}</div>
    </div>`);
  els.body.querySelectorAll("[data-mf]").forEach((b) => b.addEventListener("click", () => { ws.mailFilter = b.dataset.mf; ws.mailId = null; inboxTab(); }));
  els.body.querySelectorAll("[data-mail]").forEach((b) => b.addEventListener("click", () => { ws.mailId = b.dataset.mail; inboxTab(); }));
  els.body.querySelector("#paste-mail").addEventListener("click", () => openPasteEmail(ws.id, async (r) => {
    ws.mailFilter = "All"; ws.mailId = r.message_id; await refresh();
  }));
  const afterGmail = async (r) => { ws.mailFilter = "All"; ws.mailId = r.message_ids?.[0] || null; ws.inbox = null; ws.brief = null; await refresh(); };
  els.body.querySelector("#gmail-import").addEventListener("click", () => openGmail(ws.id, ws.ev.event.name, afterGmail));
  reopenIfReturning(ws.id, ws.ev.event.name, afterGmail);
  els.body.querySelector("#process-mail")?.addEventListener("click", async (e) => {
    busy(e.target.closest("button"), true, `Reading ${counts.New} emails…`);
    try {
      const r = await post(`${E()}/inbox/process`);
      toast(`Processed ${r.processed} emails: ${r.needs_reply} need a reply, ${r.flags_raised} flags, ${r.changes_drafted} drafted changes${r.quarantined ? `, ${r.quarantined} quarantined` : ""}`);
      ws.brief = null;
    } catch (err) { toast(err.message, "bad"); }
    ws.mailFilter = "Needs Reply";
    await refresh();
  });
  bindMailDetail();
}

function mailDetail(m, icons) {
  if (!m) return `<div class="card empty">Pick an email to see what it means for your event.</div>`;
  const pending = m.changes.filter((c) => c.status === "Pending");
  const decided = m.changes.filter((c) => c.status !== "Pending");
  return `
    <div class="card card-pad stack">
      <div>
        <div class="row wrap" style="margin-bottom:6px">
          ${m.category ? `<span class="pill ${m.category === "Suspicious" ? "red" : m.category === "Noise" ? "" : "blue"}">${icon(icons[m.category] || "mail")} ${esc(m.category)}</span>` : `<span class="pill">Not processed yet</span>`}
          ${m.urgency ? `<span class="pill ${sevTone(m.urgency)}">${esc(m.urgency)}</span>` : ""}
          ${m.dimension ? `<span class="pill">${esc(TABS.find((t) => t.id === DIM_TAB[m.dimension])?.label || m.dimension)}</span>` : ""}
        </div>
        <h2>${esc(m.subject)}</h2>
        <div class="small muted" style="margin-top:3px">${esc(m.from_name)} &lt;${esc(m.from_email)}&gt; · ${esc(fmtWhen(m.received_at))}</div>
      </div>
      ${m.ai_summary ? `<div style="background:var(--accent-bg);border-radius:10px;padding:12px 14px"><div class="tiny" style="color:var(--accent);font-weight:600;margin-bottom:2px">${icon("sparkles")} What this means</div>${esc(m.ai_summary)}
        ${m.key_facts?.length ? `<ul class="small" style="margin:8px 0 0;padding-left:18px">${m.key_facts.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}</div>` : ""}
      ${m.flags.length ? `<div>${m.flags.map((f) => `<div class="list-row"><span class="sev ${f.severity}"></span><div class="grow"><div style="font-weight:500">${esc(f.title)}</div>
        <div class="small muted">${esc(f.detail)}</div></div><span class="pill ${f.status === "Open" ? sevTone(f.severity) : "green"}">${f.status === "Open" ? esc(f.severity) : esc(f.status)}</span></div>`).join("")}</div>` : ""}
      ${RESOLVABLE.has(m.dimension) && m.status !== "Quarantined" && m.category !== "Noise" && m.status !== "New" ? `<div class="row wrap" style="gap:6px">
        <span class="small muted grow">Day-of problem with the schedule, a room, or catering?</span>
        <button class="btn sm" id="resolve-mail">${icon("alert")} Resolve with the agents</button></div>` : ""}
      ${pending.length ? `<div class="stack" style="gap:8px"><h4>Suggested changes</h4>${pending.map((c) => changeCard(c, { showOrigin: false })).join("")}</div>` : ""}
      ${decided.length ? `<div class="small muted">${decided.map((c) => `${c.status === "Applied" ? "✓" : "✕"} ${esc(c.summary.split(":")[0])} (${esc(c.status.toLowerCase())})`).join("<br>")}</div>` : ""}
      ${m.status !== "Quarantined" && m.status !== "New" && m.category !== "Noise" ? `
        <div><label class="label" for="reply">Reply draft</label>
          <textarea class="textarea" id="reply" rows="7" placeholder="No reply needed. Write one if you want.">${esc(m.reply_draft)}</textarea>
          <div class="row wrap" style="margin-top:8px">
            <button class="btn sm" id="save-reply">Save draft</button>
            <button class="btn sm" id="copy-reply">${icon("copy")} Copy</button>
            <a class="btn sm" id="mail-reply" href="mailto:${esc(m.from_email)}?subject=${encodeURIComponent(`Re: ${m.subject}`)}&body=${encodeURIComponent(m.reply_draft)}">${icon("mail")} Open in email app</a>
            ${m.status !== "Done" ? `<button class="btn primary sm" id="mark-done" style="margin-left:auto">${icon("check")} Mark as handled</button>` : `<span class="pill green" style="margin-left:auto">Handled</span>`}
          </div>
          <p class="tiny faint" style="margin-top:6px">Stagecraft never sends email for you.</p></div>` : ""}
      ${m.status === "Quarantined" ? `<div class="small" style="background:var(--red-bg);color:var(--red);border-radius:10px;padding:12px">This email was quarantined before any AI read it. Don't reply, pay, or forward anything. Verify the sender another way.</div>` : ""}
      <details><summary class="small muted" style="cursor:pointer">Original email</summary><div class="mail-body" style="margin-top:8px">${esc(m.body)}</div></details>
    </div>`;
}

function bindMailDetail() {
  const id = ws.mailId;
  const reply = document.getElementById("reply");
  const update = async (fields, msg) => {
    try { await post(`${E()}/inbox/${id}`, fields); toast(msg); ws.inbox = null; await refresh(); }
    catch (err) { toast(err.message, "bad"); }
  };
  document.getElementById("save-reply")?.addEventListener("click", () => update({ reply_draft: reply.value }, "Draft saved"));
  document.getElementById("resolve-mail")?.addEventListener("click", () => {
    const m = ws.inbox.messages.find((x) => x.id === id);
    resolveFrom({ text: `Email from ${m.from_name} (${m.subject}):\n${m.body}`, label: `the email “${m.subject}”`, message_id: m.id });
  });
  document.getElementById("mark-done")?.addEventListener("click", () => update({ status: "Done", reply_draft: reply.value }, "Marked as handled"));
  document.getElementById("copy-reply")?.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(reply.value); toast("Copied"); } catch { toast("Copy isn't available here", "bad"); }
  });
  reply?.addEventListener("input", () => {
    const m = ws.inbox.messages.find((x) => x.id === id);
    document.getElementById("mail-reply").href = `mailto:${m.from_email}?subject=${encodeURIComponent(`Re: ${m.subject}`)}&body=${encodeURIComponent(reply.value)}`;
  });
}
