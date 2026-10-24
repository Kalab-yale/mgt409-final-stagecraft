/* Stagecraft AI - Mission Control workspace (vanilla JS, no build step). */
"use strict";

const state = { events: [], venues: [], eventId: null, data: null, weather: null, track: "All", room: "All", view: "ros" };

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => "$" + Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
const icons = () => window.lucide && window.lucide.createIcons();

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.detail || body.error || (body.errors && body.errors[0]) || `HTTP ${res.status}`);
    err.body = body;
    err.status = res.status;
    throw err;
  }
  return body;
}

function toast(message, tone = "info") {
  const el = document.createElement("div");
  const dot = { ok: "#10B981", bad: "#EF4444", warn: "#F59E0B", info: "#6366F1" }[tone];
  el.className = "toast flex items-start gap-2";
  el.innerHTML = `<span class="status-dot mt-1.5" style="background:${dot}"></span><span>${esc(message)}</span>`;
  $("#toast-root").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

const loadColor = (pct) => (pct > 100 ? "#EF4444" : pct >= 95 ? "#F59E0B" : "#10B981");  // red over capacity, amber at 95%+
const statusPill = (status) =>
  status === "Operational" ? `<span class="pill pill-ok">Operational</span>`
  : status === "AV Degraded" ? `<span class="pill pill-bad">AV Degraded</span>`
  : `<span class="pill pill-bad">${esc(status)}</span>`;
const speakerDot = (status) => ({ "Confirmed On-Site": "#10B981", "In Transit": "#F59E0B", Delayed: "#EF4444" }[status] || "#64748b");
const TRACK_STYLE = {
  Keynote: "pill-accent",
  "Venture & Capital": "pill-ok",
  "Tech & AI": "pill-muted text-sky-300 border-sky-500/40",
  "Founder Workshop": "pill-muted text-pink-300 border-pink-500/40",
  "Catering & Networking": "pill-warn",
};

// ---------- sidebar ----------
async function loadEvents(selectId) {
  const data = await api("/api/events");
  state.events = data.events;
  state.venues = data.venues;
  $("#cost-saver").checked = data.cost_saver_mode;  // War-Room checkbox follows the Settings toggle
  $("#event-count").textContent = `${state.events.length}`;
  const wanted = selectId || state.eventId || localStorage.getItem("stagecraft.event");
  const target = state.events.find((e) => e.id === wanted) || state.events.find((e) => e.status === "Live War-Room") || state.events[0];
  renderEventList(target && target.id);
  fillModalSelects();
  if (target) await selectEvent(target.id);
}

function renderEventList(activeId) {
  $("#event-list").innerHTML = state.events.map((e) => `
    <button class="event-item ${e.id === activeId ? "active" : ""}" data-event="${esc(e.id)}" aria-current="${e.id === activeId}">
      <div class="flex items-start justify-between gap-2">
        <span class="text-[13px] font-medium text-slate-100 leading-snug">${esc(e.name)}</span>
        ${e.status === "Live War-Room" ? '<span class="live-dot mt-1.5 shrink-0" title="Live War-Room"></span>' : ""}
      </div>
      <div class="mt-1 flex items-center gap-2 text-[11px] text-slate-500">
        <span class="font-mono">${esc(e.date)}</span><span>·</span><span class="truncate">${esc(e.venue_city)}</span>
      </div>
      <div class="mt-1.5"><span class="pill ${e.status === "Live War-Room" ? "pill-ok" : "pill-muted"}">${esc(e.status)}</span></div>
    </button>`).join("");
  document.querySelectorAll(".event-item").forEach((b) => b.addEventListener("click", () => selectEvent(b.dataset.event)));
}

// ---------- workspace ----------
async function selectEvent(eventId) {
  state.eventId = eventId;
  state.track = "All";
  state.room = "All";
  localStorage.setItem("stagecraft.event", eventId);
  renderEventList(eventId);
  try {
    state.data = await api(`/api/events/${encodeURIComponent(eventId)}`);
  } catch (err) {
    toast(`Could not load event: ${err.message}`, "bad");
    return;
  }
  $("#main-scroll").scrollTop = 0;
  renderTelemetry();
  renderRoomDeck();
  renderFilters();
  renderTimetable();
  renderCrisisDeck();
  loadWeather(eventId);
}

function renderTelemetry() {
  const { event, venue, catering_slas: slas, active_conflicts: conflicts } = state.data;
  $("#event-name").textContent = event.name;
  document.title = `${event.name} · Stagecraft AI`;
  const status = $("#event-status");
  status.className = `pill ${event.status === "Live War-Room" ? "pill-ok" : "pill-muted"}`;
  status.textContent = event.status;
  $("#venue-line").textContent = `${venue.name} · ${venue.city}, ${venue.state_or_country}`;
  $("#corridor-line").textContent = venue.airport_corridor || "corridor not set";
  $("#date-line").textContent = event.date;
  $("#attendee-count").textContent = Number(event.attendee_count).toLocaleString();
  $("#public-link").href = `/live/${encodeURIComponent(event.id)}`;

  const sla = [...slas].sort((a, b) => b.late_penalty_usd - a.late_penalty_usd)[0];
  $("#catering-badge").innerHTML = sla
    ? `<span>${esc(sla.meal_name)}</span><span class="font-mono text-warn">cutoff ${esc(sla.hard_cutoff_time)}</span>
       <span class="font-mono text-xs text-slate-400" title="Penalty if service misses the hard cutoff">${money(sla.late_penalty_usd)} risk</span>
       <span id="sla-countdown" class="font-mono text-[11px] text-slate-500"></span>`
    : `<span class="text-slate-500">none</span>`;
  updateCountdown();

  const n = conflicts.length;
  $("#conflict-badge").title = n ? conflicts.map((c) => `${c.type}: ${c.detail}`).join("\n") : "No double-bookings, overflows, delayed speakers, or sessions in degraded rooms";
  $("#conflict-badge").innerHTML = n
    ? `<span class="status-dot" style="background:#EF4444"></span><span class="text-red-300">${n} active</span>`
    : `<span class="status-dot" style="background:#10B981"></span><span class="text-emerald-300">0 · clear</span>`;
}

function updateCountdown() {
  const el = $("#sla-countdown");
  if (!el || !state.data) return;
  const sla = [...state.data.catering_slas].sort((a, b) => b.late_penalty_usd - a.late_penalty_usd)[0];
  if (!sla) return;
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const eventDate = state.data.event.date;
  if (today === eventDate) {
    const [h, m] = sla.hard_cutoff_time.split(":").map(Number);
    const mins = h * 60 + m - (now.getHours() * 60 + now.getMinutes());
    el.textContent = mins >= 0 ? `T-${mins}m` : "passed";
  } else {
    const days = Math.round((new Date(eventDate + "T00:00:00") - new Date(today + "T00:00:00")) / 86400000);
    el.textContent = days > 0 ? `T-${days}d` : "past";
  }
}

async function loadWeather(eventId) {
  const badge = $("#weather-badge");
  badge.innerHTML = `<span class="text-slate-500">checking live…</span>`;
  try {
    const w = await api(`/api/events/${encodeURIComponent(eventId)}/weather`);
    if (state.eventId !== eventId) return;
    state.weather = w;
    const color = { LOW: "pill-ok", MODERATE: "pill-warn", HIGH: "pill-bad" }[w.transit_delay_risk];
    const c = w.conditions;
    badge.innerHTML = `<span class="pill ${color} font-mono">${esc(w.transit_delay_risk)}</span>
      <span class="font-mono">${Math.round(c.temperature_c)}°C</span><span class="text-slate-400 text-xs">${esc(c.description)}</span>
      <span class="font-mono text-[10px] text-slate-500" title="${esc(w.advisory)}">${w.source === "open-meteo-live" ? "live" : "cached"}</span>`;
    badge.title = `${w.advisory}\n${(w.risk_factors || []).join("; ")}`;
  } catch (err) {
    badge.innerHTML = `<span class="text-slate-500">unavailable</span>`;
  }
}

function renderRoomDeck() {
  $("#room-deck").innerHTML = state.data.rooms.map((r) => {
    const pct = r.utilization_pct;
    return `
    <article class="room-card ${r.status !== "Operational" ? "degraded" : ""}">
      <div class="flex items-start justify-between gap-2">
        <div class="min-w-0">
          <h3 class="text-sm font-semibold text-white leading-snug">${esc(r.name)}</h3>
          <div class="mt-0.5 flex items-center gap-2 text-[11px] text-slate-500">
            <span>${esc(r.floor_or_zone)}</span>${r.is_catering_hub ? '<span class="pill pill-warn">catering hub</span>' : ""}
          </div>
        </div>
        ${statusPill(r.status)}
      </div>
      <div class="mt-3 flex items-baseline justify-between">
        <span class="text-[10px] tracking-widest text-slate-500 font-semibold whitespace-nowrap" title="Largest expected attendance in this room vs capacity">PEAK LOAD</span>
        <span class="font-mono text-xs whitespace-nowrap"><span style="color:${loadColor(pct)}">${pct}%</span>
          <span class="text-slate-500">· ${r.peak_attendance}/${r.capacity}</span></span>
      </div>
      <div class="bar mt-1.5"><span style="width:${Math.min(pct, 100)}%;background:${loadColor(pct)}"></span></div>
      <div class="mt-2 flex items-center justify-between text-[11px] text-slate-500">
        <span>capacity <span class="font-mono text-slate-300">${r.capacity}</span></span>
        <span><span class="font-mono text-slate-300">${r.session_count}</span> session${r.session_count === 1 ? "" : "s"}</span>
      </div>
      <div class="mt-2.5 flex flex-wrap gap-1">${r.av_specs.map((a) => `<span class="av-pill">${esc(a)}</span>`).join("")}</div>
    </article>`;
  }).join("");
}

function renderFilters() {
  const tracks = ["All", ...state.data.tracks];
  $("#track-filters").innerHTML = tracks.map((t) =>
    `<button class="chip ${t === state.track ? "active" : ""}" data-track="${esc(t)}" aria-pressed="${t === state.track}">${esc(t)}</button>`).join("");
  document.querySelectorAll("#track-filters .chip").forEach((b) => b.addEventListener("click", () => {
    state.track = b.dataset.track;
    renderFilters();
    renderTimetable();
  }));
  const sel = $("#room-filter");
  sel.innerHTML = `<option value="All">All rooms</option>` +
    state.data.rooms.map((r) => `<option value="${esc(r.id)}" ${r.id === state.room ? "selected" : ""}>${esc(r.name)}</option>`).join("");
  sel.onchange = () => { state.room = sel.value; renderTimetable(); };
}

function renderTimetable() {
  const rows = state.data.sessions.filter((s) =>
    (state.track === "All" || s.track === state.track) && (state.room === "All" || s.room.id === state.room));
  $("#session-count").textContent = `${rows.length}/${state.data.sessions.length} sessions`;
  if (!rows.length) {
    $("#ros-body").innerHTML = `<div class="p-8 text-center text-sm text-slate-500 bg-panel">No sessions match these filters.</div>`;
    return;
  }
  $("#ros-body").innerHTML = rows.map((s) => {
    const pct = s.utilization_pct ?? 0;
    const roomBad = s.room.status !== "Operational";
    const upd = s.war_room_update;
    return `
    <div class="ros-grid ros-row ${s.is_locked_keynote ? "locked" : ""} ${upd ? "updated" : ""}">
      <div class="font-mono text-sm text-white">${esc(s.start_time)}<span class="text-slate-500">–${esc(s.end_time)}</span></div>
      <div class="min-w-0">
        <div class="text-sm font-medium text-slate-100 leading-snug">${esc(s.title)}</div>
        <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span class="pill ${TRACK_STYLE[s.track] || "pill-muted"}">${esc(s.track)}</span>
          ${s.is_locked_keynote ? `<span class="pill pill-accent font-mono" title="Locked keynote: agents may not move or cancel it"><i data-lucide="shield-check" class="h-3 w-3"></i>LOCKED</span>` : ""}
          ${s.status === "Conflict Flagged" ? `<span class="pill pill-bad">Conflict Flagged</span>` : ""}
          ${upd ? `<span class="pill pill-ok" title="Was ${esc(upd.from)} → now ${esc(upd.to)}"><i data-lucide="radio-tower" class="h-3 w-3"></i>Rescheduled · Updated via Stagecraft War-Room</span>`
                : s.status === "Rescheduled" ? `<span class="pill pill-ok">Rescheduled</span>` : ""}
        </div>
      </div>
      <div class="min-w-0">
        <div class="flex items-center gap-1.5 text-[13px] ${roomBad ? "text-red-300" : "text-slate-200"}">
          <span class="truncate">${esc(s.room.name)}</span>${roomBad ? `<span class="pill pill-bad">${esc(s.room.status)}</span>` : ""}
        </div>
        <div class="mt-1.5 flex items-center gap-2">
          <div class="bar flex-1"><span style="width:${Math.min(pct, 100)}%;background:${loadColor(pct)}"></span></div>
          <span class="font-mono text-[11px]" style="color:${loadColor(pct)}" title="Expected attendance / room capacity">${s.expected_attendance}/${s.room.capacity}</span>
        </div>
      </div>
      <div class="flex flex-wrap gap-1.5">
        ${s.speakers.length ? s.speakers.map((sp) => `
          <span class="speaker-chip" title="${esc(sp.vip_tier)} · ${esc(sp.status)} · ${esc(sp.travel_origin)}">
            <span class="status-dot" style="background:${speakerDot(sp.status)}"></span>${esc(sp.name)}
            <span class="tier">T${esc(sp.vip_tier.split(" ")[1])}</span>
          </span>`).join("") : `<span class="text-xs text-slate-600">—</span>`}
      </div>
      <div class="flex flex-wrap gap-1">
        ${s.required_av.map((a) => `<span class="av-pill ${s.av_gaps.includes(a) ? "gap" : ""}" ${s.av_gaps.includes(a) ? 'title="Room lacks this; needs a substitute or portable kit"' : ""}>${esc(a)}</span>`).join("") || `<span class="text-xs text-slate-600">—</span>`}
      </div>
    </div>`;
  }).join("");
  icons();
}

// ---------- view tabs ----------
document.querySelectorAll(".view-tab[data-view]").forEach((tab) => tab.addEventListener("click", () => {
  state.view = tab.dataset.view;
  document.querySelectorAll(".view-tab[data-view]").forEach((t) => {
    t.classList.toggle("active", t === tab);
    t.setAttribute("aria-selected", t === tab);
  });
  $("#view-ros").classList.toggle("hidden", state.view !== "ros");
  $("#view-warroom").classList.toggle("hidden", state.view !== "warroom");
}));

// ---------- New Event modal ----------
const AI_EXAMPLES = [
  "We are hosting the 'Global Climate & Grid Tech Forum 2026' on 2026-11-18 at the Moscone Center in San Francisco, CA for 500 attendees. Include a morning keynote, 3 breakout panels on battery storage, AI grid optimization, and climate venture capital, plus a hot lunch SLA.",
  "Africa Fintech Founders Summit on 2027-03-12 at the Skylight International Convention Center in Addis Ababa for 400 attendees: opening keynote, panels on mobile money and cross-border payments, a pitch showcase, and lunch.",
  "A 250-person Healthcare AI Day at the Boston Convention & Exhibition Center on 2027-04-20 with a keynote, clinical AI and digital health investing panels, a founder workshop, and a hosted lunch.",
];

function openModal(tab = "ai") {
  $("#modal-new-event").classList.remove("hidden");
  switchModalTab(tab);
  fillModalSelects();
  setTimeout(() => (tab === "ai" ? $("#ai-prompt") : $("#csv-name")).focus(), 50);
}
const closeModal = () => $("#modal-new-event").classList.add("hidden");

function switchModalTab(tab) {
  document.querySelectorAll(".modal-tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  document.querySelectorAll(".modal-pane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== tab));
}

