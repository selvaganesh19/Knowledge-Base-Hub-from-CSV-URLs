// Upload page: drag-and-drop selection, then live progress for the three chained
// jobs (harvest -> index -> extract).
//
// Progress is read from the job rows via /api/jobs rather than inferred client-side,
// so what the bar shows is what the server is actually doing.
(function () {
  const form = document.getElementById("upload-form");
  if (!form) return;

  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file");
  const prompt = document.getElementById("dropzone-prompt");
  const fileLabel = document.getElementById("dropzone-file");
  const result = document.getElementById("result");
  const button = document.getElementById("submit-button");

  // --- file selection ------------------------------------------------------

  dropzone.addEventListener("click", () => fileInput.click());

  // Keyboard equivalent, since the dropzone is a div rather than a native button.
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      fileInput.click();
    }
  });

  ["dragenter", "dragover"].forEach((type) =>
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.add("hover");
    })
  );

  ["dragleave", "drop"].forEach((type) =>
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.remove("hover");
    })
  );

  dropzone.addEventListener("drop", (event) => {
    const files = event.dataTransfer?.files;
    if (files && files.length) {
      fileInput.files = files;
      showFile();
    }
  });

  fileInput.addEventListener("change", showFile);

  function showFile() {
    const file = fileInput.files[0];
    if (!file) {
      prompt.hidden = false;
      fileLabel.hidden = true;
      dropzone.classList.remove("has-file");
      return;
    }
    prompt.hidden = true;
    fileLabel.hidden = false;
    fileLabel.textContent = `${file.name} · ${formatBytes(file.size)}`;
    dropzone.classList.add("has-file");
  }

  // --- submit --------------------------------------------------------------

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    if (!fileInput.files.length) {
      result.innerHTML = banner("warn", "Choose a file first.");
      return;
    }

    button.disabled = true;
    result.innerHTML = `<div class="panel"><span class="spinner"></span> Uploading…</div>`;

    const body = new FormData();
    body.append("file", fileInput.files[0]);
    const name = document.getElementById("batch-name").value.trim();
    if (name) body.append("name", name);
    const column = document.getElementById("url-column").value.trim();
    if (column) body.append("url_column", column);

    try {
      const response = await fetch("/api/upload/", { method: "POST", body });
      const payload = await response.json();

      if (!response.ok) {
        const detail = payload.detail || payload;
        const headers = (detail.headers || [])
          .map((header) => `<span class="mono">${escapeHtml(header)}</span>`)
          .join(", ");
        result.innerHTML =
          banner(
            "warn",
            `<strong>Upload rejected.</strong> ${escapeHtml(detail.error || JSON.stringify(detail))}`
          ) + (headers ? `<p class="faint small">Columns found: ${headers}</p>` : "");
        button.disabled = false;
        return;
      }

      renderBatch(payload);
    } catch (error) {
      result.innerHTML = banner("error", `Upload failed: ${escapeHtml(String(error))}`);
      button.disabled = false;
    }
  });

  function renderBatch(payload) {
    // The validation breakdown is shown before crawling starts, because "18 of 20
    // queued, 2 refused" is the answer to a question the user has right now. The
    // per-URL outcomes arrive later as the crawl finds them.
    const refused = Object.entries(payload.rejections || {});
    result.innerHTML = `
      <div class="panel">
        <h3>Batch #${payload.batch_id} · ${payload.valid_urls} URL(s) queued</h3>
        <div class="grid cols-5 summary-row" style="margin:12px 0">
          <div class="stat"><div class="value">${payload.row_count}</div><div class="label">Rows read</div></div>
          <div class="stat ok"><div class="value">${payload.valid_urls}</div><div class="label">Valid</div></div>
          <div class="stat error"><div class="value">${payload.invalid_urls}</div><div class="label">Invalid</div></div>
          <div class="stat warn"><div class="value">${payload.duplicate_urls}</div><div class="label">Duplicates</div></div>
          <div class="stat"><div class="value">${payload.empty_rows}</div><div class="label">Empty</div></div>
        </div>
        <p class="faint small">
          URL column <span class="mono">${escapeHtml(payload.url_column)}</span>
        </p>
        ${
          refused.length
            ? `<details class="small">
                 <summary class="faint">Why ${payload.invalid_urls} row(s) were refused</summary>
                 <ul class="faint small" style="margin:8px 0 0 18px">
                   ${refused
                     .map(
                       ([reason, count]) =>
                         `<li>${count} × ${escapeHtml(reason)}</li>`
                     )
                     .join("")}
                 </ul>
               </details>`
            : ""
        }
        <div class="progress" style="margin:14px 0 10px"><div id="bar"></div></div>
        <div class="job-msg" id="message">Starting…</div>
      </div>`;

    trackBatch(payload.batch_id);
  }

  // Poll the batch's jobs until nothing is queued or running any more.
  async function trackBatch(batchId) {
    const bar = document.getElementById("bar");
    const message = document.getElementById("message");
    let sawActivity = false;

    const tick = async () => {
      let jobs;
      try {
        const response = await fetch(`/api/jobs/?batch=${batchId}&limit=10`);
        jobs = await response.json();
      } catch (error) {
        // A dropped poll is not fatal; try again on the next tick.
        setTimeout(tick, 1500);
        return;
      }

      // Jobs are returned newest first; show the oldest unfinished one so the
      // order reads as harvest -> index -> extract rather than backwards.
      const active = jobs.filter((job) => job.status === "running" || job.status === "queued");
      const current = active.length ? active[active.length - 1] : jobs[0];

      if (current) {
        if (bar) {
          bar.style.width = `${current.percent}%`;
          bar.parentElement.className = "progress" +
            (current.status === "failed" ? " failed" : current.status === "success" ? " done" : "");
        }
        if (message) {
          message.textContent = `${current.kind} · ${current.processed}/${current.total} · ${current.message}`;
        }
      }

      if (active.length) {
        sawActivity = true;
        setTimeout(tick, 1200);
        return;
      }

      if (sawActivity) {
        const failed = jobs.some((job) => job.status === "failed");
        button.disabled = false;
        if (message) {
          message.innerHTML = failed
            ? '<span class="error-text">Finished with errors — opening the URL list…</span>'
            : "Done. Opening the URL list…";
        }
        setTimeout(() => {
          window.location.href = `/urls?batch=${batchId}`;
        }, 700);
      }
    };

    tick();
  }

  function banner(kind, html) {
    return `<div class="banner ${kind}">${html}</div>`;
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
})();
