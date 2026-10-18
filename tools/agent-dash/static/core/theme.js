/* Page core: the one theme rule both views share.
   Dark is the default: with no ?theme= and no saved viewer choice the page
   sets data-theme="dark" before first paint, so there is no light flash.
   The toggle and ?theme= keep working as before.
   Exports (on window.AdTheme, also on window.AdPage.theme): resolve, current,
   apply, isDark, toggle. Needs: nothing. index.html loads this in <head> so
   the shell applies before first paint; static/plan.js reads isDark() at call
   time for the mermaid theme, so the plan view follows the same rule. */
(function (global) {
  "use strict";
  var KEY = "agent-dash-theme";
  function themeSaved() {
    try { return global.localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function themeResolve(search, stored) {
    var theme = null;
    try { theme = new URLSearchParams(search || "").get("theme"); } catch (e) { theme = null; }
    if (theme !== "dark" && theme !== "light") theme = stored;
    if (theme !== "dark" && theme !== "light") theme = "dark";
    return theme;
  }
  function themeCurrent() {
    return themeResolve(global.location ? global.location.search : "", themeSaved());
  }
  function themeApply(theme) {
    var next = (theme === "dark" || theme === "light") ? theme : themeCurrent();
    try { global.document.documentElement.setAttribute("data-theme", next); } catch (e) {}
    return next;
  }
  function themeDark() {
    var set = null;
    try { set = global.document.documentElement.getAttribute("data-theme"); } catch (e) { set = null; }
    if (set) return set === "dark";
    return themeCurrent() === "dark";
  }
  function themeToggle() {
    var next = themeDark() ? "light" : "dark";
    themeApply(next);
    try { global.localStorage.setItem(KEY, next); } catch (e) {}
    return next;
  }
  var AdTheme = {key: KEY, resolve: themeResolve, current: themeCurrent, apply: themeApply, isDark: themeDark, toggle: themeToggle};
  global.AdTheme = AdTheme;
  var AdPage = (global.AdPage = global.AdPage || {});
  AdPage.theme = AdTheme;
})(window);
/* The shell applies before first paint; later scripts read AdTheme.isDark().
   :root already carries the dark set, so even a missed load stays dark. */
try { window.AdTheme.apply(); } catch (e) { try { document.documentElement.setAttribute("data-theme", "dark"); } catch (e2) {} }
