/* Stagecraft AI - Settings, Tokenomics & Guardrails admin console (Step 11).
   Loaded before app.js; only defines functions, which use app.js helpers ($, esc, api, toast, state) at call time. */
"use strict";

const admin = { tab: "usage", redTeam: null, benchmark: null, busy: false };
const fmtUsd = (n) => (Math.abs(n) >= 1 ? "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                                         : "$" + Number(n || 0).toFixed(5));
const n0 = (n) => Number(n || 0).toLocaleString();
const sevPill = (s) => `<span class="pill ${s === "CRITICAL" ? "pill-bad" : s === "HIGH" ? "pill-warn" : "pill-muted"} font-mono">${esc(s)}</span>`;
const layerShort = (l) => (l || "").split(":")[0];

function initAdmin() {
  document.querySelectorAll("[data-admin-close]").forEach((el) => el.addEventListener("click", closeAdmin));
  document.querySelectorAll("[data-admin-tab]").forEach((t) => t.addEventListener("click", () => openAdmin(t.dataset.adminTab)));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAdmin(); });
  const pill = $("#last-run-pill");
  pill.addEventListener("click", () => openAdmin("usage"));
  pill.addEventListener("keydown", (e) => { if (e.key === "Enter") openAdmin("usage"); });
}

function openAdmin(tab = admin.tab) {
  admin.tab = tab;
  $("#modal-admin").classList.remove("hidden");
  document.querySelectorAll("[data-admin-tab]").forEach((t) => t.classList.toggle("active", t.dataset.adminTab === tab));
  ({ usage: renderUsageTab, security: renderSecurityTab, system: renderSystemTab })[tab]();
}
const closeAdmin = () => $("#modal-admin").classList.add("hidden");

function adminLoading(label) {
  $("#admin-body").innerHTML = `<div class="flex items-center gap-2 text-sm text-slate-400"><span class="spinner"></span> Loading ${esc(label)}…</div>`;
}

