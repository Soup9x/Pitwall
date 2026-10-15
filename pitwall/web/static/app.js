// Live countdowns: <span data-countdown="ISO-8601">
(function () {
  function fmt(ms) {
    if (ms <= 0) return "live now";
    const s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
      m = Math.floor((s % 3600) / 60), sec = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return (d ? d + "d " : "") + pad(h) + ":" + pad(m) + ":" + pad(sec);
  }
  function tick() {
    document.querySelectorAll("[data-countdown]").forEach(function (el) {
      el.textContent = fmt(new Date(el.dataset.countdown) - new Date());
    });
  }
  tick();
  setInterval(tick, 1000);
})();

// Settings → add channel: show only the fields for the selected type, and disable hidden
// inputs so a field that appears in two sections (e.g. password) isn't submitted twice.
(function () {
  document.querySelectorAll("form.target-form").forEach(function (form) {
    const select = form.querySelector("[data-kind-select]");
    function update() {
      form.querySelectorAll("[data-kind]").forEach(function (el) {
        const show = el.dataset.kind.split(" ").indexOf(select.value) !== -1;
        el.style.display = show ? "" : "none";
        el.querySelectorAll("input").forEach(function (i) { i.disabled = !show; });
      });
    }
    select.addEventListener("change", update);
    update();
  });
})();

// Season pickers submit on change; forms with data-confirm ask first. (No inline handlers:
// the Content-Security-Policy only allows this file's scripts.)
(function () {
  document.querySelectorAll("select[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { el.form.submit(); });
  });
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const target = document.getElementById(btn.dataset.copy);
      if (!target) return;
      const text = target.value || target.textContent;
      const done = function () { btn.textContent = "Copied ✓"; };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done);
        return;
      }
      // Plain http (e.g. on the LAN) has no clipboard API: copy via a hidden text box.
      const box = document.createElement("textarea");
      box.value = text;
      box.setAttribute("readonly", "");
      box.style.position = "fixed";
      box.style.opacity = "0";
      document.body.appendChild(box);
      box.select();
      try { if (document.execCommand("copy")) done(); } finally { box.remove(); }
    });
  });
})();
