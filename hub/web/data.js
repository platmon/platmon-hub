"use strict";
(function () {
  const view = document.getElementById("data-view");
  const status = document.getElementById("data-status");
  const button = document.getElementById("data-download");
  const select = document.getElementById("data-node");
  let controller = null;
  let ticket = 0;
  let busy = false;
  const end = new Date();
  const start = new Date(end.getTime() - 86400000);
  document.getElementById("data-from").value = start.toISOString().slice(0, 16);
  document.getElementById("data-to").value = end.toISOString().slice(0, 16);

  function suspend() {
    ticket++;
    if (controller) controller.abort();
    controller = null;
    busy = false;
    button.disabled = false;
  }
  async function refresh() {
    if (view.hidden || document.hidden || busy) return;
    suspend();
    const current = ticket;
    controller = new AbortController();
    const timeout = setTimeout(() => { if (current === ticket && controller) controller.abort(); }, 5000);
    try {
      const [nodes, history, archives] = await Promise.all(["/api/nodes", "/api/history/status", "/api/data/archive/status"].map(async path => {
        const response = await fetch(path, {cache:"no-store", credentials:"omit", signal:controller.signal});
        if (!response.ok) throw new Error("Could not load data options.");
        return response.json();
      }));
      if (current !== ticket) return;
      if (nodes.schema_version !== 1 || !Array.isArray(nodes.nodes)) throw new Error("Unsupported node response.");
      const archive = archives.archive;
      document.getElementById("data-archive-status").textContent = archive && archive.enabled ? "Automatic archives: " + archive.state + (archive.created ? " · " + archive.created : "") + " · failures: " + archive.failures : "Automatic archives are disabled. Enable --archive-dir to keep daily files.";
      const selected = select.value;
      select.replaceChildren(new Option("All stored devices", ""));
      for (const node of nodes.nodes) select.append(new Option(node.name, node.node_id));
      select.value = selected;
      status.textContent = history.history && history.history.enabled ? "History enabled. Select a UTC range to download stored data." : "History is disabled. Start Hub with --history-db, or use start-hub.cmd.";
      button.disabled = !(history.history && history.history.enabled);
    } catch (error) {
      if (current === ticket) status.textContent = error.name === "AbortError" ? "Loading timed out. Refresh to retry." : error.message;
    } finally { clearTimeout(timeout); if (current === ticket) controller = null; }
  }
  document.getElementById("data-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || view.hidden || document.hidden || button.disabled) return;
    const from = Date.parse(document.getElementById("data-from").value + "Z");
    const to = Date.parse(document.getElementById("data-to").value + "Z");
    const node = document.getElementById("data-uuid").value.trim() || select.value;
    if (!Number.isFinite(from) || !Number.isFinite(to) || from < 0 || to <= from || to - from > 7 * 86400000 ||
        (node && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(node))) {
      status.textContent = "Choose a valid UUID and UTC range of at most 7 days.";
      return;
    }
    suspend();
    const current = ticket;
    busy = true;
    button.disabled = true;
    controller = new AbortController();
    const timeout = setTimeout(() => { if (current === ticket && controller) controller.abort(); }, 20000);
    status.textContent = "Preparing CSV ZIP… Other monitoring continues.";
    try {
      const params = new URLSearchParams({from_ms:String(from), to_ms:String(to)});
      if (node) params.set("node_id", node);
      const response = await fetch("/api/data/export?" + params, {cache:"no-store", credentials:"omit", signal:controller.signal});
      if (!response.ok) throw new Error(response.status === 429 ? "Another export is running. Retry shortly." : "Export unavailable. Reduce the range, check History or use the CLI.");
      if (Number(response.headers.get("Content-Length")) > 34 * 1024 * 1024) throw new Error("Download too large; use the CLI.");
      const data = await response.blob();
      if (current !== ticket) return;
      const url = URL.createObjectURL(data);
      const link = document.createElement("a");
      link.href = url;
      link.download = "platmon-data.zip";
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      status.textContent = "Download ready. See manifest.json for row counts, units and missing-data semantics.";
    } catch (error) {
      if (current === ticket) status.textContent = error.name === "AbortError" ? "Download timed out. Try a smaller range." : error.message;
    } finally { clearTimeout(timeout); if (current === ticket) { busy = false; controller = null; button.disabled = false; } }
  });
  document.addEventListener("view-change", () => { if (view.hidden) suspend(); else refresh(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) suspend(); else refresh(); });
  document.getElementById("refresh").addEventListener("click", refresh);
  if (!view.hidden) refresh();
})();
