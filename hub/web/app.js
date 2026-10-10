"use strict";
(function () {
  const list = document.getElementById("node-list");
  const summary = document.getElementById("fleet-summary");
  const details = document.getElementById("detail-body");
  const fleetNote = document.getElementById("fleet-note");
  const detailNote = document.getElementById("detail-status");
  const updated = document.getElementById("last-updated");
  const banner = document.getElementById("connection-error");
  const refreshButton = document.getElementById("refresh");
  let nodes = [];
  let selected = null;
  let fleetBusy = false;
  let detailTicket = 0;
  let lastSuccess = null;

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    return node;
  }
  function numeric(value) { return typeof value === "number" && Number.isFinite(value); }
  function percent(value) { return numeric(value) && value >= 0 && value <= 100 ? value.toFixed(1) + "%" : "N/A"; }
  function degree(value) { return numeric(value) ? value.toFixed(1) + "°C" : "N/A"; }
  function age(value) {
    if (!numeric(value) || value < 0) return "Unknown age";
    const seconds = value / 1000;
    return seconds < 60 ? seconds.toFixed(1) + "s ago" : (seconds / 60).toFixed(1) + "m ago";
  }
  function rate(value) {
    if (!numeric(value) || value < 0) return "N/A";
    if (value >= 1048576) return (value / 1048576).toFixed(1) + " MiB/s";
    if (value >= 1024) return (value / 1024).toFixed(1) + " KiB/s";
    return value.toFixed(0) + " B/s";
  }
  function statusPill(value) {
    const state = typeof value === "string" ? value : "unknown";
    const pill = element("span", "status " + (/^(live|partial|starting|stale|offline|error|incompatible)$/.test(state) ? state : ""), state);
    return pill;
  }
  function metric(label, value) {
    const cell = element("div", "metric");
    cell.append(element("span", "metric-label", label), element("span", "metric-value", value));
    return cell;
  }
  function viewMetrics(container, overview, className) {
    const data = overview && typeof overview === "object" ? overview : {};
    const grid = element("div", className);
    grid.append(
      metric("CPU", percent(data.cpu_percent)),
      metric("GPU", percent(data.gpu_percent)),
      metric("Memory", percent(data.memory_percent)),
      metric("Root disk", percent(data.disk_percent)),
      metric("Hottest", degree(data.hottest_c))
    );
    container.append(grid);
  }
  async function getJSON(path) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch(path, {cache:"no-store",credentials:"same-origin",signal:controller.signal});
      if (!response.ok) throw new Error("Hub returned HTTP " + response.status);
      const doc = await response.json();
      if (!doc || doc.schema_version !== 1) throw new Error("Unsupported Hub API response");
      return doc;
    } finally {
      clearTimeout(timer);
    }
  }
  function summaryCard(label, value) {
    const item = element("div", "summary-item");
    item.append(element("div", "label", label), element("div", "value", value));
    return item;
  }
  function renderSummary() {
    const attention = new Set(["partial", "starting", "stale", "unknown", "error", "incompatible"]);
    summary.replaceChildren(
      summaryCard("Registered", nodes.length),
      summaryCard("Live", nodes.filter(n => n.headline === "live").length),
      summaryCard("Needs attention", nodes.filter(n => attention.has(n.headline)).length),
      summaryCard("Offline", nodes.filter(n => n.headline === "offline").length)
    );
  }
  function renderCards() {
    list.replaceChildren();
    fleetNote.textContent = nodes.length + (nodes.length === 1 ? " device" : " devices");
    if (!nodes.length) {
      list.append(element("p", "empty", "No nodes are registered. Add approved endpoints using the local registry CLI."));
      return;
    }
    for (const n of nodes) {
      const card = element("button", "node-card");
      card.type = "button";
      card.setAttribute("aria-pressed", String(n.node_id === selected));
      const title = element("div", "card-header");
      const names = element("div");
      names.append(element("div", "node-name", n.name || n.node_id), element("div", "secondary", n.node_id));
      title.append(names, statusPill(n.headline));
      card.append(title);
      viewMetrics(card, n.overview, "metrics");
      const hint = n.data_current ? "Sample " + (n.sample_sequence ?? "—") + " · " + age(n.data_age_ms)
        : "Last known / not current · " + age(n.data_age_ms);
      card.append(element("div", n.data_current ? "secondary" : "last-known", hint));
      card.addEventListener("click", () => {
        selected = n.node_id;
        renderCards();
        loadDetail();
        loadHistory(true);
      });
      list.append(card);
    }
  }
  function section(root, heading) {
    const wrap = element("section", "detail-section");
    wrap.append(element("h3", "", heading));
    root.append(wrap);
    return wrap;
  }
  function table(root, headers, rows, limit) {
    if (!rows || !rows.length) {
      root.append(element("p", "empty", "No available measurements"));
      return;
    }
    const tab = element("table");
    const thead = element("thead");
    const tr = element("tr");
    for (const h of headers) tr.append(element("th", "", h));
    thead.append(tr);
    const tbody = element("tbody");
    const maxRows = limit || 12;
    for (const cells of rows.slice(0, maxRows)) {
      const row = element("tr");
      for (const cell of cells) row.append(element("td", "", cell == null ? "N/A" : cell));
      tbody.append(row);
    }
    tab.append(thead, tbody);
    root.append(tab);
    if (rows.length > maxRows) root.append(element("p", "empty", "+" + (rows.length - maxRows) + " more rows in Agent API"));
  }
  function rowsOf(obj) { return obj && typeof obj === "object" && !Array.isArray(obj) ? Object.entries(obj) : []; }
  function renderDetail(doc) {
    const n = doc.node;
    if (!n || n.node_id !== selected) throw new Error("Mismatched node detail");
    const root = document.createDocumentFragment();
    const head = element("div", "detail-header");
    const nameBlock = element("div");
    nameBlock.append(element("h3", "", n.name || n.node_id), element("div", "secondary", n.node_id));
    head.append(nameBlock, statusPill(n.headline));
    root.append(head);
    root.append(element("p", n.data_current ? "secondary" : "last-known",
      (n.data_current ? "Current" : "Last-known / not current") + " · " + age(n.data_age_ms)));
    if (n.core_error) root.append(element("p", "last-known", "Core: " + n.core_error));
    if (n.optional_error) root.append(element("p", "last-known", "Observations: " + n.optional_error));
    const stats = n.stats;
    const resources = section(root, "Resources");
    viewMetrics(resources, n.overview, "detail-metrics");
    if (stats && typeof stats === "object") {
      const identity = section(root, "System");
      const sys = stats.system && typeof stats.system === "object" ? stats.system : {};
      table(identity, ["Field", "Value"], [
        ["Model", stats.model], ["Hostname", sys.hostname], ["OS", sys.os],
        ["Kernel", sys.kernel], ["Architecture", sys.arch], ["L4T", sys.l4t]
      ], 8);
      const cpu = section(root, "CPU cores");
      const cores = Array.isArray(stats.cpu) ? stats.cpu : [];
      table(cpu, ["Core", "Usage", "Frequency"], cores.map(c => [
        c && c.id != null ? String(c.id) : "N/A",
        c ? percent(c.usage) : "N/A",
        c && numeric(c.freq) ? (c.freq / 1e6).toFixed(0) + " MHz" : "N/A"
      ]), 16);
      const temps = section(root, "Temperature");
      table(temps, ["Sensor", "Reading"], rowsOf(stats.temperature).map(([k, v]) => [k, degree(v)]), 16);
      const network = stats.network && Array.isArray(stats.network.interfaces) ? stats.network.interfaces : [];
      const net = section(root, "Network (per interface, not summed)");
      table(net, ["Interface", "RX", "TX"], network.filter(x => x && typeof x === "object").map(x => [
        x.name, rate(x.rates && x.rates.rx_bytes_per_s), rate(x.rates && x.rates.tx_bytes_per_s)
      ]), 12);
      const disks = stats.disk_io && Array.isArray(stats.disk_io.disks) ? stats.disk_io.disks : [];
      const io = section(root, "Disk I/O (per device)");
      table(io, ["Disk", "Read", "Write"], disks.filter(x => x && typeof x === "object").map(x => [
        x.name, rate(x.rates && x.rates.read_bytes_per_s), rate(x.rates && x.rates.write_bytes_per_s)
      ]), 12);
    } else {
      root.append(element("p", "empty", "No core snapshot is available yet."));
    }
    const optional = section(root, "Independent observations");
    const groups = n.observations && typeof n.observations === "object" ? n.observations : {};
    const groupRows = rowsOf(groups).map(([key, group]) => [
      key,
      group && group.state ? group.state : "Unknown",
      group && group.observation ? age(group.observation.data_age_ms) : "No observation"
    ]);
    table(optional, ["Group", "State", "Age"], groupRows, 16);
    detailNote.textContent = "Updated from Hub cache";
    details.replaceChildren(root);
  }
  async function loadDetail() {
    const ticket = ++detailTicket;
    if (!selected) {
      details.replaceChildren(element("p", "empty", "Select a device to view its latest snapshot."));
      return;
    }
    detailNote.textContent = "Loading…";
    try {
      const data = await getJSON("/api/nodes/" + encodeURIComponent(selected));
      if (ticket !== detailTicket) return;
      renderDetail(data);
    } catch (_err) {
      if (ticket === detailTicket) {
        detailNote.textContent = "Detail unavailable";
        details.replaceChildren(element("p", "empty", "Could not load node detail. The fleet view remains available."));
      }
    }
  }
  async function refreshFleet() {
    if (document.hidden || fleetBusy) return;
    fleetBusy = true;
    refreshButton.disabled = true;
    try {
      const data = await getJSON("/api/nodes");
      if (!Array.isArray(data.nodes)) throw new Error("Malformed fleet response");
      nodes = data.nodes.filter(n => n && typeof n.node_id === "string");
      banner.hidden = true;
      lastSuccess = new Date();
      if (!nodes.some(n => n.node_id === selected)) selected = nodes[0] ? nodes[0].node_id : null;
      renderSummary();
      renderCards();
      updated.textContent = "Hub updated " + lastSuccess.toLocaleTimeString();
      await loadDetail();
      loadHistory(false);
    } catch (_err) {
      // Hub reachability is independent of agent health. Never keep cached
      // browser readings marked current after losing the Hub connection.
      nodes = nodes.map(function (n) {
        return Object.assign({}, n, {
          data_current: false,
          headline: n.headline === "live" || n.headline === "partial" ? "stale" : n.headline
        });
      });
      renderSummary();
      renderCards();
      detailTicket++;
      detailNote.textContent = "Hub unavailable";
      details.replaceChildren(element("p", "empty", "Hub connection lost. Device details cannot be verified; retry to refresh."));
      banner.hidden = false;
      banner.textContent = "Hub connection lost. Node readings are last known, not current.";
      updated.textContent = lastSuccess ? "Last Hub contact " + lastSuccess.toLocaleTimeString() : "Hub unavailable";
      fleetNote.textContent = "Refresh failed";
    } finally {
      fleetBusy = false;
      refreshButton.disabled = false;
    }
  }
  const historyMetric = document.getElementById("history-metric");
  const historyRange = document.getElementById("history-range");
  const historyStatus = document.getElementById("history-status");
  const historyValue = document.getElementById("history-value");
  const historyGaps = document.getElementById("history-gaps");
  const canvas = document.getElementById("history-chart");
  let historyController = null;
  let historyTicket = 0;
  let historyData = null;
  let historyKey = "";
  let historyLoaded = 0;
  let hoverIndex = 0;

  function cancelHistory() {
    historyTicket++;
    if (historyController) historyController.abort();
    historyController = null;
  }
  function drawHistory() {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(160, canvas.getBoundingClientRect().width);
    const height = 240;
    canvas.width = Math.round(width * ratio);
    canvas.height = height * ratio;
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    const colors = getComputedStyle(document.documentElement);
    const color = name => colors.getPropertyValue(name).trim();
    ctx.font = "11px system-ui";
    ctx.fillStyle = color("--muted");
    if (!historyData || !historyData.points.length) {
      ctx.fillText("No observed history in this range", 12, 32);
      return;
    }
    const data = historyData;
    const values = data.points.flatMap(p => [p.minimum, p.maximum]).filter(numeric);
    const low = historyMetric.value === "hottest_c" ? Math.min(0, ...values) : 0;
    const high = historyMetric.value === "hottest_c" ? Math.max(1, ...values) : 100;
    const left = 38, right = width - 10, top = 12, bottom = height - 30;
    const x = time => left + (right-left) * (time-data.from_ms)/(data.to_ms-data.from_ms);
    const y = value => bottom - (bottom-top) * (value-low)/(high-low);
    ctx.strokeStyle = color("--line");
    for (let i = 0; i <= 4; i++) {
      const value = low + (high-low)*i/4;
      ctx.beginPath(); ctx.moveTo(left,y(value)); ctx.lineTo(right,y(value)); ctx.stroke();
      ctx.fillText(value.toFixed(0), 2, y(value)+4);
    }
    for (const gap of data.gaps) {
      const a = x(Math.max(data.from_ms, gap.start_ms));
      const b = x(Math.min(data.to_ms, gap.end_ms ?? data.to_ms));
      ctx.fillStyle = color(gap.reason === "offline" ? "--error-bg" : "--muted-bg");
      ctx.fillRect(a, top, Math.max(1,b-a), bottom-top);
      if (gap.reason === "agent_restart") {
        ctx.strokeStyle = color("--warn"); ctx.setLineDash([4,3]);
        ctx.beginPath(); ctx.moveTo(b,top); ctx.lineTo(b,bottom); ctx.stroke(); ctx.setLineDash([]);
      }
    }
    ctx.strokeStyle = color("--brand");
    for (const p of data.points) {
      if (!numeric(p.average)) continue;
      ctx.globalAlpha = 0.35;
      ctx.beginPath(); ctx.moveTo(x(p.timestamp_ms),y(p.minimum)); ctx.lineTo(x(p.timestamp_ms),y(p.maximum)); ctx.stroke();
    }
    ctx.globalAlpha = 1;
    let previous = null;
    ctx.beginPath();
    for (const p of data.points) {
      if (!numeric(p.average)) { previous = null; continue; }
      if (!previous || previous.segment !== p.segment || previous.instance_id !== p.instance_id) ctx.moveTo(x(p.timestamp_ms),y(p.average));
      else ctx.lineTo(x(p.timestamp_ms),y(p.average));
      previous = p;
    }
    ctx.stroke();
    ctx.fillStyle = color("--brand");
    for (const p of data.points) {
      if (numeric(p.average)) { ctx.beginPath(); ctx.arc(x(p.timestamp_ms),y(p.average),1.5,0,Math.PI*2); ctx.fill(); }
    }
    ctx.fillStyle = color("--muted");
    ctx.fillText(new Date(data.from_ms).toISOString().slice(5,16).replace("T"," "), left, height-8);
    ctx.textAlign = "right";
    ctx.fillText(new Date(data.to_ms).toISOString().slice(5,16).replace("T"," ") + " UTC", right, height-8);
  }
  function inspectHistory(index) {
    if (!historyData || !historyData.points.length) return;
    hoverIndex = Math.max(0, Math.min(index, historyData.points.length-1));
    const p = historyData.points[hoverIndex];
    const format = historyMetric.value === "hottest_c" ? degree : percent;
    historyValue.textContent = new Date(p.timestamp_ms).toISOString() + " · Average " + format(p.average) +
      " · Min " + format(p.minimum) + " · Max " + format(p.maximum) + " · " + p.count + " samples · Instance " + p.instance_id;
  }
  async function loadHistory(force) {
    if (document.hidden) return;
    const key = [selected, historyMetric.value, historyRange.value].join(":");
    if (!force && key === historyKey && (historyController || Date.now()-historyLoaded < 30000)) return;
    cancelHistory();
    const ticket = historyTicket;
    historyKey = key;
    historyData = null;
    historyValue.textContent = "Hover over the chart or use arrow keys to inspect values.";
    drawHistory();
    if (!selected) { historyStatus.textContent = "Select a device."; return; }
    historyStatus.textContent = "Loading history…";
    const controller = new AbortController();
    historyController = controller;
    const timer = setTimeout(() => controller.abort(), 5000);
    const end = Date.now();
    const query = new URLSearchParams({metric:historyMetric.value, from_ms:String(end-Number(historyRange.value)*3600000), to_ms:String(end), max_points:"1200"});
    try {
      const response = await fetch("/api/nodes/" + encodeURIComponent(selected) + "/history?" + query,
        {cache:"no-store", credentials:"same-origin", signal:controller.signal});
      const doc = await response.json();
      if (!response.ok) throw new Error(doc.error || "History unavailable");
      if (doc.schema_version !== 1 || !Array.isArray(doc.points) || doc.points.length > 1200 || !Array.isArray(doc.gaps)) throw new Error("Invalid history response");
      if (ticket !== historyTicket || document.hidden) return;
      historyData = doc;
      historyLoaded = Date.now();
      historyStatus.textContent = doc.points.length + " history points · Updated " + new Date(historyLoaded).toLocaleTimeString();
      const reasons = Array.from(new Set(doc.gaps.map(g => g.reason))).join(", ");
      historyGaps.textContent = "Average line · Min/max bars · Shaded gaps · Dashed restart boundaries" + (reasons ? " · " + reasons : "");
      drawHistory();
    } catch (err) {
      if (ticket === historyTicket && !document.hidden) {
        historyStatus.textContent = err.message === "history_disabled" ? "History is not enabled on this Hub." :
          err.message === "history_range_too_dense" ? "Too many discontinuities. Select a shorter time range." : "History unavailable. Live monitoring continues.";
      }
    } finally {
      clearTimeout(timer);
      if (ticket === historyTicket) historyController = null;
    }
  }
  canvas.addEventListener("pointermove", event => {
    if (!historyData || !historyData.points.length) return;
    const box = canvas.getBoundingClientRect();
    const fraction = Math.max(0, Math.min(1, (event.clientX-box.left-38)/(box.width-48)));
    const timestamp = historyData.from_ms + fraction*(historyData.to_ms-historyData.from_ms);
    let nearest = 0;
    for (let i = 1; i < historyData.points.length; i++) {
      if (Math.abs(historyData.points[i].timestamp_ms-timestamp) < Math.abs(historyData.points[nearest].timestamp_ms-timestamp)) nearest = i;
    }
    inspectHistory(nearest);
  });
  canvas.addEventListener("keydown", event => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault(); inspectHistory(hoverIndex + (event.key === "ArrowRight" ? 1 : -1));
    }
  });
  historyMetric.addEventListener("change", () => loadHistory(true));
  historyRange.addEventListener("change", () => loadHistory(true));
  document.getElementById("history-refresh").addEventListener("click", () => loadHistory(true));
  new ResizeObserver(drawHistory).observe(canvas);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", drawHistory);

  refreshButton.addEventListener("click", refreshFleet);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) { refreshFleet(); loadHistory(true); }
    else { cancelHistory(); historyStatus.textContent = "History paused while tab is hidden."; }
  });
  setInterval(refreshFleet, 5000);
  renderSummary();
  refreshFleet();
})();