// ================= Tab 1: Usage, tokenomics & scaling =================
async function renderUsageTab() {
  adminLoading("token meters");
  let d;
  try { d = await api("/api/admin/tokenomics"); } catch (err) { $("#admin-body").innerHTML = `<div class="result-bad">${esc(err.message)}</div>`; return; }
  if (admin.tab !== "usage") return;
  const s = d.summary, opt = s.optimized, pr = s.per_run, bench = admin.benchmark || d.latest_benchmark;
  const savings = bench ? bench.savings.total_tokens_pct : null;
  const parity = bench ? bench.quality : null;
  $("#admin-body").innerHTML = `
    <div class="rounded-xl border border-line bg-panel p-5">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <div class="flex items-center gap-4">
          <button id="cs-switch" class="switch" role="switch" aria-checked="${d.cost_saver_enabled}" aria-label="Cost-Saver mode"></button>
          <div>
            <div class="text-sm font-semibold text-white">Cost-Saver Mode <span class="text-slate-400 font-normal">(Context Pruning + Prompt Caching)</span></div>
            <div class="text-xs text-slate-500">${d.cost_saver_enabled ? "ON: agents get a compact, cache-friendly context focused on the disruption." : "OFF: agents get the full verbose JSON dump of the event (baseline)."}</div>
          </div>
        </div>
        <div class="flex flex-wrap items-center gap-3">
          <label class="flex items-center gap-2 text-xs text-slate-400"><input id="bench-cached" type="checkbox" checked class="h-4 w-4 accent-indigo-500"> reuse earlier live runs if available</label>
          <button id="bench-run" class="btn-primary"><span class="spinner hidden"></span>⚡ Run A/B Cost vs. Quality Benchmark</button>
        </div>
      </div>
      <div id="bench-result" class="mt-4">${bench ? benchmarkHtml(bench) : `<p class="text-xs text-slate-500">No benchmark yet. It runs the same crisis with Cost-Saver OFF and ON (about 2 pipeline runs, well under a cent) and checks both produce the same verified resolution.</p>`}</div>
    </div>

    <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <div class="kpi"><div class="metric-label">AVG TOKENS / RUN (COST-SAVER)</div>
        <div class="kpi-value">${opt.runs ? n0(opt.avg_total_tokens) : "—"}</div>
        <div class="mt-1 font-mono text-[11px] text-slate-400">${opt.runs ? `in ${n0(opt.avg_input_tokens)} · cached ${n0(opt.avg_cached_tokens)} · out ${n0(opt.avg_output_tokens)}` : "no optimized runs yet"}</div></div>
      <div class="kpi"><div class="metric-label">AVG COST / RUN</div>
        <div class="kpi-value">${pr.optimized_usd ? fmtUsd(pr.optimized_usd) : "—"}</div>
        <div class="mt-1 font-mono text-[11px] text-slate-400">${pr.unoptimized_usd ? `baseline ${fmtUsd(pr.unoptimized_usd)}` : ""} · ${opt.runs} run(s)</div></div>
      <div class="kpi"><div class="metric-label">${bench ? "TOKEN SAVINGS (A/B)" : "COST SAVINGS VS BASELINE"}</div>
        <div class="kpi-value text-emerald-300">${savings !== null ? savings + "%" : pr.savings_pct ? pr.savings_pct + "%" : "—"}</div>
        <div class="mt-1 text-[11px] text-slate-400">${bench ? `cost −${bench.savings.cost_pct}% · same crisis, both modes` : esc(pr.unoptimized_source)}</div></div>
      <div class="kpi"><div class="metric-label">QUALITY PARITY</div>
        <div class="kpi-value text-[15px] ${parity && parity.both_layer2_verified ? "text-emerald-300" : "text-slate-300"}">${parity ? (parity.both_layer2_verified ? "100% Constraint Satisfaction Preserved" : "Check runs") : "Run the benchmark"}</div>
        <div class="mt-1 text-[11px] text-slate-400">${parity ? (parity.identical_resolution ? "identical room/time resolution in both modes" : "both verified; different valid moves") : ""}</div></div>
    </div>

    <div class="rounded-xl border border-line bg-panel p-5">
      <div class="section-title"><i data-lucide="trending-up" class="h-4 w-4"></i> Scale Economics Projection</div>
      <div class="mt-3 rounded-lg border border-line overflow-x-auto">${scaleTableHtml(s)}</div>
      <p class="mt-2 text-[11px] text-slate-500">${s.assumptions.map(esc).join(" · ")}. Prices: ${esc(s.pricing_per_1m_tokens.model)} $${s.pricing_per_1m_tokens.input}/1M in, $${s.pricing_per_1m_tokens.cached_input}/1M cached, $${s.pricing_per_1m_tokens.output}/1M out.</p>
    </div>

    <div class="rounded-xl border border-line bg-panel p-5">
      <div class="flex items-center justify-between"><div class="section-title"><i data-lucide="activity" class="h-4 w-4"></i> Per-Agent Telemetry Log</div>
        <span class="font-mono text-[11px] text-slate-500">${s.totals.runs} runs · ${s.totals.agent_calls} agent calls · ${n0(s.totals.tokens)} tokens · ${fmtUsd(s.totals.cost_usd)} total</span></div>
      <div class="mt-3 rounded-lg border border-line overflow-x-auto max-h-[420px] overflow-y-auto">${agentLogHtml(d.agent_calls)}</div>
    </div>`;
  $("#cs-switch").addEventListener("click", toggleCostSaver);
  $("#bench-run").addEventListener("click", runBenchmark);
  icons();
}

function scaleTableHtml(s) {
  const pr = s.per_run;
  const rows = [["Single crisis run", 1, pr.unoptimized_usd, pr.optimized_usd]];
  s.scale.forEach((t) => rows.push([`${n0(t.users)} organizer${t.users === 1 ? "" : "s"} / month`, t.runs_per_month, t.monthly_unoptimized_usd, t.monthly_optimized_usd]));
  return `<table class="data-table"><thead><tr><th>SCALE</th><th class="num">RUNS / MO</th><th class="num">UNOPTIMIZED BASELINE</th>
      <th class="num">STAGECRAFT COST-SAVER</th><th class="num">SAVED / MO</th><th class="num">SAVED / YEAR</th></tr></thead><tbody>
    ${rows.map(([label, runs, base, opt], i) => `<tr class="${i === rows.length - 1 ? "hl" : ""}"><td>${label}</td><td class="num">${n0(runs)}</td>
      <td class="num">${fmtUsd(base)}</td><td class="num text-emerald-300">${fmtUsd(opt)}</td>
      <td class="num">${fmtUsd(base - opt)}</td><td class="num">${i === 0 ? "—" : fmtUsd((base - opt) * 12)}</td></tr>`).join("")}
  </tbody></table>`;
}

