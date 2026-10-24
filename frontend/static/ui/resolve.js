// "Resolve an issue": the three-agent desk (safety check -> Triage -> Solver -> Comms) for day-of disruptions.
// Nothing changes on the schedule until the organizer approves one option.
import { api, busy, esc, fmtWhen, money, post, toast } from "./core.js";
import { icon } from "./icons.js";

const STEPS = [
  { key: "A", name: "Safety check", who: "Security Gate (Layer 1)", what: "Screens the report for prompt injection before any AI reads it" },
  { key: "B", name: "Triage", who: "TriageAgent", what: "Finds the affected sessions, speakers, rooms, and catering" },
  { key: "C", name: "Solver", who: "SolverAgent", what: "Proposes two schedule fixes; a rule checker verifies both" },
  { key: "D", name: "Comms", who: "CommsAgent", what: "Drafts messages for speakers, vendors, and volunteers" },
];
const STATUS_TEXT = {
  BLOCKED_BY_SECURITY_GUARDRAIL: "Stopped by the safety check",
  HALTED_BY_BUDGET_BREAKER: "Stopped by the spending limit",
  NEEDS_HUMAN_REVIEW: "No fix passed the safety rules",
  FAILED: "Something went wrong",
};

// ctx: { id, ev, from: { text, label, flag_id, message_id } | null, refresh }
let st = { ctx: null, run: null, choice: "recommended", committed: null, root: null };
const E = () => `/api/events/${encodeURIComponent(st.ctx.id)}`;

export async function renderResolve(root, ctx) {
  st = { ctx, run: null, choice: "recommended", committed: null, root };
  composeView();
}

// ---------- 1. describe the problem ----------
async function composeView() {
  const { ev, from } = st.ctx;
  const presets = ev.crisis_presets || [];
  st.root.innerHTML = `
    <div class="card card-pad stack">
      <div>
        <h2>Resolve an issue</h2>
        <p class="muted" style="margin-top:4px">Describe what went wrong on the day: a late speaker, a broken room, catering running late.
          Three AI agents work out what it affects, propose two fixes that pass the venue's rules, and draft the messages. Nothing changes until you approve.</p>
      </div>
      ${from ? `<div class="pill blue" style="align-self:flex-start">${icon(from.message_id ? "mail" : "flag")} From ${esc(from.label)}</div>` : ""}
      <div>
        <label class="label" for="crisis-text">What happened?</label>
        <textarea class="textarea" id="crisis-text" rows="5" placeholder="e.g. Room 2400's projector died and the AI Agents panel starts at 14:45. RSVPs for it jumped to 95.">${esc(from?.text || "")}</textarea>
      </div>
      ${presets.length ? `<div><div class="label">Or try an example</div><div class="suggest">${presets.map((p) =>
        `<button type="button" data-preset="${esc(p.id)}" title="${esc(p.prompt_text)}">${esc(p.title)}</button>`).join("")}</div></div>` : ""}
      <div class="row-between wrap">
        <span class="tiny faint">${icon("shield")} Checked for prompt injection first · usually 20 to 40 seconds · about $0.002 per run</span>
        <button class="btn primary" id="run-agents">${icon("sparkles")} Find a fix</button>
      </div>
    </div>
    <div id="recent"></div>`;
  const ta = st.root.querySelector("#crisis-text");
  st.root.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
    ta.value = presets.find((p) => p.id === b.dataset.preset).prompt_text;
    ta.focus();
  }));
  st.root.querySelector("#run-agents").addEventListener("click", () => run(ta.value.trim()));
  loadRecent();
}

async function loadRecent() {
  let r;
  try { r = await api(`${E()}/crisis/runs?limit=6`); } catch { return; }
  const box = st.root.querySelector("#recent");
  if (!box || !r.runs.length) return;
  box.innerHTML = `<div class="card"><div class="card-head"><h3>${icon("clock")} Recent runs</h3></div><div class="card-body">${r.runs.map((x) => `
    <div class="list-row"><div class="grow"><div style="font-weight:500">${esc(x.scenario_title)}</div>
      <div class="tiny muted">${esc(fmtWhen(x.started_at))} · $${Number(x.cost_usd).toFixed(4)}${x.committed ? ` · approved ${esc(x.option_label || "")}` : ""}</div></div>
      <span class="pill ${x.committed ? "green" : x.status === "SUCCESS" ? "blue" : "red"}">${x.committed ? "Approved" : x.status === "SUCCESS" ? "Ready to review" : esc(STATUS_TEXT[x.status] || x.status)}</span>
      <button class="btn plain sm" data-open-run="${esc(x.run_id)}">Open</button></div>`).join("")}</div></div>`;
  box.querySelectorAll("[data-open-run]").forEach((b) => b.addEventListener("click", async () => {
    busy(b, true);
    try { st.run = await api(`${E()}/crisis/runs/${b.dataset.openRun}`); st.committed = null; st.choice = "recommended"; resultView(); }
    catch (err) { toast(err.message, "bad"); busy(b, false); }
  }));
}

