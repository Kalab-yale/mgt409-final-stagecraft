// Stagecraft app shell: hash routing and the Home screen ("Your events").
import { showAccount } from "./account.js";
import { showLive } from "./live.js";
import { loadUser, session, showAuth } from "./auth.js";
import { api, daysLabel, esc, fmtDate, fmtLong, money, pct, post, toast } from "./core.js";
import { ring, SERIES } from "./charts.js";
import { icon } from "./icons.js";
import { avatarStack } from "./people.js";
import { openNewEvent } from "./modals.js";
import { showSettings } from "./settings.js";
import { initTheme } from "./theme.js";
import { showWorkspace } from "./workspace.js";

const app = document.getElementById("app");
const crumb = document.getElementById("crumb");

async function route() {
  const [, page, eventId, tab] = (location.hash || "#/").slice(1).split("/");
  if (page === "login" || page === "signup") {
    if (session.user || (await loadUser())) { location.hash = "#/"; return; }
    showAuth(app, crumb, page);
    return;
  }
  if (!session.user && !(await loadUser())) { location.hash = "#/login"; return; }
  const today = document.getElementById("today");
  if (!today.textContent) api("/api/home").then((d) => { today.dataset.iso = d.today; today.textContent = `Today: ${fmtLong(d.today)}`; }).catch(() => {});
  if (page === "event" && eventId && tab === "live") {
    showLive(app, crumb, decodeURIComponent(eventId));
  } else if (page === "event" && eventId) {
    showWorkspace(app, crumb, decodeURIComponent(eventId), tab || "overview");
  } else if (page === "account") {
    showAccount(app, crumb, eventId);  // #/account/{section}
  } else if (page === "settings") {
    showSettings(app, crumb, eventId);  // #/settings/{section}
  } else {
    showHome();
  }
}

async function showHome() {
  crumb.innerHTML = "";
  document.title = "Your events · Stagecraft";
  app.innerHTML = `<div class="page"><div class="skeleton" style="width:220px;height:28px"></div></div>`;
  let data, work;
  try {
    [data, work] = await Promise.all([api("/api/home"), api("/api/me/work")]);
  } catch (err) {
    app.innerHTML = `<div class="page"><div class="card card-pad">Couldn't load your events: ${esc(err.message)}</div></div>`;
    return;
  }
  const t = document.getElementById("today");
  t.dataset.iso = data.today;
  t.textContent = `Today: ${fmtLong(data.today)}`;
  const ev = data.events, today = data.today;
  const urgent = ev.reduce((a, e) => a + e.health.flags.Critical + e.health.flags.High, 0);
  const thisWeek = ev.reduce((a, e) => a + e.upcoming.filter((d) => d.status !== "Overdue" && dayDiff(d.due_date, today) <= 7).length, 0);
  const overdue = ev.reduce((a, e) => a + e.upcoming.filter((d) => d.status === "Overdue").length, 0);
  const unread = ev.reduce((a, e) => a + e.health.inbox_new, 0);
  const first = ev[0];
  const hour = new Date().getHours();
  const hello = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const link = (tab) => (first ? `#/event/${encodeURIComponent(first.id)}/${tab}` : "#/");
  app.innerHTML = `
    <div class="page home">
      <section class="hero">
        <div class="hero-glow g1"></div><div class="hero-glow g2"></div><div class="hero-glow g3"></div>
        <div class="hero-copy">
          <div class="hero-date">${esc(fmtLong(today))}</div>
          <h1>${hello}, ${esc(session.user?.first_name || "there")}.</h1>
          <p>${ev.length ? `You're running ${ev.length} event${ev.length === 1 ? "" : "s"}. Here's what needs you.` : "Create your first event to get started."}</p>
          <div class="hero-chips">
            <a class="hchip ${urgent ? "hot" : ""}" href="${link("overview")}">${icon("flag")}<b>${urgent}</b> urgent red flag${urgent === 1 ? "" : "s"}</a>
            <a class="hchip ${overdue ? "hot" : ""}" href="${link("overview")}">${icon("clock")}<b>${overdue}</b> overdue</a>
            <a class="hchip" href="${link("overview")}">${icon("calendar")}<b>${thisWeek}</b> due this week</a>
            <a class="hchip" href="${link("inbox")}">${icon("inbox")}<b>${unread}</b> unread email${unread === 1 ? "" : "s"}</a>
          </div>
        </div>
      </section>

      ${myWork(work, today)}
      ${ev.length ? timeline(ev, today) : ""}

      <div class="row-between" style="margin:30px 0 14px"><h2>Your events</h2><button class="btn primary" id="new-event">${icon("plus")} New event</button></div>
      <div class="event-grid">
        ${ev.map((e, i) => eventCard(e, i, today)).join("")}
        <button class="card new-card" id="new-event-2">${icon("plus", "lg")}<b>Plan a new event</b><span class="small muted">Describe it, import a schedule, or copy one you've run.</span></button>
      </div>
    </div>`;
  document.getElementById("new-event").addEventListener("click", () => openNewEvent());
  app.querySelectorAll("[data-mw-task]").forEach((c) => c.addEventListener("change", async () => {
    const [eventId, taskId] = c.dataset.mwTask.split("|");
    try { await post(`/api/events/${encodeURIComponent(eventId)}/records/task/${taskId}`, { changes: { status: "Done" } }); toast("Nice, marked done"); showHome(); }
    catch (err) { toast(err.message, "bad"); c.checked = false; }
  }));
  document.getElementById("new-event-2").addEventListener("click", () => openNewEvent());
}

