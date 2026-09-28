// Link extracted fields to their evidence in the source text.
// Rows carry data-evidence-key="<field>"; <mark data-fields="a b"> wraps cited text.
(function () {
  "use strict";

  function marksFor(key) {
    return Array.from(document.querySelectorAll("mark.evidence")).filter(function (mark) {
      return (mark.dataset.fields || "").split(" ").indexOf(key) !== -1;
    });
  }

  function setActive(key, active) {
    marksFor(key).forEach(function (mark) { mark.classList.toggle("active", active); });
    document.querySelectorAll('[data-evidence-key="' + key + '"]').forEach(function (el) {
      if (el.tagName === "TR") el.classList.toggle("active", active);
    });
  }

  document.addEventListener("mouseover", function (event) {
    var row = event.target.closest("tr[data-evidence-key], li[data-evidence-key]");
    if (row) setActive(row.dataset.evidenceKey, true);
  });
  document.addEventListener("mouseout", function (event) {
    var row = event.target.closest("tr[data-evidence-key], li[data-evidence-key]");
    if (row && !row.contains(event.relatedTarget)) setActive(row.dataset.evidenceKey, false);
  });

  // Focus and select the value when an inline editor opens.
  document.addEventListener("htmx:load", function (event) {
    var input = event.target.querySelector && event.target.querySelector(".edit-form input[name=value]");
    if (input) { input.focus(); input.select(); }
  });

  document.addEventListener("click", function (event) {
    var link = event.target.closest("a.evidence-link");
    if (!link) return;
    event.preventDefault();
    var first = marksFor(link.dataset.evidenceKey)[0];
    if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
  });
})();
