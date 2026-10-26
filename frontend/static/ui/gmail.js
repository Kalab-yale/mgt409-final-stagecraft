// Connect Gmail and import only the emails that belong to this event: pick dates and keywords, preview (no AI, no cost),
// see which ones look related, tick, import. Read-only; Stagecraft never sends email.
import { api, busy, esc, fmtWhen, post, toast } from "./core.js";
import { icon } from "./icons.js";
import { modal } from "./modals.js";

const iso = (d) => d.toISOString().slice(0, 10);
const daysAgo = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return iso(d); };
const OPEN_FLAG = "stagecraft-open-gmail";

export async function openGmail(eventId, eventName, onDone) {
  let st;
  try { st = await api("/api/gmail/status"); } catch (err) { toast(err.message, "bad"); return; }
  if (!st.client_file) return setupDialog(st, eventId, eventName, onDone);
  if (!st.connected) return connectDialog(eventId);
  importDialog(st, eventId, eventName, onDone);
}

// After Google sends the browser back, main.js sets this flag; the Inbox tab reopens the dialog.
export const markReopen = () => sessionStorage.setItem(OPEN_FLAG, "1");
export function reopenIfReturning(eventId, eventName, onDone) {
  if (sessionStorage.getItem(OPEN_FLAG)) { sessionStorage.removeItem(OPEN_FLAG); openGmail(eventId, eventName, onDone); }
}

function setupDialog(st, eventId, eventName, onDone) {
  const m = modal("Connect Gmail", "One-time setup: Google asks every app that reads Gmail to be registered first. About 10 minutes.", `
    <ol class="setup-steps">${st.setup.map((s) => `<li>${esc(s.step)}${s.url ? ` <a href="${esc(s.url)}" target="_blank" rel="noopener">${icon("external")} Open</a>` : ""}</li>`).join("")}</ol>
    <p class="tiny muted">Use the inbox you want to connect (for testing, a dedicated address). Stagecraft only asks for read access.</p>
    <div class="row" style="justify-content:flex-end"><button class="btn" data-cancel>Close</button><button class="btn primary" id="gm-recheck">I've added the file</button></div>`);
  m.el.querySelector("[data-cancel]").addEventListener("click", m.close);
  m.el.querySelector("#gm-recheck").addEventListener("click", async () => {
    const s2 = await api("/api/gmail/status");
    if (!s2.client_file) { toast("Still can't find gmail_credentials.json. Check the file name and folder.", "bad"); return; }
    m.close();
    openGmail(eventId, eventName, onDone);
  });
}

function connectDialog(eventId) {
  const m = modal("Connect your Gmail", "", `
    <div class="gm-connect">
      <div class="gm-logo">${icon("mail", "lg")}</div>
      <ul class="small gm-promises">
        <li>${icon("lock")}<span><b>Read-only.</b> Stagecraft can't send, delete, or change anything in your mailbox.</span></li>
        <li>${icon("check")}<span><b>You choose what comes in.</b> Pick dates and keywords, preview, and tick the emails that belong to the event.</span></li>
        <li>${icon("shield")}<span><b>Only imported emails are read by AI</b>, and each passes the injection firewall first. Previews cost nothing.</span></li>
        <li>${icon("user")}<span><b>Yours alone.</b> The connection belongs to your Stagecraft account; disconnect any time.</span></li>
      </ul>
      <button class="btn gm-google" id="gm-go">${icon("mail")} Continue with Google</button>
      <p class="tiny faint">Google will say it hasn't verified this app. That's expected for a test app; choose Continue.</p>
    </div>`);
  m.el.querySelector("#gm-go").addEventListener("click", async (e) => {
    busy(e.target.closest("button"), true, "Opening Google…");
    try {
      const r = await post("/api/gmail/connect", { return_to: `#/event/${encodeURIComponent(eventId)}/inbox` });
      markReopen();
      location.href = r.auth_url;
    } catch (err) { toast(err.message, "bad"); busy(e.target.closest("button"), false); }
  });
}

