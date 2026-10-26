// Team communication and wrap-up: team updates (with an AI draft), the stakeholder status report, meeting notes -> tasks,
// and the post-event feedback summary. The AI only writes drafts; people edit, confirm, post, or send.
import { legend, STATUS } from "./charts.js";
import { bindChangeActions, changeCard } from "./chat.js";
import { api, busy, esc, fmtWhen, post, toast } from "./core.js";
import { icon } from "./icons.js";
import { modal } from "./modals.js";

const E = (id) => `/api/events/${encodeURIComponent(id)}`;
const usageNote = (u) => (u ? `<span class="tiny faint">${u.total_tokens.toLocaleString()} tokens · $${Number(u.cost_usd).toFixed(4)}</span>` : "");
const errBox = (el, err) => { el.innerHTML = `<p class="form-error">${esc(err.body?.blocked ? "That text looks like a prompt-injection attempt, so it was blocked and logged." : err.message)}</p>`; };

// ---------- Team updates (Overview card) ----------
export async function mountTeamUpdates(el, eventId) {
  let data;
  try { data = await api(`${E(eventId)}/announcements`); } catch { el.innerHTML = ""; return; }
  const items = data.announcements;
  el.innerHTML = `
    <div class="card-head"><h3>${icon("bell")} Team updates</h3>
      ${data.can_post ? `<button class="btn sm" id="ann-new">${icon("send")} Post an update</button>` : `<span class="tiny muted">Leads post here</span>`}</div>
    <div class="card-body">${items.length ? items.slice(0, 4).map((a) => `
      <div class="ann">
        <div class="row-between"><b>${esc(a.title)}</b><span class="tiny faint" style="white-space:nowrap">${esc(a.author_name)} · ${esc(fmtWhen(a.created_at))}</span></div>
        <p class="small ann-body" title="Click to expand">${esc(a.body)}</p>
        ${a.ai_drafted ? `<span class="tiny faint">${icon("sparkles")} Drafted with AI, edited and sent by ${esc(a.author_name.split(" ")[0])}</span>` : ""}
      </div>`).join("") : `<p class="muted small">${data.can_post ? "Post an update and everyone on the team gets it in their notifications." : "Updates from the event leads will show here."}</p>`}</div>`;
  el.querySelectorAll(".ann-body").forEach((p) => p.addEventListener("click", () => p.classList.toggle("open")));
  el.querySelector("#ann-new")?.addEventListener("click", () => openAnnouncement(eventId, () => mountTeamUpdates(el, eventId)));
}

function openAnnouncement(eventId, onPosted) {
  let aiDrafted = false;
  const m = modal("Post a team update", "Everyone on the organizing team with a Stagecraft account gets it in their notifications.", `
    <div class="ai-strip"><input class="input" id="an-note" maxlength="500" placeholder="Optional: what should it say? e.g. thank everyone, ask for volunteers">
      <button class="btn sm" id="an-ai">${icon("sparkles")} Draft with AI</button></div>
    <p class="tiny faint" id="an-usage" style="margin-top:-6px">The AI writes from today's deadlines, flags, and changes. You edit before sending.</p>
    <div><label class="label" for="an-title">Title</label><input class="input" id="an-title" maxlength="120" placeholder="e.g. Volunteers needed for check-in"></div>
    <div><label class="label" for="an-body">Message</label><textarea class="textarea" id="an-body" rows="7" maxlength="2000"></textarea></div>
    <div id="an-err"></div>
    <div class="row" style="justify-content:flex-end"><button class="btn" data-cancel>Cancel</button><button class="btn primary" id="an-send">${icon("send")} Post to the team</button></div>`);
  const $ = (s) => m.el.querySelector(s);
  $("[data-cancel]").addEventListener("click", m.close);
  $("#an-ai").addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    busy(b, true, "Writing…");
    $("#an-err").innerHTML = "";
    try {
      const d = await post(`${E(eventId)}/announcements/draft`, { note: $("#an-note").value });
      $("#an-title").value = d.title;
      $("#an-body").value = d.body;
      $("#an-usage").innerHTML = `${icon("sparkles")} Drafted from today's data. Edit anything before posting. ${usageNote(d.usage)}`;
      aiDrafted = true;
    } catch (err) { errBox($("#an-err"), err); }
    busy(b, false);
  });
  $("#an-send").addEventListener("click", async (e) => {
    const title = $("#an-title").value.trim(), body = $("#an-body").value.trim();
    if (title.length < 3 || body.length < 3) { $("#an-err").innerHTML = `<p class="form-error">Add a title and a message.</p>`; return; }
    const b = e.target.closest("button");
    busy(b, true, "Posting…");
    try {
      const r = await post(`${E(eventId)}/announcements`, { title, body, ai_drafted: aiDrafted });
      toast(r.notified ? `Posted. ${r.notified} teammate${r.notified === 1 ? "" : "s"} notified.` : "Posted to the team feed");
      m.close();
      await onPosted();
    } catch (err) { errBox($("#an-err"), err); busy(b, false); }
  });
}

