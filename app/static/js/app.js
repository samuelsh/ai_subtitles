(function () {
  "use strict";

  var MAX_SIZE = 2 * 1024 * 1024 * 1024;  // 2 GiB, matches server UPLOAD_LIMIT_BYTES
  var ALLOWED = [".mp3", ".wav", ".ogg", ".mp4"];

  var form = document.getElementById("transcribe_form");
  if (!form) return;

  var input = document.getElementById("audio_file");
  var dropzone = document.getElementById("dropzone");
  var dropzoneText = document.getElementById("dropzone_text");
  var submitBtn = document.getElementById("submit_btn");
  var cancelBtn = document.getElementById("cancel_btn");
  var progressEl = document.getElementById("progress");
  var progressLabel = document.getElementById("progress_label");
  var progressElapsed = document.getElementById("progress_elapsed");
  var progressFill = document.getElementById("progress_fill");
  var progressMeta = document.getElementById("progress_meta");
  var progressLog = document.getElementById("progress_log");
  var errorEl = document.getElementById("error");

  var uploadController = null;
  var eventSource = null;
  var timerId = null;
  var startedAt = 0;

  function extOk(name) {
    var i = name.lastIndexOf(".");
    if (i < 0) return false;
    return ALLOWED.indexOf(name.slice(i).toLowerCase()) > -1;
  }

  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KiB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MiB";
  }

  function fmtElapsed(ms) {
    var s = Math.floor(ms / 1000);
    var m = Math.floor(s / 60);
    s = s % 60;
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }

  function show(file) {
    dropzone.classList.add("is-set");
    dropzoneText.textContent = file.name + " · " + fmtSize(file.size);
    validate(file);
  }

  function validate(file) {
    clearError();
    if (!extOk(file.name)) {
      error("Unsupported format. Use .mp3, .wav, .ogg, or .mp4.");
      submitBtn.disabled = true;
      return false;
    }
    if (file.size > MAX_SIZE) {
      error("File is " + fmtSize(file.size) + ". Maximum size is 2 GiB.");
      submitBtn.disabled = true;
      return false;
    }
    submitBtn.disabled = false;
    return true;
  }

  function error(msg) {
    errorEl.textContent = msg;
    errorEl.hidden = false;
  }

  function clearError() { errorEl.hidden = true; errorEl.textContent = ""; }

  function startTimer() {
    startedAt = Date.now();
    progressElapsed.textContent = "00:00";
    timerId = setInterval(function () {
      progressElapsed.textContent = fmtElapsed(Date.now() - startedAt);
    }, 1000);
  }

  function stopTimer() {
    if (timerId) { clearInterval(timerId); timerId = null; }
  }

  function setBusy(busy) {
    submitBtn.disabled = busy;
    cancelBtn.hidden = !busy;
    cancelBtn.disabled = !busy;
    if (busy) {
      progressEl.hidden = false;
      progressEl.classList.remove("progress--done", "progress--error");
      startTimer();
    } else {
      stopTimer();
    }
  }

  function finishProgress(message, cls) {
    stopTimer();
    setLabel(message);
    progressEl.classList.add("progress--" + cls);
    cancelBtn.hidden = true;
    submitBtn.disabled = false;
  }

  function resetForNextUpload() {
    input.value = "";
    dropzone.classList.remove("is-set");
    dropzoneText.textContent = "Drop a file here or click to browse";
    submitBtn.disabled = true;
  }

  function setLabel(text) { progressLabel.textContent = text; }

  function setFill(completed, total) {
    var pct = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
    progressFill.style.width = pct + "%";
  }

  function setMeta(text) { progressMeta.textContent = text || ""; }

  function appendLog(text, cls) {
    var li = document.createElement("li");
    li.textContent = text;
    if (cls) li.className = cls;
    progressLog.appendChild(li);
    // Keep the most recent ~20 entries visible.
    while (progressLog.children.length > 20) {
      progressLog.removeChild(progressLog.firstChild);
    }
  }

  function resetProgress() {
    progressFill.style.width = "0%";
    progressMeta.textContent = "";
    progressLog.innerHTML = "";
    setLabel("Uploading…");
  }

  function teardown() {
    if (eventSource) { eventSource.close(); eventSource = null; }
    if (uploadController) { uploadController.abort(); uploadController = null; }
    stopTimer();
  }

  input.addEventListener("change", function () {
    if (input.files && input.files[0]) show(input.files[0]);
  });

  dropzone.addEventListener("dragover", function (e) {
    e.preventDefault();
    dropzone.classList.add("is-drag");
  });
  dropzone.addEventListener("dragleave", function () {
    dropzone.classList.remove("is-drag");
  });
  dropzone.addEventListener("drop", function (e) {
    e.preventDefault();
    dropzone.classList.remove("is-drag");
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      var f = e.dataTransfer.files[0];
      input.files = e.dataTransfer.files;
      show(f);
    }
  });
  dropzone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      input.click();
    }
  });

  cancelBtn.addEventListener("click", function () {
    teardown();
    setBusy(false);
    error("Cancelled.");
  });

  function downloadResult(jobId) {
    // GET /result/<id> streams the subtitles with Content-Disposition, so the
    // browser handles the Save As dialog automatically.
    window.location.href = "/result/" + encodeURIComponent(jobId);
  }

  function openProgressStream(jobId) {
    setLabel("Transcribing…");
    setMeta("Waiting for status…");
    eventSource = new EventSource("/progress?job_id=" + encodeURIComponent(jobId));

    eventSource.addEventListener("snapshot", function (ev) {
      var d = JSON.parse(ev.data);
      applySnapshot(d);
    });

    eventSource.addEventListener("done", function (ev) {
      var d = JSON.parse(ev.data);
      applySnapshot(d);
      appendLog("Transcription complete.", "ok");
      finishProgress("Download ready — check your browser downloads.", "done");
      teardown();
      resetForNextUpload();
      // Trigger the file download. Browsers handle Save As via the
      // Content-Disposition header; this doesn't navigate away from the
      // page, so the UI stays usable.
      setTimeout(function () { downloadResult(jobId); }, 100);
      // Fade the progress card after the user has had a moment to see it.
      setTimeout(function () {
        progressEl.hidden = true;
      }, 4000);
    });

    eventSource.addEventListener("error", function (ev) {
      var msg = "Transcription failed.";
      try {
        if (ev.data) {
          var d = JSON.parse(ev.data);
          msg = d.message || d.error_message || msg;
        }
      } catch (e) { /* fall through with default */ }
      teardown();
      finishProgress(msg, "error");
      error(msg);
      appendLog(msg, "err");
      resetForNextUpload();
    });

    // Network/transport errors also fire `error` with no data; we treat
    // those distinctly so we don't overwrite a real server-side error.
    eventSource.addEventListener("open", function () {
      appendLog("Connected to progress stream.");
    });
  }

  function applySnapshot(d) {
    var total = d.total_chunks || 0;
    var done = d.completed || 0;
    var failed = d.failed || 0;
    setFill(done, total);
    setMeta(total > 0
      ? done + " / " + total + " chunks done" + (failed ? " · " + failed + " failed" : "")
      : "Chunking audio…");
    if (d.status === "running" && total > 0) {
      setLabel("Transcribing…");
    }
    if (d.in_flight && d.in_flight.length) {
      appendLog("Chunks " + d.in_flight.join(",") + " in flight");
    }
    // Surface any per-chunk errors that were recorded on the job.
    if (d.errors && d.errors.length) {
      // Only log errors we haven't already shown (keep a small set in DOM).
      var seen = {};
      Array.prototype.forEach.call(progressLog.children, function (li) {
        seen[li.textContent] = true;
      });
      d.errors.forEach(function (err) {
        if (!seen[err]) appendLog(err, "err");
      });
    }
    // ETA estimate: elapsed / completed * remaining.
    if (done > 0 && total > done) {
      var elapsed = Date.now() - startedAt;
      var perChunk = elapsed / done;
      var etaMs = perChunk * (total - done);
      setMeta((done + " / " + total + " chunks done") +
        " · ETA " + fmtElapsed(etaMs) +
        (failed ? " · " + failed + " failed" : ""));
    }
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (!input.files || !input.files[0]) { error("Choose a file first."); return; }
    if (!validate(input.files[0])) return;

    clearError();
    resetProgress();
    setBusy(true);
    uploadController = new AbortController();

    var body = new FormData(form);

    fetch(form.action, { method: "POST", body: body, signal: uploadController.signal })
      .then(function (res) {
        return res.json().then(function (j) {
          return { status: res.status, json: j };
        });
      })
      .then(function (r) {
        if (r.status === 202 && r.json && r.json.job_id) {
          appendLog("Job accepted: " + r.json.job_id.slice(0, 8) + "…");
          openProgressStream(r.json.job_id);
          return;
        }
        // Error path: show server message or a generic one.
        var msg = (r.json && (r.json.error || r.json.message)) ||
          ("Request rejected (HTTP " + r.status + ")");
        if (r.status === 409 && r.json && r.json.retry_after) {
          msg += " Retry in " + r.json.retry_after + "s.";
        }
        teardown();
        setBusy(false);
        error(msg);
      })
      .catch(function (err) {
        teardown();
        setBusy(false);
        if (err.name === "AbortError") return;
        error(err.message || "Network error during upload. Please try again.");
      });
  });
})();