function agentLogHtml(calls) {
  if (!calls.length) return `<p class="p-4 text-sm text-slate-500">No agent calls logged yet. Run a crisis in the War-Room or create an event with the AI Architect.</p>`;
  return `<table class="data-table"><thead><tr><th>TIME (UTC)</th><th>SCENARIO</th><th>AGENT</th><th>MODE</th>
      <th class="num">INPUT</th><th class="num">CACHED</th><th class="num">OUTPUT</th><th class="num">LATENCY</th><th class="num">COST</th></tr></thead><tbody>
    ${calls.map((c) => `<tr><td class="font-mono text-[11px] text-slate-400 whitespace-nowrap">${esc(c.timestamp.slice(5, 16).replace("T", " "))}</td>
      <td class="max-w-[220px] truncate" title="${esc(c.scenario_title)}">${esc(c.scenario_title)}</td><td class="whitespace-nowrap">${esc(c.agent_name)}</td>
      <td>${c.cost_saver_enabled ? '<span class="pill pill-ok">Saver</span>' : '<span class="pill pill-muted">Full</span>'}</td>
      <td class="num">${n0(c.input_tokens)}</td><td class="num">${n0(c.cached_input_tokens)}</td><td class="num">${n0(c.output_tokens)}</td>
      <td class="num">${n0(Math.round(c.latency_ms))} ms</td><td class="num">${fmtUsd(c.cost_usd)}</td></tr>`).join("")}
  </tbody></table>`;
}