// ---------- Status report ----------
const AUDIENCES = [["chair", "Conference chair", "Candid: risks, budget, decisions"], ["sponsors", "Sponsors", "Outward-facing: no internal budget"],
                   ["som_events", "SOM events office", "Logistics: headcount, rooms, catering"]];

export function openStatusReport(eventId) {
  let audience = "chair";
  const m = modal("Write a status report", "An email-ready update from live data. Edit it, then copy it or open it in your email app.", `
    <div class="aud-pick" role="radiogroup" aria-label="Who is it for">${AUDIENCES.map(([k, l, s]) => `
      <button class="option ${k === audience ? "active" : ""}" role="radio" aria-checked="${k === audience}" data-aud="${k}"><span><strong>${l}</strong><br><span class="tiny muted">${s}</span></span></button>`).join("")}</div>
    <div class="row"><button class="btn primary" id="sr-go">${icon("sparkles")} Write it</button><span id="sr-usage"></span></div>
    <div id="sr-out" hidden>
      <div><label class="label" for="sr-subj">Subject</label><input class="input" id="sr-subj"></div>
      <div style="margin-top:10px"><label class="label" for="sr-body">Email</label><textarea class="textarea mono-ish" id="sr-body" rows="14"></textarea></div>
      <div class="row" style="justify-content:flex-end;margin-top:10px"><button class="btn" id="sr-copy">${icon("copy")} Copy</button><a class="btn primary" id="sr-mail" href="#">${icon("mail")} Open in email app</a></div>
    </div>
    <div id="sr-err"></div>`);
  m.el.querySelector(".modal").classList.add("wide");
  const $ = (s) => m.el.querySelector(s);
  const syncMail = () => { $("#sr-mail").href = `mailto:?subject=${encodeURIComponent($("#sr-subj").value)}&body=${encodeURIComponent($("#sr-body").value)}`; };
  m.el.querySelectorAll("[data-aud]").forEach((b) => b.addEventListener("click", () => {
    audience = b.dataset.aud;
    m.el.querySelectorAll("[data-aud]").forEach((x) => { x.classList.toggle("active", x === b); x.setAttribute("aria-checked", x === b); });
  }));
  $("#sr-go").addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    busy(b, true, "Writing…");
    $("#sr-err").innerHTML = "";
    try {
      const r = await post(`${E(eventId)}/status-report`, { audience });
      $("#sr-subj").value = r.subject;
      $("#sr-body").value = r.body;
      $("#sr-out").hidden = false;
      $("#sr-usage").innerHTML = usageNote(r.usage);
      syncMail();
    } catch (err) { errBox($("#sr-err"), err); }
    busy(b, false);
    b.innerHTML = `${icon("refresh")} Write again`;
  });
  $("#sr-subj").addEventListener("input", syncMail);
  $("#sr-body").addEventListener("input", syncMail);
  $("#sr-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(`Subject: ${$("#sr-subj").value}\n\n${$("#sr-body").value}`); toast("Copied"); }
    catch { $("#sr-body").select(); toast("Press Cmd+C to copy", "bad"); }
  });
}

