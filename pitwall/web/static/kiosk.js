// Kiosk display. Data comes from the JSON block in the page, then from /kiosk/data every 60 s.
// Everything is written with textContent (never innerHTML), so no data is treated as markup.
// Options (query string): rotate=1 alternates the race weekend and standings views,
// interval=N seconds per view (10-3600, default 30), layout=portrait.
(function () {
  "use strict";
  var params = new URLSearchParams(location.search);
  var token = params.get("token") || "";
  var rotate = params.get("rotate") === "1";
  var interval = Math.min(3600, Math.max(10, parseInt(params.get("interval"), 10) || 30));
  var REFRESH_MS = 60 * 1000;
  var TRIVIA_MS = 30 * 1000;
  var SHIFT_MS = 3 * 60 * 1000;
  var stage = document.getElementById("stage");
  var data = null;
  var offset = 0; // server time minus device time, so the countdown uses the server's clock
  var lastOk = null; // when data last arrived (server time)
  var offline = false;
  var factIndex = 0;
  var rotateWeekend = true;

  function el(key) { return document.querySelector('[data-k="' + key + '"]'); }
  function make(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function now() { return new Date(Date.now() + offset); }
  function fmtTime(d, withSeconds) {
    try {
      return new Intl.DateTimeFormat(undefined, {
        hour: "2-digit", minute: "2-digit", second: withSeconds ? "2-digit" : undefined,
        hour12: false, timeZone: data && data.tz ? data.tz : undefined,
      }).format(d);
    } catch (e) {
      return d.toTimeString().slice(0, withSeconds ? 8 : 5);
    }
  }
  function pts(n) { return n === null || n === undefined ? "" : (Math.round(n) === n ? String(n) : n.toFixed(1)); }

  // ------------------------------------------------------------------ rendering
  function standings(listKey, rows, constructors) {
    var ol = el(listKey);
    clear(ol);
    if (!rows.length) {
      ol.appendChild(make("li", "k-held", data.held
        ? "Hidden by spoiler mode until " + data.held + " is marked as watched."
        : "No standings yet."));
      return;
    }
    rows.forEach(function (r) {
      var li = make("li", r.followed ? "followed" : "");
      li.appendChild(make("span", "k-pos mono", r.pos));
      var code = make("span", constructors ? "k-code" : "k-code mono", constructors ? r.name : r.code);
      code.style.setProperty("--team", r.color || "transparent");
      if (r.followed) {
        var star = make("span", "k-star", "★");
        star.title = "Followed";
        code.appendChild(star);
      }
      li.appendChild(code);
      if (!constructors) li.appendChild(make("span", "k-name", r.name));
      li.appendChild(make("span", "k-pts mono", pts(r.points)));
      li.appendChild(make("span", "k-gap mono", r.gap ? "−" + pts(r.gap) : (r.pos === 1 ? "Leader" : "")));
      var chg = r.change;
      var arrow = !chg ? "–" : (chg > 0 ? "▲" + chg : "▼" + (-chg));
      var c = make("span", "k-chg mono " + (!chg ? "same" : (chg > 0 ? "up" : "down")), arrow);
      c.title = !chg ? "No change" : (chg > 0 ? "Up " + chg : "Down " + (-chg));
      li.appendChild(c);
      ol.appendChild(li);
    });
  }

  function render() {
    if (!data) return;
    var ev = el("event");
    clear(ev);
    if (data.event) {
      ev.appendChild(document.createTextNode(data.event.name + " "));
      ev.appendChild(make("span", "k-sub", "Round " + data.event.round + (data.event.place ? " · " + data.event.place : "")));
    }
    var n = data.next;
    el("next-label").textContent = n ? n.session + " · " + n.event : "No sessions scheduled";
    el("next-when").textContent = n ? n.when + (n.track && n.track !== n.when ? "  ·  track " + n.track : "") : "";
    el("next-forecast").textContent = n && n.forecast ? n.forecast : "";
    el("round").textContent = data.round ? "after round " + data.round : "";
    standings("drivers", data.drivers || [], false);
    standings("constructors", data.constructors || [], true);

    var tl = el("timeline");
    clear(tl);
    (data.timeline || []).forEach(function (s) {
      var li = make("li", s.state);
      li.appendChild(make("span", "", s.marker || ""));
      li.appendChild(make("span", "", s.label + (s.state === "live" ? " · LIVE" : "")));
      li.appendChild(make("span", "mono", s.when));
      tl.appendChild(li);
    });
    var wx = el("weather");
    clear(wx);
    (data.weather || []).forEach(function (w) {
      var li = make("li");
      li.appendChild(make("span", "k-muted", w.label + " "));
      li.appendChild(document.createTextNode(w.text));
      wx.appendChild(li);
    });
    if (!(data.weather || []).length) wx.appendChild(make("li", "k-muted", "No forecast yet"));
    var ty = el("tyres");
    clear(ty);
    if (data.tyres) {
      [["h", "H", data.tyres.hard], ["m", "M", data.tyres.medium], ["s", "S", data.tyres.soft]].forEach(function (t) {
        var chip = make("span", "k-tyre " + t[0]);
        chip.appendChild(make("b", "", t[1]));
        chip.appendChild(document.createTextNode(t[2]));
        ty.appendChild(chip);
      });
    } else {
      ty.appendChild(make("span", "k-muted", "Not announced"));
    }
    var pen = el("penalties");
    clear(pen);
    (data.penalties || []).forEach(function (p) {
      var li = make("li");
      li.appendChild(document.createTextNode(p.driver + " "));
      li.appendChild(make("span", "k-muted", p.text));
      pen.appendChild(li);
    });
    if (!(data.penalties || []).length) pen.appendChild(make("li", "k-muted", "None"));
    showFact(false);
    setView();
    tick();
  }

  function setView() {
    var view = data && data.weekend ? "weekend" : "normal";
    if (rotate && data && data.weekend && !rotateWeekend) view = "normal";
    stage.setAttribute("data-view", view);
  }

  function showFact(advance) {
    var p = el("trivia");
    var facts = (data && data.trivia) || [];
    if (!facts.length) {
      p.textContent = data && data.held
        ? "Trivia is paused while spoiler mode holds " + data.held + "."
        : "Trivia comes back when the next race is on the calendar.";
      return;
    }
    if (advance) factIndex = (factIndex + 1) % facts.length;
    factIndex = factIndex % facts.length;
    p.classList.add("fade");
    setTimeout(function () {
      p.textContent = facts[factIndex];
      p.classList.remove("fade");
    }, advance ? 600 : 0);
  }

  // ------------------------------------------------------------------ clock + countdown
  function tick() {
    var t = now();
    el("clock").textContent = fmtTime(t, false);
    var box = el("countdown");
    var n = data && data.next;
    if (!n || !n.start) { box.textContent = ""; return; }
    var ms = Date.parse(n.start) - t.getTime();
    clear(box);
    if (ms <= 0) {
      box.className = "k-digits mono live";
      box.textContent = "LIVE NOW";
      return;
    }
    box.className = "k-digits mono";
    var s = Math.floor(ms / 1000);
    var parts = [[Math.floor(s / 86400), "d"], [Math.floor(s % 86400 / 3600), "h"], [Math.floor(s % 3600 / 60), "m"], [s % 60, "s"]];
    if (parts[0][0] === 0) parts.shift();
    parts.forEach(function (p, i) {
      box.appendChild(document.createTextNode(i && p[0] < 10 ? "0" + p[0] : String(p[0])));
      box.appendChild(make("span", "u", p[1]));
    });
  }

  // ------------------------------------------------------------------ data refresh
  function setOffline(state) {
    offline = state;
    var badge = el("offline");
    if (state && lastOk) {
      badge.textContent = "Offline – last updated " + fmtTime(lastOk, false);
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
  }

  function accept(d) {
    data = d;
    var server = Date.parse(d.generated_at);
    if (!isNaN(server)) {
      offset = server - Date.now();
      lastOk = new Date(server);
    }
    render();
  }

  function refresh() {
    var url = "/kiosk/data?token=" + encodeURIComponent(token);
    fetch(url, { cache: "no-store", credentials: "omit" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) { accept(d); setOffline(false); })
      .catch(function () { setOffline(true); });
  }

  // ------------------------------------------------------------------ screen care
  function shift() {
    // Burn-in protection: nudge the whole layout a few pixels every few minutes.
    var dx = Math.round(Math.random() * 12 - 6);
    var dy = Math.round(Math.random() * 8 - 4);
    stage.style.transform = "translate(" + dx + "px, " + dy + "px)";
  }

  var lock = null;
  function keepAwake() {
    if (!("wakeLock" in navigator) || document.visibilityState !== "visible") return;
    navigator.wakeLock.request("screen").then(function (l) {
      lock = l;
      lock.addEventListener("release", function () { lock = null; });
    }).catch(function () { /* not allowed (e.g. plain http): the screen settings decide */ });
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && !lock) keepAwake();
  });

  // ------------------------------------------------------------------ start
  try {
    accept(JSON.parse(document.getElementById("kiosk-data").textContent));
  } catch (e) { /* the first refresh fills it in */ }
  setInterval(tick, 1000);
  setInterval(refresh, REFRESH_MS);
  setInterval(function () { showFact(true); }, TRIVIA_MS);
  setInterval(shift, SHIFT_MS);
  if (rotate) {
    setInterval(function () { rotateWeekend = !rotateWeekend; setView(); }, interval * 1000);
  }
  keepAwake();
})();
