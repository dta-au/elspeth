// site.js — shared project-site behaviour: render icons, theme toggle, and
// copy-to-clipboard on command/config blocks. The initial theme is set pre-paint
// by an inline <head> script (reads localStorage + prefers-color-scheme); this
// only handles the toggle, persists the choice, and progressively enhances code
// blocks marked `.copyable`.
(function () {
  // --- theme toggle --------------------------------------------------------
  var t = document.getElementById("theme-toggle");
  if (t) {
    t.hidden = false;
    setIcon();
    t.addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("elspeth-theme", next); } catch (e) {}
      setIcon();
    });
  }

  // Native details keeps navigation usable without JavaScript. Escape closes
  // it and returns keyboard focus to the control that opened it.
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
  function setIcon() {
    var dark = document.documentElement.getAttribute("data-theme") !== "light";
    t.innerHTML = '<i data-lucide="' + (dark ? "moon" : "sun") + '"></i>';
    if (window.lucide) window.lucide.createIcons();
    // Reflect the action the control performs, for screen readers (A11y).
    t.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  }

  // --- copy-to-clipboard ---------------------------------------------------
  var status = document.createElement("p");
  status.className = "sr-only";
  status.setAttribute("role", "status");
  document.body.appendChild(status);
  document.querySelectorAll("pre.copyable").forEach(function (pre) {
    // Capture the payload before injecting the button; strip leading shell
    // prompts ("$ ") per line so pasted commands are clean. Config blocks have
    // no prompt and copy verbatim.
    var payload = pre.textContent.replace(/^[ \t]*\$[ \t]/gm, "").replace(/\s+$/, "");
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.setAttribute("aria-label", "Copy to clipboard");
    btn.innerHTML = '<i data-lucide="copy"></i>';
    pre.appendChild(btn);
    btn.addEventListener("click", function () {
      copyText(payload).then(function (ok) {
        status.textContent = ok ? "Copied to clipboard." : "Copy failed. Select the command text and copy it manually.";
        if (!ok) return;
        flash(btn);
      });
    });
  });

  function flash(btn) {
    btn.classList.add("copied");
    btn.setAttribute("aria-label", "Copied");
    btn.innerHTML = '<i data-lucide="check"></i>';
    if (window.lucide) window.lucide.createIcons();
    setTimeout(function () {
      btn.classList.remove("copied");
      btn.setAttribute("aria-label", "Copy to clipboard");
      btn.innerHTML = '<i data-lucide="copy"></i>';
      if (window.lucide) window.lucide.createIcons();
    }, 1600);
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return fallbackCopy(text); }
      );
    }
    return Promise.resolve(fallbackCopy(text));
  }

  // Fallback for non-secure contexts (e.g. opening the files over file://).
  function fallbackCopy(text) {
    var previousFocus = document.activeElement;
    var ta = document.createElement("textarea");
    try {
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "absolute";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      return ok;
    } catch (e) {
      return false;
    } finally {
      ta.remove();
      if (previousFocus) previousFocus.focus();
    }
  }

  // Render all icons last, after the toggle + copy buttons are in the DOM.
  if (window.lucide) window.lucide.createIcons();
})();
