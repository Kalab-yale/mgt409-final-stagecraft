// Small, dependency-free charts in plain HTML/SVG: rings, horizontal bars, stacked bars, legends, and one shared
// hover/focus tooltip. Marks carry color; every value is also written as text (labels or a table), so color is
// never the only channel and tooltips only enhance.
import { esc } from "./core.js";

// Categorical slots in fixed order (validated for color-vision deficiency, adjacent pairs) + reserved status colors.
const LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
const DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];  // same hues, stepped for dark surfaces
export const SERIES = [...LIGHT];  // mutated in place by setChartTheme, so every importer sees the current mode
export const STATUS = { good: "#0ca30c", warning: "#fab219", serious: "#ec835a", critical: "#d03b3b" };  // mode-invariant
const TRACK = "var(--track)";

export function setChartTheme(dark) {
  SERIES.splice(0, SERIES.length, ...(dark ? DARK : LIGHT));
}

// ---------- tooltip (one for the whole app) ----------
let tipEl = null;
function tip() {
  if (!tipEl) {
    tipEl = document.createElement("div");
    tipEl.className = "chart-tip";
    tipEl.setAttribute("role", "tooltip");
    document.body.appendChild(tipEl);
    const show = (e) => {
      const t = e.target.closest?.("[data-tip-value]");
      if (!t) return;
      tipEl.replaceChildren();
      const v = document.createElement("div"); v.className = "v"; v.textContent = t.dataset.tipValue;
      const l = document.createElement("div"); l.className = "l"; l.textContent = t.dataset.tipLabel || "";
      if (t.dataset.tipColor) { const k = document.createElement("span"); k.className = "key"; k.style.background = t.dataset.tipColor; l.prepend(k); }
      tipEl.append(v, l);
      tipEl.style.display = "block";
      const r = t.getBoundingClientRect();
      tipEl.style.left = `${Math.min(innerWidth - tipEl.offsetWidth - 8, Math.max(8, r.left + r.width / 2 - tipEl.offsetWidth / 2))}px`;
      tipEl.style.top = `${Math.max(8, r.top - tipEl.offsetHeight - 8)}px`;
      t.classList.add("hot");
    };
    const hide = (e) => { const t = e.target.closest?.("[data-tip-value]"); if (t) t.classList.remove("hot"); tipEl.style.display = "none"; };
    document.addEventListener("pointerover", show);
    document.addEventListener("pointerout", hide);
    document.addEventListener("focusin", show);
    document.addEventListener("focusout", hide);
  }
}
const tipAttrs = (value, label, color) => { tip(); return `data-tip-value="${esc(value)}" data-tip-label="${esc(label)}"${color ? ` data-tip-color="${color}"` : ""} tabindex="0"`; };

// ---------- ring meter ----------
export function ring(pct, { size = 56, stroke = 6, color = SERIES[0], track = TRACK, label = "", sub = "", tipLabel = "" } = {}) {
  const r = (size - stroke) / 2, c = 2 * Math.PI * r, p = Math.max(0, Math.min(100, pct));
  return `<div class="ring" style="width:${size}px;height:${size}px" ${tipAttrs(`${Math.round(p)}%`, tipLabel || label)}>
    <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" aria-hidden="true">
      <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${track}" stroke-width="${stroke}"/>
      ${p >= 1.5 ? `<circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${color}" stroke-width="${stroke}" stroke-linecap="round"
        stroke-dasharray="${(c * p) / 100} ${c}" transform="rotate(-90 ${size / 2} ${size / 2})" class="ring-arc"/>` : ""}
    </svg>
    <div class="ring-label"><b>${esc(label)}</b>${sub ? `<span>${esc(sub)}</span>` : ""}</div>
  </div>`;
}

// ---------- legend ----------
export const legend = (items) => `<div class="legend">${items.map(([label, color]) => `<span><i style="background:${color}"></i>${esc(label)}</span>`).join("")}</div>`;

// ---------- stacked horizontal bar (segments separated by a 2px surface gap) ----------
export function stack(segments, { height = 12, total = null, name = "" } = {}) {
  const sum = segments.reduce((a, s) => a + s.value, 0);
  const shown = segments.filter((s) => s.value > 0);
  if (!sum || !shown.length) return `<div class="segbar" style="height:${height}px"></div>`;
  return `<div class="segbar" style="height:${height}px" role="img" aria-label="${esc(name)}: ${shown.map((s) => `${s.value} ${s.label}`).join(", ")}">${shown.map((s, i) =>
    `<span class="seg ${i === shown.length - 1 ? "end" : ""}" style="flex:${s.value} 0 0;background:${s.color}" ${tipAttrs(s.value.toLocaleString(), `${s.label}${name ? ` · ${name}` : ""}`, s.color)}></span>`).join("")}
    ${total !== null && sum < total ? `<span style="flex:${total - sum} 0 0"></span>` : ""}</div>`;
}

// ---------- horizontal bars (one series), value at the tip ----------
export function hbars(rows, { color = SERIES[0], max = null, fmt = (v) => v.toLocaleString(), unit = "" } = {}) {
  const m = max ?? Math.max(1, ...rows.map((r) => r.value));
  return `<div class="hbars">${rows.map((r) => `
    <div class="hbar-row"><span class="hbar-label">${esc(r.label)}</span>
      <span class="hbar-track"><span class="hbar" style="width:${Math.max(r.value ? 1.5 : 0, (100 * r.value) / m)}%;background:${r.color || color}" ${tipAttrs(fmt(r.value) + unit, r.label, r.color || color)}></span></span>
      <span class="hbar-value">${fmt(r.value)}${r.sub ? ` <span class="faint">${esc(r.sub)}</span>` : ""}</span></div>`).join("")}</div>`;
}

// ---------- paired bars (two series per category, e.g. planned vs spent) ----------
export function pairs(rows, [a, b], { fmt = (v) => v.toLocaleString() } = {}) {
  const m = Math.max(1, ...rows.flatMap((r) => [r.a, r.b]));
  return `<div class="pairs">${rows.map((r) => `
    <div class="pair-row"><span class="hbar-label">${esc(r.label)}</span>
      <div class="pair-bars">
        <span class="hbar-track"><span class="hbar" style="width:${(100 * r.a) / m}%;background:${a.color}" ${tipAttrs(fmt(r.a), `${a.label} · ${r.label}`, a.color)}></span></span>
        <span class="hbar-track"><span class="hbar" style="width:${(100 * r.b) / m}%;background:${b.color}" ${tipAttrs(fmt(r.b), `${b.label} · ${r.label}`, b.color)}></span></span>
      </div>
      <span class="hbar-value">${fmt(r.b)}<span class="faint"> / ${fmt(r.a)}</span>${r.note ? `<span class="pill red tiny" style="margin-left:6px">${esc(r.note)}</span>` : ""}</span></div>`).join("")}</div>`;
}
