/* Stagecraft AI - Crisis War-Room view (Step 10).
   Loaded before app.js; only defines functions, which use app.js helpers ($, esc, api, toast, state) at call time. */
"use strict";

const PIPELINE = [
  { agent: "Security Gate (Layer 1)", label: "0. Security Gatekeeper", icon: "shield-check" },
  { agent: "TriageAgent", label: "1. Triage & Weather Analyst", icon: "radar" },
  { agent: "SolverAgent", label: "2. Deterministic Constraint Solver", icon: "cpu" },
  { agent: "CommsAgent", label: "3. Stakeholder Comms Agent", icon: "megaphone" },
];
const PRESET_ICON = { "Flight/Transit Delay": "plane", "AV/Room Failure": "monitor-x", "Catering/Overrun": "utensils", "Red-Team Prompt Injection": "shield-alert" };
const war = { presetId: null, result: null, option: "recommended", commsTab: "messages", committed: null, timer: null, started: 0, checked: new Set() };

const usd = (n, digits = 4) => "$" + Number(n || 0).toFixed(digits);
const roomName = (id) => (state.data.rooms.find((r) => r.id === id) || { name: id }).name;
const roomCap = (id) => (state.data.rooms.find((r) => r.id === id) || { capacity: 0 }).capacity;
const sessionById = (id) => state.data.sessions.find((s) => s.id === id);
const speakerName = (id) => (state.data.speakers.find((s) => s.id === id) || { name: id }).name;

function initWarroom() {
  $("#dispatch").addEventListener("click", dispatchWarRoom);
  $("#crisis-text").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) dispatchWarRoom(); });
}

// ---------- command deck ----------
function renderCrisisDeck() {
  war.presetId = null;
  war.result = null;
  war.committed = null;
  $("#crisis-text").value = "";
  $("#warroom-result").innerHTML = "";
  $("#pipeline-alerts").innerHTML = "";
  const presets = state.data.crisis_presets;
  $("#crisis-preset-list").innerHTML = presets.length ? presets.map((c, i) => `
    <button class="preset-card ${c.category === "Red-Team Prompt Injection" ? "redteam" : ""}" data-preset="${esc(c.id)}">
      <div class="flex items-start justify-between gap-2">
        <span class="flex items-start gap-2 text-[13px] font-medium text-white leading-snug">
          <i data-lucide="${PRESET_ICON[c.category] || "alert-triangle"}" class="h-4 w-4 mt-0.5 shrink-0 ${c.category === "Red-Team Prompt Injection" ? "text-bad" : "text-accent"}"></i>
          <span><span class="font-mono text-[10px] text-slate-500">PRESET ${i + 1}</span><br>${esc(c.title)}</span>
        </span>
        <span class="pill ${c.severity === "Critical" ? "pill-bad" : c.severity === "High" ? "pill-warn" : "pill-muted"} font-mono">${esc(c.severity)}</span>
      </div>
      <p class="mt-2 text-[11.5px] text-slate-400 line-clamp-3">${esc(c.prompt_text)}</p>
    </button>`).join("") : `<p class="text-sm text-slate-500">No presets for this event. Type a crisis on the right.</p>`;
  document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
    const preset = presets.find((c) => c.id === b.dataset.preset);
    war.presetId = preset.id;
    $("#crisis-text").value = preset.prompt_text;
    document.querySelectorAll("[data-preset]").forEach((x) => x.classList.toggle("selected", x === b));
  }));
  renderPipeline("idle");
  icons();
}

// ---------- pipeline trace ----------
function statusClass(status) {
  return { PASSED: ["ok", "pill-ok"], COMPLETED: ["ok", "pill-ok"], BLOCKED: ["bad", "pill-bad"], HALTED: ["warn", "pill-warn"], FAILED: ["bad", "pill-bad"] }[status] || ["", "pill-muted"];
}

