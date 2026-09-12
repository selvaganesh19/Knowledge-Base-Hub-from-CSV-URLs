// Search page behaviour.
//
// The answer comes back as light markdown with [n] citations. It is rendered by a
// small converter rather than a full markdown library: model output here is
// constrained to headings, bold, and lists, so a general parser would be more code
// and more surface area for injected markup. Everything is escaped before any
// formatting is applied.
(function () {
  const form = document.getElementById("search-form");
  if (!form) return;

  const input = document.getElementById("query");
  const button = document.getElementById("search-button");
  const output = document.getElementById("search-output");
  const status = document.getElementById("search-status");
  const clearButton = document.getElementById("clear-button");
  const suggestions = document.getElementById("suggestions");

  // The clear button only exists once there is something to clear, and the
  // suggestions are hidden after the first search so the answer is not pushed down
  // by a row of chips on every result page.
  function syncControls() {
    if (clearButton) clearButton.hidden = !input.value.trim();
    if (suggestions) suggestions.hidden = Boolean(output.innerHTML.trim());
  }

  if (clearButton) {
    clearButton.addEventListener("click", () => {
      input.value = "";
      output.innerHTML = "";
      status.innerHTML = "";
      const url = new URL(window.location.href);
      url.searchParams.delete("q");
      window.history.replaceState({}, "", url);
      syncControls();
      input.focus();
    });
  }

  input.addEventListener("input", syncControls);

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = input.value.trim();
    if (!query) {
      input.focus();
      return;
    }
    // Keep the URL shareable without reloading the page.
    const url = new URL(window.location.href);
    url.searchParams.set("q", query);
    window.history.replaceState({}, "", url);
    runSearch(query);
  });

  // A query in the URL means the page was opened from a link or a bookmark.
  if (input.value.trim()) runSearch(input.value.trim());
  syncControls();

  async function runSearch(query) {
    button.disabled = true;
    status.innerHTML = '<span class="spinner"></span> Searching…';
    output.innerHTML = skeleton();

    try {
      const response = await fetch("/api/search/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      const payload = await response.json();

      if (!response.ok) {
        status.textContent = "";
        output.innerHTML = banner(
          "error",
          `Search failed: ${escapeHtml(payload.detail || response.status)}`
        );
        return;
      }
      render(payload);
    } catch (error) {
      status.textContent = "";
      output.innerHTML = banner("error", `Search failed: ${escapeHtml(String(error))}`);
    } finally {
      button.disabled = false;
      syncControls();
    }
  }

  function render(payload) {
    const parts = [];

    status.innerHTML =
      `${payload.results.length} source${payload.results.length === 1 ? "" : "s"} · ` +
      `${payload.latency_ms} ms · LLM: <span class="mono">${escapeHtml(payload.provider)}</span>` +
      (payload.low_confidence
        ? ' · <span style="color:var(--warn)">low confidence — no strong match</span>'
        : "");

    if (payload.note) parts.push(banner("warn", escapeHtml(payload.note)));

    if (payload.answer) {
      parts.push(`<div class="answer">${renderMarkdown(payload.answer)}`);
      if (payload.sources.length) {
        parts.push(
          '<div class="sources">Sources: ' +
            payload.sources
              .map(
                (source) =>
                  `<a href="${escapeAttr(source.url)}" target="_blank" rel="noopener">` +
                  `[${source.index}] ${escapeHtml(shorten(source.title || source.url, 52))}</a>`
              )
              .join("") +
            "</div>"
        );
      }
      parts.push("</div>");
    }

    if (payload.people.length) {
      parts.push(
        `<h2>People${payload.answer ? " found" : ""}</h2><div class="grid cols-3">` +
          payload.people.map(personCard).join("") +
          "</div>"
      );
    }

    if (payload.results.length) {
      parts.push("<h2>Retrieved passages</h2>" + payload.results.map(resultCard).join(""));
    } else if (!payload.answer) {
      parts.push(
        `<div class="panel empty"><h2>No matches</h2>` +
          `<p>Nothing in the index is close to “${escapeHtml(payload.query)}”. ` +
          `Harvest and index some pages first, or try a broader question.</p>` +
          `<a class="button" href="/upload">Upload a file</a></div>`
      );
    }

    output.innerHTML = parts.join("");
  }

  function personCard(person) {
    const rows = [];
    if (person.email) rows.push(`<div>${escapeHtml(person.email)}</div>`);
    if (person.phone) rows.push(`<div>${escapeHtml(person.phone)}</div>`);
    if (person.location) rows.push(`<div>${escapeHtml(person.location)}</div>`);
    if (person.linkedin_url) {
      rows.push(
        `<div><a href="${escapeAttr(person.linkedin_url)}" target="_blank" rel="noopener">LinkedIn</a></div>`
      );
    }
    if (person.source_url) {
      rows.push(
        `<div><a href="${escapeAttr(person.source_url)}" target="_blank" rel="noopener">Source page</a></div>`
      );
    }
    if (person.confidence !== null && person.confidence !== undefined) {
      rows.push(`<div class="score">confidence ${person.confidence.toFixed(2)}</div>`);
    }

    return (
      `<div class="card">` +
      `<div class="name">${escapeHtml(person.name)}</div>` +
      (person.title ? `<div class="title">${escapeHtml(person.title)}</div>` : "") +
      (person.company ? `<div class="org">${escapeHtml(person.company)}</div>` : "") +
      (person.bio ? `<div class="bio">${escapeHtml(shorten(person.bio, 320))}</div>` : "") +
      (rows.length ? `<div class="meta">${rows.join("")}</div>` : "") +
      `</div>`
    );
  }

  function resultCard(result) {
    const extra = result.matched_chunks
      .map(
        (chunk) =>
          `<details><summary><span class="score">${chunk.score.toFixed(3)}</span>` +
          (chunk.heading ? ` · ${escapeHtml(chunk.heading)}` : "") +
          `</summary><div class="small" style="white-space:pre-wrap">` +
          `${escapeHtml(shorten(chunk.text, 1400))}</div></details>`
      )
      .join("");

    const title = result.title || result.url;
    return (
      `<div class="panel result">` +
      `<div class="result-head">` +
      `<div><a href="${escapeAttr(result.final_url || result.url)}" target="_blank" rel="noopener">` +
      `<strong>${escapeHtml(title)}</strong></a>` +
      `<div class="result-url">${escapeHtml(result.url)}</div></div>` +
      `<div class="score">best ${result.score.toFixed(3)}</div>` +
      `</div>` +
      `<div class="snippet">${escapeHtml(shorten(result.text, 700))}</div>` +
      extra +
      `</div>`
    );
  }

  function skeleton() {
    return (
      '<div class="panel"><div class="skeleton" style="width:70%"></div>' +
      '<div class="skeleton" style="width:92%"></div>' +
      '<div class="skeleton" style="width:84%"></div>' +
      '<div class="skeleton" style="width:48%;margin-bottom:0"></div></div>'
    );
  }

  function banner(kind, html) {
    return `<div class="banner ${kind}">${html}</div>`;
  }

  function renderMarkdown(text) {
    const lines = escapeHtml(text).split("\n");
    const out = [];
    let inList = false;

    for (const raw of lines) {
      const line = raw.trimEnd();

      if (/^[-*]\s+/.test(line)) {
        if (!inList) {
          out.push("<ul>");
          inList = true;
        }
        out.push(`<li>${inline(line.replace(/^[-*]\s+/, ""))}</li>`);
        continue;
      }
      if (inList) {
        out.push("</ul>");
        inList = false;
      }

      if (/^#{1,6}\s+/.test(line)) {
        out.push(`<h3>${inline(line.replace(/^#{1,6}\s+/, ""))}</h3>`);
      } else if (!line) {
        out.push("");
      } else {
        out.push(`<p>${inline(line)}</p>`);
      }
    }
    if (inList) out.push("</ul>");
    return out.join("\n");
  }

  function inline(text) {
    return (
      text
        // Citations arrive as [1], 【1】 or 【1†L9-L11】; render all of them as a
        // clean [1] link so the line-reference form does not leak into the answer.
        .replace(/[\[【](\d+)[^\]】]*[\]】]/g, '<a href="#citation-$1" class="citation">[$1]</a>')
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/`(.+?)`/g, '<span class="mono">$1</span>')
    );
  }

  function shorten(value, length) {
    const text = String(value ?? "");
    return text.length <= length ? text : text.slice(0, length - 1) + "…";
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function escapeAttr(value) {
    return escapeHtml(value);
  }
})();
