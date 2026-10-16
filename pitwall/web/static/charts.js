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

// Season progression (standings page). One JSON block holds both championships; the controls
// switch kind, top 5 and "gap to leader" without a reload. Followed drivers (or their teams)
// are drawn in team colour and labelled at the line end; everyone else is muted.
(function () {
  if (typeof Chart === "undefined") return;
  var canvas = document.querySelector("canvas[data-chart='progression']");
  if (!canvas) return;
  var src = document.getElementById(canvas.dataset.src);
  var all;
  try { all = JSON.parse(src.textContent); } catch (e) { return; }
  var css = getComputedStyle(document.documentElement);
  var muted = css.getPropertyValue("--muted").trim();
  var grid = css.getPropertyValue("--border").trim();
  var text = css.getPropertyValue("--text").trim();
  var accent = css.getPropertyValue("--accent").trim();
  var mono = css.getPropertyValue("--font-mono").trim() || "monospace";
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var state = { kind: "driver", top5: window.innerWidth < 640, gap: false };
  var top5Box = document.querySelector("[data-prog='top5']");
  if (top5Box) top5Box.checked = state.top5;

  function hexA(hex, a) {
    var m = /^#?([0-9a-f]{6})$/i.exec((hex || "").trim());
    if (!m) return hex;
    var n = parseInt(m[1], 16);
    return "rgba(" + (n >> 16) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  function datasets() {
    var d = all[state.kind];
    var series = d.series.filter(function (s) { return !state.top5 || (s.final_position && s.final_position <= 5); });
    var leader = d.rounds.map(function (_r, i) {
      var best = null;
      d.series.forEach(function (s) { var v = s.points[i]; if (v !== null && (best === null || v > best)) best = v; });
      return best;
    });
    // Muted lines first so highlighted ones are drawn on top.
    series = series.slice().sort(function (a, b) { return (a.followed ? 1 : 0) - (b.followed ? 1 : 0); });
    return series.map(function (s) {
      var colour = s.followed ? (s.color || accent) : hexA(muted, 0.55);
      return {
        label: s.code, fullName: s.name, followed: s.followed,
        data: s.points.map(function (v, i) {
          if (v === null) return null;
          return state.gap && leader[i] !== null ? Math.round((v - leader[i]) * 10) / 10 : v;
        }),
        borderColor: colour, backgroundColor: colour,
        borderWidth: s.followed ? 3 : 1.5, pointRadius: s.followed && window.innerWidth >= 640 ? 2.5 : 0, pointHoverRadius: 4,
        spanGaps: false, tension: 0.15,
      };
    });
  }

  // Draws each followed line's code (and, with top 5, every line's) just after its last point.
  var endLabels = {
    id: "endLabels",
    afterDatasetsDraw: function (chart) {
      var ctx = chart.ctx;
      ctx.save();
      ctx.font = "700 11px " + mono;
      ctx.textBaseline = "middle";
      chart.data.datasets.forEach(function (ds, i) {
        if (!(ds.followed || state.top5)) return;
        var meta = chart.getDatasetMeta(i);
        if (meta.hidden) return;
        for (var j = ds.data.length - 1; j >= 0; j--) {
          if (ds.data[j] !== null) {
            var pt = meta.data[j];
            ctx.fillStyle = ds.followed ? text : muted;
            ctx.fillText(ds.label, pt.x + 5, pt.y);
            break;
          }
        }
      });
      ctx.restore();
    },
  };

  var labels = function () {
    return all[state.kind].rounds.map(function (r) { return r.label + (r.sprint ? " ⚡" : ""); });
  };
  var chart = new Chart(canvas, {
    type: "line",
    data: { labels: labels(), datasets: datasets() },
    plugins: [endLabels],
    options: {
      animation: reduce ? false : { duration: 300 },
      maintainAspectRatio: false,
      layout: { padding: { right: 40 } },
      interaction: { mode: "nearest", intersect: false, axis: "xy" },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: function (items) {
              var r = all[state.kind].rounds[items[0].dataIndex];
              return "Round " + r.round + " · " + r.name + (r.sprint ? " (sprint weekend)" : "");
            },
            label: function (item) {
              var v = item.parsed.y;
              return item.dataset.fullName + ": " + (state.gap ? (v === 0 ? "leader" : v + " pts") : v + " pts");
            },
          },
        },
      },
      scales: {
        x: { ticks: { color: muted, autoSkip: true, maxRotation: 0 }, grid: { color: grid } },
        y: {
          title: { display: true, text: "points", color: muted },
          ticks: { color: muted }, grid: { color: grid },
        },
      },
    },
  });
  var box = canvas.closest("[data-skeleton]");
  if (box) box.classList.remove("skeleton");

  function update() {
    chart.data.labels = labels();
    chart.data.datasets = datasets();
    chart.options.scales.y.title.text = state.gap ? "points behind the leader" : "points";
    chart.update();
  }
  document.querySelectorAll("[data-prog]").forEach(function (el) {
    el.addEventListener("change", function () {
      if (el.dataset.prog === "kind") state.kind = el.value;
      else state[el.dataset.prog] = el.checked;
      update();
    });
  });
})();
