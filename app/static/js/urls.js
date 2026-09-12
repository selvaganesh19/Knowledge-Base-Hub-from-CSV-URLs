// Live progress for pages that show job state.
//
// Two modes, because the two pages need different things:
//   * the dashboard renders [data-job-id] containers, so their bars are updated
//     in place from the job row;
//   * the URL list has no job containers, but should still pick up newly
//     harvested rows once a running job finishes.
// Without the second mode the list page would sit on a stale snapshot until the
// user thought to reload it.
(function () {
  const containers = document.querySelectorAll("[data-job-id]");
  const POLL_MS = 2000;

  async function fetchJobs() {
    try {
      const response = await fetch("/api/jobs/?limit=10");
      if (!response.ok) return null;
      return await response.json();
    } catch (error) {
      // A transient failure should not stop the loop; try again next tick.
      return null;
    }
  }

  async function tick() {
    const jobs = await fetchJobs();
    if (!jobs) {
      setTimeout(tick, POLL_MS);
      return;
    }

    const active = jobs.filter(
      (job) => job.status === "running" || job.status === "queued"
    );

    for (const container of containers) {
      const job = jobs.find((candidate) => String(candidate.id) === container.dataset.jobId);
      if (!job) continue;
      const bar = container.querySelector("[data-job-bar]");
      const message = container.querySelector("[data-job-message]");
      if (bar) bar.style.width = `${job.percent}%`;
      if (message) message.textContent = `${job.processed}/${job.total} · ${job.message}`;
    }

    if (active.length) {
      if (!containers.length) {
        // No job panel on this page - show the one-line status instead.
        let status = document.getElementById("job-status");
        if (!status) {
          status = document.createElement("div");
          status.id = "job-status";
          status.className = "banner info";
          const main = document.querySelector("main");
          if (main) main.prepend(status);
        }
        status.textContent =
          `${active[0].kind} running · ${active[0].processed}/${active[0].total} · ${active[0].message}`;
      }
      setTimeout(tick, POLL_MS);
      return;
    }

    // Nothing is running any more. Reload once so the tables show the new rows,
    // but only if this page saw a job running - otherwise every page load would
    // reload itself in a loop.
    if (window.__kbhubSawActiveJob) {
      window.location.reload();
      return;
    }
  }

  async function start() {
    const jobs = await fetchJobs();
    if (!jobs) {
      if (containers.length) setTimeout(tick, POLL_MS);
      return;
    }
    window.__kbhubSawActiveJob = jobs.some(
      (job) => job.status === "running" || job.status === "queued"
    );
    if (window.__kbhubSawActiveJob) {
      tick();
    } else if (containers.length) {
      // Containers were rendered from a job that has since finished.
      tick();
    }
  }

  if (containers.length || document.querySelector(".panel table")) {
    start();
  }
})();
