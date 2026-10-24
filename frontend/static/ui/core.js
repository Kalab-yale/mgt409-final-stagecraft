// Shared helpers: API calls, escaping, formatting, toasts, and the tab registry.

export async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.error || body.detail || (body.errors && body.errors[0]) || `Request failed (${res.status})`);
    err.body = body;
    throw err;
  }
  return body;
}
export const post = (path, data = {}) => api(path, { method: "POST", body: JSON.stringify(data) });

export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const D = (iso) => new Date(`${String(iso).slice(0, 10)}T12:00:00`);
export const fmtDate = (iso) => D(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
export const fmtLong = (iso) => D(iso).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
export const fmtTime = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
export const fmtWhen = (iso) => {
  const d = new Date(iso);
  return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${fmtTime(iso)}`;
};
export const money = (n) => "$" + Math.round(Number(n || 0)).toLocaleString();
export const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
export function daysLabel(days) {
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days === -1) return "yesterday";
  return days > 0 ? `in ${days} days` : `${-days} days ago`;
}

export function toast(message, tone = "") {
  const el = document.createElement("div");
  el.className = `toast ${tone}`;
  el.textContent = message;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

export function busy(button, on, label) {
  if (!button) return;
  if (on) {
    button.dataset.label = button.innerHTML;
    button.disabled = true;
    button.innerHTML = `<span class="spinner ${button.classList.contains("primary") ? "" : "dark"}"></span>${label ? ` ${esc(label)}` : ""}`;
  } else {
    button.disabled = false;
    if (button.dataset.label) button.innerHTML = button.dataset.label;
  }
}

// Workspace tabs (ids match the backend assistant tabs) and which tab owns each dimension.
export const TABS = [
  { id: "overview", label: "Overview", icon: "overview", assistant: "Overview assistant" },
  { id: "program", label: "Program", icon: "program", assistant: "Program assistant" },
  { id: "venue", label: "Venue", icon: "venue", assistant: "Venue assistant" },
  { id: "food", label: "Food", icon: "food", assistant: "Food assistant" },
  { id: "attendees", label: "Attendees", icon: "attendees", assistant: "Attendees assistant" },
  { id: "budget", label: "Tasks and budget", icon: "budget", assistant: "Tasks and budget assistant" },
  { id: "inbox", label: "Inbox", icon: "inbox", assistant: "Inbox assistant" },
];
export const DIM_TAB = { Program: "program", Venue: "venue", Food: "food", Attendees: "attendees", Budget: "budget", General: "overview" };

export const sevTone = (s) => ({ Critical: "red", High: "orange", Medium: "orange", Low: "" }[s] ?? "");
export const deadlineTone = (s) => ({ Overdue: "red", "Due soon": "orange", Done: "green", Upcoming: "" }[s] ?? "");
export const statusTone = (s) => ({ Blocked: "red", "At risk": "orange", "On track": "blue", Done: "green" }[s] ?? "");