// ---------- 2. run ----------
async function run(text) {
  if (text.length < 5) { toast("Describe the problem in a sentence or two", "bad"); return; }
  st.root.innerHTML = `
    <div class="card card-pad stack">
      <div class="row"><span class="spinner dark"></span><h3>The agents are working on it</h3></div>
      <p class="small muted" style="margin-top:-4px">"${esc(text.length > 180 ? text.slice(0, 180) + "…" : text)}"</p>
      <div class="steps">${STEPS.map((s) => stepHtml(s, null)).join("")}</div>
    </div>`;
  try {
    st.run = await post(`${E()}/crisis/run`, { crisis_text: text });
  } catch (err) {
    st.root.innerHTML = `<div class="card card-pad">Couldn't run the agents: ${esc(err.message)} <button class="btn sm" id="back">Try again</button></div>`;
    st.root.querySelector("#back").addEventListener("click", composeView);
    return;
  }
  st.choice = "recommended";
  st.committed = null;
  resultView();
}

function stepHtml(s, h, finished = false) {
  const tone = !h ? "" : h.status === "BLOCKED" || h.status === "FAILED" || h.status === "HALTED" ? "red" : "green";
  const label = !h ? (finished ? "Didn't run" : "Waiting") : { PASSED: "Passed", COMPLETED: "Done", BLOCKED: "Blocked", HALTED: "Stopped", FAILED: "Failed", SKIPPED: "Skipped" }[h.status];
  return `<div class="step ${h ? "" : "pending"}">
    <div class="row-between"><span class="step-name">${esc(s.name)}</span><span class="pill ${tone}">${esc(label)}</span></div>
    <p class="small muted">${esc(h?.summary || (finished ? "Stopped earlier, so this step never ran." : s.what))}</p>
    ${h && h.total_tokens ? `<div class="tiny faint">${h.total_tokens.toLocaleString()} tokens · $${h.cost_usd.toFixed(4)} · ${(h.latency_ms / 1000).toFixed(1)}s</div>` : ""}
  </div>`;
}

// ---------- 3. review ----------
function resultView() {
  const r = st.run;
  const trace = r.agent_handoff_trace || [];
  const steps = STEPS.map((s) => stepHtml(s, trace.find((h) => h.step === s.key), true));
  const breaker = trace.find((h) => h.step === "!");
  const tk = r.tokenomics_summary || {};
  const cost = `${(tk.total_tokens || 0).toLocaleString()} tokens · $${Number(tk.total_cost_usd || 0).toFixed(4)} · ${((r.total_latency_ms || 0) / 1000).toFixed(0)}s
    ${r.status === "SUCCESS" ? `<span class="faint">(about ${r.manual_baseline_minutes || 45} min by hand)</span>` : ""}`;
  st.root.innerHTML = `
    <div class="row-between wrap">
      <div><h2>${esc(r.scenario_title)}</h2><div class="small muted" style="margin-top:2px">${cost}</div></div>
      <button class="btn sm" id="new-run">${icon("plus")} New issue</button>
    </div>
    <div class="card"><div class="card-head"><h3>${icon("sparkles")} How the agents handed off</h3></div>
      <div class="card-body"><div class="steps">${steps.join("")}</div>
      ${breaker ? `<p class="small" style="margin-top:10px;color:var(--red)">${icon("alert")} ${esc(breaker.agent_name)}: ${esc(breaker.summary)}</p>` : ""}</div></div>
    ${r.status === "SUCCESS" || (r.triage && r.solver) ? reviewHtml(r) : stoppedHtml(r)}`;
  st.root.querySelector("#new-run").addEventListener("click", () => { st.ctx.from = null; composeView(); });
  bindReview();
}

function stoppedHtml(r) {
  if (r.status === "BLOCKED_BY_SECURITY_GUARDRAIL") {
    const g = r.security_gate || {};
    return `<div class="card card-pad notice red">
      <h3>${icon("shield")} Stopped before any AI read it</h3>
      <p style="margin-top:6px">The safety check found instructions hidden in this text, the kind used to trick AI agents into cancelling sessions or leaking data.
        No agent ran, nothing was changed, and it cost $0.</p>
      ${(g.findings || []).length ? `<ul class="small" style="margin:8px 0 0;padding-left:18px">${g.findings.map((f) => `<li>${esc(f.category)}: “${esc(f.matched_text)}”</li>`).join("")}</ul>` : ""}
      <p class="tiny" style="margin-top:8px">Logged as incident ${esc(g.incident_id || r.security_incident?.incident_id || "")}. See AI usage and safety for details.</p>
    </div>`;
  }
  return `<div class="card card-pad notice orange"><h3>${icon("alert")} ${esc(STATUS_TEXT[r.status] || r.status)}</h3>
    <p style="margin-top:6px">${esc(r.error || "")}</p>
    <p class="small muted" style="margin-top:6px">${r.status === "HALTED_BY_BUDGET_BREAKER" ? "The run hit its token or dollar limit, so it stopped instead of overspending. You can raise the limit under AI usage and safety." : "Nothing was changed. Try describing the problem differently, or handle it in the Program tab."}</p></div>`;
}

