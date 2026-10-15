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