// ---------- Meeting notes -> tasks (Team tab) ----------
const SAMPLE_NOTES = `Weekly organizer sync
- Jordan to recruit 6 more check-in volunteers by Friday
- Priya will post the speaker lineup on LinkedIn next Tuesday
- Tomás and Jordan: confirm the coffee station locations with the caterer
- Discussed moving the photo booth (no decision yet)`;

export function openMeetingNotes(eventId, onChanged) {
  const m = modal("Meeting notes to tasks", "Paste your notes. The AI drafts tasks with owners from the team and due dates; you confirm each one.", `
    <textarea class="textarea" id="mn-notes" rows="9" maxlength="5000" placeholder="Paste meeting notes or minutes…"></textarea>
    <div class="row-between"><button class="btn plain sm" id="mn-sample">Use sample notes</button>
      <button class="btn primary" id="mn-go">${icon("sparkles")} Draft tasks</button></div>
    <div id="mn-err"></div>
    <div id="mn-out"></div>`);
  m.el.querySelector(".modal").classList.add("wide");
  const $ = (s) => m.el.querySelector(s);
  $("#mn-sample").addEventListener("click", () => { $("#mn-notes").value = SAMPLE_NOTES; });
  $("#mn-go").addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    busy(b, true, "Reading the notes…");
    $("#mn-err").innerHTML = "";
    try {
      const r = await post(`${E(eventId)}/team/notes-to-tasks`, { notes: $("#mn-notes").value });
      $("#mn-out").innerHTML = `
        <div class="row-between" style="margin:6px 0 10px"><b>${r.changes.length} task${r.changes.length === 1 ? "" : "s"} drafted</b>${usageNote(r.usage)}</div>
        ${r.unknown_owners.length ? `<p class="small notice">${icon("alert")} Not on the team, so left unassigned: ${r.unknown_owners.map(esc).join(", ")}. Add them on the Team tab first.</p>` : ""}
        ${r.skipped.length ? `<p class="tiny muted">Skipped (already a task or invalid): ${r.skipped.map(esc).join("; ")}</p>` : ""}
        <div class="grid-2">${r.changes.map((c) => changeCard(c)).join("") || `<p class="muted small">No action items found.</p>`}</div>`;
      bindChangeActions($("#mn-out"), eventId, async () => {
        await onChanged();
        const left = await api(`${E(eventId)}/changes`);
        const pending = new Set(left.changes.map((c) => c.id));
        $("#mn-out").querySelectorAll(".change[data-change]").forEach((card) => {
          if (!pending.has(card.dataset.change)) card.outerHTML = `<div class="change done small muted">${icon("check")} Done</div>`;
        });
      });
    } catch (err) { errBox($("#mn-err"), err); }
    busy(b, false);
  });
}

// ---------- Post-event feedback ----------
const SAMPLE_FEEDBACK = `Loved the keynote, really inspiring and well organized.
The lunch line was way too long, I waited 25 minutes.
Great networking, I met two potential cofounders.
Panels ran over time and the afternoon felt rushed.
Lunch was slow but the food was good.
Fantastic speakers, would come again next year.
Room 2400 was too small for the fintech panel; people stood in the hall.
Check-in was quick and the volunteers were friendly.
Wish there had been more time for Q&A in the panels.
The pitch showcase was the highlight of my day.`;

