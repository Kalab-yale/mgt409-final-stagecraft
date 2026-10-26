// Visual summaries inside an event: the road to event day (workstream timeline), the schedule as a room-by-time
// grid, who is coming, and where the money goes. Built on charts.js; every value is also shown as text.
import { hbars, legend, pairs, ring, SERIES, stack, STATUS } from "./charts.js";
import { esc, fmtDate, money, pct } from "./core.js";
import { icon } from "./icons.js";

const D = (iso) => new Date(`${String(iso).slice(0, 10)}T12:00:00`);
const days = (a, b) => Math.round((D(a) - D(b)) / 864e5);
const STATUS_COLOR = { "On track": SERIES[0], "At risk": STATUS.serious, Blocked: STATUS.critical, Done: STATUS.good };

// ---------- Overview: the road to event day ----------
export function roadmap(plan, eventDate) {
  const today = plan.today;
  const starts = plan.initiatives.map((i) => i.tasks.map((t) => t.due_date).filter(Boolean).sort()[0] || today);
  const from = [today, ...starts].sort()[0];
  const to = eventDate > plan.initiatives.map((i) => i.target_date).sort().slice(-1)[0] ? eventDate : plan.initiatives.map((i) => i.target_date).sort().slice(-1)[0];
  const span = Math.max(1, days(to, from) + 3);
  const x = (iso) => `${(100 * (days(iso, from) + 1)) / span}%`;
  const months = [];
  for (let d = D(from); d <= D(to); d.setDate(d.getDate() + 1)) if (d.getDate() === 1 || +d === +D(from)) months.push(d.toISOString().slice(0, 10));
  const weekTicks = [];
  for (let d = D(from); d <= D(to); d.setDate(d.getDate() + 7)) weekTicks.push(d.toISOString().slice(0, 10));
  return `
    <div class="card"><div class="card-head"><h3>${icon("calendar")} Road to event day</h3>
      ${legend(Object.entries(STATUS_COLOR).map(([k, c]) => [k, c]))}</div>
      <div class="card-body">
        <div class="road">
          <div class="road-rows">
            ${plan.initiatives.map((i, n) => {
              const s = starts[n] < from ? from : starts[n];
              const color = STATUS_COLOR[i.status] || SERIES[0];
              return `<div class="road-row">
                <div class="road-label"><div class="ellipsis" style="font-weight:500">${esc(i.title)}</div><div class="tiny muted ellipsis">${esc(i.owner || "Unassigned")} · ${i.tasks_done}/${i.tasks_total} tasks</div></div>
                <div class="road-lane">
                  <div class="road-bar" style="left:${x(s)};width:calc(${x(i.target_date)} - ${x(s)})" data-tip-value="${i.progress_pct}% done"
                       data-tip-label="${esc(i.title)} · ${esc(i.status)} · target ${esc(fmtDate(i.target_date))}" data-tip-color="${color}" tabindex="0">
                    <span class="road-fill" style="width:${Math.max(i.progress_pct, 4)}%;background:${color}"></span></div>
                  <span class="road-pct" style="left:calc(${x(i.target_date)} + 8px)">${i.progress_pct}%</span>
                </div></div>`;
            }).join("")}
          </div>
          <div class="road-overlay">
            ${weekTicks.map((w) => `<div class="road-tick" style="left:${x(w)}"><span>${esc(D(w).toLocaleDateString(undefined, { month: "short", day: "numeric" }))}</span></div>`).join("")}
            <div class="road-today" style="left:${x(today)}"><span>Today</span></div>
            <div class="road-event" style="left:${x(eventDate)}"><span>${icon("flag")} Event day</span></div>
          </div>
        </div>
      </div></div>`;
}

