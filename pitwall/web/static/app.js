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
