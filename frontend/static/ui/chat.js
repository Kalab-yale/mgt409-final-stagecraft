// The assistant panel on every workspace tab, plus the change cards (Confirm / Discard) used across the app.
import { api, busy, esc, fmtDate, post, TABS, toast } from "./core.js";
import { icon } from "./icons.js";

const SUGGEST = {
  overview: ["What should I focus on today?", "Summarize the red flags", "What's due this week?"],
  team: ["Who is overloaded right now?", "Assign the AV quote approval to Malik and Chris", "Add Dana Lee to the volunteers team"],
  program: ["Which speakers are still travelling?", "Is anyone double-booked?", "Move the AI Agents panel to Zhang Auditorium"],
  venue: ["Which rooms are close to capacity?", "Is the weather a travel risk?", "Which vendor contracts are unsigned?"],
  food: ["What's our dietary breakdown?", "When is the final headcount due?", "What if lunch runs 30 minutes late?"],
  attendees: ["How many people are on the waitlist?", "Find attendees from Kora Health", "How many VIPs are confirmed?"],
  budget: ["Where are we over budget?", "Which tasks are blocked?", "Which sponsors haven't paid yet?"],
  inbox: ["Which emails need a reply?", "Summarize the important emails", "Draft a reply to Leila Haddad"],
};

let chat = { eventId: null, tab: null, onChanged: () => {}, el: null };
let canApproveBudget = true;  // set from the planning payload's `me` (leads and the PMO approve money changes)
export const setApprover = (me) => { canApproveBudget = !me || me.can_approve_budget; };
const touchesBudget = (c) => c.payload?.table === "budget_items" || (c.payload?.table === "vendors" && (c.diff || []).some((d) => d.field === "contract_value_usd"));

// ---------- change cards ----------
const LABELS = { assignees: "Owners", is_pmo: "PMO", due_date: "Due date", planned_usd: "Planned", actual_usd: "Spent", contract_value_usd: "Contract value",
                 contract_status: "Contract", rsvp_status: "RSVP", attendee_type: "Type", late_penalty_usd: "Late penalty",
                 hard_cutoff_time: "Must be served by", scheduled_time: "Service time", expected_attendance: "Expected attendance" };
const label = (f) => LABELS[f] || f.charAt(0).toUpperCase() + f.slice(1).replaceAll("_", " ");
const value = (f, v) => {
  if (v === null || v === undefined || v === "") return "(empty)";
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(v))) return fmtDate(v);
  if (/_usd$/.test(f) && !isNaN(v)) return "$" + Number(v).toLocaleString();
  return String(v);
};

export function changeCard(c, { showOrigin = true } = {}) {
  const hasOwners = (c.diff || []).some((d) => d.field === "assignees");
  const rows = (c.diff || []).filter((d) => !["event_id", "source", "created_at"].includes(d.field) && !(hasOwners && d.field === "owner")).slice(0, 6).map((d) => d.before === null || d.before === undefined
    ? `<span class="f">${esc(label(d.field))}</span><span class="new">${esc(value(d.field, d.after))}</span>`
    : `<span class="f">${esc(label(d.field))}</span><span><span class="old">${esc(value(d.field, d.before))}</span> → <span class="new">${esc(value(d.field, d.after))}</span></span>`).join("");
  const origin = (c.origin || "").startsWith("inbox:") ? "From an email" : c.origin === "meeting_notes" ? "From meeting notes" : `${esc(TABS.find((t) => t.id === c.tab)?.label || c.tab)} assistant`;
  return `
    <div class="change" data-change="${esc(c.id)}">
      <div class="row-between"><span class="pill blue">Waiting for your OK</span>${showOrigin ? `<span class="tiny faint">${origin}</span>` : ""}</div>
      <div class="what" style="margin-top:6px">${esc(c.summary.split(":")[0])}</div>
      ${rows ? `<div class="diff">${rows}</div>` : ""}
      ${c.reason ? `<p class="small muted" style="margin-bottom:8px">Why: ${esc(c.reason)}</p>` : ""}
      ${touchesBudget(c) && !canApproveBudget
        ? `<div class="row"><button class="btn primary sm" disabled title="Only the PMO or an event lead can approve budget changes">${icon("lock")} Needs a lead's OK</button><button class="btn sm" data-act="discard">Discard</button></div>`
        : `<div class="row"><button class="btn primary sm" data-act="confirm">${icon("check")} Confirm</button><button class="btn sm" data-act="discard">Discard</button></div>`}
    </div>`;
}

export function bindChangeActions(container, eventId, after) {
  container.querySelectorAll(".change[data-change]").forEach((card) => {
    card.querySelectorAll("[data-act]").forEach((btn) => btn.addEventListener("click", async () => {
      const id = card.dataset.change;
      const confirm = btn.dataset.act === "confirm";
      busy(btn, true);
      try {
        const r = await post(`/api/events/${encodeURIComponent(eventId)}/changes/${id}/${confirm ? "confirm" : "reject"}`);
        if (r.change.status === "Failed") toast(`Couldn't apply: ${r.change.error}`, "bad");
        else toast(confirm ? "Saved" : "Discarded");
      } catch (err) {
        toast(err.message, "bad");
      }
      await after();
    }));
  });
}

