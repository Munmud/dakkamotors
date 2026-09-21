/*
 * "Add another row" for the formsets on the car page.
 *
 * Progressive enhancement, on the same terms as direct-upload.js beside it: the buttons
 * ship with `hidden` set and this script removes it. A page whose JavaScript never
 * arrives shows the rows Django rendered and no button that does nothing.
 *
 * This file is served under a content-hashed name. It was not, once, and a change
 * here never reached production: collectstatic ran inside the Lambda, where every
 * file carries a 1980 timestamp, so it judged the copy already on S3 newer and
 * skipped it -- while browsers held the old one under a year-long cache header. The
 * buttons "did nothing" for a day. The hash is what makes a change here a new URL.
 *
 * New rows come from Django's own `empty_form`, parked in a <template> with the string
 * `__prefix__` where the row number goes. Not from cloning the last visible row: a
 * clone copies what is *in* the row, and for a photo that includes the hidden
 * `image_key` direct-upload.js has just filled in with an S3 object name. Two rows
 * carrying one key attach the same object to the car twice, because _apply_images
 * creates one IMG# per row that has a name and does not deduplicate.
 */
(function () {
  "use strict";

  function managementField(prefix, suffix) {
    return document.querySelector('input[name="' + prefix + '-' + suffix + '"]');
  }

  /* Adds one row. Returns whether another could still be added afterwards, so the
     caller can retire the button both at the cap and when something went wrong. */
  function addRow(prefix) {
    var body = document.querySelector('[data-formset-rows="' + prefix + '"]');
    var template = document.querySelector('[data-formset-empty="' + prefix + '"]');
    var total = managementField(prefix, "TOTAL_FORMS");
    if (!body || !template || !total) return false;

    var index = parseInt(total.value, 10);
    if (isNaN(index)) return false;

    // The cap arrives in the management form rather than in a data- attribute, so the
    // server stays the one place that decides it. Specs publish MAX_PAIRS here; the
    // media formsets publish Django's default, which is no practical limit.
    var maxField = managementField(prefix, "MAX_NUM_FORMS");
    var max = maxField ? parseInt(maxField.value, 10) : NaN;
    if (!isNaN(max) && index >= max) return false;

    // innerHTML on a detached <tbody> parses a <tr> correctly, because the fragment is
    // parsed in the context of the element it is assigned to. The same string given to
    // a <div> would have its row thrown away by the parser.
    var host = document.createElement("tbody");
    host.innerHTML = template.innerHTML.replace(/__prefix__/g, index);

    var added = null;
    while (host.firstElementChild) {
      added = body.appendChild(host.firstElementChild);
    }
    if (!added) return false;

    // A table with no rows ships hidden, so its header does not sit on the page as a
    // stray line of labels above nothing. The first row is what makes it a table.
    var table = body.closest("table");
    if (table) table.hidden = false;

    // Only once the row is actually in the document. A management form claiming a row
    // that is not on the page posts an index Django cannot match to any input.
    total.value = index + 1;

    var first = added.querySelector("input, select, textarea");
    if (first) first.focus();

    return isNaN(max) || index + 1 < max;
  }

  // Delegated, so a button in a panel rendered after this ran still works.
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-formset-add]");
    if (!button) return;
    event.preventDefault();
    if (!addRow(button.getAttribute("data-formset-add"))) button.disabled = true;
  });

  Array.prototype.forEach.call(
    document.querySelectorAll("[data-formset-add]"),
    function (button) { button.hidden = false; }
  );
})();