// Everything assigned to the signed-in person, across events (they are linked to a team by their email).
function myWork(w, today) {
  if (!w.memberships.length) {
    return `<section class="card card-pad mywork empty-work">${icon("team")}<div><b>You're not on an organizing team yet.</b>
      <p class="small muted">Ask the PMO to add you on an event's Team tab using your email (${esc(session.user?.email || "")}). Your tasks and alerts will show up here.</p></div></section>`;
  }
  const rows = [
    ...w.tasks.map((t) => ({ ...t, kind: "task" })),
    ...w.deadlines.map((d) => ({ ...d, kind: "deadline", overdue: d.status === "Overdue" })),
  ].sort((a, b) => (a.due_date || "9999").localeCompare(b.due_date || "9999")).slice(0, 8);
  const due = (r) => !r.due_date ? "no due date" : r.overdue ? "overdue" : daysLabel(dayDiff(r.due_date, today));
  return `
    <section class="card mywork">
      <div class="card-head"><h3>${icon("user")} My work</h3>
        <span class="muted small">${w.tasks.length} open task${w.tasks.length === 1 ? "" : "s"} · ${w.deadlines.length} deadline${w.deadlines.length === 1 ? "" : "s"} you own</span></div>
      <div class="card-body">
        ${w.approvals.map((a) => `<a class="approve-banner" href="#/event/${encodeURIComponent(a.event_id)}/overview">${icon("clipboard")}
          <span class="grow"><b>${a.count} change${a.count === 1 ? "" : "s"} waiting for your OK</b> <span class="muted">· ${esc(a.event_name)}</span></span>Review ${icon("next")}</a>`).join("")}
        ${rows.map((r) => `
          <div class="list-row mw-row">
            ${r.kind === "task" ? `<input type="checkbox" class="checkbox" data-mw-task="${esc(r.event_id)}|${esc(r.id)}" aria-label="Mark ${esc(r.title)} done">` : `<span class="mw-dl" title="Deadline">${icon("calendar")}</span>`}
            <a class="grow mw-title" href="#/event/${encodeURIComponent(r.event_id)}/${r.kind === "task" ? "team" : "overview"}">
              <span>${esc(r.title)}</span><span class="tiny muted">${esc(r.event_name)}${r.shared_with ? ` · shared with ${r.shared_with} other${r.shared_with === 1 ? "" : "s"}` : ""}${r.kind === "deadline" ? " · deadline" : ""}</span></a>
            ${r.priority === "High" ? `<span class="pill orange tiny">High</span>` : ""}
            <span class="tiny ${r.overdue ? "red-text" : "muted"}" style="white-space:nowrap">${esc(due(r))}</span>
          </div>`).join("") || `<p class="small muted">${icon("check")} Nothing on your plate. Enjoy it.</p>`}
      </div>
    </section>`;
}

const dayDiff = (a, b) => Math.round((new Date(`${a}T12:00:00`) - new Date(`${b}T12:00:00`)) / 864e5);
const EVENT_COLORS = SERIES;  // fixed order: event 1 = slot 1, event 2 = slot 2 ...

