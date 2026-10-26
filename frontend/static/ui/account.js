// Account settings: profile, password and sessions, appearance (theme and accent), and deleting the account.
import { hue, initials, refreshUser, session, strength } from "./auth.js";
import { api, busy, esc, fmtWhen, post, toast } from "./core.js";
import { icon } from "./icons.js";
import { ACCENTS, currentAccent, currentTheme, setTheme, THEMES } from "./theme.js";

const SECTIONS = [["profile", "Profile", "user"], ["security", "Security", "lock"], ["appearance", "Appearance", "palette"], ["danger", "Delete account", "trash"]];
let root = null;

export function showAccount(app, crumb, section) {
  if (!SECTIONS.some(([k]) => k === section)) section = "profile";
  crumb.innerHTML = `${icon("next")}<strong>Account</strong>`;
  document.title = "Account · Stagecraft";
  const u = session.user;
  app.innerHTML = `
    <div class="page account">
      <div class="acct-hero">
        <span class="avatar xl" id="acct-avatar" style="--h:${hue(u.email)}">${esc(initials(u.display_name))}</span>
        <div><h1 id="acct-name">${esc(u.display_name)}</h1><p class="muted">${esc(u.email)} · member since ${esc(new Date(u.created_at).toLocaleDateString(undefined, { month: "long", year: "numeric" }))}</p></div>
      </div>
      <div class="acct">
        <nav class="acct-nav" aria-label="Account sections">${SECTIONS.map(([k, l, ic]) => `
          <a href="#/account/${k}" class="${k === section ? "active" : ""} ${k === "danger" ? "danger" : ""}" ${k === section ? 'aria-current="page"' : ""}>${icon(ic)} ${l}</a>`).join("")}</nav>
        <div class="acct-body" id="acct-body"></div>
      </div>
    </div>`;
  root = app.querySelector("#acct-body");
  ({ profile, security, appearance, danger })[section]();
}

const field = (id, label, attrs = "", hint = "") => `<div><label class="label" for="${id}">${label}</label><input class="input" id="${id}" ${attrs}>${hint}</div>`;
const err = (el, m) => (el.innerHTML = m ? `<p class="form-error">${esc(m)}</p>` : "");
const $ = (s) => root.querySelector(s);

// ---------------- Profile ----------------
function profile() {
  const u = session.user;
  root.innerHTML = `
    <section class="card card-pad stack">
      <div><h2>Your name</h2><p class="small muted">Shown in the top bar, on activity ("confirmed by …"), and to your team.</p></div>
      <div class="grid-2">${field("p-first", "First name", `value="${esc(u.first_name)}" maxlength="60" autocomplete="given-name"`)}${field("p-last", "Last name", `value="${esc(u.last_name)}" maxlength="60" autocomplete="family-name"`)}</div>
      <div id="p-err"></div>
      <div class="row" style="justify-content:flex-end"><button class="btn primary" id="p-save">Save name</button></div>
    </section>
    <section class="card card-pad stack">
      <div><h2>Email</h2><p class="small muted">You log in with this. Changing it asks for your password.</p></div>
      <div class="grid-2">${field("e-new", "New email", `type="email" value="${esc(u.email)}" maxlength="254" autocomplete="email"`)}
        <div><label class="label" for="e-pw">Current password</label><input class="input" id="e-pw" type="password" autocomplete="current-password" maxlength="128"></div></div>
      <div id="e-err"></div>
      <div class="row" style="justify-content:flex-end"><button class="btn" id="e-save">Change email</button></div>
    </section>
    <section class="card card-pad stack" id="acct-gmail"><div><h2>Connected accounts</h2>
      <p class="small muted">Connect your Gmail (read-only) to import event emails from the Inbox tab.</p></div><div class="skeleton" style="width:50%"></div></section>
    <section class="card card-pad"><div class="kv">
      <div><span class="muted small">Member since</span><b>${esc(fmtWhen(u.created_at))}</b></div>
      <div><span class="muted small">Last log in</span><b>${u.last_login_at ? esc(fmtWhen(u.last_login_at)) : "This session"}</b></div>
      <div><span class="muted small">Account id</span><b class="num">${esc(u.id)}</b></div></div></section>`;
  loadGmailCard();
  const preview = () => {
    const name = `${$("#p-first").value} ${$("#p-last").value}`.trim() || "?";
    document.getElementById("acct-avatar").textContent = initials(name);
  };
  $("#p-first").addEventListener("input", preview);
  $("#p-last").addEventListener("input", preview);
  $("#p-save").addEventListener("click", async (e) => {
    busy(e.target.closest("button"), true);
    try {
      const r = await post("/api/account/profile", { first_name: $("#p-first").value, last_name: $("#p-last").value });
      refreshUser(r.user);
      document.getElementById("acct-name").textContent = r.user.display_name;
      err($("#p-err"));
      toast("Name saved");
    } catch (ex) { err($("#p-err"), ex.message); }
    busy(e.target.closest("button"), false);
  });
  $("#e-save").addEventListener("click", async (e) => {
    busy(e.target.closest("button"), true);
    try {
      const r = await post("/api/account/email", { email: $("#e-new").value, password: $("#e-pw").value });
      refreshUser(r.user);
      $("#e-pw").value = "";
      err($("#e-err"));
      toast(`Email changed to ${r.user.email}`);
    } catch (ex) { err($("#e-err"), ex.message); }
    busy(e.target.closest("button"), false);
  });
}