export async function openFeedback(eventId) {
  const m = modal("Post-event feedback", "Paste survey answers, one response per line. The AI finds the themes; code checks its counts and quotes.", `
    <div id="fb-saved"></div>
    <textarea class="textarea" id="fb-text" rows="8" placeholder="One response per line (a CSV column pasted from Google Forms or Qualtrics works)"></textarea>
    <div class="row-between"><button class="btn plain sm" id="fb-sample">Use sample responses</button>
      <button class="btn primary" id="fb-go">${icon("sparkles")} Summarize</button></div>
    <div id="fb-err"></div>
    <div id="fb-out"></div>`);
  m.el.querySelector(".modal").classList.add("wide");
  const $ = (s) => m.el.querySelector(s);
  $("#fb-sample").addEventListener("click", () => { $("#fb-text").value = SAMPLE_FEEDBACK; });
  try {
    const saved = (await api(`${E(eventId)}/feedback`)).summary;
    if (saved) { $("#fb-out").innerHTML = feedbackHtml(saved); $("#fb-saved").innerHTML = `<p class="tiny muted">Showing the last summary (${esc(fmtWhen(saved.created_at))}). Paste new responses to replace it.</p>`; }
  } catch { /* nothing saved yet */ }
  $("#fb-go").addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    busy(b, true, "Reading responses…");
    $("#fb-err").innerHTML = "";
    try { $("#fb-out").innerHTML = feedbackHtml(await post(`${E(eventId)}/feedback/summarize`, { responses: $("#fb-text").value })); $("#fb-saved").innerHTML = ""; }
    catch (err) { errBox($("#fb-err"), err); }
    busy(b, false);
  });
}

const TONE = { positive: STATUS.good, mixed: "#8e8e93", negative: STATUS.critical };

function feedbackHtml(fb) {
  const s = fb.summary, total = s.positive + s.mixed + s.negative || 1;
  const maxT = Math.max(1, ...s.themes.map((t) => t.count));
  const seg = (k, n) => n ? `<span class="seg" style="flex:${n} 0 0;background:${TONE[k]}" title="${n} ${k}"></span>` : "";
  const list = (title, items, ic) => items.length ? `<div><div class="label">${icon(ic)} ${title}</div><ul class="small fb-list">${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : "";
  return `
    <div class="fb">
      <div class="row-between"><p class="brief-headline" style="margin:0">${esc(s.headline)}</p>${usageNote(fb.usage)}</div>
      <div class="fb-sent">
        <div class="segbar" style="height:14px" role="img" aria-label="${s.positive} positive, ${s.mixed} mixed, ${s.negative} negative">${seg("positive", s.positive)}${seg("mixed", s.mixed)}${seg("negative", s.negative)}</div>
        ${legend([[`Positive ${s.positive} (${Math.round(100 * s.positive / total)}%)`, TONE.positive], [`Mixed ${s.mixed}`, TONE.mixed], [`Negative ${s.negative}`, TONE.negative]])}
        <p class="tiny faint">${fb.responses} responses read${fb.screened_out ? ` · ${fb.screened_out} screened out by the injection firewall` : ""}${fb.checks.counts_add_up ? " · counts checked" : " · counts didn't add up; treat as approximate"}${fb.checks.quotes_dropped ? ` · ${fb.checks.quotes_dropped} quote(s) not found in the responses were removed` : ""}</p>
      </div>
      <div class="label">Themes</div>
      <div class="hbars">${s.themes.map((t) => `
        <div class="hbar-row"><span class="hbar-label">${esc(t.theme)}</span>
          <span class="hbar-track"><span class="hbar" style="width:${(100 * t.count) / maxT}%;background:${TONE[t.sentiment]}"></span></span>
          <span class="hbar-value">${t.count}</span></div>`).join("")}</div>
      <div class="grid-2">${list("What people liked", s.praise, "check")}${list("What to fix", s.complaints, "alert")}</div>
      ${s.quotes.length ? `<div class="fb-quotes">${s.quotes.map((q) => `<blockquote>“${esc(q.replace(/^"|"$/g, ""))}”</blockquote>`).join("")}</div>` : ""}
      ${list("For next year", s.actions, "next")}
    </div>`;
}
