// Charts drawn with the vendored Chart.js (static/vendor). Data comes from
// <script type="application/json" id="..."> blocks referenced by data-chart on a <canvas>;
// no inline scripts (Content-Security-Policy).
(function () {
  function ready(canvas) {
    var box = canvas.closest("[data-skeleton]");
    if (box) box.classList.remove("skeleton");
  }
  if (typeof Chart === "undefined") {
    document.querySelectorAll("canvas[data-chart]").forEach(ready);
    return;
  }
  var css = getComputedStyle(document.documentElement);
  var muted = css.getPropertyValue("--muted").trim();
  var grid = css.getPropertyValue("--border").trim();
  var accent = css.getPropertyValue("--accent").trim();
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  document.querySelectorAll("canvas[data-chart='temp']").forEach(function (canvas) {
    var src = document.getElementById(canvas.dataset.src);
    if (!src) return;
    var data;
    try { data = JSON.parse(src.textContent); } catch (e) { ready(canvas); return; }
    ready(canvas);
    new Chart(canvas, {
      type: "line",
      data: {
        datasets: [{
          label: data.label,
          data: data.points.map(function (p) { return { x: p[0], y: p[1] }; }),
          borderColor: accent, backgroundColor: accent, borderWidth: 2, pointRadius: 0, tension: 0.25,
        }],
      },
      options: {
        animation: reduce ? false : { duration: 400 },
        maintainAspectRatio: false,
        parsing: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: function (items) { return Math.round(items[0].parsed.x) + " min"; },
              label: function (item) { return item.parsed.y + " " + data.unit; },
            },
          },
        },
        scales: {
          x: { type: "linear", title: { display: true, text: "minutes from session start", color: muted },
               ticks: { color: muted }, grid: { color: grid } },
          y: { title: { display: true, text: data.unit, color: muted }, ticks: { color: muted }, grid: { color: grid } },
        },
      },
    });
  });
})();

// Race control timeline filters: checkboxes with data-rc-filter="sc" show/hide rows with
// data-kind="sc" (state is per page view).
(function () {
  var boxes = document.querySelectorAll("input[data-rc-filter]");
  if (!boxes.length) return;
  function apply() {
    var on = {};
    boxes.forEach(function (b) { on[b.dataset.rcFilter] = b.checked; });
    document.querySelectorAll("[data-rc-kind]").forEach(function (row) {
      row.hidden = !on[row.dataset.rcKind];
    });
  }
  boxes.forEach(function (b) { b.addEventListener("change", apply); });
  apply();
})();
