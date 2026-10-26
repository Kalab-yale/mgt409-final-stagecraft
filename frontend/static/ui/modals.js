// Dialogs: create a new event (describe it / import a schedule / copy an event) and paste an email.
import { api, busy, esc, post, toast } from "./core.js";
import { icon } from "./icons.js";

export function modal(title, sub, body) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="m-title">
    <div class="modal-head"><div><h2 id="m-title">${esc(title)}</h2>${sub ? `<p class="muted small" style="margin-top:3px">${esc(sub)}</p>` : ""}</div>
      <button class="btn plain sm" data-close aria-label="Close">${icon("x")}</button></div>
    <div class="modal-body">${body}</div></div>`;
  const close = () => { back.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  back.querySelector("[data-close]").addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  document.body.appendChild(back);
  setTimeout(() => back.querySelector("textarea, input")?.focus(), 30);
  return { el: back, close };
}

const errorText = (err) => {
  const b = err.body || {};
  if (b.blocked) return "That text looks like a prompt-injection attempt, so it was blocked and logged.";
  return (b.errors && b.errors.join(" · ")) || err.message;
};

// ---------- New event ----------
const EXAMPLE = "Africa Fintech Founders Summit on 2027-03-12 at the Skylight International Convention Center in Addis Ababa for 400 attendees: an opening keynote, panels on mobile money and cross-border payments, a pitch showcase, and lunch.";

export async function openNewEvent() {
  let meta;
  try { meta = await api("/api/events"); } catch (err) { toast(err.message, "bad"); return; }
  const m = modal("New event", "Start from a sentence, a schedule, or an event you've run before.", `
    <div class="stack" style="gap:8px" role="radiogroup" aria-label="How to start">
      <button class="option active" data-mode="ai">${icon("sparkles", "lg")}<span><strong>Describe it</strong><br><span class="small muted">The AI drafts rooms, speakers, a conflict-free schedule, catering, and a starter plan. About 15 seconds.</span></span></button>
      <button class="option" data-mode="csv">${icon("list", "lg")}<span><strong>Import a schedule</strong><br><span class="small muted">Paste sessions as CSV at one of your venues. No AI needed.</span></span></button>
      <button class="option" data-mode="copy">${icon("copy", "lg")}<span><strong>Copy an existing event</strong><br><span class="small muted">Same venue and program, new name and date.</span></span></button>
    </div>
    <div id="mode-body"></div>
    <div id="mode-error"></div>
    <div class="row" style="justify-content:flex-end"><button class="btn" data-cancel>Cancel</button><button class="btn primary" id="create">Create event</button></div>`);
  let mode = "ai";
  const body = m.el.querySelector("#mode-body");
  const venues = meta.venues;
  const draw = () => {
    m.el.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    body.innerHTML = mode === "ai" ? `
      <label class="label" for="ne-prompt">Describe the event: name, date, city or venue, size, and what's on the program</label>
      <textarea class="textarea" id="ne-prompt" rows="5"></textarea>
      <button class="btn plain sm" type="button" id="ne-example" style="align-self:flex-start">Use an example</button>`
      : mode === "csv" ? `
      <div class="grid-2"><div><label class="label" for="ne-name">Event name</label><input class="input" id="ne-name" placeholder="Healthcare AI Day 2027"></div>
        <div><label class="label" for="ne-venue">Venue</label><select class="select" id="ne-venue">${venues.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join("")}</select></div>
        <div><label class="label" for="ne-date">Date</label><input class="input" id="ne-date" type="date"></div>
        <div><label class="label" for="ne-size">Expected attendees</label><input class="input" id="ne-size" type="number" min="1" value="250"></div></div>
      <div><label class="label" for="ne-csv">Sessions (CSV: title,track,room_name,start_time,end_time,expected_attendance,speaker_name)</label>
        <textarea class="textarea" id="ne-csv" rows="6" style="font-family:ui-monospace,Menlo,monospace;font-size:12px"></textarea>
        <button class="btn plain sm" type="button" id="ne-sample">Fill a sample for this venue</button></div>`
      : `
      <div><label class="label" for="ne-template">Copy from</label><select class="select" id="ne-template">${meta.events.map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join("")}</select></div>
      <div class="grid-2"><div><label class="label" for="ne-name">New name</label><input class="input" id="ne-name" placeholder="Yale Black Venture Summit 2027"></div>
        <div><label class="label" for="ne-date">Date</label><input class="input" id="ne-date" type="date"></div></div>
      <div><label class="label" for="ne-size">Expected attendees</label><input class="input" id="ne-size" type="number" min="1" value="350"></div>`;
    body.querySelector("#ne-example")?.addEventListener("click", () => { body.querySelector("#ne-prompt").value = EXAMPLE; });
    body.querySelector("#ne-sample")?.addEventListener("click", () => {
      const v = venues.find((x) => x.id === body.querySelector("#ne-venue").value);
      const rooms = [...v.rooms].sort((a, b) => b.capacity - a.capacity);
      const halls = rooms.filter((r) => !r.is_catering_hub), hub = rooms.find((r) => r.is_catering_hub) || halls[0];
      const q = (s) => (s.includes(",") ? `"${s}"` : s), cap = (r) => Math.floor(r.capacity * 0.8);
      body.querySelector("#ne-csv").value = ["title,track,room_name,start_time,end_time,expected_attendance,speaker_name",
        `Opening Keynote,Keynote,${q(halls[0].name)},09:00,10:00,${cap(halls[0])},Dana Whitlock`,
        `Panel: What's Next,Tech & AI,${q((halls[1] || halls[0]).name)},10:15,11:15,${cap(halls[1] || halls[0])},Ravi Menon;Grace Osei`,
        `Lunch,Catering & Networking,${q(hub.name)},12:00,13:00,${cap(hub)},`].join("\n");
    });
  };
  m.el.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => { mode = b.dataset.mode; draw(); }));
  m.el.querySelector("[data-cancel]").addEventListener("click", m.close);
  draw();

  m.el.querySelector("#create").addEventListener("click", async (e) => {
    const btn = e.currentTarget, err = m.el.querySelector("#mode-error");
    err.innerHTML = "";
    const val = (id) => body.querySelector(id)?.value.trim();
    let req;
    if (mode === "ai") {
      if ((val("#ne-prompt") || "").length < 20) { err.innerHTML = `<p class="small" style="color:var(--red)">Add a sentence or two: name, date, place, size, and sessions.</p>`; return; }
      req = post("/api/events/generate-ai", { prompt: val("#ne-prompt") });
    } else {
      if (!val("#ne-name") || !val("#ne-date")) { err.innerHTML = `<p class="small" style="color:var(--red)">Add a name and a date.</p>`; return; }
      const base = { event_name: val("#ne-name"), event_date: val("#ne-date"), attendee_count: Number(val("#ne-size")) || 100 };
      req = post("/api/events/import-csv", mode === "csv" ? { ...base, venue_id: val("#ne-venue"), csv_text: val("#ne-csv") } : { ...base, template_event_id: val("#ne-template") });
    }
    busy(btn, true, mode === "ai" ? "Drafting your event…" : "Creating…");
    try {
      const r = await req;
      toast(`Created ${r.event.name}`);
      m.close();
      location.hash = `#/event/${encodeURIComponent(r.event_id)}/overview`;
    } catch (ex) {
      err.innerHTML = `<p class="small" style="color:var(--red)">${esc(errorText(ex))}</p>`;
      busy(btn, false);
    }
  });
}

