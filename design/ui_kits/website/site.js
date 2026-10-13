// site.js — shared marketing-site behaviour: icons, theme and navigation.
(function () {
  if (window.lucide) window.lucide.createIcons();
  var t = document.getElementById("theme-toggle");
  if (t) {
    t.addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      t.innerHTML = '<i data-lucide="' + (next === "dark" ? "moon" : "sun") + '"></i>';
      if (window.lucide) window.lucide.createIcons();
    });
  }
  // Native details works without JavaScript; Escape restores focus to Menu.
  var menu = document.querySelector(".nav-menu");
  if (menu) {
    menu.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && menu.open) {
        menu.open = false;
        menu.querySelector("summary").focus();
      }
    });
    document.addEventListener("click", function (event) {
      if (!menu.contains(event.target)) menu.open = false;
    });
  }
})();