// ---------------- Security ----------------
async function security() {
  root.innerHTML = `
    <section class="card card-pad stack">
      <div><h2>Change password</h2><p class="small muted">Changing it signs you out on every other device.</p></div>
      <div><label class="label" for="s-cur">Current password</label><input class="input" id="s-cur" type="password" autocomplete="current-password" maxlength="128"></div>
      <div class="grid-2">
        <div><label class="label" for="s-new">New password</label><input class="input" id="s-new" type="password" autocomplete="new-password" maxlength="128">
          <div class="pw-meter" aria-hidden="true"><span id="s-bar"></span></div><div class="tiny muted" id="s-hint">At least 8 characters.</div></div>
        <div><label class="label" for="s-new2">Confirm new password</label><input class="input" id="s-new2" type="password" autocomplete="new-password" maxlength="128"></div>
      </div>
      <div id="s-err"></div>
      <div class="row" style="justify-content:flex-end"><button class="btn primary" id="s-save">Change password</button></div>
    </section>
    <section class="card"><div class="card-head"><h3>${icon("monitor")} Where you're logged in</h3><button class="btn sm" id="s-others">Sign out everywhere else</button></div>
      <div class="card-body" id="s-list"><div class="skeleton" style="width:60%"></div></div></section>
    <section class="card card-pad">
      <h3 style="margin-bottom:8px">${icon("shield")} How your account is protected</h3>
      <ul class="small muted protect">
        <li>Your password is never stored. Only a salted scrypt hash is kept: slow and memory-hungry on purpose, so guessing is expensive.</li>
        <li>Your login cookie can't be read by scripts on the page, and only a hash of it is stored on the server.</li>
        <li>Five wrong passwords lock the account for 15 minutes; any one device is limited to 20 login attempts a minute.</li>
        <li>Changing your email, password, or deleting the account always asks for your current password.</li>
      </ul></section>`;
  $("#s-new").addEventListener("input", (e) => {
    const s = strength(e.target.value);
    Object.assign($("#s-bar").style, { width: `${s.pct}%`, background: s.color });
    $("#s-hint").textContent = e.target.value ? s.label : "At least 8 characters.";
  });
  $("#s-save").addEventListener("click", async (e) => {
    const nw = $("#s-new").value;
    if (nw.length < 8) return err($("#s-err"), "Use a password with at least 8 characters.");
    if (nw !== $("#s-new2").value) return err($("#s-err"), "The two new passwords don't match.");
    busy(e.target.closest("button"), true);
    try {
      const r = await post("/api/account/password", { current_password: $("#s-cur").value, new_password: nw, confirm_password: $("#s-new2").value });
      ["#s-cur", "#s-new", "#s-new2"].forEach((s) => ($(s).value = ""));
      err($("#s-err"));
      toast(`Password changed${r.other_sessions_ended ? `; signed out ${r.other_sessions_ended} other session${r.other_sessions_ended === 1 ? "" : "s"}` : ""}`);
      loadSessions();
    } catch (ex) { err($("#s-err"), ex.message); }
    busy(e.target.closest("button"), false);
  });
  $("#s-others").addEventListener("click", async (e) => {
    busy(e.target.closest("button"), true);
    try { const r = await post("/api/account/sessions/revoke-others"); toast(r.ended ? `Signed out ${r.ended} other session${r.ended === 1 ? "" : "s"}` : "No other sessions"); }
    catch (ex) { toast(ex.message, "bad"); }
    busy(e.target.closest("button"), false);
    loadSessions();
  });
  loadSessions();
}

