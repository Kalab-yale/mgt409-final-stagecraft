// Stagecraft app shell: hash routing and the Home screen ("Your events").
import { api, daysLabel, esc, fmtDate, fmtLong, money, pct } from "./core.js";
import { icon } from "./icons.js";
import { openNewEvent } from "./modals.js";
import { showSettings } from "./settings.js";
import { showWorkspace } from "./workspace.js";

const app = document.getElementById("app");
const crumb = document.getElementById("crumb");

function route() {
  const [, page, eventId, tab] = (location.hash || "#/").slice(1).split("/");
  document.querySelector("[data-settings-link]")?.classList.toggle("active-soft", page === "settings");
  if (page === "event" && eventId) {
    showWorkspace(app, crumb, decodeURIComponent(eventId), tab || "overview");
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
  let data;
  try {
    data = await api("/api/home");
  } catch (err) {
    app.innerHTML = `<div class="page"><div class="card card-pad">Couldn't load your events: ${esc(err.message)}</div></div>`;
    return;
  }
  const t = document.getElementById("today");
  t.dataset.iso = data.today;
  t.textContent = `Today: ${fmtLong(data.today)}`;
  app.innerHTML = `
    <div class="page">
      <div class="page-head">
        <div>
          <h1>Your events</h1>
          <p class="muted" style="margin-top:4px">Pick an event to see its dashboard, red flags, inbox, and assistants.</p>
        </div>
        <button class="btn primary" id="new-event">${icon("plus")} New event</button>
      </div>
      <div class="event-grid">
        ${data.events.map((e) => eventCard(e, data.today)).join("") || `<div class="card empty">No events yet. Create your first one.</div>`}
      </div>
    </div>`;
  document.getElementById("new-event").addEventListener("click", () => openNewEvent());
}

function eventCard(e, today) {
  const h = e.health;
  const urgent = h.flags.Critical + h.flags.High;
  const next = h.next_deadlines[0];
  const regPct = pct(h.registrations.active, h.registrations.cap);
  return `
    <a class="card event-card" href="#/event/${encodeURIComponent(e.id)}/overview">
      <div class="row-between" style="align-items:flex-start">
        <div class="grow">
          <h2 style="font-size:17px">${esc(e.name)}</h2>
          <div class="meta">${esc(fmtDate(e.date))} · ${esc(e.venue_city)} · ${esc(e.venue_name)}</div>
        </div>
        <span class="pill ${h.phase === "Final week" ? "orange" : h.phase === "Planning" ? "blue" : ""}">${esc(h.phase)}</span>
      </div>
      <div class="stats">
        <div><div class="stat-big num">${h.days_to_go >= 0 ? h.days_to_go : "Done"}</div><div class="stat-label">${h.days_to_go >= 0 ? "days to go" : "event passed"}</div></div>
        <div><div class="stat-big num">${h.progress_pct}%</div><div class="stat-label">planned</div></div>
        <div><div class="stat-big num" style="color:${urgent ? "var(--red)" : "inherit"}">${h.flags.total}</div><div class="stat-label">red flags${urgent ? `, ${urgent} urgent` : ""}</div></div>
      </div>
      <div class="bar ${h.progress_pct >= 70 ? "green" : ""}" aria-label="Planning progress"><span style="width:${h.progress_pct}%"></span></div>
      <div class="stack" style="gap:6px;margin-top:14px">
        <div class="row small">${icon("calendar")}<span class="grow">${next ? `Next: ${esc(next.title)} · <span class="${next.status === "Overdue" ? "" : "muted"}" style="${next.status === "Overdue" ? "color:var(--red);font-weight:500" : ""}">${next.status === "Overdue" ? "overdue" : esc(daysLabel(Math.round((new Date(next.due_date) - new Date(today)) / 864e5)))}</span>` : "No open deadlines"}</span></div>
        <div class="row small muted">${icon("attendees")}<span>${h.registrations.active} of ${h.registrations.cap} registered (${regPct}%)${h.registrations.waitlist ? ` · ${h.registrations.waitlist} waitlisted` : ""}</span></div>
        <div class="row small muted">${icon("budget")}<span>${money(h.budget.spent_usd)} of ${money(h.budget.planned_usd)} spent</span>${h.inbox_new ? `<span class="pill blue" style="margin-left:auto">${h.inbox_new} new emails</span>` : ""}</div>
      </div>
    </a>`;
}

window.addEventListener("hashchange", route);
api("/api/home").then((d) => { const t = document.getElementById("today"); t.dataset.iso = d.today; t.textContent = `Today: ${fmtLong(d.today)}`; }).catch(() => {});
route();