// ---------- Paste an email ----------
export function openPasteEmail(eventId, onDone) {
  const m = modal("Paste an email", "The Inbox Watcher reads it right away: it sorts it, flags risks, drafts changes, and writes a reply draft.", `
    <div class="grid-2"><div><label class="label" for="pe-name">From (name)</label><input class="input" id="pe-name" placeholder="Dana Price"></div>
      <div><label class="label" for="pe-email">From (email)</label><input class="input" id="pe-email" placeholder="dana@caterer.com"></div></div>
    <div><label class="label" for="pe-subject">Subject</label><input class="input" id="pe-subject"></div>
    <div><label class="label" for="pe-body">Email text</label><textarea class="textarea" id="pe-body" rows="8"></textarea></div>
    <div id="pe-error"></div>
    <div class="row" style="justify-content:flex-end"><button class="btn" data-cancel>Cancel</button><button class="btn primary" id="pe-go">${icon("sparkles")} Read this email</button></div>`);
  m.el.querySelector("[data-cancel]").addEventListener("click", m.close);
  m.el.querySelector("#pe-go").addEventListener("click", async (e) => {
    const v = (id) => m.el.querySelector(id).value.trim();
    const err = m.el.querySelector("#pe-error");
    if (!v("#pe-email").includes("@") || v("#pe-body").length < 5) { err.innerHTML = `<p class="small" style="color:var(--red)">Add the sender's email and the email text.</p>`; return; }
    busy(e.target.closest("button"), true, "Reading…");
    try {
      const r = await post(`/api/events/${encodeURIComponent(eventId)}/inbox/paste`, { from_name: v("#pe-name"), from_email: v("#pe-email"), subject: v("#pe-subject"), body: v("#pe-body") });
      toast(r.status === "Quarantined" ? "Quarantined: it looked like phishing or an injection attempt" : `Read: ${r.summary || r.subject}`, r.status === "Quarantined" ? "bad" : "");
      m.close();
      await onDone(r);
    } catch (ex) {
      err.innerHTML = `<p class="small" style="color:var(--red)">${esc(errorText(ex))}</p>`;
      busy(e.target.closest("button"), false);
    }
  });
}