async function loadSessions() {
  const box = $("#s-list");
  if (!box) return;
  let r;
  try { r = await api("/api/account/sessions"); } catch (ex) { box.textContent = ex.message; return; }
  box.innerHTML = r.sessions.map((s) => `
    <div class="list-row" style="align-items:center">
      <span class="sess-ic">${icon(/iPhone|Android|iPad/.test(s.device) ? "user" : "monitor")}</span>
      <div class="grow"><div style="font-weight:500">${esc(s.device)} ${s.current ? `<span class="pill green tiny">This device</span>` : ""}</div>
        <div class="tiny muted">Signed in ${esc(fmtWhen(s.created_at))} · last active ${esc(fmtWhen(s.last_seen_at))}</div></div>
      ${s.current ? "" : `<button class="btn sm" data-end="${esc(s.id)}">Sign out</button>`}
    </div>`).join("") || `<p class="small muted">No active sessions.</p>`;
  box.querySelectorAll("[data-end]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/api/account/sessions/${b.dataset.end}`, { method: "DELETE" }); toast("Signed out that session"); } catch (ex) { toast(ex.message, "bad"); }
    loadSessions();
  }));
}

// ---------------- Appearance ----------------
function appearance() {
  const art = (k) => `<div class="theme-art ${k}" aria-hidden="true"><i></i><b></b><span></span><span></span><em></em></div>`;
  root.innerHTML = `
    <section class="card card-pad stack">
      <div><h2>Theme</h2><p class="small muted">Saved to your account, so it follows you to other browsers.</p></div>
      <div class="theme-pick" role="radiogroup" aria-label="Theme">${THEMES.map(([k, l]) => `
        <button class="theme-card ${currentTheme() === k ? "active" : ""}" role="radio" aria-checked="${currentTheme() === k}" data-t="${k}">
          ${art(k)}<span class="row" style="gap:6px;justify-content:center">${icon(k === "light" ? "sun" : k === "dark" ? "moon" : "monitor")} ${l}</span>
          ${k === "system" ? `<span class="tiny muted">Follows your computer</span>` : ""}</button>`).join("")}</div>
    </section>
    <section class="card card-pad stack">
      <div><h2>Accent colour</h2><p class="small muted">Buttons, links, and highlights.</p></div>
      <div class="accent-pick" role="radiogroup" aria-label="Accent colour">${ACCENTS.map(([k, l, c]) => `
        <button class="accent-dot ${currentAccent() === k ? "active" : ""}" role="radio" aria-checked="${currentAccent() === k}" data-a="${k}" style="--dot:${c}">
          <span></span>${l}</button>`).join("")}</div>
      <div class="accent-preview"><button class="btn primary sm" tabindex="-1">${icon("check")} Confirm</button><span class="pill blue">Waiting for your OK</span>
        <a href="#/account/appearance" tabindex="-1">A link</a><span class="segmented"><button class="active" tabindex="-1">On</button><button tabindex="-1">Off</button></span></div>
    </section>`;
  root.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", async () => { await setTheme(b.dataset.t); appearance(); }));
  root.querySelectorAll("[data-a]").forEach((b) => b.addEventListener("click", async () => { await setTheme(null, b.dataset.a); appearance(); }));
}

