// The Team tab: the PMO, everyone on the organizing team, how loaded each person is, and who owns which task.
import { session } from "./auth.js";
import { legend, SERIES, stack, STATUS } from "./charts.js";
import { api, busy, esc, fmtDate, post, toast } from "./core.js";
import { icon } from "./icons.js";
import { modal } from "./modals.js";
import { avatar, avatarStack, openAssign } from "./people.js";
import { openMeetingNotes } from "./updates.js";

const TEAMS = ["Leadership", "Program", "Venue and logistics", "Food", "Registration", "Sponsorship and budget", "Marketing", "Volunteers"];
const AREAS = [["Program", "Program"], ["Venue", "Venue"], ["Food", "Food"], ["Attendees", "Attendees"], ["Budget", "Tasks and budget"], ["General", "General"]];
const view = { filter: "open", person: "" };

// ctx: { id, plan, root, refresh, bind }
export function renderTeam(ctx) {
  const { plan } = ctx;
  const t = plan.team, members = t.members, byId = Object.fromEntries(members.map((m) => [m.id, m]));
  const pmo = members.find((m) => m.is_pmo);
  const tasks = plan.initiatives.flatMap((i) => i.tasks.map((x) => ({ ...x, workstream: i.title }))).concat(plan.tasks_without_initiative);
  const open = tasks.filter((x) => x.status !== "Done");
  const me = members.find((m) => session.user && m.email.toLowerCase() === session.user.email.toLowerCase());
  const shown = open.filter((x) => view.filter === "unassigned" ? !x.assignees?.length
    : view.filter === "shared" ? (x.assignees?.length || 0) > 1
    : view.filter === "mine" ? me && x.assignees?.includes(me.id)
    : view.filter === "person" ? x.assignees?.includes(view.person) : true)
    .sort((a, b) => (a.due_date || "9999").localeCompare(b.due_date || "9999"));
  const shared = open.filter((x) => (x.assignees?.length || 0) > 1).length;
  const maxOpen = Math.max(1, ...members.map((m) => m.open_tasks));

  ctx.root.innerHTML = `
    <div class="row-between wrap">
      <div><h2>Organizing team</h2><p class="muted small" style="margin-top:2px">Who runs the plan, who owns what, and who needs help.</p></div>
      <div class="row"><button class="btn sm" id="notes-tasks">${icon("sparkles")} Meeting notes to tasks</button><button class="btn sm" id="add-person">${icon("plus")} Add person</button><button class="btn primary sm" id="new-task">${icon("plus")} New task</button></div>
    </div>
    ${!me && session.user ? `<div class="notice small">${icon("user")} You're not on this event's team yet, so you can't approve budget changes or post team updates.
      ${pmo ? `Ask ${esc(pmo.name)} (the PMO) to add you with <b>${esc(session.user.email)}</b>.` : `Add yourself below with <b>${esc(session.user.email)}</b>, or create your own event: its creator runs it.`}</div>` : ""}
    <div class="team-top">
      <div class="card pmo-card">
        ${pmo ? `
        <div class="row" style="gap:14px;align-items:center">
          ${avatar(pmo, "xl")}
          <div class="grow"><div class="pmo-kicker">${icon("crown")} PMO · runs the plan</div>
            <div class="pmo-name">${esc(pmo.name)}</div><div class="small" style="opacity:.8">${esc(pmo.role)}${pmo.email ? ` · <a href="mailto:${esc(pmo.email)}">${esc(pmo.email)}</a>` : ""}</div></div>
        </div>
        <div class="pmo-stats"><div><b>${pmo.open_tasks}</b><span>open tasks</span></div><div><b>${pmo.overdue_tasks}</b><span>overdue</span></div>
          <div><b>${t.unassigned_open_tasks}</b><span>unassigned on the team</span></div></div>` : `
        <div class="pmo-kicker">${icon("crown")} No PMO yet</div><p style="margin-top:6px">Pick one person to run the plan and chase owners.</p>`}
        <label class="pmo-change small">Change PMO <select class="select sm" id="pmo-select">
          ${!pmo ? `<option value="">Choose…</option>` : ""}${members.map((m) => `<option value="${esc(m.id)}" ${m.is_pmo ? "selected" : ""}>${esc(m.name)}</option>`).join("")}</select></label>
      </div>
      <div class="team-kpis">
        <div class="card kpi"><div class="stat-label">People</div><div class="v num">${members.length}</div><div class="s">${t.teams.length} teams</div></div>
        <div class="card kpi"><div class="stat-label">Open tasks</div><div class="v num">${open.length}</div><div class="s">${shared} shared by 2+ people</div></div>
        <div class="card kpi"><div class="stat-label">Unassigned</div><div class="v num" style="color:${t.unassigned_open_tasks ? "var(--red)" : "inherit"}">${t.unassigned_open_tasks}</div><div class="s">${t.unassigned_open_tasks ? "need an owner" : "every task has an owner"}</div></div>
        <div class="card kpi"><div class="stat-label">Stretched</div><div class="v num">${members.filter((m) => m.stretched).length}</div><div class="s">over 7 open tasks</div></div>
      </div>
    </div>

    <div class="card"><div class="card-head"><h3>${icon("list")} Workload</h3>${legend([["Overdue", STATUS.critical], ["Blocked", STATUS.serious], ["On track", SERIES[0]]])}</div>
      <div class="card-body workload">${members.map((m) => `
        <button class="wl-row" data-person="${esc(m.id)}" aria-label="Show ${esc(m.name)}'s tasks">
          <span class="row" style="gap:8px;min-width:0">${avatar(m, "xs")}<span class="wl-name">${esc(m.name)}</span></span>
          <span class="wl-bar">${stack([
            { value: m.overdue_tasks, color: STATUS.critical, label: "overdue" },
            { value: m.blocked_tasks, color: STATUS.serious, label: "blocked" },
            { value: Math.max(0, m.open_tasks - m.overdue_tasks - m.blocked_tasks), color: SERIES[0], label: "on track" }], { height: 12, total: maxOpen, name: m.name })}</span>
          <span class="wl-val">${m.open_tasks} open${m.stretched ? ` <span class="pill orange tiny">stretched</span>` : ""}</span>
        </button>`).join("")}</div></div>

    <div class="member-grid">${members.map((m) => `
        <div class="card member-card ${m.is_pmo ? "is-pmo" : ""}">
          <div class="row" style="gap:12px;align-items:flex-start">${avatar(m, "lg")}
            <div class="grow" style="min-width:0"><div class="row" style="gap:6px"><b class="ellipsis">${esc(m.name)}</b>${m.is_pmo ? `<span class="pill blue tiny">${icon("crown")} PMO</span>` : m.access === "Lead" ? `<span class="pill tiny" title="Can approve budget changes">Lead</span>` : ""}${me && me.id === m.id ? `<span class="pill green tiny">You</span>` : ""}</div>
              <div class="small muted ellipsis">${esc(m.role || "Team member")}</div>
              ${m.email ? `<a class="tiny ellipsis" style="display:block" href="mailto:${esc(m.email)}">${esc(m.email)}</a>` : ""}</div>
            <button class="btn plain sm icon-only" data-edit="${esc(m.id)}" aria-label="Edit ${esc(m.name)}">${icon("settings")}</button></div>
          <div class="member-stats"><span><b>${m.open_tasks}</b> open</span><span class="${m.overdue_tasks ? "red-text" : ""}"><b>${m.overdue_tasks}</b> overdue</span><span><b>${m.deadlines_owned}</b> deadlines</span></div>
          <div class="row-between" style="margin-top:10px"><span class="pill team-pill">${esc(m.team)}</span><button class="btn plain sm" data-person="${esc(m.id)}">See tasks ${icon("next")}</button></div>
          ${m.workstreams_led.length ? `<div class="tiny muted" style="margin-top:6px">Leads: ${m.workstreams_led.map(esc).join(", ")}</div>` : ""}
        </div>`).join("")}</div>

    <div class="card" id="assignments"><div class="card-head"><h3>${icon("clipboard")} Assignments</h3><span class="muted small">${shown.length} of ${open.length} open tasks</span></div>
      <div class="card-body">
        <div class="row wrap" style="margin-bottom:10px">
          <div class="segmented">${[["open", "All open"], ["unassigned", `Unassigned${t.unassigned_open_tasks ? ` ${t.unassigned_open_tasks}` : ""}`], ["shared", "Shared"], ...(me ? [["mine", "Mine"]] : [])]
            .map(([k, l]) => `<button class="${view.filter === k ? "active" : ""}" data-filter="${k}">${l}</button>`).join("")}</div>
          <select class="select sm" id="person-filter" style="width:auto" aria-label="Filter by person"><option value="">Anyone</option>${members.map((m) =>
            `<option value="${esc(m.id)}" ${view.filter === "person" && view.person === m.id ? "selected" : ""}>${esc(m.name)}</option>`).join("")}</select>
        </div>
        ${shown.map((x) => `
          <div class="list-row task-assign">
            <input type="checkbox" class="checkbox" data-task="${esc(x.id)}" aria-label="Mark ${esc(x.title)} done">
            <div class="grow"><div style="font-weight:500">${esc(x.title)}</div>
              <div class="tiny muted">${esc(x.workstream || x.dimension)}${x.due_date ? ` · due ${esc(fmtDate(x.due_date))}` : ""}</div></div>
            ${x.status === "Blocked" ? `<span class="pill red">Blocked</span>` : x.priority === "High" ? `<span class="pill orange">High</span>` : ""}
            ${avatarStack(x.assignees, byId)}
            ${t.suggestions?.[x.id] ? `<button class="btn sm suggest" data-suggest="${esc(x.id)}" title="Suggested: ${esc(t.suggestions[x.id].reason)}">${avatar(byId[t.suggestions[x.id].member_id], "xs")} Assign ${esc(t.suggestions[x.id].name.split(" ")[0])}</button>` : ""}
            <button class="btn sm" data-assign="${esc(x.id)}">${icon("user")} Assign</button>
          </div>`).join("") || `<p class="muted small">Nothing here.</p>`}
      </div></div>`;

  const r = ctx.root;
  r.querySelectorAll("[data-person]").forEach((b) => b.addEventListener("click", () => {
    Object.assign(view, { filter: "person", person: b.dataset.person });
    renderTeam(ctx);
    document.getElementById("assignments").scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  r.querySelectorAll("[data-filter]").forEach((b) => b.addEventListener("click", () => { Object.assign(view, { filter: b.dataset.filter, person: "" }); renderTeam(ctx); }));
  r.querySelector("#person-filter").addEventListener("change", (e) => { Object.assign(view, e.target.value ? { filter: "person", person: e.target.value } : { filter: "open", person: "" }); renderTeam(ctx); });
  r.querySelectorAll("[data-assign]").forEach((b) => b.addEventListener("click", () => openAssign(b, {
    eventId: ctx.id, task: tasks.find((x) => x.id === b.dataset.assign), members, onSaved: ctx.refresh, suggestion: t.suggestions?.[b.dataset.assign] })));
  r.querySelectorAll("[data-suggest]").forEach((b) => b.addEventListener("click", async () => {
    const sg = t.suggestions[b.dataset.suggest];
    busy(b, true);
    try { await post(`/api/events/${encodeURIComponent(ctx.id)}/records/task/${b.dataset.suggest}`, { changes: { assignees: [sg.member_id] } }); toast(`Assigned to ${sg.name}`); await ctx.refresh(); }
    catch (err) { toast(err.message, "bad"); busy(b, false); }
  }));
  r.querySelector("#notes-tasks").addEventListener("click", () => openMeetingNotes(ctx.id, ctx.refresh));
  r.querySelector("#pmo-select").addEventListener("change", async (e) => {
    if (!e.target.value) return;
    try { await post(`/api/events/${encodeURIComponent(ctx.id)}/team/pmo`, { member_id: e.target.value }); toast(`${byId[e.target.value].name} is now the PMO`); await ctx.refresh(); }
    catch (err) { toast(err.message, "bad"); }
  });
  r.querySelector("#add-person").addEventListener("click", () => memberModal(ctx, null));
  r.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => memberModal(ctx, byId[b.dataset.edit])));
  r.querySelector("#new-task").addEventListener("click", () => taskModal(ctx, members));
  ctx.bind(r);
}

function memberModal(ctx, m) {
  const md = modal(m ? `Edit ${m.name}` : "Add someone to the team", m ? "" : "They don't need an account; this is the team roster tasks are assigned from.", `
    <div class="grid-2"><div><label class="label" for="m-name">Name</label><input class="input" id="m-name" value="${esc(m?.name || "")}" maxlength="80"></div>
      <div><label class="label" for="m-email">Email</label><input class="input" id="m-email" type="email" value="${esc(m?.email || "")}"></div>
      <div><label class="label" for="m-role">Role</label><input class="input" id="m-role" value="${esc(m?.role || "")}" placeholder="e.g. Signage volunteer"></div>
      <div><label class="label" for="m-team">Team</label><select class="select" id="m-team">${TEAMS.map((g) => `<option ${g === (m?.team || "Volunteers") ? "selected" : ""}>${g}</option>`).join("")}</select></div></div>
    <label class="row small"><input type="checkbox" class="checkbox" id="m-lead" ${m?.access === "Lead" ? "checked" : ""}> Lead: can approve budget changes</label>
    <label class="row small"><input type="checkbox" class="checkbox" id="m-pmo" ${m?.is_pmo ? "checked" : ""}> PMO (runs the plan; replaces the current PMO)</label>
    <div id="m-err"></div>
    <div class="row" style="justify-content:space-between">
      ${m ? `<button class="btn danger sm" id="m-del">Remove from team</button>` : "<span></span>"}
      <div class="row"><button class="btn" data-cancel>Cancel</button><button class="btn primary" id="m-save">${m ? "Save" : "Add to team"}</button></div></div>`);
  const $ = (s) => md.el.querySelector(s);
  $("[data-cancel]").addEventListener("click", md.close);
  $("#m-save").addEventListener("click", async (e) => {
    const fields = { name: $("#m-name").value.trim(), email: $("#m-email").value.trim(), role: $("#m-role").value.trim(), team: $("#m-team").value, is_pmo: $("#m-pmo").checked,
                     access: $("#m-lead").checked || $("#m-pmo").checked ? "Lead" : "Member" };
    if (!fields.name) { $("#m-err").innerHTML = `<p class="form-error">Add a name.</p>`; return; }
    busy(e.target.closest("button"), true);
    try {
      const base = `/api/events/${encodeURIComponent(ctx.id)}/records/member`;
      await post(m ? `${base}/${m.id}` : base, m ? { changes: fields } : { fields });
      toast(m ? "Saved" : `Added ${fields.name}`);
      md.close();
      await ctx.refresh();
    } catch (err) { $("#m-err").innerHTML = `<p class="form-error">${esc(err.message)}</p>`; busy(e.target.closest("button"), false); }
  });
  $("#m-del")?.addEventListener("click", async () => {
    if (!confirm(`Remove ${m.name} from the team? They'll be taken off their ${m.open_tasks + m.done_tasks} task(s).`)) return;
    try { await api(`/api/events/${encodeURIComponent(ctx.id)}/records/member/${m.id}`, { method: "DELETE" }); toast(`Removed ${m.name}`); md.close(); await ctx.refresh(); }
    catch (err) { toast(err.message, "bad"); }
  });
}

function taskModal(ctx, members) {
  const md = modal("New task", "Give it one or more owners. Shared tasks show everyone's face.", `
    <div><label class="label" for="t-title">Task</label><input class="input" id="t-title" maxlength="160" placeholder="e.g. Print room signs"></div>
    <div class="grid-2"><div><label class="label" for="t-area">Area</label><select class="select" id="t-area">${AREAS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></div>
      <div><label class="label" for="t-due">Due</label><input class="input" id="t-due" type="date"></div>
      <div><label class="label" for="t-prio">Priority</label><select class="select" id="t-prio"><option>High</option><option selected>Medium</option><option>Low</option></select></div>
      <div><label class="label" for="t-ws">Workstream</label><select class="select" id="t-ws"><option value="">None</option>${ctx.plan.initiatives.map((i) => `<option value="${esc(i.id)}">${esc(i.title)}</option>`).join("")}</select></div></div>
    <div><div class="label">Owners</div><div class="chip-pick">${members.map((m) => `
      <label class="pick"><input type="checkbox" value="${esc(m.id)}">${avatar(m, "xs")}<span>${esc(m.name)}</span></label>`).join("")}</div></div>
    <div id="t-err"></div>
    <div class="row" style="justify-content:flex-end"><button class="btn" data-cancel>Cancel</button><button class="btn primary" id="t-save">Create task</button></div>`);
  const $ = (s) => md.el.querySelector(s);
  $("[data-cancel]").addEventListener("click", md.close);
  $("#t-save").addEventListener("click", async (e) => {
    const fields = { title: $("#t-title").value.trim(), dimension: $("#t-area").value, priority: $("#t-prio").value,
                     due_date: $("#t-due").value || null, initiative_id: $("#t-ws").value || null,
                     assignees: [...md.el.querySelectorAll(".pick input:checked")].map((c) => c.value) };
    if (!fields.title) { $("#t-err").innerHTML = `<p class="form-error">Name the task.</p>`; return; }
    busy(e.target.closest("button"), true);
    try {
      await post(`/api/events/${encodeURIComponent(ctx.id)}/records/task`, { fields });
      toast("Task created");
      md.close();
      await ctx.refresh();
    } catch (err) { $("#t-err").innerHTML = `<p class="form-error">${esc(err.message)}</p>`; busy(e.target.closest("button"), false); }
  });
}