// The next three weeks across every event: one dot per deadline, event days as flags, overdue items in the margin.
function timeline(events, today) {
  const DAYS = 21;
  const items = events.flatMap((e, i) => e.upcoming.map((d) => ({ ...d, event: e, color: EVENT_COLORS[i % EVENT_COLORS.length] })));
  const late = items.filter((d) => d.status === "Overdue");
  const soon = items.filter((d) => d.status !== "Overdue");
  const byDay = {};
  soon.forEach((d) => (byDay[dayDiff(d.due_date, today)] ??= []).push(d));
  const pos = (n) => `${(100 * (n + 0.5)) / (DAYS + 1)}%`;
  const ticks = Array.from({ length: DAYS + 1 }, (_, n) => n).filter((n) => n % 7 === 0);
  const tickDate = (n) => { const d = new Date(`${today}T12:00:00`); d.setDate(d.getDate() + n); return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }); };
  return `
    <section class="card tl-card">
      <div class="card-head"><h3>${icon("calendar")} The next three weeks</h3>
        <div class="legend">${events.map((e, i) => `<span><i style="background:${EVENT_COLORS[i % EVENT_COLORS.length]}"></i>${esc(e.name)}</span>`).join("")}</div></div>
      <div class="card-body tl-body">
        <div class="tl-late">${late.length ? `<div class="tl-late-n">${late.length}</div><div class="tiny">overdue</div>
          ${late.slice(0, 3).map((d) => `<a class="tl-late-item" href="#/event/${encodeURIComponent(d.event.id)}/overview" title="${esc(d.event.name)}"><i style="background:${d.color}"></i>${esc(d.title)}</a>`).join("")}`
          : `<div class="tl-late-n ok">0</div><div class="tiny">overdue</div>`}</div>
        <div class="tl">
          <div class="tl-axis"></div>
          ${ticks.map((n) => `<div class="tl-tick" style="left:${pos(n)}"><span>${n === 0 ? "Today" : esc(tickDate(n))}</span></div>`).join("")}
          ${events.map((e, i) => { const n = dayDiff(e.date, today); return n >= 0 && n <= DAYS ? `<div class="tl-event" style="left:${pos(n)};--c:${EVENT_COLORS[i % EVENT_COLORS.length]}"><span>${icon("flag")} Event day</span></div>` : ""; }).join("")}
          ${Object.entries(byDay).map(([n, ds]) => `<div class="tl-col" style="left:${pos(Number(n))}">${ds.map((d) => `
            <a class="tl-dot" href="#/event/${encodeURIComponent(d.event.id)}/overview" style="background:${d.color}"
               data-tip-value="${esc(d.title)}" data-tip-label="${esc(d.event.name)} · ${esc(fmtDate(d.due_date))}" data-tip-color="${d.color}" aria-label="${esc(d.title)}, ${esc(fmtDate(d.due_date))}"></a>`).join("")}</div>`).join("")}
        </div>
      </div>
    </section>`;
}

// Generated cover art: a stage with spotlights, colored per event, with the city on it.
function cover(e, i) {
  const h1 = [226, 168, 280, 18, 330][i % 5], h2 = (h1 + 38) % 360, glow = (h1 + 190) % 360;
  const seed = [...e.id].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 997, 7);
  const lights = [0, 1, 2].map((k) => {
    const x = 70 + k * 140 + ((seed * (k + 2)) % 40) - 20, tilt = (k - 1) * 38 + ((seed + k * 13) % 24) - 12;
    return `<polygon points="${x},0 ${x + tilt - 58},150 ${x + tilt + 58},150" fill="url(#cone${i})"/>
      <circle cx="${x}" cy="2" r="9" fill="hsl(${glow} 100% 88%)" opacity=".9"/><circle cx="${x}" cy="2" r="26" fill="url(#halo${i})"/>`;
  }).join("");
  const crowd = Array.from({ length: 30 }, (_, k) => `<circle cx="${k * 14.5}" cy="${146 - ((k * seed) % 6)}" r="${8 + ((k + seed) % 3)}" fill="hsl(${h2} 60% 9%)" opacity=".75"/>`).join("");
  return `<div class="cover" aria-hidden="true">
    <svg viewBox="0 0 420 150" preserveAspectRatio="xMidYMid slice">
      <defs>
        <linearGradient id="bg${i}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl(${h1} 68% 30%)"/><stop offset="1" stop-color="hsl(${h2} 62% 15%)"/></linearGradient>
        <linearGradient id="cone${i}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="hsl(${glow} 100% 85%)" stop-opacity=".42"/><stop offset=".85" stop-color="hsl(${glow} 100% 85%)" stop-opacity="0"/></linearGradient>
        <radialGradient id="halo${i}"><stop offset="0" stop-color="hsl(${glow} 100% 85%)" stop-opacity=".55"/><stop offset="1" stop-color="hsl(${glow} 100% 85%)" stop-opacity="0"/></radialGradient>
        <radialGradient id="floor${i}" cx=".5" cy="1" r=".7"><stop offset="0" stop-color="hsl(${glow} 90% 80%)" stop-opacity=".35"/><stop offset="1" stop-color="hsl(${glow} 90% 80%)" stop-opacity="0"/></radialGradient>
      </defs>
      <rect width="420" height="150" fill="url(#bg${i})"/>
      <rect y="70" width="420" height="80" fill="url(#floor${i})"/>${lights}${crowd}
    </svg>
    <div class="cover-city">${icon("pin")} ${esc(e.venue_city)}</div>
    <div class="cover-days"><b>${e.health.days_to_go >= 0 ? e.health.days_to_go : "Done"}</b><span>${e.health.days_to_go >= 0 ? "days to go" : ""}</span></div>
  </div>`;
}