function fillModalSelects() {
  const venueSel = $("#csv-venue");
  const prev = venueSel.value;
  venueSel.innerHTML = state.venues.map((v) => `<option value="${esc(v.id)}">${esc(v.name)} · ${esc(v.city)} (${v.rooms.length} rooms)</option>`).join("") +
    `<option value="__custom__">➕ Custom city / new venue…</option>`;
  if (prev) venueSel.value = prev;
  $("#clone-template").innerHTML = state.events.map((e) => `<option value="${esc(e.id)}">${esc(e.name)} · ${esc(e.venue_city)}</option>`).join("");
  if (!$("#csv-date").value) $("#csv-date").value = "2027-02-12";
  if (!$("#clone-date").value) $("#clone-date").value = "2027-11-13";
}

function sampleCsv() {
  const venueId = $("#csv-venue").value;
  const venue = state.venues.find((v) => v.id === venueId);
  const rooms = venue ? [...venue.rooms].sort((a, b) => b.capacity - a.capacity) : [];
  const hub = rooms.find((r) => r.is_catering_hub);
  const halls = rooms.filter((r) => !r.is_catering_hub);
  const big = halls[0] ? halls[0].name : "Main Hall";
  const b1 = halls[1] ? halls[1].name : "Breakout A";
  const b2 = halls[2] ? halls[2].name : "Breakout B";
  const lunch = hub ? hub.name : "Atrium";
  const cap = (name, fallback) => { const r = rooms.find((x) => x.name === name); return r ? Math.floor(r.capacity * 0.85) : fallback; };
  const q = (s) => (s.includes(",") ? `"${s}"` : s);
  return [
    "title,track,room_name,start_time,end_time,expected_attendance,speaker_name",
    `Opening Keynote: The Next Decade,Keynote,${q(big)},09:00,10:00,${cap(big, 280)},Dana Whitlock`,
    `Clinical AI in Practice,Tech & AI,${q(b1)},10:15,11:15,${cap(b1, 100)},Ravi Menon;Grace Osei`,
    `Funding the Next Wave,Venture & Capital,${q(b2)},10:15,11:15,${cap(b2, 90)},Lena Park`,
    `Lunch & Networking,Catering & Networking,${q(lunch)},12:00,13:00,${cap(lunch, 280)},`,
    `Closing Fireside,Keynote,${q(big)},13:15,14:00,${cap(big, 250)},Dana Whitlock;Lena Park`,
  ].join("\n");
}