function renderPipeline(mode, result) {
  const trace = (result && result.agent_handoff_trace) || [];
  const nodes = PIPELINE.map((n) => {
    const step = trace.find((t) => t.agent_name === n.agent);
    let cls = "", badge;
    if (mode === "idle") badge = `<span class="pill pill-muted font-mono">STANDBY</span>`;
    else if (mode === "running") { cls = "running"; badge = `<span class="pill pill-accent font-mono">WORKING…</span>`; }
    else if (!step) { cls = "skip"; badge = `<span class="pill pill-muted font-mono">NOT RUN</span>`; }
    else { const [c, p] = statusClass(step.status); cls = c; badge = `<span class="pill ${p} font-mono">${esc(step.status)}</span>`; }
    const isAgent = n.agent !== "Security Gate (Layer 1)";
    const metrics = step ? `
      <div class="grid grid-cols-3 gap-2">
        <div><div class="metric-label">LATENCY</div><div class="metric">${Math.round(step.latency_ms).toLocaleString()} ms</div></div>
        <div><div class="metric-label">TOKENS in/cache/out</div><div class="metric">${isAgent ? `${step.input_tokens.toLocaleString()}/${step.cached_input_tokens.toLocaleString()}/${step.output_tokens.toLocaleString()}` : "0 · no LLM"}</div></div>
        <div><div class="metric-label">COST</div><div class="metric">${usd(step.cost_usd)}</div></div>
      </div>
      ${step.tools_called.length ? `<details class="text-xs"><summary class="cursor-pointer text-slate-400 hover:text-slate-200">Tools invoked (${step.tools_called.length})</summary>
        <div class="mt-1.5 space-y-1">${step.tools_called.map((t) => `<span class="tool-pill">${esc(t)}</span>`).join("")}</div></details>` : ""}
      <p class="text-[11.5px] text-slate-400 leading-snug">${esc(step.summary)}</p>` : `<p class="text-[11.5px] text-slate-500">${mode === "running" ? "Waiting for the desk…" : mode === "idle" ? "Ready." : "Not reached in this run."}</p>`;
    return `<div class="pipe-node ${cls}">
      <div class="flex items-start justify-between gap-2">
        <span class="flex items-center gap-2 text-[13px] font-semibold text-white"><i data-lucide="${n.icon}" class="h-4 w-4 text-accent shrink-0"></i>${n.label}</span>${badge}
      </div>${metrics}</div>`;
  });
  const lit = (i) => {
    const a = trace.find((t) => t.agent_name === PIPELINE[i].agent);
    const b = trace.find((t) => t.agent_name === PIPELINE[i + 1].agent);
    return a && b && ["PASSED", "COMPLETED"].includes(a.status);
  };
  $("#pipeline").innerHTML = nodes.map((html, i) => html + (i < nodes.length - 1
    ? `<div class="pipe-arrow ${lit(i) ? "lit" : ""}"><i data-lucide="chevrons-right" class="h-5 w-5"></i></div>` : "")).join("");
  const extra = trace.filter((t) => !PIPELINE.some((n) => n.agent === t.agent_name));
  $("#pipeline-alerts").innerHTML = extra.map((t) => `<div class="result-bad"><span class="font-medium">${esc(t.agent_name)} · ${esc(t.status)}</span> — ${esc(t.summary)}</div>`).join("");
  icons();
}