function eventCard(e, i, today) {
  const h = e.health;
  const urgent = h.flags.Critical + h.flags.High;
  const regPct = pct(h.registrations.active, h.registrations.cap);
  const pmo = e.team.find((m) => m.is_pmo);
  return `
    <a class="card event-card" href="#/event/${encodeURIComponent(e.id)}/overview">
      ${cover(e, i)}
      <div class="event-body">
        <div class="row-between" style="align-items:flex-start;gap:10px">
          <div class="grow"><h2 style="font-size:17px;line-height:1.3">${esc(e.name)}</h2>
            <div class="meta">${esc(fmtDate(e.date))} · ${esc(e.venue_name)}</div></div>
          <span class="pill ${h.phase === "Final week" ? "orange" : "blue"}">${esc(h.phase)}</span>
        </div>
        <div class="event-stats">
          ${ring(h.progress_pct, { size: 64, stroke: 7, label: `${h.progress_pct}%`, sub: "planned", tipLabel: "Planning progress" })}
          <div class="es"><b style="color:${urgent ? "var(--red)" : "inherit"}">${h.flags.total}</b><span>red flags${urgent ? `, ${urgent} urgent` : ""}</span></div>
          <div class="es"><b>${regPct}%</b><span>${h.registrations.active} of ${h.registrations.cap} registered</span></div>
          <div class="es"><b>${Math.round(pct(h.budget.spent_usd, h.budget.planned_usd))}%</b><span>of ${money(h.budget.planned_usd)} spent</span></div>
        </div>
        <div class="event-next">${e.upcoming.slice(0, 3).map((d) => `
          <div class="row small"><span class="dot ${d.status === "Overdue" ? "red" : d.status === "Due soon" ? "orange" : "gray"}"></span><span class="grow ellipsis">${esc(d.title)}</span>
            <span class="${d.status === "Overdue" ? "red-text" : "muted"} tiny" style="white-space:nowrap">${d.status === "Overdue" ? "overdue" : esc(daysLabel(dayDiff(d.due_date, today)))}</span></div>`).join("") || `<div class="small muted">No deadlines in the next three weeks.</div>`}</div>
        <div class="row-between event-foot">
          <span class="row" style="gap:8px">${avatarStack(e.team.map((m, k) => k), Object.fromEntries(e.team.map((m, k) => [k, m])), { max: 5 })}
            <span class="tiny muted">${pmo ? `PMO ${esc(pmo.name.split(" ")[0])}` : `${e.team.length} people`}</span></span>
          ${h.inbox_new ? `<span class="pill blue">${icon("inbox")} ${h.inbox_new} new</span>` : ""}
        </div>
      </div>
    </a>`;
}

// Back from Google's "Connect Gmail" screen: say how it went, then tidy the address bar.
const qs = new URLSearchParams(location.search);
if (qs.has("gmail") || qs.has("gmail_error")) {
  setTimeout(() => toast(qs.has("gmail") ? "Gmail connected (read-only). Pick dates and import." : qs.get("gmail_error"), qs.has("gmail") ? "" : "bad"), 600);
  history.replaceState(null, "", location.pathname + location.hash);
}

window.addEventListener("hashchange", route);
initTheme(route);  // re-render the current screen when the theme changes (charts bake in their colours)
route();
