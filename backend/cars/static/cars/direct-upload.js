/*
 * Direct-to-S3 uploads for the Dakka Motors admin.
 *
 * Uploading through Lambda is capped at roughly 4.5 MB (6 MB Lambda payload limit, minus
 * the ~33% that base64 encoding adds in API Gateway). Phone photos regularly exceed that
 * and video always does. This sends the bytes straight to S3 and hands Django only the
 * resulting object name.
 *
 * Progressive enhancement on purpose: the file inputs remain ordinary file inputs, so if
 * this script fails to load the admin still saves through Django's normal upload path.
 */
(function () {
  "use strict";

  var SIGN_URL = "/api/admin/uploads/sign/";

  function csrfToken() {
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (input) return input.value;
    var match = document.cookie.match(/(^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[2]) : "";
  }

  /* The hidden field name is derived from the file input's own name, so this works for
     both `video` on the car form and `images-0-image` on the inline formset. */
  function keyFieldName(input) {
    return input.name.replace(/image$/, "image_key").replace(/video$/, "video_key");
  }

  function hiddenKeyField(input) {
    var name = keyFieldName(input);
    var field = input.form.querySelector('input[name="' + name + '"]');
    if (!field) {
      field = document.createElement("input");
      field.type = "hidden";
      field.name = name;
      input.form.appendChild(field);
    }
    return field;
  }

  function statusLine(input) {
    var el = input.parentNode.querySelector(".direct-upload-status");
    if (!el) {
      el = document.createElement("div");
      el.className = "direct-upload-status";
      input.parentNode.appendChild(el);
    }
    return el;
  }

  function setBusy(form, busy) {
    // Submitting mid-upload would save a row pointing at an object that does not exist
    // yet, so the save buttons are disabled until every upload settles.
    var buttons = form.querySelectorAll('input[type=submit], button[type=submit]');
    Array.prototype.forEach.call(buttons, function (button) {
      button.disabled = busy;
    });
  }

  var inFlight = 0;

  function track(form, delta) {
    inFlight += delta;
    setBusy(form, inFlight > 0);
  }

  function sign(kind, file) {
    return fetch(SIGN_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken(),
      },
      body: JSON.stringify({
        kind: kind,
        content_type: file.type,
        size: file.size,
      }),
    }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) throw new Error(body.detail || "Upload was refused.");
        return body;
      });
    });
  }

  function putToS3(target, file, onProgress) {
    return new Promise(function (resolve, reject) {
      var data = new FormData();
      Object.keys(target.fields).forEach(function (name) {
        data.append(name, target.fields[name]);
      });
      // S3 requires the file to be the last field in the form.
      data.append("file", file);

      var xhr = new XMLHttpRequest();
      xhr.open("POST", target.url, true);
      xhr.upload.onprogress = function (event) {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      };
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error("S3 rejected the upload (" + xhr.status + ")."));
      };
      xhr.onerror = function () {
        reject(new Error("Network error while uploading."));
      };
      xhr.send(data);
    });
  }

  function handle(input, kind) {
    var file = input.files && input.files[0];
    if (!file) return;

    var form = input.form;
    var status = statusLine(input);
    var keyField = hiddenKeyField(input);
    keyField.value = "";

    track(form, 1);
    status.className = "direct-upload-status is-busy";
    status.textContent = "Preparing…";

    sign(kind, file)
      .then(function (target) {
        return putToS3(target, file, function (fraction) {
          status.textContent = "Uploading " + Math.round(fraction * 100) + "%";
        }).then(function () {
          keyField.value = target.name;
          // Clear the input so the browser does not also post the bytes through
          // Lambda, which is exactly the limit this avoids.
          input.value = "";
          status.className = "direct-upload-status is-done";
          status.textContent = "Uploaded " + file.name + " — save to finish.";
        });
      })
      .catch(function (error) {
        keyField.value = "";
        status.className = "direct-upload-status is-error";
        status.textContent = error.message;
      })
      .then(function () {
        track(form, -1);
      });
  }

  function kindFor(input) {
    if (/video/.test(input.name)) return "video";
    if (/image/.test(input.name)) return "image";
    return null;
  }

  // Delegated so inline rows added by the admin's "add another" link are covered too.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input || input.tagName !== "INPUT" || input.type !== "file") return;
    if (!input.form) return;
    var kind = kindFor(input);
    if (kind) handle(input, kind);
  });
})();