// ---------------- Delete account ----------------
function danger() {
  const demo = session.user.email === "test@stagecraft.com";
  root.innerHTML = `
    <section class="card card-pad stack danger-card">
      <div><h2>Delete your account</h2><p class="small muted">This removes your login and signs you out everywhere. Events and the team roster stay (they belong to the
        organizing team, not one person). This can't be undone.</p></div>
      ${demo ? `<div class="small notice-inline">${icon("lock")} The demo test account is protected and can't be deleted.</div>` : ""}
      <div class="grid-2">
        <div><label class="label" for="d-pw">Current password</label><input class="input" id="d-pw" type="password" autocomplete="current-password" maxlength="128" ${demo ? "disabled" : ""}></div>
        <div><label class="label" for="d-confirm">Type DELETE to confirm</label><input class="input" id="d-confirm" autocomplete="off" maxlength="20" ${demo ? "disabled" : ""}></div>
      </div>
      <div id="d-err"></div>
      <div class="row" style="justify-content:flex-end"><button class="btn danger" id="d-go" ${demo ? "disabled" : ""}>${icon("trash")} Delete my account</button></div>
    </section>`;
  $("#d-go").addEventListener("click", async (e) => {
    if ($("#d-confirm").value.trim().toUpperCase() !== "DELETE") return err($("#d-err"), "Type DELETE to confirm.");
    busy(e.target.closest("button"), true);
    try {
      await post("/api/account/delete", { password: $("#d-pw").value, confirm: $("#d-confirm").value });
      refreshUser(null);
      toast("Your account was deleted");
      location.hash = "#/login";
    } catch (ex) { err($("#d-err"), ex.message); busy(e.target.closest("button"), false); }
  });
}

async function loadGmailCard() {
  const box = $("#acct-gmail");
  if (!box) return;
  let st;
  try { st = await api("/api/gmail/status"); } catch { return; }
  const body = st.connected
    ? `<div class="row-between list-row" style="border:none;padding:0"><div class="row">${icon("mail")}<div><b>${esc(st.email || "Gmail")}</b>
         <div class="tiny muted">Read-only · connected ${esc(fmtWhen(st.connected_at))}</div></div></div><button class="btn sm" id="g-disc">Disconnect</button></div>`
    : st.client_file
      ? `<div class="row-between"><span class="small muted">Not connected.</span><button class="btn primary sm" id="g-conn">${icon("mail")} Connect Gmail</button></div>`
      : `<p class="small muted">Gmail needs a one-time Google setup first (Inbox tab → Import from Gmail shows the steps and links).</p>`;
  box.innerHTML = `<div><h2>Connected accounts</h2><p class="small muted">Connect your Gmail (read-only) to import event emails from the Inbox tab.</p></div>${body}`;
  box.querySelector("#g-disc")?.addEventListener("click", async () => { await post("/api/gmail/disconnect"); toast("Gmail disconnected"); loadGmailCard(); });
  box.querySelector("#g-conn")?.addEventListener("click", async (e) => {
    busy(e.target.closest("button"), true);
    try { const r = await post("/api/gmail/connect", { return_to: "#/account/profile" }); location.href = r.auth_url; }
    catch (ex) { toast(ex.message, "bad"); busy(e.target.closest("button"), false); }
  });
}
