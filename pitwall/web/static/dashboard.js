// Dashboard → Rearrange: drag cards by their handle to reorder them. The new order is saved
// with the same form endpoint the ↑/↓ buttons use (which also work without this script).
(function () {
  "use strict";
  var grid = document.getElementById("dashboard-grid");
  var form = document.getElementById("layout-form");
  if (!grid || !form) return;
  var dragged = null;

  function cards() { return Array.prototype.slice.call(grid.querySelectorAll("[data-card]")); }

  function save() {
    var data = new FormData();
    data.append("csrf_token", form.querySelector("[name=csrf_token]").value);
    data.append("mode", form.querySelector("[name=mode]").value);
    data.append("order", cards().map(function (c) { return c.dataset.card; }).join(","));
    fetch("/prefs/dashboard", {
      method: "POST", body: data, credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" },
    }).finally(function () {
      // Reload either way: the ↑/↓ buttons then match the order that was actually saved.
      location.reload();
    });
  }

  cards().forEach(function (card) {
    card.addEventListener("dragstart", function (e) {
      dragged = card;
      card.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", card.dataset.card);
    });
    card.addEventListener("dragend", function () {
      card.classList.remove("dragging");
      cards().forEach(function (c) { c.classList.remove("drop-before", "drop-after"); });
    });
    card.addEventListener("dragover", function (e) {
      if (!dragged || dragged === card) return;
      e.preventDefault();
      var r = card.getBoundingClientRect();
      var wide = r.width > grid.getBoundingClientRect().width * 0.6;
      var before = wide ? e.clientY < r.top + r.height / 2 : e.clientX < r.left + r.width / 2;
      card.classList.toggle("drop-before", before);
      card.classList.toggle("drop-after", !before);
    });
    card.addEventListener("dragleave", function () {
      card.classList.remove("drop-before", "drop-after");
    });
    card.addEventListener("drop", function (e) {
      if (!dragged || dragged === card) return;
      e.preventDefault();
      var before = card.classList.contains("drop-before");
      card.classList.remove("drop-before", "drop-after");
      grid.insertBefore(dragged, before ? card : card.nextSibling);
      save();
    });
  });
})();