// ---------- the panel ----------
export function mountChat(el, eventId, tab, onChanged) {
  chat = { eventId, tab, onChanged, el };
  const t = TABS.find((x) => x.id === tab);
  el.innerHTML = `
    <div class="chat-head">
      <div class="row-between"><h3 class="row">${icon("sparkles")} ${esc(t.assistant)}</h3>
        <button class="btn plain sm chat-toggle" data-close-chat aria-label="Close assistant">${icon("x")}</button></div>
      <p class="small muted" style="margin-top:2px">Answers from your event data. Any change waits for your OK.</p>
    </div>
    <div class="chat-scroll" id="chat-scroll"><div class="skeleton" style="width:60%"></div></div>
    <div class="chat-foot">
      <div class="pending-stack" id="chat-pending"></div>
      <div class="suggest" id="chat-suggest"></div>
      <form class="composer" id="chat-form">
        <label class="sr-only" for="chat-input">Message the ${esc(t.label)} assistant</label>
        <textarea id="chat-input" rows="1" placeholder="Ask, or paste details to add…"></textarea>
        <button class="send" id="chat-send" aria-label="Send">${icon("arrowUp")}</button>
      </form>
    </div>`;
  const input = el.querySelector("#chat-input");
  autosize(input);
  input.addEventListener("input", () => autosize(input));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  el.querySelector("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); send(); });
  el.querySelector("[data-close-chat]").addEventListener("click", () => el.classList.remove("open"));
  loadHistory();
}

export function prefillChat(text) {
  const input = chat.el?.querySelector("#chat-input");
  if (!input) return;
  input.value = text;
  autosize(input);
  input.focus();
}

function autosize(t) {
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 140) + "px";
}

async function loadHistory() {
  const { eventId, tab, el } = chat;
  let h;
  try {
    h = await api(`/api/events/${encodeURIComponent(eventId)}/assistants/${tab}/history`);
  } catch (err) {
    el.querySelector("#chat-scroll").innerHTML = `<p class="small muted">${esc(err.message)}</p>`;
    return;
  }
  if (chat.tab !== tab || chat.eventId !== eventId) return;
  const scroll = el.querySelector("#chat-scroll");
  scroll.innerHTML = h.messages.length ? h.messages.map(messageHtml).join("")
    : `<div class="msg assistant">Hi! I can look things up and draft changes for this part of the event. What do you need?</div>`;
  scroll.scrollTop = scroll.scrollHeight;
  renderPending(h.pending);
  renderSuggestions(SUGGEST[tab] || []);
}

function messageHtml(m) {
  if (m.role === "system") return `<div class="msg system">${icon(m.content.startsWith("Applied") ? "check" : "x")} ${esc(m.content)}</div>`;
  if (m.role === "user") return `<div class="msg user">${esc(m.content)}</div>`;
  const meta = m.meta || {};
  const steps = (meta.tool_calls || []).filter((s) => !s.startsWith("drafted:"));
  const u = meta.usage || {};
  return `<div class="msg assistant">${esc(m.content)}${steps.length || u.total_tokens ? `
    <details><summary>How I worked it out</summary>${steps.map((s) => `<div>· ${esc(s)}</div>`).join("")}
    ${u.total_tokens ? `<div class="faint">${u.total_tokens.toLocaleString()} tokens · $${Number(u.cost_usd).toFixed(4)} · ${(u.latency_ms / 1000).toFixed(1)}s</div>` : ""}</details>` : ""}</div>`;
}

function renderPending(list) {
  const box = chat.el.querySelector("#chat-pending");
  box.innerHTML = list.map((c) => changeCard(c, { showOrigin: false })).join("");
  bindChangeActions(box, chat.eventId, async () => { await chat.onChanged(); await loadHistory(); });
}

function renderSuggestions(items) {
  const box = chat.el.querySelector("#chat-suggest");
  box.innerHTML = items.slice(0, 3).map((s) => `<button type="button">${esc(s)}</button>`).join("");
  box.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    const input = chat.el.querySelector("#chat-input");
    input.value = b.textContent;
    send();
  }));
}

async function send() {
  const { eventId, tab, el } = chat;
  const input = el.querySelector("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  autosize(input);
  const scroll = el.querySelector("#chat-scroll");
  scroll.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(text)}</div><div class="typing" id="typing"><span></span><span></span><span></span></div>`);
  scroll.scrollTop = scroll.scrollHeight;
  el.querySelector("#chat-suggest").innerHTML = "";
  const sendBtn = el.querySelector("#chat-send");
  sendBtn.disabled = true;
  try {
    const r = await post(`/api/events/${encodeURIComponent(eventId)}/assistants/${tab}/chat`, { message: text });
    if (chat.tab !== tab) return;
    el.querySelector("#typing")?.remove();
    scroll.insertAdjacentHTML("beforeend", messageHtml({ role: "assistant", content: r.reply, meta: { tool_calls: r.tool_calls, usage: r.usage } }));
    scroll.scrollTop = scroll.scrollHeight;
    const h = await api(`/api/events/${encodeURIComponent(eventId)}/assistants/${tab}/history`);
    renderPending(h.pending);
    renderSuggestions(r.followups?.length ? r.followups : SUGGEST[tab] || []);
  } catch (err) {
    el.querySelector("#typing")?.remove();
    scroll.insertAdjacentHTML("beforeend", `<div class="msg system">${esc(err.message)}</div>`);
  } finally {
    sendBtn.disabled = false;
  }
}
