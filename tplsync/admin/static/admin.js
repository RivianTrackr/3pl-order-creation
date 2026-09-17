// Confirmation prompts, the live-processing warning and busy buttons.
document.addEventListener("submit", function (event) {
  var form = event.target;

  if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
    event.preventDefault();
    return;
  }

  var mode = form.querySelector("[data-live-warning]");
  if (mode && mode.value === "live" &&
      !window.confirm("This creates a real order in 3PL Central, completes it if stock allows, and updates the Syncore PO. Continue?")) {
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

// Ship Via override form: list the chosen carrier's services.
(function () {
  var form = document.getElementById("override-form");
  if (!form) return;
  var carriers = JSON.parse(form.dataset.carriers || "[]");
  var carrierSelect = form.querySelector("#carrier");
  var serviceSelect = form.querySelector("#mode");
  var initial = serviceSelect.dataset.selected;

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
  }

  carrierSelect.addEventListener("change", fill);
  fill();
})();