function benchmarkHtml(b) {
  const a = b.unoptimized, o = b.optimized, q = b.quality;
  const row = (label, x, y, fmt, better = "lower") => {
    const win = better === "lower" ? y < x : y > x;
    return `<tr><td>${label}</td><td class="num">${fmt(x)}</td><td class="num ${win ? "text-emerald-300" : ""}">${fmt(y)}</td></tr>`;
  };
  return `
    <div class="rounded-lg border border-line overflow-hidden">
      <div class="flex flex-wrap items-center justify-between gap-2 bg-panel2 px-4 py-2.5">
        <span class="text-sm font-medium text-white">A/B: ${esc(b.scenario)}</span>
        <span class="font-mono text-[11px] text-slate-400">${esc(b.source)} · ${esc((b.created_at || "").replace("T", " ").slice(0, 16))} UTC</span>
      </div>
      <div class="grid gap-4 p-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
        <div class="overflow-x-auto"><table class="data-table"><thead><tr><th>METRIC</th><th class="num">COST-SAVER OFF</th><th class="num">COST-SAVER ON</th></tr></thead><tbody>
          ${row("Input tokens", a.input_tokens, o.input_tokens, n0)}
          ${row("Cached input tokens", a.cached_input_tokens, o.cached_input_tokens, n0, "higher")}
          ${row("Output tokens", a.output_tokens, o.output_tokens, n0)}
          ${row("Total tokens", a.total_tokens, o.total_tokens, n0)}
          ${row("Cost per run", a.cost_usd, o.cost_usd, fmtUsd)}
          ${row("Wall-clock latency", a.latency_ms, o.latency_ms, (v) => (v / 1000).toFixed(1) + " s")}
          <tr><td>Status</td><td class="num">${esc(a.status)}</td><td class="num">${esc(o.status)}</td></tr>
          <tr><td>Layer 2 verified</td><td class="num">${a.layer2_verified ? "✓" : "✗"}</td><td class="num">${o.layer2_verified ? "✓" : "✗"}</td></tr>
          ${a.cost_breakdown_usd ? `<tr><td class="text-slate-400 text-[11.5px]">↳ uncached / cached / output $</td>
            <td class="num text-[11px] text-slate-400">${["uncached_input", "cached_input", "output"].map((k) => a.cost_breakdown_usd[k].toFixed(5)).join(" / ")}</td>
            <td class="num text-[11px] text-slate-400">${["uncached_input", "cached_input", "output"].map((k) => o.cost_breakdown_usd[k].toFixed(5)).join(" / ")}</td></tr>` : ""}
          <tr><td>Recommended plan</td><td class="text-right text-[11.5px]">${esc(a.recommended_option || "—")}</td><td class="text-right text-[11.5px]">${esc(o.recommended_option || "—")}</td></tr>
        </tbody></table></div>
        <div class="space-y-3">
          <div class="grid grid-cols-3 gap-2">
            <div class="kpi"><div class="metric-label">TOKENS SAVED</div><div class="kpi-value text-emerald-300 text-lg">${b.savings.total_tokens_pct}%</div></div>
            <div class="kpi"><div class="metric-label">COST SAVED</div><div class="kpi-value text-emerald-300 text-lg">${b.savings.cost_pct}%</div></div>
            <div class="kpi"><div class="metric-label">INPUT SAVED</div><div class="kpi-value text-emerald-300 text-lg">${b.savings.input_tokens_pct}%</div></div>
          </div>
          <div class="${q.both_layer2_verified ? "result-ok" : "result-bad"}">
            <div class="font-medium ${q.both_layer2_verified ? "text-emerald-200" : "text-red-200"}">Quality: ${esc(q.constraint_satisfaction)}</div>
            <div class="mt-1 text-slate-300 text-[13px]">${esc(q.verdict)}</div>
            <div class="mt-2 flex flex-wrap gap-1.5">
              <span class="pill ${q.both_succeeded ? "pill-ok" : "pill-bad"}">both runs succeeded</span>
              <span class="pill ${q.both_layer2_verified ? "pill-ok" : "pill-bad"}">both Layer-2 verified</span>
              <span class="pill ${q.identical_resolution ? "pill-ok" : "pill-warn"}">${q.identical_resolution ? "identical resolution" : "different valid resolution"}</span>
            </div>
          </div>
          ${b.production_token_budget ? `<div class="text-[12px] text-slate-400">Production budget ${n0(b.production_token_budget)} tokens/run:
            baseline ${b.baseline_exceeds_production_budget ? '<span class="text-red-300 font-medium">EXCEEDS it (would trip the circuit breaker)</span>' : "fits"},
            Cost-Saver uses <span class="font-mono text-emerald-300">${b.optimized_budget_used_pct}%</span>.
            ${String(b.source).includes("cache bypassed") ? "Gateway response cache bypassed, so every token and second was freshly measured." : ""}</div>` : ""}
        </div>
      </div>
    </div>`;
}

async function toggleCostSaver() {
  const sw = $("#cs-switch");
  const next = sw.getAttribute("aria-checked") !== "true";
  try {
    const r = await api("/api/admin/tokenomics/toggle-cost-saver", { method: "POST", body: JSON.stringify({ enabled: next }) });
    $("#cost-saver").checked = r.cost_saver_enabled;
    toast(`Cost-Saver ${r.cost_saver_enabled ? "ON: compact, cache-friendly context" : "OFF: full context baseline"}`, r.cost_saver_enabled ? "ok" : "warn");
    renderUsageTab();
  } catch (err) { toast(err.message, "bad"); }
}

async function runBenchmark() {
  const btn = $("#bench-run");
  setBusy(btn, true);
  const useCached = $("#bench-cached").checked;
  $("#bench-result").innerHTML = `<div class="flex items-center gap-2 text-sm text-slate-400"><span class="spinner"></span>
    ${useCached ? "Looking for earlier runs, otherwise running" : "Running"} the same crisis with Cost-Saver OFF, then ON (live agents, ~40-60 s)…</div>`;
  try {
    admin.benchmark = await api("/api/admin/tokenomics/benchmark", { method: "POST", body: JSON.stringify({ event_id: state.eventId, use_cached: useCached }) });
    toast(`Benchmark: ${admin.benchmark.savings.total_tokens_pct}% fewer tokens, ${admin.benchmark.quality.constraint_satisfaction}`, "ok");
    renderUsageTab();
  } catch (err) {
    $("#bench-result").innerHTML = `<div class="result-bad">Benchmark failed: ${esc(err.message)}</div>`;
    setBusy(btn, false);
  }
}

