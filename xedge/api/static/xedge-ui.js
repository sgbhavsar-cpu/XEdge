// xEdge Web UI — small, dependency-free live-update helper (ADR-007
// addendum: replaces an originally-planned htmx dependency with plain JS,
// since vendoring third-party JS from an agent-chosen source wasn't
// something the user had explicitly confirmed). Polls the same
// already-authenticated JSON API the rest of the UI talks to; the
// session cookie is sent automatically by the browser on same-origin
// fetch requests.

const xedgeUi = (() => {
  const POLL_INTERVAL_MS = 2000;
  const LOG_POLL_INTERVAL_MS = 1000;
  // Mirrors xedge.core.system_tags.SYSTEM_TAG_NAMES — kept in sync by hand,
  // same as config_ui.py's KNOWN_DRIVER_TYPES list.
  const SYSTEM_TAG_NAMES = [
    "status",
    "status_time",
    "tag_count",
    "reads_per_second",
    "reads_per_minute",
    "reads_per_hour",
    "error_count",
    "consecutive_failures",
    "uptime_seconds",
    "connectivity_state",
  ];
  const SYSTEM_RATE_FIELDS = new Set(["reads_per_second", "reads_per_minute", "reads_per_hour"]);

  function updateDriversTable(drivers) {
    for (const driver of drivers) {
      const row = document.querySelector(
        `#drivers-table tr[data-instance-id="${CSS.escape(driver.instance_id)}"]`
      );
      if (!row) continue;
      const stateCell = row.querySelector(".xedge-state");
      if (stateCell) stateCell.textContent = driver.state;
      const connectivityCell = row.querySelector(".xedge-connectivity-state");
      if (connectivityCell) connectivityCell.textContent = driver.connectivity_state;
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

  function updateTagsTable(tags) {
    const tbody = document.querySelector("#driver-tags-table tbody");
    if (!tbody) return;
    tbody.textContent = "";
    if (tags.length === 0) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.textContent = "No tags collected yet.";
      row.appendChild(cell);
      tbody.appendChild(row);
      return;
    }
    for (const tag of tags) {
      const row = document.createElement("tr");
      const fields = [tag.tag_id, tag.value, tag.quality, tag.timestamp, tag.source_address, tag.detail];
      for (const field of fields) {
        const cell = document.createElement("td");
        cell.textContent = field === null || field === undefined ? "" : String(field);
        row.appendChild(cell);
      }
      tbody.appendChild(row);
    }
  }

  function updateSystemTags(system) {
    for (const name of SYSTEM_TAG_NAMES) {
      const cell = document.getElementById(`system-${name}`);
      if (!cell) continue;
      const value = system ? system[name] : undefined;
      if (value === null || value === undefined) {
        cell.textContent = "…";
      } else if (typeof value === "number" && SYSTEM_RATE_FIELDS.has(name)) {
        cell.textContent = value.toFixed(2);
      } else if (name === "uptime_seconds" && typeof value === "number") {
        cell.textContent = value.toFixed(0);
      } else {
        cell.textContent = String(value);
      }
    }
  }

  async function pollDriverTagsOnce(instanceId) {
    try {
      const response = await fetch(`/api/v1/drivers/${encodeURIComponent(instanceId)}/tags`);
      if (response.status === 401) {
        window.location.href = "/ui/login";
        return;
      }
      const data = await response.json();
      updateTagsTable(data.tags);
      updateSystemTags(data.system);
    } catch (err) {
      // Network hiccups shouldn't spam the console on every poll tick.
    }
  }

  function pollDriverTags(instanceId) {
    pollDriverTagsOnce(instanceId);
    setInterval(() => pollDriverTagsOnce(instanceId), POLL_INTERVAL_MS);
  }

  // Tails /api/v1/logs into the <pre id="paneId"> element, re-evaluating
  // `getFilters()` (-> {instanceId, source}) on every tick so a page can
  // change its filter (e.g. a <select>) without a page reload; a filter
  // change clears the pane and restarts from seq 0 so old/new-filter lines
  // never mix in the same view.
  function startLogTail(paneId, getFilters) {
    const pane = document.getElementById(paneId);
    if (!pane) return;
    let sinceSeq = 0;
    let lastFilterKey = null;

    async function pollOnce() {
      const filters = getFilters();
      const filterKey = JSON.stringify(filters);
      if (filterKey !== lastFilterKey) {
        lastFilterKey = filterKey;
        sinceSeq = 0;
        pane.textContent = "";
      }
      const query = new URLSearchParams({ since_seq: String(sinceSeq), limit: "200" });
      if (filters.instanceId) query.set("instance_id", filters.instanceId);
      if (filters.source) query.set("source", filters.source);
      try {
        const response = await fetch(`/api/v1/logs?${query.toString()}`);
        if (response.status === 401) {
          window.location.href = "/ui/login";
          return;
        }
        const entries = await response.json();
        for (const entry of entries) {
          sinceSeq = Math.max(sinceSeq, entry.seq);
          const line = document.createElement("div");
          line.textContent = `${entry.ts || ""} [${entry.level || ""}] ${entry.event || ""}`;
          pane.appendChild(line);
        }
        if (entries.length > 0) {
          pane.scrollTop = pane.scrollHeight;
        }
      } catch (err) {
        // Network hiccups shouldn't spam the console on every poll tick.
      }
    }

    pollOnce();
    setInterval(pollOnce, LOG_POLL_INTERVAL_MS);
  }

  function formatFleetTimestamp(iso) {
    return iso ? new Date(iso).toLocaleString() : "never";
  }

  async function pollFleetStatusOnce() {
    try {
      const response = await fetch("/api/v1/fleet/status");
      if (response.status === 401) {
        window.location.href = "/ui/login";
        return;
      }
      const data = await response.json();
      const registeredCell = document.getElementById("fleet-registered");
      if (registeredCell) {
        registeredCell.textContent = data.registered
          ? `yes (${data.device_id})`
          : data.last_error
            ? `no — ${data.last_error}`
            : "not yet";
      }
      const managerCell = document.getElementById("fleet-manager-url");
      if (managerCell) managerCell.textContent = data.manager_url || "";
      const heartbeatCell = document.getElementById("fleet-last-heartbeat");
      if (heartbeatCell) {
        const ok = data.last_heartbeat_ok === null || data.last_heartbeat_ok === undefined
          ? ""
          : data.last_heartbeat_ok ? " (ok)" : " (failed)";
        heartbeatCell.textContent = formatFleetTimestamp(data.last_heartbeat_at) + ok;
      }
      const applyCell = document.getElementById("fleet-last-config-apply");
      if (applyCell) {
        const apply = data.last_config_apply;
        applyCell.textContent = apply
          ? `v${apply.version}: ${apply.success ? "applied" : `rejected — ${apply.error}`}`
          : "none";
      }
    } catch (err) {
      // Network hiccups shouldn't spam the console on every poll tick.
    }
  }

  function pollFleetStatus() {
    pollFleetStatusOnce();
    setInterval(pollFleetStatusOnce, POLL_INTERVAL_MS);
  }

  return { pollDashboard, pollDriverTags, startLogTail, pollFleetStatus };
})();
