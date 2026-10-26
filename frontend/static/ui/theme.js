// Light / dark / system theme and accent colour: applied instantly, remembered in this browser, and saved to the account.
import { setChartTheme } from "./charts.js";
import { post } from "./core.js";

export const THEMES = [["light", "Light"], ["dark", "Dark"], ["system", "System"]];
export const ACCENTS = [["blue", "Blue", "#0071e3"], ["indigo", "Indigo", "#5856d6"], ["green", "Green", "#1f8a3e"], ["pink", "Pink", "#d1305f"]];
const media = window.matchMedia("(prefers-color-scheme: dark)");
const state = { theme: localStorage.getItem("stagecraft-theme") || "system", accent: localStorage.getItem("stagecraft-accent") || "blue" };
let onRepaint = () => {};

export const currentTheme = () => state.theme;
export const currentAccent = () => state.accent;
export const isDark = () => document.documentElement.dataset.theme === "dark";

function apply() {
  const dark = state.theme === "dark" || (state.theme === "system" && media.matches);
  const changed = document.documentElement.dataset.theme !== (dark ? "dark" : "light");
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.documentElement.dataset.accent = state.accent;
  setChartTheme(dark);
  return changed;
}

// main.js passes a function that re-renders the current screen (charts bake their colours into the markup).
export function initTheme(repaint) {
  onRepaint = repaint;
  apply();
  media.addEventListener("change", () => { if (state.theme === "system" && apply()) onRepaint(); });
}

// Called after login: the account's saved choice wins over this browser's.
export function adoptUserTheme(user) {
  if (!user) return;
  let changed = false;
  if (user.theme && user.theme !== state.theme) { state.theme = user.theme; localStorage.setItem("stagecraft-theme", user.theme); changed = true; }
  if (user.accent && user.accent !== state.accent) { state.accent = user.accent; localStorage.setItem("stagecraft-accent", user.accent); changed = true; }
  if (changed && apply()) onRepaint();
}

export async function setTheme(theme, accent) {
  if (theme) { state.theme = theme; localStorage.setItem("stagecraft-theme", theme); }
  if (accent) { state.accent = accent; localStorage.setItem("stagecraft-accent", accent); }
  if (apply() || accent) onRepaint();
  try { await post("/api/account/preferences", { theme: theme || undefined, accent: accent || undefined }); } catch { /* kept locally */ }
}