// ================= Tab 2: Security guardrails & red-team =================
const GUARD_FIELDS = [
  ["max_cost_per_run_usd", "Max Budget per Run", "USD", 0.01],
  ["max_tokens_per_run", "Max Tokens per Run", "tokens", 1000],
  ["max_agent_loop_iterations", "Max Loop Iterations", "per agent", 1],
];
const GUARD_TOGGLES = [
  ["block_locked_keynote_cancellation", "Locked Keynote Deletion Shield"],
  ["enforce_strict_room_capacity", "Strict Capacity & AV Verification Lock"],
  ["redact_sensitive_pii_and_keys", "Outbound PII Redaction (keys are always redacted)"],
];

async function renderSecurityTab() {
  adminLoading("guardrails");
  let d;
  try { d = await api("/api/admin/security"); } catch (err) { $("#admin-body").innerHTML = `<div class="result-bad">${esc(err.message)}</div>`; return; }
  if (admin.tab !== "security") return;
  const c = d.config;
  $("#admin-body").innerHTML = `
    <div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
      <div class="rounded-xl border border-line bg-panel p-5">
        <div class="section-title"><i data-lucide="sliders-horizontal" class="h-4 w-4"></i> Active Guardrail Controls</div>
        <div class="mt-4 space-y-3">
          ${GUARD_FIELDS.map(([k, label, unit, step]) => `<label class="flex items-center justify-between gap-3 text-sm"><span class="text-slate-300">${label} <span class="text-xs text-slate-500">(${unit})</span></span>
            <input data-guard="${k}" type="number" min="0" step="${step}" value="${c[k]}" class="input-dark w-36 text-right font-mono"></label>`).join("")}
          ${GUARD_TOGGLES.map(([k, label]) => `<div class="flex items-center justify-between gap-3 text-sm"><span class="text-slate-300">${label}</span>
            <button class="switch" role="switch" data-guard-toggle="${k}" aria-checked="${c[k]}" aria-label="${esc(label)}"></button></div>`).join("")}
        </div>
        <div class="mt-4 flex items-center justify-between gap-2">
          <span class="text-[11px] text-slate-500">Layer 3 enforces these mid-run; onboarding gets 3x the token/loop budget.</span>
          <button id="guard-save" class="btn-primary"><span class="spinner hidden"></span>Save Guardrails</button>
        </div>
      </div>
      <div class="rounded-xl border border-line bg-panel p-5">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="section-title"><i data-lucide="swords" class="h-4 w-4"></i> Red-Team Self-Test</div>
          <button id="redteam-run" class="btn-primary bg-bad hover:bg-red-600"><span class="spinner hidden"></span>🚨 Run 3-Layer Red-Team Security Self-Test</button>
        </div>
        <p class="mt-1 text-xs text-slate-500">Fires a real injection email, a hallucinated solver plan, and a runaway-loop budget spike at the live guardrails. No LLM tokens are spent.</p>
        <div id="redteam-results" class="mt-4 space-y-3">${admin.redTeam ? redTeamHtml(admin.redTeam) : ""}</div>
      </div>
    </div>
    <div class="rounded-xl border border-line bg-panel p-5">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="section-title"><i data-lucide="scroll-text" class="h-4 w-4"></i> Security Incident Audit Log</div>
        <span class="font-mono text-[11px] text-slate-500">${Object.entries(d.counts).map(([l, n]) => `${layerShort(l)}: ${n}`).join(" · ")} · total ${d.incidents.length}</span>
      </div>
      <div class="mt-3 rounded-lg border border-line overflow-x-auto max-h-[420px] overflow-y-auto">${incidentTableHtml(d.incidents)}</div>
    </div>`;
  document.querySelectorAll("[data-guard-toggle]").forEach((b) => b.addEventListener("click", () =>
    b.setAttribute("aria-checked", b.getAttribute("aria-checked") !== "true")));
  $("#guard-save").addEventListener("click", saveGuardrails);
  $("#redteam-run").addEventListener("click", runRedTeam);
  icons();
}

