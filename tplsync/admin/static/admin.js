// Confirmation prompts, the live-processing warning and busy buttons.
document.addEventListener("submit", function (event) {
  var form = event.target;

  if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
    event.preventDefault();
    return;
  }

  var mode = form.querySelector("[data-live-warning]");
  if (mode && mode.value === "live" &&
      !window.confirm("This creates a real order in 3PL Central and completes it if there's enough stock. Continue?")) {
    event.preventDefault();
    return;
  }

  var button = event.submitter;
  if (button && button.dataset.loadingText) {
    window.setTimeout(function () {
      button.disabled = true;
      button.textContent = button.dataset.loadingText;
    }, 0);
  }
});

// Ship Via override form: list the chosen carrier's services and billing codes.
(function () {
  var form = document.getElementById("override-form");
  if (!form) return;
  var carriers = JSON.parse(form.dataset.carriers || "[]");
  var carrierSelect = form.querySelector("#carrier");
  var serviceSelect = form.querySelector("#mode");
  var billingSelect = form.querySelector("#billing_code");
  var initial = serviceSelect.dataset.selected;
  var initialBilling = billingSelect.dataset.selected;

  function fill() {
    var carrier = carriers.find(function (c) { return c.name === carrierSelect.value; });
    serviceSelect.textContent = "";
    var placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = carrier ? "Choose a service" : "Choose a carrier first";
    serviceSelect.appendChild(placeholder);
    (carrier ? carrier.services : []).forEach(function (s) {
      var option = document.createElement("option");
      option.value = s.code;
      option.textContent = s.description + " (" + s.code + ")";
      if (s.code === initial) option.selected = true;
      serviceSelect.appendChild(option);
    });
    initial = null;

    billingSelect.textContent = "";
    var standard = document.createElement("option");
    standard.value = "";
    standard.textContent = "Default (" + billingSelect.dataset.default + ")";
    billingSelect.appendChild(standard);
    (carrier ? carrier.billing_codes : []).forEach(function (code) {
      var option = document.createElement("option");
      option.value = code;
      option.textContent = code;
      if (code === initialBilling) option.selected = true;
      billingSelect.appendChild(option);
    });
    initialBilling = null;
  }

  carrierSelect.addEventListener("change", fill);
  fill();
})();

// Selects that filter a page as soon as they change.
document.querySelectorAll("[data-autosubmit]").forEach(function (select) {
  select.addEventListener("change", function () { select.form.submit(); });
});

// Close buttons on flash messages.
document.querySelectorAll("[data-dismiss-notice]").forEach(function (button) {
  button.addEventListener("click", function () { button.closest(".notice").remove(); });
});

// Table rows that open a page when clicked anywhere (links and buttons inside still work).
document.querySelectorAll("tr[data-href]").forEach(function (row) {
  row.addEventListener("click", function (event) {
    if (event.target.closest("a, button, input, select, label, form")) return;
    window.location.href = row.dataset.href;
  });
});

// Instant filter for a list of rows: <input data-filter-rows="#tbody-id">, rows carry data-filter-text.
document.querySelectorAll("input[data-filter-rows]").forEach(function (input) {
  var body = document.querySelector(input.dataset.filterRows);
  if (!body) return;
  var empty = body.closest(".table-wrap") && body.closest(".table-wrap").querySelector(".empty-filter");
  input.addEventListener("input", function () {
    var needle = input.value.trim().toLowerCase();
    var shown = 0;
    body.querySelectorAll("tr[data-filter-text]").forEach(function (row) {
      var match = !needle || row.dataset.filterText.indexOf(needle) !== -1;
      row.hidden = !match;
      if (match) shown += 1;
    });
    if (empty) empty.hidden = shown !== 0;
  });
});

// Bulk actions on the dashboard: tick POs, then Retry or Dismiss them together.
(function () {
  var form = document.getElementById("bulk-form");
  if (!form) return;
  var items = Array.prototype.slice.call(document.querySelectorAll("[data-bulk-item]"));
  var all = document.querySelector("[data-bulk-all]");
  var count = form.querySelector("[data-bulk-count]");
  function update() {
    var n = items.filter(function (i) { return i.checked; }).length;
    form.hidden = n === 0;
    count.textContent = n + " selected";
    if (all) { all.checked = n > 0 && n === items.length; all.indeterminate = n > 0 && n < items.length; }
  }
  items.forEach(function (i) { i.addEventListener("change", update); });
  if (all) all.addEventListener("change", function () {
    items.forEach(function (i) { i.checked = all.checked; });
    update();
  });
})();

// Column chart tooltips: each day's column (its full-height hit area) shows the date and count.
document.querySelectorAll("[data-chart]").forEach(function (figure) {
  var tip = figure.querySelector(".chart-tip");
  var value = tip.querySelector("[data-tip-value]");
  var label = tip.querySelector("[data-tip-label]");
  function show(target) {
    var n = Number(target.dataset.count);
    value.textContent = n + (n === 1 ? " order" : " orders");
    label.textContent = target.dataset.label;
    tip.hidden = false;
    var box = figure.getBoundingClientRect(), r = target.getBoundingClientRect();
    var x = r.left + r.width / 2 - box.left;
    tip.style.left = Math.min(Math.max(x, 60), box.width - 60) + "px";
    figure.querySelectorAll(".bar-hit.is-active").forEach(function (el) { el.classList.remove("is-active"); });
    target.classList.add("is-active");
  }
  function hide() {
    tip.hidden = true;
    figure.querySelectorAll(".bar-hit.is-active").forEach(function (el) { el.classList.remove("is-active"); });
  }
  figure.querySelectorAll(".bar-hit").forEach(function (hit) {
    hit.addEventListener("pointerenter", function () { show(hit); });
    hit.addEventListener("focus", function () { show(hit); });
    hit.addEventListener("blur", hide);
  });
  figure.addEventListener("pointerleave", hide);
});