function importDialog(st, eventId, eventName, onDone) {
  const short = (eventName.match(/\(([A-Z]{2,})\)/) || [])[1] || "";
  const m = modal("Import from Gmail", "", `
    <div class="row-between gm-acct"><span class="row small">${icon("mail")} Connected as <b>${esc(st.email || "your Gmail")}</b> <span class="pill tiny">read-only</span></span>
      <button class="btn plain sm" id="gm-disc">Disconnect</button></div>
    <div class="gm-filters">
      <div><label class="label" for="gm-from">From</label><input class="input" type="date" id="gm-from" value="${daysAgo(14)}"></div>
      <div><label class="label" for="gm-to">To</label><input class="input" type="date" id="gm-to" value="${iso(new Date())}"></div>
      <div class="grow"><label class="label" for="gm-kw">Keywords (optional)</label><input class="input" id="gm-kw" placeholder="${esc(short ? `e.g. ${short}, catering, invoice` : "e.g. catering, invoice")}"></div>
      <div><label class="label" for="gm-sender">From sender (optional)</label><input class="input" id="gm-sender" placeholder="name@company.com"></div>
      <button class="btn primary" id="gm-search">${icon("search")} Search</button>
    </div>
    <div class="row small muted" style="gap:14px"><label class="row" style="gap:6px"><input type="checkbox" class="checkbox" id="gm-only"> Only show emails that look related</label>
      <span id="gm-count"></span></div>
    <div id="gm-results" class="gm-results"><p class="small muted">Pick dates (and keywords if you like), then Search. Nothing is read by AI until you import.</p></div>
    <div id="gm-err"></div>
    <div class="row-between"><span class="tiny faint">Previews use only the subject, sender, and Gmail's short snippet.</span>
      <div class="row"><button class="btn" data-cancel>Close</button><button class="btn primary" id="gm-import" disabled>Import selected</button></div></div>`);
  m.el.querySelector(".modal").classList.add("wide");
  const $ = (s) => m.el.querySelector(s);
  let rows = [];
  const selected = () => [...m.el.querySelectorAll(".gm-row input:checked")].map((c) => c.value);
  const sync = () => {
    const n = selected().length;
    $("#gm-import").disabled = !n;
    $("#gm-import").textContent = n ? `Import ${n} selected` : "Import selected";
  };
  const draw = () => {
    const only = $("#gm-only").checked;
    const shown = rows.filter((r) => !only || r.related);
    $("#gm-results").innerHTML = shown.map((r) => `
      <label class="gm-row ${r.already_imported ? "done" : ""}">
        <input type="checkbox" class="checkbox" value="${esc(r.id)}" ${r.related && !r.already_imported ? "checked" : ""} ${r.already_imported ? "disabled" : ""}>
        <div class="grow" style="min-width:0">
          <div class="row-between"><span class="gm-from ellipsis">${esc(r.from_name)} <span class="tiny faint">${esc(r.from_email)}</span></span><span class="tiny faint" style="white-space:nowrap">${esc(fmtWhen(r.date))}</span></div>
          <div class="gm-subj ellipsis">${esc(r.subject)}</div>
          <div class="tiny muted gm-snip">${esc(r.snippet)}</div>
          <div class="row wrap" style="gap:4px;margin-top:4px">${r.already_imported ? `<span class="pill tiny green">${icon("check")} Already imported</span>` : ""}
            ${r.reasons.map((x) => `<span class="pill tiny blue">${icon("sparkles")} ${esc(x)}</span>`).join("")}
            ${!r.related && !r.already_imported ? `<span class="pill tiny">Not obviously related</span>` : ""}</div>
        </div>
      </label>`).join("") || `<p class="small muted">No emails match. Widen the dates or remove keywords.</p>`;
    m.el.querySelectorAll(".gm-row input").forEach((c) => c.addEventListener("change", sync));
    sync();
  };
  $("#gm-only").addEventListener("change", draw);
  $("#gm-search").addEventListener("click", async (e) => {
    $("#gm-err").innerHTML = "";
    busy(e.target.closest("button"), true, "Searching…");
    try {
      const r = await post(`/api/events/${encodeURIComponent(eventId)}/gmail/preview`,
        { after: $("#gm-from").value || null, before: $("#gm-to").value || null, keywords: $("#gm-kw").value, sender: $("#gm-sender").value, limit: 40 });
      rows = r.messages;
      $("#gm-count").textContent = `${rows.length} found · ${r.related} look related${r.more ? " · showing the newest 40" : ""}`;
      draw();
    } catch (err) { $("#gm-err").innerHTML = `<p class="form-error">${esc(err.message)}</p>`; }
    busy(e.target.closest("button"), false);
  });
  $("#gm-import").addEventListener("click", async (e) => {
    const ids = selected();
    busy(e.target.closest("button"), true, `Importing and reading ${ids.length}…`);
    try {
      const r = await post(`/api/events/${encodeURIComponent(eventId)}/gmail/import`, { ids });
      const p = r.processing;
      toast(`Imported ${r.imported}${p ? `: ${p.needs_reply} need a reply, ${p.flags_raised} flags, ${p.changes_drafted} drafted changes${p.quarantined ? `, ${p.quarantined} quarantined` : ""}` : ""}`);
      m.close();
      await onDone(r);
    } catch (err) { $("#gm-err").innerHTML = `<p class="form-error">${esc(err.message)}</p>`; busy(e.target.closest("button"), false); }
  });
  $("#gm-disc").addEventListener("click", async () => {
    if (!confirm("Disconnect Gmail? Emails already imported stay in Stagecraft.")) return;
    await post("/api/gmail/disconnect");
    toast("Gmail disconnected");
    m.close();
  });
  m.el.querySelector("[data-cancel]").addEventListener("click", m.close);
}