const roomName = (id) => st.ctx.ev.rooms.find((r) => r.id === id)?.name || id;

function optionHtml(key, plan, verified) {
  const chosen = st.choice === key;
  const moves = plan.proposed_changes.map((m) => {
    return `<div class="move"><div style="font-weight:500">${esc(m.session_title)}</div>
      <div class="small"><span class="old">${esc(roomName(m.old_room_id))} ${esc(m.old_start_time)}–${esc(m.old_end_time)}</span> →
        <span class="new">${esc(roomName(m.new_room_id))} ${esc(m.new_start_time)}–${esc(m.new_end_time)}</span></div>
      <div class="tiny muted">${esc(m.change_rationale)}</div></div>`;
  }).join("");
  return `<button type="button" class="option-card ${chosen ? "active" : ""}" data-choice="${key}" ${verified ? "" : "disabled"} aria-pressed="${chosen}">
    <div class="row-between"><span class="pill ${key === "recommended" ? "blue" : ""}">${key === "recommended" ? "Recommended" : "Alternative"}</span>
      ${verified ? `<span class="pill green">${icon("check")} Passes venue rules</span>` : `<span class="pill red">Failed the rule check</span>`}</div>
    <h3 style="margin-top:8px">${esc(plan.option_label)}</h3>
    <div class="row small muted" style="margin-top:2px">Score ${plan.optimization_score}/100${plan.catering_penalty_saved_usd ? ` · saves ${money(plan.catering_penalty_saved_usd)} in catering penalties` : ""}</div>
    <div class="moves">${moves || `<p class="small muted">No session moves (room status change only).</p>`}</div>
    ${plan.room_status_updates.length ? `<div class="tiny muted">${plan.room_status_updates.map((u) => `${esc(roomName(u.room_id))} → ${esc(u.new_status)}`).join(" · ")}</div>` : ""}
    <p class="small" style="margin-top:8px"><span class="muted">Trade-offs:</span> ${esc(plan.tradeoffs)}</p>
  </button>`;
}

function reviewHtml(r) {
  const t = r.triage, s = r.solver, c = r.comms;
  const sessions = Object.fromEntries(st.ctx.ev.sessions.map((x) => [x.id, x.title]));
  const affected = [...t.primary_affected_session_ids, ...t.cascading_affected_session_ids].map((id) => sessions[id] || id);
  const done = st.committed || (r.committed ? {} : null);
  return `
    <div class="card card-pad">
      <div class="row wrap"><span class="pill ${t.severity === "CRITICAL" ? "red" : t.severity === "HIGH" ? "orange" : ""}">${esc(t.severity.toLowerCase())}</span>
        <span class="pill">${esc(t.disruption_category)}</span>${t.catering_sla_at_risk ? `<span class="pill orange">${icon("food")} Catering at risk</span>` : ""}
        ${t.estimated_delay_mins ? `<span class="pill">${t.estimated_delay_mins} min delay</span>` : ""}</div>
      <p style="margin-top:10px">${esc(t.crisis_summary)}</p>
      ${affected.length ? `<p class="small muted" style="margin-top:6px">Affects: ${affected.map(esc).join(", ")}</p>` : ""}
      ${t.live_weather_transit_summary ? `<p class="small muted" style="margin-top:4px">${icon("cloud")} ${esc(t.live_weather_transit_summary)}</p>` : ""}
    </div>
    <div>
      <h3 style="margin-bottom:10px">Pick a fix</h3>
      <div class="grid-2">${optionHtml("recommended", s.recommended_option, s.deterministic_verification_passed)}${optionHtml("alternative", s.alternative_option, s.alternative_verified)}</div>
      ${s.solver_reasoning_summary ? `<p class="small muted" style="margin-top:10px">${icon("sparkles")} ${esc(s.solver_reasoning_summary)}</p>` : ""}
    </div>
    ${c ? commsHtml(c) : ""}
    <div class="card card-pad approve-bar" id="approve">${done ? doneHtml(done) : r.status === "SUCCESS" ? `
      <div class="grow"><div style="font-weight:600">Approve ${st.choice === "recommended" ? "the recommended fix" : "the alternative"}?</div>
        <div class="small muted">The schedule updates right away and the rule checker runs once more first. Messages are drafts: you send them.
          ${st.choice === "alternative" && c ? " The drafted messages describe the recommended fix, so edit them before sending." : ""}</div></div>
      <button class="btn primary" id="commit">${icon("check")} Approve and update the schedule</button>` : `<p class="small muted">This run can't be approved (${esc(STATUS_TEXT[r.status] || r.status)}).</p>`}</div>`;
}