// ---------- dispatch ----------
async function dispatchWarRoom() {
  const text = $("#crisis-text").value.trim();
  if (text.length < 5) return toast("Pick a preset or describe the crisis first.", "warn");
  const btn = $("#dispatch");
  setBusy(btn, true);
  war.result = null;
  war.committed = null;
  war.option = "recommended";
  war.commsTab = "messages";
  war.checked = new Set();
  $("#warroom-result").innerHTML = "";
  renderPipeline("running");
  war.started = performance.now();
  war.timer = setInterval(() => { $("#pipeline-meta").textContent = `running · ${((performance.now() - war.started) / 1000).toFixed(1)} s`; }, 100);
  try {
    const result = await api(`/api/events/${encodeURIComponent(state.eventId)}/crisis/run`, {
      method: "POST", body: JSON.stringify({ crisis_text: text, cost_saver: $("#cost-saver").checked }),
    });
    war.result = result;
    renderPipeline("done", result);
    const t = result.tokenomics_summary;
    $("#pipeline-meta").textContent = `${result.status} · ${(result.total_latency_ms / 1000).toFixed(1)} s · ${t.total_tokens.toLocaleString()} tokens · ${usd(t.total_cost_usd)}`;
    renderResult();
    updateLastRunPill(result);
  } catch (err) {
    renderPipeline("idle");
    $("#pipeline-meta").textContent = "error";
    $("#warroom-result").innerHTML = `<div class="result-bad">Run failed: ${esc(err.message)}</div>`;
  } finally {
    clearInterval(war.timer);
    setBusy(btn, false);
  }
}

function updateLastRunPill(r) {
  const t = r.tokenomics_summary;
  const pill = $("#last-run-pill");
  pill.classList.remove("hidden");
  pill.innerHTML = r.status === "BLOCKED_BY_SECURITY_GUARDRAIL"
    ? `🛡️ Last run: attack blocked in ${Math.round(r.total_latency_ms)} ms · <span class="text-emerald-300">0 tokens ($0.0000)</span>`
    : `⚡ Last run: ${t.total_tokens.toLocaleString()} tokens (${usd(t.total_cost_usd)}) · Solved in ${(r.total_latency_ms / 1000).toFixed(1)}s vs ~${r.manual_baseline_minutes}m manual`;
}

// ---------- results ----------
function renderResult() {
  const r = war.result;
  const box = $("#warroom-result");
  if (r.status === "BLOCKED_BY_SECURITY_GUARDRAIL") {
    const inc = r.security_incident || {};
    const findings = (r.security_gate && r.security_gate.findings) || [];
    box.innerHTML = `
      <div class="firewall" role="alert">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex items-center gap-3">
            <div class="h-11 w-11 rounded-xl bg-bad/20 border border-bad/50 grid place-items-center text-red-300"><i data-lucide="shield-x" class="h-6 w-6"></i></div>
            <div>
              <div class="text-lg font-bold tracking-wide text-red-200">🛡️ SECURITY FIREWALL INTERCEPTED ATTACK</div>
              <div class="text-sm text-red-200/80">${esc(inc.defense_layer || "Layer 1")} · severity <span class="font-mono">${esc(inc.severity || "CRITICAL")}</span> · incident <span class="font-mono">${esc(inc.incident_id || "")}</span></div>
            </div>
          </div>
          <span class="pill pill-ok font-mono text-xs">0 LLM Agent Tokens Spent ($0.0000)</span>
        </div>
        <div class="mt-4 grid gap-2">
          ${findings.map((f) => `<div class="flex flex-wrap items-baseline gap-2 text-sm"><span class="pill pill-bad">${esc(f.category)}</span>
            <span class="text-slate-400">${esc(f.pattern)}:</span><span class="font-mono text-red-200">"${esc(f.matched_text)}"</span></div>`).join("")}
        </div>
        <div class="mt-4 text-sm text-slate-300"><span class="text-slate-500">Action taken:</span> ${esc(inc.action_taken || "Blocked before any agent ran")}</div>
        <p class="mt-2 text-xs text-slate-500">Logged to <span class="font-mono">security_audit_logs</span>. No agent read this text, no tool ran, and nothing in the schedule changed.</p>
      </div>`;
    return icons();
  }
  if (r.status !== "SUCCESS") {
    box.innerHTML = `<div class="result-bad"><div class="font-medium text-red-200">${esc(r.status.replaceAll("_", " "))}</div><div class="mt-1 text-slate-300">${esc(r.error || "")}</div></div>`;
    return;
  }
  box.innerHTML = `${triageBanner(r)}<div id="plan-section"></div><div id="comms-section"></div>`;
  renderPlanSection();
  renderCommsSection();
}

