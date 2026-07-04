// xEdge Web UI — small, dependency-free live-update helper (ADR-007
// addendum: replaces an originally-planned htmx dependency with plain JS,
// since vendoring third-party JS from an agent-chosen source wasn't
// something the user had explicitly confirmed). Polls the same
// already-authenticated JSON API the rest of the UI talks to; the
// session cookie is sent automatically by the browser on same-origin
// fetch requests.

const xedgeUi = (() => {
  const POLL_INTERVAL_MS = 2000;

  function updateDriversTable(drivers) {
    for (const driver of drivers) {
      const row = document.querySelector(
        `#drivers-table tr[data-instance-id="${CSS.escape(driver.instance_id)}"]`
      );
      if (!row) continue;
      const stateCell = row.querySelector(".xedge-state");
      if (stateCell) stateCell.textContent = driver.state;
      const readsCell = row.querySelector(".xedge-tag-read-count");
      if (readsCell) readsCell.textContent = driver.metrics.tag_read_count;
      const errorsCell = row.querySelector(".xedge-error-count");
      if (errorsCell) errorsCell.textContent = driver.metrics.error_count;
      const lastErrorCell = row.querySelector(".xedge-last-error");
      if (lastErrorCell) lastErrorCell.textContent = driver.last_error || "";
    }
  }

  function updateNorthboundStatus(status) {
    const el = document.getElementById("northbound-status");
    if (!el) return;
    if (status.northbound_connected === null) {
      el.textContent = "disabled";
    } else if (status.northbound_connected) {
      el.textContent = "connected";
    } else {
      el.textContent = "disconnected";
    }
  }

  async function pollOnce() {
    try {
      const [driversResponse, statusResponse] = await Promise.all([
        fetch("/api/v1/drivers"),
        fetch("/api/v1/status"),
      ]);
      if (driversResponse.status === 401 || statusResponse.status === 401) {
        window.location.href = "/ui/login";
        return;
      }
      updateDriversTable(await driversResponse.json());
      updateNorthboundStatus(await statusResponse.json());
    } catch (err) {
      // Network hiccups shouldn't spam the console on every poll tick.
    }
  }

  function pollDashboard() {
    pollOnce();
    setInterval(pollOnce, POLL_INTERVAL_MS);
  }

  return { pollDashboard };
})();