function incidentTableHtml(rows) {
  if (!rows.length) return `<p class="p-4 text-sm text-slate-500">No incidents yet. Run the red-team self-test or dispatch the Preset 4 booby-trap.</p>`;
  return `<table class="data-table"><thead><tr><th>TIME (UTC)</th><th>SEVERITY</th><th>LAYER</th><th>EVENT</th><th>BLOCKED REASON</th><th>ACTION TAKEN</th></tr></thead><tbody>
    ${rows.map((i) => `<tr><td class="font-mono text-[11px] text-slate-400 whitespace-nowrap">${esc(i.timestamp.slice(5, 19).replace("T", " "))}</td>
      <td>${sevPill(i.severity)}</td><td class="whitespace-nowrap" title="${esc(i.defense_layer)}">${esc(layerShort(i.defense_layer))}</td>
      <td class="font-mono text-[11px]">${esc(i.event_id)}</td>
      <td class="max-w-[380px]"><div class="line-clamp-2" title="${esc(i.blocked_reason)}">${esc(i.blocked_reason)}</div></td>
      <td class="text-slate-400 max-w-[240px]">${esc(i.action_taken)}</td></tr>`).join("")}
  </tbody></table>`;
}

function redTeamHtml(r) {
  return `
    <div class="${r.all_passed ? "result-ok" : "result-bad"} flex items-center justify-between gap-2">
      <span class="font-medium ${r.all_passed ? "text-emerald-200" : "text-red-200"}">${r.all_passed ? "All 3 defenses fired and logged to security_audit_logs" : "A defense did NOT fire, investigate"}</span>
      <span class="font-mono text-[11px] text-slate-400">event ${esc(r.event_id)}</span>
    </div>
    ${r.results.map((t) => `
      <article class="rounded-xl border ${t.passed ? "border-ok/50" : "border-bad/60"} bg-[#0E1525] p-4">
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div><div class="text-sm font-semibold text-white">${esc(t.test)}</div><div class="text-xs text-slate-500">${esc(t.layer)}</div></div>
          <div class="flex flex-wrap gap-1.5">
            <span class="pill ${t.defense_fired ? "pill-ok" : "pill-bad"} font-mono">${t.defense_fired ? (t.expected === "TRIPPED" ? "CIRCUIT TRIPPED" : "INTERCEPTED") : "NOT BLOCKED"}</span>
            ${t.severity ? sevPill(t.severity) : ""}
            <span class="pill ${t.logged_to_audit ? "pill-ok" : "pill-bad"}">${t.logged_to_audit ? "logged ✓" : "not logged"}</span>
          </div>
        </div>
        <div class="mt-3 metric-label">ATTACK PAYLOAD</div>
        <div class="payload mt-1">${esc(typeof t.attack_input === "string" ? t.attack_input : JSON.stringify(t.attack_input, null, 2))}</div>
        <div class="mt-3 metric-label">BLOCKED BECAUSE</div>
        <ul class="mt-1 space-y-0.5 text-[12.5px] text-slate-300 list-disc pl-5">${t.details.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        <div class="mt-2 font-mono text-[10.5px] text-slate-500">incident ${esc(t.incident_id || "—")}</div>
      </article>`).join("")}`;
}

async function saveGuardrails() {
  const btn = $("#guard-save");
  const body = {};
  document.querySelectorAll("[data-guard]").forEach((i) => { body[i.dataset.guard] = i.dataset.guard === "max_cost_per_run_usd" ? parseFloat(i.value) : parseInt(i.value, 10); });
  document.querySelectorAll("[data-guard-toggle]").forEach((b) => { body[b.dataset.guardToggle] = b.getAttribute("aria-checked") === "true"; });
  setBusy(btn, true);
  try {
    await api("/api/admin/security/update-config", { method: "POST", body: JSON.stringify(body) });
    toast("Guardrails saved and active for the next run", "ok");
  } catch (err) {
    toast(`Rejected: ${(err.body && err.body.error) || err.message}`, "bad");
  } finally { setBusy(btn, false); }
}

async function runRedTeam() {
  const btn = $("#redteam-run");
  setBusy(btn, true);
  try {
    admin.redTeam = await api("/api/admin/security/red-team-test", { method: "POST", body: JSON.stringify({ event_id: state.eventId }) });
    toast(admin.redTeam.all_passed ? "Red-team: all 3 layers intercepted their attack" : "Red-team: a defense failed", admin.redTeam.all_passed ? "ok" : "bad");
    renderSecurityTab();
  } catch (err) {
    toast(err.message, "bad");
    setBusy(btn, false);
  }
}

// ================= Tab 3: Venues, models & evidence =================
async function renderSystemTab() {
  adminLoading("system status");
  let d;
  try { d = await api("/api/admin/system"); } catch (err) { $("#admin-body").innerHTML = `<div class="result-bad">${esc(err.message)}</div>`; return; }
  if (admin.tab !== "system") return;
  const w = state.weather;
  $("#admin-body").innerHTML = `
    <div class="grid gap-3 md:grid-cols-3">
      <div class="kpi"><div class="metric-label">LLM PROVIDER</div>
        <div class="mt-1 text-sm text-white font-medium">${esc(d.model)} · ${esc(d.provider)}</div>
        <div class="mt-2 flex flex-wrap gap-1.5"><span class="pill ${d.api_key_loaded ? "pill-ok" : "pill-bad"}">${d.api_key_loaded ? "API key loaded (value hidden)" : "No API key"}</span>
          ${d.env_files_found.map((f) => `<span class="pill pill-muted font-mono">${esc(f)} found</span>`).join("")}</div>
        <div class="mt-2 text-[11px] text-slate-500">Dual-path lookup: ${d.env_lookup_order.map(esc).join(" → ")}</div></div>
      <div class="kpi"><div class="metric-label">LIVE VENUE TELEMETRY</div>
        <div class="mt-1 text-sm text-white font-medium">${esc(d.weather_source)}</div>
        <div class="mt-2">${w ? `<span class="pill ${w.source === "open-meteo-live" ? "pill-ok" : "pill-warn"}">${w.source === "open-meteo-live" ? "Open-Meteo Live API Connected" : "Offline fallback cache"}</span>
          <span class="ml-1 font-mono text-[11px] text-slate-400">${esc(w.venue.city)} ${Math.round(w.conditions.temperature_c)}°C · ${esc(w.transit_delay_risk)}</span>` : '<span class="pill pill-muted">not checked yet</span>'}</div></div>
      <div class="kpi"><div class="metric-label">MODE &amp; PRICING</div>
        <div class="mt-1 text-sm text-white font-medium">Cost-Saver ${d.cost_saver_mode ? "ON" : "OFF"}</div>
        <div class="mt-2 font-mono text-[11px] text-slate-400">$${d.pricing_per_1m_tokens.input} in · $${d.pricing_per_1m_tokens.cached_input} cached · $${d.pricing_per_1m_tokens.output} out / 1M</div>
        <div class="mt-1 font-mono text-[11px] text-slate-500">db: ${esc(d.database)}</div></div>
    </div>

    <div class="grid gap-5 xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
      <div class="rounded-xl border border-line bg-panel p-5">
        <div class="section-title"><i data-lucide="map" class="h-4 w-4"></i> Venue Blueprints</div>
        <div class="mt-3 rounded-lg border border-line overflow-x-auto">
          <table class="data-table"><thead><tr><th>VENUE</th><th>CITY</th><th class="num">LAT, LON</th><th>CORRIDOR</th><th class="num">ROOMS</th></tr></thead><tbody>
          ${d.venues.map((v) => `<tr><td>${esc(v.name)}</td><td>${esc(v.city)}</td><td class="num">${v.latitude.toFixed(4)}, ${v.longitude.toFixed(4)}</td>
            <td class="font-mono text-[11px] text-slate-400">${esc(v.airport_corridor || "—")}</td><td class="num">${v.room_count}</td></tr>`).join("")}
          </tbody></table>
        </div>
      </div>
      <div class="rounded-xl border border-line bg-panel p-5">
        <div class="section-title"><i data-lucide="building" class="h-4 w-4"></i> Quick Venue Creator</div>
        <div class="mt-3 grid gap-2 sm:grid-cols-2">
          <input id="v-name" class="input-dark sm:col-span-2" placeholder="Venue name (e.g. Hynes Convention Center)">
          <input id="v-city" class="input-dark" placeholder="City (e.g. Boston)">
          <input id="v-region" class="input-dark" placeholder="State / country (optional)">
          <input id="v-corridor" class="input-dark sm:col-span-2 font-mono text-xs" placeholder="Airport / rail corridor (e.g. BOS / South Station)">
          <textarea id="v-rooms" rows="4" class="input-dark sm:col-span-2 font-mono text-xs" placeholder="One room per line: name | capacity | AV, AV | hub&#10;Ballroom A | 500 | LED Video Wall, Stage Mic Array&#10;Room 101 | 120 | Dual Projection&#10;Atrium | 400 | Ambient PA | hub"></textarea>
        </div>
        <div class="mt-3 flex items-center justify-between gap-2"><span class="text-[11px] text-slate-500">City is geocoded (Open-Meteo) for live weather.</span>
          <button id="v-create" class="btn-primary"><span class="spinner hidden"></span>Create Venue</button></div>
      </div>
    </div>

    <div class="rounded-xl border border-line bg-panel p-5 flex flex-wrap items-center justify-between gap-3">
      <div><div class="section-title"><i data-lucide="file-json" class="h-4 w-4"></i> Evidence Export</div>
        <p class="mt-1 text-xs text-slate-500">All real tokenomics runs, A/B benchmarks, security incidents, and crisis-resolution timings as one JSON file for the report.</p>
        <div id="evidence-summary" class="mt-2 font-mono text-[11px] text-slate-400"></div></div>
      <button id="evidence-export" class="btn-primary"><span class="spinner hidden"></span>📥 Export Run Evidence JSON</button>
    </div>`;
  $("#v-create").addEventListener("click", createVenue);
  $("#evidence-export").addEventListener("click", exportEvidence);
  icons();
}

async function createVenue() {
  const rooms = $("#v-rooms").value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
    const [name, cap, av, hub] = l.split("|").map((x) => (x || "").trim());
    return { name, capacity: parseInt(cap, 10), av_specs: av ? av.split(",").map((x) => x.trim()).filter(Boolean) : [], is_catering_hub: /hub/i.test(hub || "") };
  });
  const body = { name: $("#v-name").value.trim(), city: $("#v-city").value.trim(), state_or_country: $("#v-region").value.trim(),
                 airport_corridor: $("#v-corridor").value.trim(), rooms };
  const btn = $("#v-create");
  setBusy(btn, true);
  try {
    const v = await api("/api/admin/venues", { method: "POST", body: JSON.stringify(body) });
    toast(`Created ${v.name} (${v.latitude.toFixed(3)}, ${v.longitude.toFixed(3)}) with ${v.rooms.length} room(s)`, "ok");
    await loadEvents(state.eventId);
    renderSystemTab();
  } catch (err) {
    toast((err.body && err.body.detail) || err.message, "bad");
    setBusy(btn, false);
  }
}

async function exportEvidence() {
  const btn = $("#evidence-export");
  setBusy(btn, true);
  try {
    const bundle = await api("/api/admin/export-evidence");
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: `stagecraft-evidence-${bundle.generated_at.slice(0, 10)}.json` });
    document.body.appendChild(a); a.click(); a.remove();
    const cr = bundle.crisis_resolution;
    $("#evidence-summary").textContent = `${bundle.tokenomics.workflow_runs.length} metered runs · ${bundle.benchmarks.length} benchmark(s) · ${bundle.security.incident_counts.total} incidents · ${cr.successful_runs} resolved crises, avg ${cr.avg_resolution_seconds}s`;
    toast("Evidence JSON downloaded", "ok");
  } catch (err) { toast(err.message, "bad"); } finally { setBusy(btn, false); }
}
