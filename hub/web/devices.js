"use strict";
(function () {
  const monitoring = document.getElementById("monitoring-view");
  const management = document.getElementById("devices-view");
  const tabs = [document.getElementById("monitoring-tab"), document.getElementById("devices-tab")];
  const addButton = document.getElementById("add-device");
  const status = document.getElementById("devices-status");
  const list = document.getElementById("devices-list");
  const dialog = document.getElementById("device-dialog");
  const form = document.getElementById("device-form");
  const fields = document.getElementById("device-fields");
  const formStatus = document.getElementById("device-form-status");
  const saveButton = document.getElementById("device-save");
  const cancelButton = document.getElementById("device-cancel");
  const removePreview = document.getElementById("device-remove-preview");
  const controllers = new Set();
  let available = false;
  let ticket = 0;
  let busy = false;
  let entries = [];
  let editing = null;
  let operation = "add";

  function element(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }
  async function request(path, body) {
    const controller = new AbortController();
    controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const headers = {"X-Platmon-Admin":"1"};
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const response = await fetch(path, {method:body === undefined ? "GET" : "POST",
        headers, body:body === undefined ? undefined : JSON.stringify(body),
        cache:"no-store", credentials:"omit", signal:controller.signal});
      const data = await response.json();
      if (!response.ok) {
        const error = new Error(typeof data.message === "string" ? data.message : "Device management unavailable.");
        error.status = response.status;
        throw error;
      }
      if (data.schema_version !== 1) throw new Error("Unsupported device management response.");
      return data;
    } finally { clearTimeout(timeout); controllers.delete(controller); }
  }
  function suspend() {
    const pending = busy;
    ticket++;
    available = false;
    for (const controller of controllers) controller.abort();
    busy = false;
    entries = [];
    editing = null;
    if (dialog.open) dialog.close();
    list.replaceChildren();
    addButton.disabled = true;
    if (pending) status.textContent = "A change may already be saved. Refresh Devices before retrying.";
  }
  function showView(devices) {
    monitoring.hidden = devices;
    management.hidden = !devices;
    tabs.forEach((tab, index) => {
      const selected = index === (devices ? 1 : 0);
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    document.getElementById("refresh").setAttribute("aria-label", devices ? "Refresh device registry" : "Refresh fleet data");
    history.replaceState(null, "", devices ? "#devices" : "#monitoring");
    suspend();
    if (devices) refresh();
    document.dispatchEvent(new Event("view-change"));
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => showView(index === 1));
    tab.addEventListener("keydown", event => {
      let target;
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") target = 1 - index;
      else if (event.key === "Home") target = 0;
      else if (event.key === "End") target = 1;
      else return;
      event.preventDefault();
      showView(target === 1);
      tabs[target].focus();
    });
  });
  window.addEventListener("hashchange", () => showView(location.hash === "#devices"));

  function render() {
    list.replaceChildren();
    if (!available) return;
    if (!entries.length) { list.append(element("p", "empty", "No devices registered. Use Add device to register an Agent.")); return; }
    for (const node of entries) {
      const card = element("article", "management-card");
      const description = element("div", "management-description");
      description.append(element("h2", "", node.name), element("p", "secondary", node.endpoint),
        element("p", "secondary", node.node_id), element("span", node.enabled ? "scope-chip" : "status disabled", node.enabled ? "Enabled" : "Disabled"));
      const actions = element("div", "device-actions");
      for (const [label, action] of [["Edit", "update"], [node.enabled ? "Disable" : "Enable", "toggle"], ["Remove", "remove"]]) {
        const button = element("button", "refresh-button" + (action === "remove" ? " danger-button" : ""), label);
        button.type = "button";
        button.disabled = busy;
        button.addEventListener("click", () => openDialog(action, node));
        actions.append(button);
      }
      card.append(description, actions);
      list.append(card);
    }
  }
  function validateEntries(data) {
    if (!Array.isArray(data.nodes) || data.nodes.length > 256 || data.nodes.some(node =>
      !node || typeof node.node_id !== "string" || typeof node.name !== "string" || typeof node.endpoint !== "string" || typeof node.enabled !== "boolean")) {
      throw new Error("Invalid registry response. Refresh Devices.");
    }
    return data.nodes;
  }
  async function refresh() {
    if (management.hidden || document.hidden || busy) return;
    const current = ticket;
    busy = true;
    addButton.disabled = true;
    render();
    try {
      const capability = await request("/api/admin/status");
      if (current !== ticket) return;
      const local = location.protocol === "http:" && ["127.0.0.1", "localhost"].includes(location.hostname) && Number(location.port || 80) === capability.local_port;
      if (!capability.enabled || !local) {
        available = false;
        entries = [];
        status.textContent = !capability.enabled ? "Device management is disabled. Start Hub with --web-admin, or use the local registry CLI." : "Device management is available only on the local Hub page.";
        return;
      }
      const data = await request("/api/admin/nodes");
      if (current !== ticket) return;
      entries = validateEntries(data);
      available = true;
      status.textContent = entries.length + " registered devices · Registry settings · Fleet applies changes automatically.";
    } catch (error) {
      if (current !== ticket) return;
      status.textContent = "Could not refresh Devices. Displayed settings may be outdated. " + error.message;
    } finally {
      if (current === ticket) { busy = false; addButton.disabled = !available; render(); }
    }
  }
  addButton.addEventListener("click", () => openDialog("add", null));
  document.getElementById("refresh").addEventListener("click", refresh);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) suspend();
    else if (!management.hidden) refresh();
  });

  function openDialog(action, node) {
    if (busy || !available || dialog.open) return;
    form.reset();
    editing = node;
    operation = action === "toggle" ? "update" : action;
    fields.hidden = operation === "remove";
    fields.disabled = operation === "remove";
    removePreview.hidden = operation !== "remove";
    cancelButton.textContent = "Cancel";
    cancelButton.disabled = false;
    saveButton.disabled = false;
    formStatus.textContent = "";
    document.getElementById("device-dialog-title").textContent = operation === "add" ? "Add device" : operation === "remove" ? "Remove device" : "Edit device";
    document.getElementById("device-dialog-help").textContent = operation === "remove" ?
      "Remove this registration and stop future polling. Existing History and events are preserved. Cancel to keep the device." : "Changes apply automatically and preserve the device identity and History.";
    saveButton.textContent = operation === "remove" ? "Remove device" : "Save device";
    saveButton.classList.toggle("danger-button", operation === "remove");
    if (node) {
      const endpoint = new URL(node.endpoint);
      document.getElementById("device-name").value = node.name;
      document.getElementById("device-host").value = endpoint.hostname;
      document.getElementById("device-port").value = endpoint.port || "80";
      document.getElementById("device-enabled").checked = action === "toggle" ? !node.enabled : node.enabled;
      removePreview.textContent = node.name + " · " + node.endpoint + " · " + node.node_id;
    }
    dialog.showModal();
    if (operation === "remove") cancelButton.focus();
    else document.getElementById("device-name").focus();
  }
  cancelButton.addEventListener("click", () => { if (!busy) dialog.close(); });
  dialog.addEventListener("cancel", event => { if (busy) event.preventDefault(); });
  dialog.addEventListener("close", () => { if (!dialog.open) editing = null; });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !available || saveButton.disabled || !form.reportValidity()) return;
    const current = ticket;
    let body;
    const target = operation === "add" ? "/api/admin/nodes" : "/api/admin/nodes/" + encodeURIComponent(editing.node_id) + "/" + operation;
    if (operation === "remove") body = {expected:editing, confirm_node_id:editing.node_id};
    else {
      body = {name:document.getElementById("device-name").value, host:document.getElementById("device-host").value.trim(),
        port:Number(document.getElementById("device-port").value), enabled:document.getElementById("device-enabled").checked};
      if (editing) body.expected = editing;
    }
    busy = true;
    fields.disabled = true;
    saveButton.disabled = true;
    cancelButton.disabled = true;
    addButton.disabled = true;
    render();
    formStatus.textContent = operation === "remove" ? "Removing registration…" : "Saving device…";
    let saved = false;
    try {
      const data = await request(target, body);
      if (current !== ticket) return;
      if (data.saved !== true || typeof data.node_id !== "string") throw new Error("Cannot confirm save. Refresh Devices before retrying.");
      saved = true;
      formStatus.textContent = "Saved to Registry. Fleet applies the change automatically; History is preserved.";
      status.textContent = "Registry saved. Waiting for Fleet reload; inspect monitoring for current Agent status.";
      cancelButton.textContent = "Close";
      document.dispatchEvent(new Event("registry-change"));
      const registry = await request("/api/admin/nodes");
      if (current === ticket) { entries = validateEntries(registry); render(); }
    } catch (error) {
      if (current !== ticket) return;
      if (saved) {
        formStatus.textContent = "Registry saved, but list refresh failed. Close and refresh Devices.";
        return;
      }
      formStatus.textContent = error.name === "AbortError" ? "Save response timed out. Refresh Devices before retrying; the change may already be saved." : error.message;
      fields.disabled = operation === "remove";
      saveButton.disabled = false;
    } finally {
      if (current === ticket) { busy = false; cancelButton.disabled = false; addButton.disabled = !available; render(); }
    }
  });
  if (location.hash === "#devices") showView(true);
})();
