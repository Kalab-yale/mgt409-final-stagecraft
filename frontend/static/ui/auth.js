// Log in / create account screens, the signed-in user, and the user menu in the top bar.
import { api, esc, post } from "./core.js";
import { icon } from "./icons.js";
import { mountBell, stopBell } from "./notifications.js";
import { adoptUserTheme, currentTheme, setTheme, THEMES } from "./theme.js";

export const session = { user: null };

export async function loadUser() {
  try {
    session.user = (await api("/api/auth/me", { quiet401: true })).user;
  } catch {
    session.user = null;
  }
  renderUserMenu();
  return session.user;
}

export const initials = (name) => name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");

function renderUserMenu() {
  const slot = document.getElementById("user-slot");
  document.querySelectorAll("[data-signed-in]").forEach((el) => (el.hidden = !session.user));
  if (!slot) return;
  const bell = document.getElementById("bell-slot");
  if (!session.user) { slot.innerHTML = ""; if (bell) bell.innerHTML = ""; stopBell(); return; }
  adoptUserTheme(session.user);
  if (bell && !bell.firstChild) mountBell(bell);
  const u = session.user;
  slot.innerHTML = `
    <div class="user-menu">
      <button class="avatar-btn" id="user-btn" aria-haspopup="menu" aria-expanded="false" title="${esc(u.display_name)}">
        <span class="avatar sm" style="--h:${hue(u.email)}">${esc(initials(u.display_name))}</span></button>
      <div class="menu account-menu" id="user-pop" role="menu" hidden>
        <div class="menu-head row" style="gap:12px">
          <span class="avatar lg" style="--h:${hue(u.email)}">${esc(initials(u.display_name))}</span>
          <div style="min-width:0"><div style="font-weight:600" class="ellipsis">${esc(u.display_name)}</div><div class="tiny muted ellipsis">${esc(u.email)}</div></div>
        </div>
        <a role="menuitem" href="#/account/profile">${icon("user")} Account settings</a>
        <a role="menuitem" href="#/account/security">${icon("lock")} Password and sessions</a>
        <a role="menuitem" href="#/settings">${icon("shield")} AI usage and safety</a>
        <div class="menu-theme"><span class="small muted">Theme</span>
          <div class="segmented" role="radiogroup" aria-label="Theme">${THEMES.map(([k, l]) => `
            <button role="radio" aria-checked="${currentTheme() === k}" class="${currentTheme() === k ? "active" : ""}" data-theme-pick="${k}" title="${l}">
              ${icon(k === "light" ? "sun" : k === "dark" ? "moon" : "monitor")}<span class="sr-only">${l}</span></button>`).join("")}</div></div>
        <button role="menuitem" id="logout">${icon("logout")} Log out</button>
      </div>
    </div>`;
  const btn = slot.querySelector("#user-btn"), pop = slot.querySelector("#user-pop");
  const close = () => { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); };
  btn.addEventListener("click", (e) => { e.stopPropagation(); pop.hidden = !pop.hidden; btn.setAttribute("aria-expanded", String(!pop.hidden)); });
  pop.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", close);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  window.addEventListener("hashchange", close);
  pop.querySelectorAll("a[role=menuitem]").forEach((a) => a.addEventListener("click", close));
  pop.querySelectorAll("[data-theme-pick]").forEach((b) => b.addEventListener("click", async () => {
    await setTheme(b.dataset.themePick);
    pop.querySelectorAll("[data-theme-pick]").forEach((x) => { const on = x === b; x.classList.toggle("active", on); x.setAttribute("aria-checked", String(on)); });
  }));
  slot.querySelector("#logout").addEventListener("click", async () => {
    await post("/api/auth/logout");
    session.user = null;
    renderUserMenu();
    location.hash = "#/login";
  });
}

export function refreshUser(user) {
  session.user = user;
  renderUserMenu();
}

