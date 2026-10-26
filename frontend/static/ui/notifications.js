// The bell in the top bar: tasks assigned to you, urgent red flags on events you lead, and changes waiting for your OK.
import { api, esc, fmtWhen, post } from "./core.js";
import { icon } from "./icons.js";

const KIND_ICON = { assigned: "user", flag: "flag", approvals: "clipboard", announcement: "bell" };
let timer = null;
let wired = false;  // page-wide listeners are added once, even across log out / log in
let data = { items: [], unread: 0 };

export function mountBell(slot) {
  slot.innerHTML = `
    <div class="bell-wrap">
      <button class="icon-btn" id="bell-btn" aria-haspopup="dialog" aria-expanded="false" aria-label="Notifications">${icon("bell")}<span class="bell-count" id="bell-count" hidden></span></button>
      <div class="menu notif-pop" id="bell-pop" role="dialog" aria-label="Notifications" hidden></div>
    </div>`;
  const btn = slot.querySelector("#bell-btn"), pop = slot.querySelector("#bell-pop");
  const close = () => { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); };
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!pop.hidden) return close();
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    await refresh();
  });
  pop.addEventListener("click", (e) => e.stopPropagation());
  if (!wired) {
    wired = true;
    const closeAny = () => { const p = document.getElementById("bell-pop"), b = document.getElementById("bell-btn"); if (p) p.hidden = true; b?.setAttribute("aria-expanded", "false"); };
    document.addEventListener("click", closeAny);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAny(); });
    window.addEventListener("hashchange", () => { closeAny(); refresh(); });
  }
  clearInterval(timer);
  timer = setInterval(refresh, 60_000);
  refresh();
}

export function stopBell() { clearInterval(timer); }

export async function refresh() {
  try { data = await api("/api/notifications", { quiet401: true }); } catch { return; }
  const count = document.getElementById("bell-count");
  if (!count) return;
  count.hidden = !data.unread;
  count.textContent = data.unread > 9 ? "9+" : data.unread;
  const pop = document.getElementById("bell-pop");
  if (pop && !pop.hidden) draw(pop);
}

function draw(pop) {
  pop.innerHTML = `
    <div class="row-between notif-head"><b>Notifications</b>${data.items.some((i) => !i.read_at && i.kind !== "approvals") ? `<button class="btn plain sm" id="notif-all">Mark all read</button>` : ""}</div>
    <div class="notif-list">${data.items.map((n) => `
      <a class="notif ${n.read_at ? "" : "unread"} k-${esc(n.kind)}" href="${esc(n.link || "#/")}" data-id="${esc(n.id)}">
        <span class="notif-ic">${icon(KIND_ICON[n.kind] || "sparkles")}</span>
        <span class="grow" style="min-width:0"><span class="notif-title">${esc(n.title)}</span>
          <span class="tiny muted notif-body">${esc(n.body || "")}</span>
          ${n.kind !== "approvals" ? `<span class="tiny faint">${esc(fmtWhen(n.created_at))}</span>` : ""}</span>
        ${n.read_at ? "" : `<span class="dot blue" aria-label="unread"></span>`}
      </a>`).join("") || `<div class="empty small">${icon("check")} You're all caught up.</div>`}</div>`;
  pop.querySelector("#notif-all")?.addEventListener("click", async () => { await post("/api/notifications/read", {}); await refresh(); draw(pop); });
  pop.querySelectorAll(".notif").forEach((a) => a.addEventListener("click", () => {
    if (!a.dataset.id.startsWith("approvals:") && a.classList.contains("unread")) post("/api/notifications/read", { ids: [a.dataset.id] }).then(refresh);
  }));
}
