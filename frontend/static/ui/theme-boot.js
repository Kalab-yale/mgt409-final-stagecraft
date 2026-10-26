// Runs before the stylesheet paints (plain script in <head>): apply the saved theme and accent so dark mode never
// flashes white. theme.js takes over once the app loads (and syncs with the account's saved preference).
(function () {
  try {
    var t = localStorage.getItem("stagecraft-theme") || "system";
    var a = localStorage.getItem("stagecraft-accent") || "blue";
    var dark = t === "dark" || (t === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    document.documentElement.setAttribute("data-accent", a);
  } catch (e) { /* storage blocked: stay light */ }
})();