function triageBanner(r) {
  const t = r.triage;
  const rec = r.solver.recommended_option;
  const sev = { CRITICAL: "pill-bad", HIGH: "pill-warn", MODERATE: "pill-muted" }[t.severity];
  const affected = [...t.primary_affected_session_ids, ...t.cascading_affected_session_ids].map((id) => sessionById(id)).filter(Boolean);
  const sla = [...state.data.catering_slas].sort((a, b) => b.late_penalty_usd - a.late_penalty_usd)[0];
  const catering = rec.catering_penalty_saved_usd > 0
    ? `<span class="pill pill-ok font-mono">${money(rec.catering_penalty_saved_usd)} saved</span>`
    : t.catering_sla_at_risk ? `<span class="pill pill-warn">At risk${sla ? ` · ${money(sla.late_penalty_usd)}` : ""}</span>` : `<span class="pill pill-ok">Not at risk</span>`;
  return `
  <div class="rounded-xl border border-line bg-panel p-5">
    <div class="flex flex-wrap items-center gap-2">
      <div class="section-title"><i data-lucide="activity" class="h-4 w-4"></i> Triage &amp; SLA Impact</div>
      <span class="pill ${sev} font-mono">${esc(t.severity)}</span><span class="pill pill-muted">${esc(t.disruption_category)}</span>
      ${t.estimated_delay_mins ? `<span class="pill pill-warn font-mono">+${t.estimated_delay_mins} min</span>` : ""}
    </div>
    <p class="mt-3 text-sm text-slate-200 leading-relaxed">${esc(t.crisis_summary)}</p>
    <div class="mt-4 grid gap-4 md:grid-cols-4 text-sm">
      <div><div class="metric-label">AFFECTED SESSIONS</div><div class="mt-1 space-y-0.5">${affected.map((s) => `<div>${esc(s.title)} <span class="font-mono text-xs text-slate-500">${esc(s.start_time)}</span></div>`).join("") || "—"}</div></div>
      <div><div class="metric-label">AFFECTED SPEAKERS</div><div class="mt-1 flex flex-wrap gap-1">${t.affected_speaker_ids.map((id) => `<span class="speaker-chip">${esc(speakerName(id))}</span>`).join("") || "—"}</div></div>
      <div><div class="metric-label">LIVE WEATHER · CORRIDOR</div><div class="mt-1 text-slate-300 text-[13px]">${esc(t.live_weather_transit_summary)}</div></div>
      <div><div class="metric-label">CATERING SLA</div><div class="mt-1">${catering}</div>
        ${t.attendance_updates.length ? `<div class="mt-2 metric-label">HEADCOUNT</div><div class="font-mono text-[13px]">${t.attendance_updates.map((u) => `${u.new_expected_attendance} RSVPs`).join(", ")}</div>` : ""}</div>
    </div>
  </div>`;
}