// A stable color per person (used for avatars across the app).
export function hue(seed) {
  let h = 0;
  for (const c of String(seed)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

const ART = `
  <div class="auth-art" aria-hidden="true">
    <div class="beam b1"></div><div class="beam b2"></div><div class="beam b3"></div>
    <div class="auth-copy">
      <div class="brand light"><span class="brand-mark">S</span> Stagecraft</div>
      <h1>Run the summit,<br>not the inbox.</h1>
      <p>AI agents read your email, flag what's slipping, and re-plan the day in seconds. You approve every change.</p>
    </div>
    <div class="float-cards">
      <div class="fcard f1"><span class="dot red"></span><div><b>AV contract not signed</b><div class="tiny">Red flag · due Friday</div></div></div>
      <div class="fcard f2"><span class="ic">${icon("check")}</span><div><b>Panel moved to Zhang Auditorium</b><div class="tiny">Approved · 27 s · $0.003</div></div></div>
      <div class="fcard f3"><span class="ic blue">${icon("sparkles")}</span><div><b>9 emails need a reply</b><div class="tiny">Inbox Watcher · drafts ready</div></div></div>
    </div>
  </div>`;

export function showAuth(app, crumb, mode) {
  crumb.innerHTML = "";
  document.title = `${mode === "signup" ? "Create account" : "Log in"} · Stagecraft`;
  const signup = mode === "signup";
  app.innerHTML = `
    <div class="auth">
      ${ART}
      <div class="auth-panel">
        <form class="auth-form" id="auth-form" novalidate>
          <h2>${signup ? "Create your account" : "Welcome back"}</h2>
          <p class="muted" style="margin:4px 0 22px">${signup ? "Plan your next event with an AI team beside you." : "Log in to see your events."}</p>
          ${signup ? `<div class="grid-2" style="gap:12px">
            <div><label class="label" for="f-first">First name</label><input class="input" id="f-first" autocomplete="given-name" required maxlength="60"></div>
            <div><label class="label" for="f-last">Last name</label><input class="input" id="f-last" autocomplete="family-name" required maxlength="60"></div></div>` : ""}
          <div><label class="label" for="f-email">Email</label><input class="input" id="f-email" type="email" autocomplete="email" required maxlength="254"></div>
          <div><label class="label" for="f-pw">Password</label>
            <div class="pw-wrap"><input class="input" id="f-pw" type="password" autocomplete="${signup ? "new-password" : "current-password"}" required maxlength="128">
            <button type="button" class="pw-toggle" aria-label="Show password" data-toggle="f-pw">Show</button></div>
            ${signup ? `<div class="pw-meter" aria-hidden="true"><span id="pw-bar"></span></div><div class="tiny muted" id="pw-hint">At least 8 characters.</div>` : ""}</div>
          ${signup ? `<div><label class="label" for="f-pw2">Confirm password</label><input class="input" id="f-pw2" type="password" autocomplete="new-password" required maxlength="128"></div>` : ""}
          <div id="auth-error" role="alert"></div>
          <button class="btn primary lg" id="auth-go" type="submit">${signup ? "Create account" : "Log in"}</button>
          <p class="small muted" style="text-align:center;margin-top:14px">${signup
            ? `Already have an account? <a href="#/login">Log in</a>`
            : `New here? <a href="#/signup">Create an account</a>`}</p>
          ${signup ? "" : `<p class="tiny faint" style="text-align:center;margin-top:18px">Demo account: test@stagecraft.com · password</p>`}
        </form>
      </div>
    </div>`;
  const $ = (id) => app.querySelector(id);
  setTimeout(() => (signup ? $("#f-first") : $("#f-email")).focus(), 30);
  app.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", () => {
    const i = $("#" + b.dataset.toggle);
    i.type = i.type === "password" ? "text" : "password";
    b.textContent = i.type === "password" ? "Show" : "Hide";
    b.setAttribute("aria-label", i.type === "password" ? "Show password" : "Hide password");
  }));
  if (signup) $("#f-pw").addEventListener("input", (e) => meter(e.target.value));
  $("#auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#auth-error"), btn = $("#auth-go");
    const v = (id) => $(id)?.value ?? "";
    const show = (m) => (err.innerHTML = `<p class="form-error">${esc(m)}</p>`);
    if (signup) {
      if (!v("#f-first").trim() || !v("#f-last").trim()) return show("Please enter your first and last name.");
      if (v("#f-pw").length < 8) return show("Use a password with at least 8 characters.");
      if (v("#f-pw") !== v("#f-pw2")) return show("The two passwords don't match.");
    }
    if (!v("#f-email").includes("@")) return show("Please enter a valid email address.");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span>`;
    try {
      const r = signup
        ? await post("/api/auth/signup", { first_name: v("#f-first"), last_name: v("#f-last"), email: v("#f-email"), password: v("#f-pw"), confirm_password: v("#f-pw2") })
        : await post("/api/auth/login", { email: v("#f-email"), password: v("#f-pw") });
      session.user = r.user;
      renderUserMenu();
      location.hash = "#/";
    } catch (ex) {
      show(ex.message);
      btn.disabled = false;
      btn.textContent = signup ? "Create account" : "Log in";
    }
  });
}

export function strength(pw) {
  const score = [pw.length >= 8, pw.length >= 12, /[A-Z]/.test(pw) && /[a-z]/.test(pw), /\d/.test(pw), /[^A-Za-z0-9]/.test(pw)].filter(Boolean).length;
  const levels = [["#c4141b", "Too short"], ["#c4141b", "Weak"], ["#e8840c", "Okay"], ["#e8840c", "Good"], ["#34a853", "Strong"], ["#34a853", "Very strong"]];
  const [color, label] = pw.length < 8 ? levels[0] : levels[score];
  return { color, label, pct: Math.max(8, (score / 5) * 100) };
}

function meter(pw) {
  const { color, label, pct } = strength(pw);
  const bar = document.getElementById("pw-bar"), hint = document.getElementById("pw-hint");
  bar.style.width = `${pct}%`;
  bar.style.background = color;
  hint.textContent = pw ? label : "At least 8 characters.";
}