function setBusy(button, busy) {
  button.disabled = busy;
  button.querySelector(".spinner").classList.toggle("hidden", !busy);
}

function showResult(target, ok, html) {
  $(target).innerHTML = `<div class="${ok ? "result-ok" : "result-bad"}">${html}</div>`;
  icons();
}

function errorList(err) {
  const b = err.body || {};
  const items = b.errors || (b.security && b.security.findings && b.security.findings.map((f) => `${f.category}: "${f.matched_text}"`)) ||
    (b.detail && (Array.isArray(b.detail) ? b.detail.map((d) => `${d.loc.slice(-1)}: ${d.msg}`) : [b.detail])) || [err.message];
  const head = b.blocked ? "Blocked by the security firewall" : "Could not create the event";
  return `<div class="font-medium text-red-200">${head}</div><ul class="mt-1.5 list-disc pl-5 space-y-0.5 text-red-200/90">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;
}

async function afterCreate(result, target) {
  const c = result.counts || {};
  const usage = result.usage ? ` · <span class="font-mono">${result.usage.total_tokens.toLocaleString()} tokens · $${result.usage.cost_usd.toFixed(5)}</span>` : "";
  showResult(target, true, `<div class="font-medium text-emerald-200">Created ${esc(result.event.name)}</div>
    <div class="mt-1 text-slate-300">${esc(result.venue.name)} · ${esc(result.venue.city)} · <span class="font-mono">${c.rooms} rooms · ${c.sessions} sessions · ${c.speakers} speakers</span>${usage}</div>`);
  toast(`Created ${result.event.name}`, "ok");
  await loadEvents(result.event_id);
  setTimeout(closeModal, 900);
}

$("#ai-examples").innerHTML = AI_EXAMPLES.map((t, i) =>
  `<button type="button" class="chip text-xs" data-example="${i}">${esc(t.split(" ").slice(0, 6).join(" "))}…</button>`).join("");
document.querySelectorAll("[data-example]").forEach((b) => b.addEventListener("click", () => { $("#ai-prompt").value = AI_EXAMPLES[+b.dataset.example]; }));

$("#ai-generate").addEventListener("click", async () => {
  const prompt = $("#ai-prompt").value.trim();
  if (prompt.length < 10) return showResult("#ai-result", false, "Describe the event in a sentence or two (name, city/venue, date, size, sessions).");
  const btn = $("#ai-generate");
  setBusy(btn, true);
  showResult("#ai-result", true, `<span class="text-slate-300">Onboarding Architect is drafting rooms, speakers, a conflict-free schedule, catering SLAs and crisis presets…</span>`);
  try {
    await afterCreate(await api("/api/events/generate-ai", { method: "POST", body: JSON.stringify({ prompt }) }), "#ai-result");
  } catch (err) {
    showResult("#ai-result", false, errorList(err));
  } finally {
    setBusy(btn, false);
  }
});

$("#csv-venue").addEventListener("change", () => $("#csv-custom-fields").classList.toggle("hidden", $("#csv-venue").value !== "__custom__"));
$("#csv-sample").addEventListener("click", () => { $("#csv-text").value = sampleCsv(); });
$("#csv-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (file) $("#csv-text").value = await file.text();
});
$("#csv-submit").addEventListener("click", async () => {
  const custom = $("#csv-venue").value === "__custom__";
  const body = {
    event_name: $("#csv-name").value.trim(), event_date: $("#csv-date").value, attendee_count: +$("#csv-attendees").value,
    csv_text: $("#csv-text").value,
    ...(custom ? { custom_city: $("#csv-city").value.trim(), custom_venue_name: $("#csv-venue-name").value.trim(), airport_corridor: $("#csv-corridor").value.trim() }
               : { venue_id: $("#csv-venue").value }),
  };
  const btn = $("#csv-submit");
  setBusy(btn, true);
  try {
    await afterCreate(await api("/api/events/import-csv", { method: "POST", body: JSON.stringify(body) }), "#csv-result");
  } catch (err) {
    showResult("#csv-result", false, errorList(err));
  } finally {
    setBusy(btn, false);
  }
});

$("#clone-submit").addEventListener("click", async () => {
  const body = { event_name: $("#clone-name").value.trim(), event_date: $("#clone-date").value,
                 attendee_count: +$("#clone-attendees").value, template_event_id: $("#clone-template").value };
  const btn = $("#clone-submit");
  setBusy(btn, true);
  try {
    await afterCreate(await api("/api/events/import-csv", { method: "POST", body: JSON.stringify(body) }), "#clone-result");
  } catch (err) {
    showResult("#clone-result", false, errorList(err));
  } finally {
    setBusy(btn, false);
  }
});

document.querySelectorAll(".modal-tab").forEach((t) => t.addEventListener("click", () => switchModalTab(t.dataset.tab)));
document.querySelectorAll("[data-close]").forEach((el) => el.addEventListener("click", closeModal));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

// ---------- sidebar actions ----------
$("#btn-new-event").addEventListener("click", () => openModal("ai"));
$("#btn-reset").addEventListener("click", async () => {
  if (!confirm("Reset all demo data? Custom events and war-room changes will be removed.")) return;
  await api("/api/demo/reset", { method: "POST" });
  localStorage.removeItem("stagecraft.event");
  state.eventId = null;
  toast("Demo data restored to the seed state", "ok");
  await loadEvents();
});
$("#btn-settings").addEventListener("click", () => openAdmin("usage"));

// ---------- boot ----------
initWarroom();
initAdmin();
loadEvents().catch((err) => toast(`Failed to load workspace: ${err.message}`, "bad")).finally(icons);
setInterval(updateCountdown, 30000);
