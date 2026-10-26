// People on the organizing team: avatars, assignee stacks, and the "Assign" picker used on every task list.
import { hue, initials } from "./auth.js";
import { esc, post, toast } from "./core.js";
import { icon } from "./icons.js";

export function avatar(m, size = "sm") {
  if (!m) return "";
  return `<span class="avatar ${size}" style="--h:${hue(m.email || m.name)}" title="${esc(m.name)}${m.role ? ` · ${esc(m.role)}` : ""}">${esc(initials(m.name))}</span>`;
}

export function avatarStack(ids, byId, { max = 4, size = "xs" } = {}) {
  const people = (ids || []).map((i) => byId[i]).filter(Boolean);
  if (!people.length) return `<span class="unassigned">${icon("user")} Unassigned</span>`;
  const extra = people.length - max;
  return `<span class="avatar-stack" title="${esc(people.map((p) => p.name).join(", "))}">${people.slice(0, max).map((p) => avatar(p, size)).join("")}${extra > 0 ? `<span class="avatar ${size} more">+${extra}</span>` : ""}</span>`;
}

// A small popover with a checkbox per team member. Saves straight away (a person's own edit, logged).
export function openAssign(anchor, { eventId, task, members, onSaved, suggestion = null }) {
  document.querySelector(".assign-pop")?.remove();
  const pop = document.createElement("div");
  pop.className = "assign-pop menu";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", `Assign ${task.title}`);
  const chosen = new Set(task.assignees || []);
  if (suggestion && !chosen.size) members = [...members].sort((a, b) => (b.id === suggestion.member_id) - (a.id === suggestion.member_id));
  pop.innerHTML = `
    <div class="menu-head"><div style="font-weight:600">Assign</div><div class="tiny muted">${esc(task.title)}</div></div>
    ${suggestion && !(task.assignees || []).length ? `<div class="assign-suggest tiny">${icon("sparkles")} Suggested: <b>${esc(suggestion.name)}</b> · ${esc(suggestion.reason)}</div>` : ""}
    <input class="input sm" placeholder="Search the team" aria-label="Search the team" data-q>
    <div class="assign-list">${members.map((m) => `
      <label class="assign-row ${suggestion?.member_id === m.id && !chosen.size ? "suggested" : ""}" data-name="${esc(m.name.toLowerCase())}"><input type="checkbox" class="checkbox" value="${esc(m.id)}" ${chosen.has(m.id) ? "checked" : ""}>
        ${avatar(m, "xs")}<span class="grow"><span style="font-weight:500">${esc(m.name)}</span>${m.is_pmo ? ` <span class="pill blue tiny">PMO</span>` : ""}
        <span class="tiny muted" style="display:block">${esc(m.role || m.team)} · ${m.open_tasks ?? 0} open</span></span></label>`).join("")}</div>
    <div class="row" style="justify-content:flex-end;padding:8px 6px 4px"><button class="btn sm" data-cancel>Cancel</button><button class="btn primary sm" data-save>Save</button></div>`;
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.position = "fixed";
  pop.style.top = `${Math.min(r.bottom + 6, innerHeight - pop.offsetHeight - 12)}px`;
  pop.style.left = `${Math.max(12, Math.min(r.right - pop.offsetWidth, innerWidth - pop.offsetWidth - 12))}px`;
  const close = () => { pop.remove(); document.removeEventListener("mousedown", outside); document.removeEventListener("keydown", onKey); };
  const outside = (e) => { if (!pop.contains(e.target) && e.target !== anchor) close(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  setTimeout(() => { document.addEventListener("mousedown", outside); document.addEventListener("keydown", onKey); pop.querySelector("[data-q]").focus(); }, 0);
  pop.querySelector("[data-q]").addEventListener("input", (e) => {
    const q = e.target.value.toLowerCase();
    pop.querySelectorAll(".assign-row").forEach((row) => (row.hidden = q && !row.dataset.name.includes(q)));
  });
  pop.querySelector("[data-cancel]").addEventListener("click", close);
  pop.querySelector("[data-save]").addEventListener("click", async () => {
    const ids = [...pop.querySelectorAll("input[type=checkbox]:checked")].map((c) => c.value);
    try {
      await post(`/api/events/${encodeURIComponent(eventId)}/records/task/${task.id}`, { changes: { assignees: ids } });
      toast(ids.length ? `Assigned to ${ids.map((i) => members.find((m) => m.id === i)?.name.split(" ")[0]).join(", ")}` : "Unassigned");
      close();
      await onSaved();
    } catch (err) { toast(err.message, "bad"); }
  });
}
