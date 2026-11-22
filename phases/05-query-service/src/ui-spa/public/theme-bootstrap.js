(function () {
  "use strict";

  var storageKey = "robot-vault.theme";
  var theme = "dark";

  try {
    var storedTheme = window.localStorage.getItem(storageKey);
    if (storedTheme === "dark" || storedTheme === "light") theme = storedTheme;
  } catch (_error) {
    // Storage may be blocked. Dark remains the deterministic safe default.
  }

  var root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.setAttribute("data-theme", theme);
  root.style.colorScheme = theme;

  var themeColor = document.querySelector('meta[name="theme-color"]');
  var color = themeColor && themeColor.getAttribute("data-" + theme);
  if (themeColor && color) themeColor.setAttribute("content", color);
})();
