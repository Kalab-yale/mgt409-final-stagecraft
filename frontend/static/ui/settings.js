// "AI usage and safety": what the AI costs, proof that cost-cutting didn't hurt quality, the guardrails, and connections.
import { api, busy, esc, fmtWhen, post, toast } from "./core.js";
import { icon } from "./icons.js";

const SECTIONS = [["usage", "Usage and cost"], ["safety", "Safety"], ["connections", "Connections"]];
const usd = (n) => (Math.abs(n) >= 1 ? "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "$" + Number(n || 0).toFixed(4));
const n0 = (n) => Number(n || 0).toLocaleString();
const AGENT_LABEL = { TriageAgent: "Triage", SolverAgent: "Solver", CommsAgent: "Comms", InboxWatcher: "Inbox Watcher", BriefWriter: "Daily brief",
                      OnboardingAgent: "New-event setup" };
const agentLabel = (a) => AGENT_LABEL[a] || a.replace(/Assistant$/, " assistant");
const LAYERS = [
  { key: "Layer 1", name: "Injection firewall", what: "Screens every email, pasted text, and issue report for hidden instructions before any AI reads it. Blocked text costs $0." },
  { key: "Layer 2", name: "Action lock", what: "A rule checker (not AI) verifies every schedule change: room capacity, double-booking, locked keynotes, closed rooms." },
  { key: "Layer 3", name: "Spending limit", what: "Stops any run that goes over its token, dollar, or step limit, so a confused agent can't loop or overspend." },
];

let root = null;

export async function showSettings(app, crumb, section) {
  if (!SECTIONS.some(([k]) => k === section)) section = "usage";
  crumb.innerHTML = `${icon("next")}<strong>AI usage and safety</strong>`;
  document.title = "AI usage and safety · Stagecraft";
  app.innerHTML = `<div class="page">
    <div class="page-head"><div><h1>AI usage and safety</h1>
      <p class="muted" style="margin-top:4px">What the AI costs, the proof that saving money didn't make it worse, and the guardrails that keep it safe.</p></div></div>
    <div class="segmented settings-nav" role="tablist">${SECTIONS.map(([k, label]) => `<button class="${k === section ? "active" : ""}" data-sec="${k}">${label}</button>`).join("")}</div>
    <div class="stack" id="settings-body" style="gap:18px"><div class="card card-pad"><div class="skeleton" style="width:50%"></div></div></div></div>`;
  app.querySelectorAll("[data-sec]").forEach((b) => b.addEventListener("click", () => { location.hash = `#/settings/${b.dataset.sec}`; }));
  root = app.querySelector("#settings-body");
  try {
    await { usage: usageSection, safety: safetySection, connections: connectionsSection }[section]();
  } catch (err) {
    root.innerHTML = `<div class="card card-pad">Couldn't load this page: ${esc(err.message)}</div>`;
  }
}

// ================= Usage and cost =================
async function usageSection() {
  const d = await api("/api/admin/tokenomics");
  const s = d.summary, pr = s.per_run, b = d.latest_benchmark;
  root.innerHTML = `
    <div class="card card-pad row-between wrap">
      <div class="row" style="gap:14px;align-items:flex-start">
        <button class="switch" id="cs" role="switch" aria-checked="${d.cost_saver_enabled}" aria-label="Cost-Saver mode"></button>
        <div><div style="font-weight:600">Cost-Saver mode is ${d.cost_saver_enabled ? "on" : "off"}</div>
          <p class="small muted" style="max-width:640px">${d.cost_saver_enabled
            ? "Agents get a short, focused summary of the event (only the rooms, sessions, and people the problem touches), arranged so the AI provider can reuse cached parts of the prompt at a 90% discount."
            : "Agents get the full event data on every call. More tokens, same answers. Useful as the comparison baseline."}</p></div>
      </div>
    </div>
    <div class="stat-grid">
      ${stat("Total AI spend", usd(s.totals.cost_usd), `${n0(s.totals.tokens)} tokens · ${s.totals.agent_calls} AI calls`)}
      ${stat("Cost per issue resolved", pr.optimized_usd ? usd(pr.optimized_usd) : "–", pr.unoptimized_usd ? `vs ${usd(pr.unoptimized_usd)} with full context` : "run an issue to measure")}
      ${stat("Saved by Cost-Saver", pr.savings_pct ? `${pr.savings_pct}%` : "–", esc(pr.unoptimized_source))}
      ${stat("Per organizer, per month", usd(s.per_user_month.optimized_usd), `${s.per_user_month.runs} issues a month`)}
    </div>
    <div class="card" id="bench">${benchHtml(b)}</div>
    <div class="grid-2">
      <div class="card"><div class="card-head"><h3>${icon("attendees")} If more organizers used it</h3></div><div class="card-body table-wrap">
        <table class="table"><thead><tr><th>Organizers</th><th class="r">Per month</th><th class="r">Without Cost-Saver</th><th class="r">Saved per year</th></tr></thead>
        <tbody>${s.scale.map((x) => `<tr><td class="num">${n0(x.users)}</td><td class="r num">${usd(x.monthly_optimized_usd)}</td><td class="r num muted">${usd(x.monthly_unoptimized_usd)}</td>
          <td class="r num" style="color:var(--green)">${usd(x.annual_savings_usd)}</td></tr>`).join("")}</tbody></table>
        <p class="tiny faint" style="margin-top:8px">${s.assumptions.map(esc).join(" · ")}. ${esc(s.pricing_per_1m_tokens.model)}: $${s.pricing_per_1m_tokens.input} per million input tokens, $${s.pricing_per_1m_tokens.cached_input} cached, $${s.pricing_per_1m_tokens.output} output.</p></div></div>
      <div class="card"><div class="card-head"><h3>${icon("sparkles")} Where the tokens go</h3></div><div class="card-body table-wrap">
        <table class="table"><thead><tr><th>Agent</th><th class="r">Calls</th><th class="r">Avg tokens</th><th class="r">Avg cost</th><th class="r">Avg time</th></tr></thead>
        <tbody>${Object.entries(s.by_agent).sort((a, b) => b[1].calls - a[1].calls).map(([a, x]) => `<tr><td>${esc(agentLabel(a))}</td><td class="r num">${x.calls}</td>
          <td class="r num">${n0(x.avg_tokens)}</td><td class="r num">${usd(x.avg_cost_usd)}</td><td class="r num">${(x.avg_latency_ms / 1000).toFixed(1)}s</td></tr>`).join("")}</tbody></table></div></div>
    </div>
    <div class="card"><div class="card-head"><h3>${icon("clock")} Recent AI calls</h3><span class="muted small">every call is logged with its tokens and cost</span></div>
      <div class="card-body table-wrap"><table class="table"><thead><tr><th>When</th><th>Agent</th><th>For</th><th class="r">Tokens</th><th class="r">Cached</th><th class="r">Cost</th></tr></thead>
      <tbody>${d.agent_calls.sort((a, b) => b.timestamp.localeCompare(a.timestamp)).slice(0, 15).map((c) => `<tr><td class="small muted" style="white-space:nowrap">${esc(fmtWhen(c.timestamp))}</td>
        <td>${esc(agentLabel(c.agent_name))}</td><td class="small">${esc(c.scenario_title)}</td><td class="r num">${n0(c.total_tokens)}</td>
        <td class="r num muted">${n0(c.cached_input_tokens)}</td><td class="r num">${usd(c.cost_usd)}</td></tr>`).join("")}</tbody></table></div></div>`;
  root.querySelector("#cs").addEventListener("click", async (e) => {
    try { await post("/api/admin/tokenomics/toggle-cost-saver", { enabled: e.currentTarget.getAttribute("aria-checked") !== "true" }); await usageSection(); }
    catch (err) { toast(err.message, "bad"); }
  });
  bindBench();
}

const stat = (label, value, sub) => `<div class="card kpi"><div class="stat-label">${label}</div><div class="v num">${value}</div><div class="s">${sub}</div></div>`;

function benchHtml(b) {
  const head = `<div class="card-head"><h3>${icon("check")} Did saving money make it worse?</h3>
    <button class="btn sm" id="bench-run" title="Runs the same issue twice, with Cost-Saver off and on (about $0.006)">${icon("refresh")} Run a fresh comparison</button></div>`;
  if (!b) return `${head}<div class="card-body"><p class="muted">No comparison yet. It resolves the same issue twice, once with full context and once with Cost-Saver, and checks both reach the same verified fix.</p></div>`;
  const side = (x, label, win) => `<div class="side ${win ? "win" : ""}"><div class="row-between"><strong>${label}</strong>${x.layer2_verified ? `<span class="pill green">${icon("check")} Verified</span>` : `<span class="pill red">Not verified</span>`}</div>
    <div class="num" style="font-size:20px;font-weight:600;margin-top:6px">${n0(x.total_tokens)} <span class="small muted" style="font-weight:400">tokens</span></div>
    <div class="small muted">${usd(x.cost_usd)} · ${(x.latency_ms / 1000).toFixed(1)}s · ${x.notifications} messages, ${x.volunteer_tasks} volunteer tasks</div>
    <div class="small" style="margin-top:6px">${esc(x.recommended_option)}</div></div>`;
  const q = b.quality, sv = b.savings;
  return `${head}<div class="card-body stack">
    <p><strong style="color:${q.both_layer2_verified ? "var(--green)" : "var(--orange)"}">${q.both_layer2_verified ? "No quality loss." : "Check the runs."}</strong> ${esc(q.verdict.replace(/^No quality loss:\s*(.)/, (_, c) => c.toUpperCase()))}</p>
    <div class="ab">${side(b.unoptimized, "Full context", false)}${side(b.optimized, "Cost-Saver", true)}</div>
    <p class="small muted">Same issue (“${esc(b.scenario)}”): ${sv.total_tokens_pct}% fewer tokens, ${sv.cost_pct}% lower cost, ${sv.latency_pct}% faster.
      ${q.identical_resolution ? "Both chose the same room and time." : "Both found a valid fix, with different moves."} Constraints: ${esc(q.constraint_satisfaction)}.
      ${b.baseline_exceeds_production_budget ? "The full-context run would have gone over the normal token limit." : ""} ${esc(b.source)} · ${esc(fmtWhen(b.created_at))}</p></div>`;
}

function bindBench() {
  root.querySelector("#bench-run")?.addEventListener("click", async (e) => {
    busy(e.currentTarget, true, "Running both modes (about a minute)…");
    try {
      const b = await post("/api/admin/tokenomics/benchmark", { use_cached: false });
      root.querySelector("#bench").innerHTML = benchHtml(b);
      toast("Comparison finished");
      bindBench();
    } catch (err) { toast(err.message, "bad"); busy(e.currentTarget, false); }
  });
}

// ================= Safety =================
async function safetySection() {
  const d = await api("/api/admin/security");
  const c = d.config;
  const count = (k) => Object.entries(d.counts).find(([l]) => l.startsWith(k))?.[1] || 0;
  root.innerHTML = `
    <div class="grid-3">${LAYERS.map((l) => `<div class="card card-pad"><div class="layer-tag">${l.key}</div><h3 style="margin-top:2px">${l.name}</h3>
      <p class="small muted" style="margin-top:4px">${l.what}</p><div class="num" style="font-size:22px;font-weight:600;margin-top:8px">${count(l.key)}</div>
      <div class="tiny muted">times it stepped in</div></div>`).join("")}</div>
    <div class="grid-2">
      <div class="card"><div class="card-head"><h3>${icon("settings")} Limits and rules</h3></div><div class="card-body">
        ${num("max_cost_per_run_usd", "Spending limit per run (USD)", c.max_cost_per_run_usd, 0.01, 0.001)}
        ${num("max_tokens_per_run", "Token limit per run", c.max_tokens_per_run, 1000, 1000)}
        ${num("max_agent_loop_iterations", "Max steps per agent", c.max_agent_loop_iterations, 1, 1)}
        ${tog("block_locked_keynote_cancellation", "Never move or cancel locked keynotes", c.block_locked_keynote_cancellation)}
        ${tog("enforce_strict_room_capacity", "Never put more people in a room than it holds", c.enforce_strict_room_capacity)}
        ${tog("redact_sensitive_pii_and_keys", "Scrub phone numbers, card numbers, and keys from drafts", c.redact_sensitive_pii_and_keys)}
        <div class="row" style="justify-content:flex-end;margin-top:10px"><button class="btn primary sm" id="save-guard">Save</button></div></div></div>
      <div class="card"><div class="card-head"><h3>${icon("shield")} Attack test</h3><button class="btn sm" id="redteam">Run the attack test</button></div>
        <div class="card-body" id="redteam-out"><p class="small muted">Fires three attacks at the app: a hidden-instruction email that tries to cancel the keynote and leak contacts,
          an AI-proposed schedule that breaks the venue rules, and a runaway agent loop. Each should be stopped by a different layer. Costs nothing: no AI is called.</p></div></div>
    </div>
    <div class="card"><div class="card-head"><h3>${icon("alert")} Incident log</h3><span class="muted small">${d.incidents.length} logged</span></div>
      <div class="card-body table-wrap">${d.incidents.length ? `<table class="table"><thead><tr><th>When</th><th>Layer</th><th>Severity</th><th>What happened</th><th>What we did</th></tr></thead>
        <tbody>${d.incidents.slice(0, 25).map((i) => `<tr><td class="small muted" style="white-space:nowrap">${esc(fmtWhen(i.timestamp))}</td>
          <td class="small" style="white-space:nowrap">${esc(LAYERS.find((l) => i.defense_layer.startsWith(l.key))?.name || i.defense_layer)}</td>
          <td><span class="pill ${i.severity === "CRITICAL" ? "red" : i.severity === "HIGH" ? "orange" : ""}">${esc(i.severity.toLowerCase())}</span></td>
          <td class="small">${esc(i.blocked_reason.length > 160 ? i.blocked_reason.slice(0, 160) + "…" : i.blocked_reason)}</td>
          <td class="small muted">${esc(i.action_taken.length > 120 ? i.action_taken.slice(0, 120) + "…" : i.action_taken)}</td></tr>`).join("")}</tbody></table>`
        : `<p class="muted small">Nothing has been blocked yet.</p>`}</div></div>`;
  root.querySelector("#save-guard").addEventListener("click", async (e) => {
    const body = {};
    root.querySelectorAll("[data-num]").forEach((i) => { body[i.dataset.num] = Number(i.value); });
    root.querySelectorAll("[data-tog]").forEach((t) => { body[t.dataset.tog] = t.getAttribute("aria-checked") === "true"; });
    busy(e.currentTarget, true);
    try { await post("/api/admin/security/update-config", body); toast("Saved"); } catch (err) { toast(err.message, "bad"); }
    busy(e.currentTarget, false);
  });
  root.querySelectorAll("[data-tog]").forEach((t) => t.addEventListener("click", () => t.setAttribute("aria-checked", t.getAttribute("aria-checked") !== "true")));
  root.querySelector("#redteam").addEventListener("click", async (e) => {
    busy(e.currentTarget, true, "Attacking…");
    try {
      const r = await post("/api/admin/security/red-team-test");
      root.querySelector("#redteam-out").innerHTML = `<p style="margin-bottom:6px"><span class="pill ${r.all_passed ? "green" : "red"}">${r.all_passed ? "All attacks stopped" : "Some attacks got through"}</span></p>
        ${r.results.map((x) => `<div class="check-row"><span class="pill ${x.passed ? "green" : "red"}" style="margin-top:1px">${x.passed ? icon("check") : icon("x")}</span>
          <div class="grow"><div style="font-weight:500">${esc(x.test.replace(/^\w+ \d+: /, ""))}</div>
          <div class="tiny muted">Stopped by ${esc(LAYERS.find((l) => x.layer.startsWith(l.key))?.name || x.layer)}${x.details?.length ? ` · ${esc(x.details[0])}` : ""}</div></div></div>`).join("")}`;
      safetyCountsRefresh();
    } catch (err) { toast(err.message, "bad"); }
    busy(e.currentTarget, false);
  });
}

async function safetyCountsRefresh() {
  // The attack test logs new incidents; reload the page section quietly after a moment so the log shows them.
  const out = root.querySelector("#redteam-out").innerHTML;
  await safetySection();
  root.querySelector("#redteam-out").innerHTML = out;
}

const num = (k, label, v, step, min) => `<div class="field-row"><label class="small" for="g-${k}">${label}</label>
  <input class="input num" id="g-${k}" data-num="${k}" type="number" step="${step}" min="${min}" value="${v}"></div>`;
const tog = (k, label, v) => `<div class="field-row"><span class="small">${label}</span>
  <button class="switch" style="justify-self:end" role="switch" data-tog="${k}" aria-checked="${v}" aria-label="${label}"></button></div>`;

// ================= Connections =================
async function connectionsSection() {
  const [sys, gm] = await Promise.all([api("/api/admin/system"), api("/api/connectors/gmail/status")]);
  root.innerHTML = `
    <div class="grid-2">
      <div class="card"><div class="card-head"><h3>${icon("sparkles")} AI model</h3><span class="pill ${sys.api_key_loaded ? "green" : "red"}">${sys.api_key_loaded ? "Connected" : "No API key"}</span></div>
        <div class="card-body small stack" style="gap:6px">
          <div><span class="muted">Model:</span> ${esc(sys.model)}</div><div><span class="muted">Through:</span> ${esc(sys.provider)}</div>
          <div><span class="muted">API key:</span> ${sys.api_key_loaded ? `loaded from ${esc(sys.env_files_found.join(", "))} (never shown or sent to the browser)` : "add PORTKEY_API_KEY to .env"}</div>
          <div><span class="muted">Price per million tokens:</span> $${sys.pricing_per_1m_tokens.input} input · $${sys.pricing_per_1m_tokens.cached_input} cached · $${sys.pricing_per_1m_tokens.output} output</div></div></div>
      <div class="card"><div class="card-head"><h3>${icon("mail")} Gmail</h3><span class="pill ${gm.mode === "gmail" ? "green" : ""}">${gm.mode === "gmail" ? "Connected" : "Using the demo inbox"}</span></div>
        <div class="card-body small stack" style="gap:8px">
          <p>${esc(gm.note)} Read-only: Stagecraft searches and reads email through the course Gmail MCP server and never sends.</p>
          ${gm.available && !gm.signed_in ? `<ol style="margin:0;padding-left:18px">${gm.setup_steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
            <div class="mail-body" style="font-family:ui-monospace,Menlo,monospace;font-size:12px">${esc(gm.login_command)}</div>` : ""}
          ${gm.available ? `<div class="row"><button class="btn sm" id="gm-check">Test connection</button><span id="gm-out" class="small"></span></div>` : ""}
          <p class="tiny faint">Import emails from an event's Inbox tab.</p></div></div>
      <div class="card"><div class="card-head"><h3>${icon("cloud")} Weather and travel</h3><span class="pill green">Connected</span></div>
        <div class="card-body small">${esc(sys.weather_source)}. Used for travel-risk flags and by the Triage agent.</div></div>
      <div class="card"><div class="card-head"><h3>${icon("clipboard")} Evidence for the write-up</h3></div>
        <div class="card-body small stack" style="gap:8px"><p>One JSON file with every logged AI call, the cost comparison, the scale projection, the guardrail settings, and the incident log.</p>
          <div class="row wrap"><a class="btn sm" href="/api/admin/export-evidence" download>${icon("clipboard")} Download evidence</a>
            <a class="btn plain sm" href="/classic">Open the original console</a></div></div></div>
    </div>
    <div class="card"><div class="card-head"><h3>${icon("venue")} Venues</h3><span class="muted small">${sys.venues.length} on file</span></div>
      <div class="card-body">${sys.venues.map((v) => `<div class="list-row"><div class="grow"><div style="font-weight:500">${esc(v.name)}</div>
        <div class="tiny muted">${esc(v.city)}, ${esc(v.state_or_country)} · ${esc(v.airport_corridor)}</div></div><span class="small muted">${v.room_count} rooms</span></div>`).join("")}</div></div>`;
  root.querySelector("#gm-check")?.addEventListener("click", async (e) => {
    busy(e.currentTarget, true, "Testing…");
    try { const r = await post("/api/connectors/gmail/check"); root.querySelector("#gm-out").innerHTML = `<span style="color:${r.ok ? "var(--green)" : "var(--red)"}">${esc(r.reason)}</span>`; }
    catch (err) { toast(err.message, "bad"); }
    busy(e.currentTarget, false);
  });
}