function commsHtml(c) {
  return `<div class="card"><div class="card-head"><h3>${icon("mail")} Drafted messages</h3><span class="muted small">${c.speaker_and_moderator_messages.length} messages · ${c.volunteer_action_checklist.length} volunteer tasks</span></div>
    <div class="card-body stack">
      <div class="brief-box"><div class="tiny" style="font-weight:600;color:var(--accent)">For the event lead</div>${esc(c.executive_brief)}</div>
      <div class="small"><span class="muted">Attendee banner:</span> ${esc(c.public_attendee_banner_alert)}</div>
      ${c.speaker_and_moderator_messages.map((m, i) => `
        <details class="msg-draft"><summary><span style="font-weight:500">${esc(m.recipient_name)}</span> <span class="muted small">· ${esc(m.recipient_role)} · ${esc(m.channel)}</span></summary>
          <div class="small" style="margin:8px 0 4px;font-weight:500">${esc(m.subject_or_header)}</div>
          <div class="mail-body">${esc(m.message_body)}</div>
          <div class="row" style="margin-top:8px"><button class="btn sm" data-copy="${i}">${icon("copy")} Copy</button>
            ${m.channel === "Email" && m.recipient_contact.includes("@") ? `<a class="btn sm" href="mailto:${esc(m.recipient_contact)}?subject=${encodeURIComponent(m.subject_or_header)}&body=${encodeURIComponent(m.message_body)}">${icon("mail")} Open in email app</a>` : ""}</div>
        </details>`).join("")}
      ${c.volunteer_action_checklist.length ? `<div><h4 style="margin-bottom:4px">Volunteer checklist</h4>${c.volunteer_action_checklist.map((v) => `
        <label class="list-row small" style="cursor:pointer"><input type="checkbox" class="checkbox"><div class="grow">${esc(v.instruction)}
          <div class="tiny muted">${esc(v.assigned_team)} · ${esc(v.priority.startsWith("IMMEDIATE") ? "next 5 minutes" : "next 15 minutes")}</div></div></label>`).join("")}</div>` : ""}
      <p class="tiny faint">${icon("shield")} Personal details and keys were scrubbed from these drafts${c.scrubber_redactions ? ` (${c.scrubber_redactions} redaction${c.scrubber_redactions === 1 ? "" : "s"})` : ""}. Stagecraft never sends them for you.</p>
    </div></div>`;
}

function doneHtml(res) {
  const id = encodeURIComponent(st.ctx.id);
  return `<div class="grow"><div class="row" style="font-weight:600;color:var(--green)">${icon("check")} Approved${res.option_label ? `: ${esc(res.option_label)}` : ""}</div>
      ${res.applied_changes?.length ? `<div class="small muted">${res.applied_changes.map((c) => `${esc(c.title)}: ${esc(c.from)} → ${esc(c.to)}`).join("<br>")}</div>` : `<div class="small muted">The schedule has been updated.</div>`}</div>
    <div class="row wrap"><a class="btn sm" href="/api/events/${id}/calendar.ics?only_updated=true">${icon("calendar")} Calendar update (.ics)</a>
      <a class="btn sm" href="/live/${id}" target="_blank" rel="noopener">${icon("external")} Attendee page</a>
      <a class="btn sm" href="#/event/${id}/program">See the schedule</a></div>`;
}

function bindReview() {
  st.root.querySelectorAll("[data-choice]").forEach((b) => b.addEventListener("click", () => {
    if (st.committed || st.run.committed) return;
    st.choice = b.dataset.choice;
    resultView();
  }));
  st.root.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    const m = st.run.comms.speaker_and_moderator_messages[+b.dataset.copy];
    try { await navigator.clipboard.writeText(`${m.subject_or_header}\n\n${m.message_body}`); toast("Copied"); } catch { toast("Copy isn't available here", "bad"); }
  }));
  st.root.querySelector("#commit")?.addEventListener("click", async (e) => {
    busy(e.currentTarget, true, "Checking and saving…");
    const from = st.ctx.from || {};
    try {
      st.committed = await post(`${E()}/crisis/commit`, { run_id: st.run.run_id, option_choice: st.choice, flag_id: from.flag_id || null, message_id: from.message_id || null });
      st.run.committed = true;
      toast("Schedule updated");
      await st.ctx.refresh();
    } catch (err) {
      toast(err.message, "bad");
      busy(e.currentTarget, false);
      return;
    }
    resultView();
  });
}