// ---------- Program: rooms across, time down ----------
export function scheduleGrid(ev) {
  const toMin = (t) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));
  const sessions = ev.sessions;
  const start = Math.floor(Math.min(...sessions.map((s) => toMin(s.start_time))) / 60) * 60;
  const end = Math.ceil(Math.max(...sessions.map((s) => toMin(s.end_time))) / 60) * 60;
  const rooms = ev.rooms.filter((r) => sessions.some((s) => s.room.id === r.id));
  const tracks = [...new Set(sessions.map((s) => s.track))];
  const color = (t) => SERIES[tracks.indexOf(t) % SERIES.length];
  const STEP = 5, slots = (end - start) / STEP;  // 5-minute rows, so 11:40 starts land exactly
  const hours = Array.from({ length: (end - start) / 60 + 1 }, (_, k) => start + k * 60);
  return `
    <div class="sched-wrap">
      ${legend(tracks.map((t) => [t, color(t)]))}
      <div class="sched" style="grid-template-columns:64px repeat(${rooms.length}, minmax(150px, 1fr));grid-template-rows:auto repeat(${slots}, 6px)">
        <div class="sched-corner"></div>
        ${rooms.map((r, k) => `<div class="sched-room" style="grid-column:${k + 2}"><b>${esc(r.name.split(" - ")[0])}</b><span>${r.capacity} seats${r.status !== "Operational" ? ` · <span class="red-text">${esc(r.status)}</span>` : ""}</span></div>`).join("")}
        ${hours.slice(0, -1).map((h) => `<div class="sched-hour" style="grid-row:${(h - start) / STEP + 2} / span 12">${String(Math.floor(h / 60)).padStart(2, "0")}:00</div>
          <div class="sched-line" style="grid-row:${(h - start) / STEP + 2};grid-column:2 / -1"></div>`).join("")}
        ${sessions.map((s) => {
          const k = rooms.findIndex((r) => r.id === s.room.id);
          const r0 = (toMin(s.start_time) - start) / STEP + 2, span = Math.max(1, (toMin(s.end_time) - toMin(s.start_time)) / STEP);
          const full = s.utilization_pct >= 95;
          return `<div class="sched-block" style="grid-column:${k + 2};grid-row:${r0} / span ${span};--c:${color(s.track)}"
              data-tip-value="${esc(s.start_time)}–${esc(s.end_time)} · ${s.expected_attendance}/${s.room.capacity} seats" data-tip-label="${esc(s.title)}" data-tip-color="${color(s.track)}" tabindex="0">
            <div class="sb-title">${s.is_locked_keynote ? icon("shield") + " " : ""}${esc(s.title)}</div>
            ${span >= 9 ? `<div class="sb-meta">${esc(s.start_time)}–${esc(s.end_time)}${s.speakers.length ? ` · ${esc(s.speakers.map((p) => p.name.split(" ").slice(-1)[0]).join(", "))}` : ""}</div>` : ""}
            ${span >= 12 ? `<div class="sb-meta ${full ? "red-text" : ""}">${s.expected_attendance}/${s.room.capacity} seats${s.war_room_update ? " · moved" : ""}</div>` : ""}
          </div>`;
        }).join("")}
      </div>
    </div>`;
}

// ---------- Attendees: who is coming ----------
export function attendeeCharts(plan) {
  const a = plan.attendees, h = plan.health, s = a.by_status;
  const order = [["Confirmed", SERIES[0]], ["Registered", SERIES[2]], ["Waitlist", SERIES[3]], ["Cancelled", SERIES[1]]];
  const diet = Object.entries(a.dietary_active).filter(([k]) => k !== "None").sort((x, y) => y[1] - x[1]);
  const types = Object.entries(a.by_type).sort((x, y) => y[1] - x[1]);
  const fill = pct(h.registrations.active, h.registrations.cap);
  return `
    <div class="card"><div class="card-head"><h3>${icon("attendees")} Who's coming</h3><span class="muted small">${a.total} people on the list</span></div>
      <div class="card-body att-viz">
        <div class="att-cap">${ring(fill, { size: 108, stroke: 11, label: `${fill}%`, sub: "of seats", color: fill > 100 ? STATUS.critical : SERIES[0], tipLabel: `${h.registrations.active} of ${h.registrations.cap} seats taken` })}
          <div class="small" style="text-align:center;margin-top:8px"><b>${h.registrations.active}</b> of ${h.registrations.cap} seats<br><span class="muted">${h.registrations.waitlist} on the waitlist</span></div></div>
        <div class="att-break">
          <div class="label">RSVP status</div>
          ${stack(order.map(([k, c]) => ({ value: s[k] || 0, color: c, label: k })), { height: 16, name: "RSVPs" })}
          <div class="legend" style="margin-top:8px">${order.map(([k, c]) => `<span><i style="background:${c}"></i>${k} <b class="num">${s[k] || 0}</b></span>`).join("")}</div>
          <div class="grid-2" style="margin-top:18px;gap:24px">
            <div><div class="label">Attendee type</div>${hbars(types.map(([k, v]) => ({ label: k, value: v })))}</div>
            <div><div class="label">Dietary needs (registered)</div>${hbars(diet.map(([k, v]) => ({ label: k, value: v })), { color: SERIES[2] })}</div>
          </div>
        </div>
      </div></div>`;
}

// ---------- Budget: planned vs spent ----------
export function budgetChart(plan) {
  const cats = Object.entries(plan.budget.by_category).sort((x, y) => y[1].planned_usd - x[1].planned_usd);
  const k = (v) => (v >= 1000 ? `$${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}K` : money(v));
  return `
    <div class="card"><div class="card-head"><h3>${icon("dollar")} Where the money goes</h3>${legend([["Planned", SERIES[0]], ["Spent or committed", SERIES[1]]])}</div>
      <div class="card-body">${pairs(cats.map(([c, v]) => ({ label: c, a: v.planned_usd, b: v.spent_usd, note: v.spent_usd > v.planned_usd ? `over ${k(v.spent_usd - v.planned_usd)}` : "" })),
        [{ label: "Planned", color: SERIES[0] }, { label: "Spent", color: SERIES[1] }], { fmt: k })}</div></div>`;
}