function renderPlanSection() {
  const r = war.result;
  const solver = r.solver;
  const opts = [["recommended", "Option A · Recommended", solver.recommended_option, solver.deterministic_verification_passed],
                ["alternative", "Option B · Alternative", solver.alternative_option, solver.alternative_verified]];
  const [, , plan, verified] = opts.find((o) => o[0] === war.option);
  const attendance = Object.fromEntries(r.triage.attendance_updates.map((u) => [u.session_id, u.new_expected_attendance]));
  const rows = plan.proposed_changes.map((m) => {
    const att = attendance[m.session_id] ?? (sessionById(m.session_id) || {}).expected_attendance ?? 0;
    const buffer = roomCap(m.new_room_id) - att;
    return `<tr>
      <td class="font-medium text-slate-100">${esc(m.session_title)}</td>
      <td><div class="diff-old">${esc(roomName(m.old_room_id))}</div><div class="diff-old font-mono text-xs">${esc(m.old_start_time)}–${esc(m.old_end_time)}</div></td>
      <td class="text-slate-500"><i data-lucide="arrow-right" class="h-4 w-4"></i></td>
      <td><div class="diff-new">${esc(roomName(m.new_room_id))}</div><div class="diff-new font-mono text-xs">${esc(m.new_start_time)}–${esc(m.new_end_time)}</div></td>
      <td class="font-mono text-xs"><span class="${buffer >= 0 ? "text-emerald-300" : "text-red-300"}">${buffer >= 0 ? "+" : ""}${buffer}</span><div class="text-slate-500">${att}/${roomCap(m.new_room_id)}</div></td>
      <td class="text-xs text-slate-400 leading-relaxed">${esc(m.change_rationale)}</td></tr>`;
  }).join("");
  const done = war.committed;
  $("#plan-section").innerHTML = `
  <div class="rounded-xl border border-line bg-panel p-5">
    <div class="section-title"><i data-lucide="git-compare-arrows" class="h-4 w-4"></i> Resolution Options</div>
    <div class="mt-3 flex flex-wrap gap-2" role="tablist">
      ${opts.map(([key, label, p, ok]) => `<button class="opt-tab ${key === war.option ? "active" : ""}" data-option="${key}" role="tab" aria-selected="${key === war.option}" ${done ? "disabled" : ""}>
        <div class="flex items-center justify-between gap-2"><span class="text-xs font-semibold text-slate-300">${label}</span><span class="font-mono text-lg font-semibold text-white">${p.optimization_score}<span class="text-xs text-slate-500">/100</span></span></div>
        <div class="mt-0.5 text-[13px] text-slate-100 leading-snug">${esc(p.option_label)}</div></button>`).join("")}
    </div>
    <div class="mt-4 flex flex-wrap items-center gap-2">
      <span class="pill pill-accent">${esc(plan.strategy_type)}</span>
      ${verified ? `<span class="pill pill-ok"><i data-lucide="badge-check" class="h-3 w-3"></i>✓ 100% Capacity &amp; AV Verified (Layer 2)</span>` : `<span class="pill pill-bad">Not verified by Layer 2</span>`}
      ${plan.room_status_updates.map((u) => `<span class="pill pill-bad">${esc(roomName(u.room_id))} → ${esc(u.new_status)}</span>`).join("")}
    </div>
    <div class="mt-4 rounded-lg border border-line overflow-x-auto">
      <table class="diff-table">
        <thead><tr><th>SESSION</th><th>OLD ROOM · TIME</th><th></th><th>NEW ROOM · TIME</th><th>SEAT BUFFER</th><th>RATIONALE</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="6" class="text-slate-400">No room or time moves in this option.</td></tr>`}</tbody>
      </table>
    </div>
    <div class="mt-3 grid gap-3 md:grid-cols-2 text-[13px]">
      <div><span class="metric-label">ATTENDEE IMPACT</span><p class="mt-1 text-slate-300">${esc(plan.attendee_disruption_minimized_note)}</p></div>
      <div><span class="metric-label">TRADE-OFFS</span><p class="mt-1 text-slate-300">${esc(plan.tradeoffs)}</p></div>
    </div>
    <div class="mt-5 border-t border-line pt-4 flex flex-wrap items-center justify-between gap-3">
      ${done ? commitDoneHtml(done) : `
        <span class="text-xs text-slate-500">Nothing changes until you approve. The plan is re-verified by the Layer 2 lock at commit time.</span>
        <button id="commit-btn" class="btn-primary bg-ok hover:bg-emerald-600" ${verified ? "" : "disabled"}><span class="spinner hidden"></span>✅ Approve &amp; Commit ${war.option === "recommended" ? "Option A" : "Option B"} to Live Schedule</button>`}
    </div>
  </div>`;
  document.querySelectorAll("[data-option]").forEach((b) => b.addEventListener("click", () => { war.option = b.dataset.option; renderPlanSection(); }));
  const cb = $("#commit-btn");
  if (cb) cb.addEventListener("click", commitPlan);
  icons();
}

function commitDoneHtml(c) {
  return `<div class="flex items-center gap-2 text-sm text-emerald-300"><i data-lucide="circle-check-big" class="h-5 w-5"></i>
      Committed ${esc(c.option_label)} · ${c.applied_changes.length} session(s) updated${c.comms_match_committed_plan ? "" : ` · <span class="text-warn">drafts were written for Option A, review before sending</span>`}</div>
    <div class="flex flex-wrap gap-2">
      <a class="btn-ghost text-xs" href="${c.icsUrl}" download="${esc(c.ics_filename)}"><i data-lucide="calendar-plus" class="h-3.5 w-3.5"></i> 📅 Download Updated .ics Calendar</a>
      <a class="btn-ghost text-xs" href="/live/${encodeURIComponent(state.eventId)}" target="_blank" rel="noopener"><i data-lucide="smartphone" class="h-3.5 w-3.5"></i> Open Public Live Page ↗</a>
    </div>`;
}

async function commitPlan() {
  const btn = $("#commit-btn");
  setBusy(btn, true);
  try {
    const res = await api(`/api/events/${encodeURIComponent(state.eventId)}/crisis/commit`, {
      method: "POST", body: JSON.stringify({ run_id: war.result.run_id, option_choice: war.option }),
    });
    res.icsUrl = URL.createObjectURL(new Blob([res.ics], { type: "text/calendar" }));
    war.committed = res;
    state.data = res.event_state;
    renderTelemetry();
    renderRoomDeck();
    renderFilters();
    renderTimetable();
    renderPlanSection();
    toast(`Committed to the live schedule: ${res.applied_changes.map((c) => `${c.title} → ${c.to}`).join("; ")}`, "ok");
  } catch (err) {
    const reasons = (err.body && err.body.errors) || [err.message];
    toast(`Commit refused: ${reasons[0]}`, "bad");
    setBusy(btn, false);
  }
}

// ---------- comms drawer ----------
function renderCommsSection() {
  const c = war.result.comms;
  const tabs = [["messages", `Speaker & Stakeholder Messages (${c.speaker_and_moderator_messages.length})`, "mail"],
                ["checklist", `Volunteer & AV Checklist (${c.volunteer_action_checklist.length})`, "list-todo"],
                ["brief", "Executive Brief & Public Banner", "presentation"]];
  const chan = { Email: "pill-accent", "SMS / WhatsApp Urgent": "pill-warn", "Slack War-Room": "pill-muted" };
  let body = "";
  if (war.commsTab === "messages") {
    body = `<div class="grid gap-3 xl:grid-cols-2">${c.speaker_and_moderator_messages.map((m, i) => `
      <article class="msg-card">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div><div class="text-sm font-semibold text-white">${esc(m.recipient_name)}</div><div class="text-xs text-slate-500">${esc(m.recipient_role)} · <span class="font-mono">${esc(m.recipient_contact)}</span></div></div>
          <span class="pill ${chan[m.channel] || "pill-muted"}">${esc(m.channel)}</span>
        </div>
        <div class="mt-3 text-[13px] font-medium text-slate-200">${esc(m.subject_or_header)}</div>
        <div class="msg-body mt-1.5">${esc(m.message_body)}</div>
        <div class="mt-3 flex flex-wrap gap-2">
          <button class="btn-ghost text-xs" data-copy="${i}"><i data-lucide="copy" class="h-3.5 w-3.5"></i> Copy Draft</button>
          ${m.recipient_contact.includes("@") ? `<a class="btn-ghost text-xs" href="mailto:${encodeURIComponent(m.recipient_contact)}?subject=${encodeURIComponent(m.subject_or_header)}&body=${encodeURIComponent(m.message_body)}"><i data-lucide="send" class="h-3.5 w-3.5"></i> Send via Email Client (mailto:)</a>` : ""}
        </div>
      </article>`).join("")}</div>`;
  } else if (war.commsTab === "checklist") {
    const pri = (p) => (p.startsWith("IMMEDIATE") ? "pill-bad" : "pill-warn");
    body = `<div class="space-y-2">${c.volunteer_action_checklist.map((t) => `
      <label class="check-row ${war.checked.has(t.task_id) ? "done" : ""}">
        <input type="checkbox" class="mt-1 h-4 w-4 accent-emerald-500" data-task="${esc(t.task_id)}" ${war.checked.has(t.task_id) ? "checked" : ""}>
        <div class="min-w-0"><div class="flex flex-wrap items-center gap-2"><span class="font-mono text-xs text-slate-500">${esc(t.task_id)}</span>
          <span class="pill ${pri(t.priority)} font-mono">${esc(t.priority)}</span><span class="text-xs font-medium text-slate-300">${esc(t.assigned_team)}</span></div>
          <div class="check-text mt-1 text-[13px] text-slate-200">${esc(t.instruction)}</div></div>
      </label>`).join("")}</div>
      <div class="mt-2 font-mono text-xs text-slate-500">${war.checked.size}/${c.volunteer_action_checklist.length} done</div>`;
  } else {
    body = `<div class="grid gap-3 md:grid-cols-2">
      <div class="msg-card"><div class="metric-label">EXECUTIVE BRIEF · SUMMIT DIRECTOR</div><p class="mt-2 text-sm text-slate-200 leading-relaxed">${esc(c.executive_brief)}</p></div>
      <div class="msg-card"><div class="metric-label">PUBLIC ATTENDEE BANNER (shown on the live page after commit)</div>
        <div class="mt-2 rounded-lg border border-warn/50 bg-warn/10 px-3 py-2 text-sm text-amber-200">${esc(c.public_attendee_banner_alert)}</div></div>
    </div>`;
  }
  $("#comms-section").innerHTML = `
  <div class="rounded-xl border border-line bg-panel p-5">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="section-title"><i data-lucide="megaphone" class="h-4 w-4"></i> Stakeholder Comms &amp; Volunteer Actions</div>
      <span class="font-mono text-[11px] text-slate-500">outbound scrubber: ${c.scrubber_redactions} redaction(s) · drafts for Option A</span>
    </div>
    <div class="mt-3 flex flex-wrap gap-1 border-b border-line" role="tablist">
      ${tabs.map(([k, label, icon]) => `<button class="modal-tab ${k === war.commsTab ? "active" : ""}" data-comms="${k}" role="tab"><i data-lucide="${icon}" class="h-4 w-4"></i> ${label}</button>`).join("")}
    </div>
    <div class="mt-4">${body}</div>
  </div>`;
  document.querySelectorAll("[data-comms]").forEach((b) => b.addEventListener("click", () => { war.commsTab = b.dataset.comms; renderCommsSection(); }));
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    const m = c.speaker_and_moderator_messages[+b.dataset.copy];
    const text = `To: ${m.recipient_name} <${m.recipient_contact}> via ${m.channel}\nSubject: ${m.subject_or_header}\n\n${m.message_body}`;
    try { await navigator.clipboard.writeText(text); toast(`Draft for ${m.recipient_name} copied`, "ok"); }
    catch { toast("Clipboard unavailable in this browser", "warn"); }
  }));
  document.querySelectorAll("[data-task]").forEach((cb) => cb.addEventListener("change", () => {
    cb.checked ? war.checked.add(cb.dataset.task) : war.checked.delete(cb.dataset.task);
    renderCommsSection();
  }));
  icons();
}
