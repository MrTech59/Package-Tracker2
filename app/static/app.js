/* Package Tracker — frontend logic (vanilla JS, no build step). */

const state = {
  packages: [],
  config: null,
  stats: null,
  filters: { q: "", status: "all", carrier: "all", sort: "newest" },
  expanded: new Set(),
  busy: new Set(),
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------- utils */

async function api(path, options = {}) {
  const opts = { headers: { "Content-Type": "application/json" }, ...options };
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  const res = await fetch(path, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) {
    const detail = data?.detail;
    const message = Array.isArray(detail)
      ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
      : detail || data?.error || `Request failed (${res.status})`;
    throw new Error(message);
  }
  return data;
}

function toast(message, kind = "", timeout = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("toasts").appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 200);
  }, timeout);
}

function relativeTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Math.round((Date.now() - then) / 1000);
  const abs = Math.abs(diff);
  if (abs < 60) return "just now";
  const units = [
    ["minute", 60], ["hour", 3600], ["day", 86400], ["month", 2592000], ["year", 31536000],
  ];
  let label = "just now", value = 0;
  for (const [name, secs] of units) {
    if (abs >= secs) { label = name; value = Math.floor(abs / secs); }
  }
  const plural = value === 1 ? "" : "s";
  return diff >= 0 ? `${value} ${label}${plural} ago` : `in ${value} ${label}${plural}`;
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00` : iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function daysUntil(isoDate) {
  if (!isoDate) return null;
  const target = new Date(`${isoDate}T12:00:00`);
  if (Number.isNaN(target.getTime())) return null;
  const today = new Date();
  today.setHours(12, 0, 0, 0);
  return Math.round((target - today) / 86400000);
}

function etaText(isoDate) {
  const days = daysUntil(isoDate);
  if (days === null) return formatDate(isoDate);
  if (days === 0) return `Today`;
  if (days === 1) return `Tomorrow`;
  if (days === -1) return `Yesterday`;
  if (days > 1) return `in ${days} days`;
  return `${Math.abs(days)} days ago`;
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

/* ------------------------------------------------------------------- render */

function visiblePackages() {
  const { q, status, carrier, sort } = state.filters;
  let list = [...state.packages];

  if (q) {
    const needle = q.toLowerCase();
    list = list.filter((p) =>
      (p.name || "").toLowerCase().includes(needle) ||
      (p.tracking_number || "").toLowerCase().includes(needle) ||
      (p.notes || "").toLowerCase().includes(needle) ||
      (p.carrier_name || "").toLowerCase().includes(needle)
    );
  }
  if (status !== "all") list = list.filter((p) => p.status === status);
  if (carrier !== "all") list = list.filter((p) => p.carrier_id === carrier);

  const sorters = {
    newest: (a, b) => b.id - a.id,
    oldest: (a, b) => a.id - b.id,
    name: (a, b) => (a.name || "").localeCompare(b.name || ""),
    eta: (a, b) => {
      const av = a.expected_date || "9999-12-31";
      const bv = b.expected_date || "9999-12-31";
      return av.localeCompare(bv);
    },
  };
  list.sort(sorters[sort] || sorters.newest);
  return list;
}

function renderStats() {
  const s = state.stats;
  if (!s) return;
  $("stat-in-flight").textContent = s.in_flight;
  $("stat-out-for-delivery").textContent = s.out_for_delivery;
  $("stat-delivered").textContent = s.delivered;
  $("stat-problems").textContent = s.problems;
  $("stat-total").textContent = s.total;

  document.querySelectorAll(".stat-card").forEach((card) => {
    card.classList.toggle("is-active", card.dataset.stat === `filter-${state.filters.status}`);
  });

  const upcoming = s.upcoming || [];
  $("upcoming-panel").hidden = upcoming.length === 0;
  $("upcoming-list").innerHTML = upcoming.map((p) => {
    const days = daysUntil(p.expected_date);
    const soon = days !== null && days <= 2;
    return `<li>
      <span>${escapeHtml(p.name)}</span>
      <span class="upcoming-eta" style="${soon ? "color:var(--st-out)" : ""}">${escapeHtml(etaText(p.expected_date))}</span>
    </li>`;
  }).join("");
}

function statusOptions(selected) {
  const statuses = state.config?.statuses || [];
  return statuses.map((s) =>
    `<option value="${s.value}"${s.value === selected ? " selected" : ""}>${escapeHtml(s.label)}</option>`
  ).join("");
}

function renderPackage(p) {
  const isBusy = state.busy.has(p.id);
  const isOpen = state.expanded.has(p.id);

  const eta = p.expected_date
    ? `<span title="Expected delivery">📅 ${escapeHtml(etaText(p.expected_date))}</span>`
    : "";

  const tracking = p.has_tracking
    ? `<span class="tracking-code">${escapeHtml(p.tracking_number)}</span>
       <a href="${escapeHtml(p.tracking_url)}" target="_blank" rel="noopener noreferrer">track ↗</a>`
    : `<span style="color:var(--text-faint)">no tracking number</span>`;

  const delivered = p.delivered_at
    ? `<span>✅ ${escapeHtml(relativeTime(p.delivered_at))}</span>` : "";

  const checked = p.last_checked_at
    ? `<span class="package-updated">checked ${escapeHtml(relativeTime(p.last_checked_at))}</span>` : "";

  const error = p.last_error
    ? `<div class="package-error">⚠ ${escapeHtml(p.last_error)}</div>` : "";

  const notes = p.notes
    ? `<div class="package-notes">${escapeHtml(p.notes)}</div>` : "";

  return `
  <li class="package-card" data-status="${escapeHtml(p.status)}" data-id="${p.id}">
    <div class="package-main">
      <div class="package-heading">
        <span class="package-name">${escapeHtml(p.name)}</span>
        <span class="package-meta">
          <span>${escapeHtml(p.carrier_name)}</span>
          ${tracking}
          ${eta}
          ${delivered}
          ${checked}
        </span>
      </div>
      <span class="status-badge" data-status="${escapeHtml(p.status)}">${escapeHtml(p.status_label)}</span>
    </div>
    ${notes}
    ${error}
    <div class="package-actions">
      <select class="status-select" data-action="status" ${isBusy ? "disabled" : ""} aria-label="Change status">
        ${statusOptions(p.status)}
      </select>
      <button class="btn btn-ghost btn-sm" data-action="refresh" ${isBusy || !p.can_refresh ? "disabled" : ""}>
        ${isBusy ? '<span class="spinner"></span> Checking…' : "↻ Check now"}
      </button>
      <button class="btn btn-ghost btn-sm" data-action="edit">✎ Edit</button>
      <button class="btn btn-ghost btn-sm" data-action="history">${isOpen ? "▾" : "▸"} History</button>
      <button class="btn btn-danger btn-sm" data-action="delete">🗑 Delete</button>
    </div>
    ${isOpen ? `<ul class="timeline" data-timeline="${p.id}"><li>Loading history…</li></ul>` : ""}
  </li>`;
}

function renderList() {
  const list = visiblePackages();
  const listEl = $("package-list");
  const emptyEl = $("empty-state");

  const noPackagesAtAll = state.packages.length === 0;
  const noMatches = list.length === 0 && !noPackagesAtAll;

  emptyEl.hidden = list.length > 0;
  if (noMatches) {
    emptyEl.querySelector("h3").textContent = "No matching packages";
    emptyEl.querySelector("p").textContent = "Try clearing your search or filters.";
  } else if (noPackagesAtAll) {
    emptyEl.querySelector("h3").textContent = "No packages yet";
    emptyEl.querySelector("p").textContent = "Add your first shipment to start tracking it.";
  }

  listEl.innerHTML = list.map(renderPackage).join("");
  state.expanded.forEach((id) => loadTimeline(id));
}

/* ----------------------------------------------------------------- timeline */

async function loadTimeline(packageId) {
  const el = document.querySelector(`[data-timeline="${packageId}"]`);
  if (!el) return;
  try {
    const { events } = await api(`/api/packages/${packageId}/events?limit=25`);
    if (!events.length) {
      el.innerHTML = "<li>No events recorded yet.</li>";
      return;
    }
    el.innerHTML = events.map((e) => `
      <li>
        <span class="tl-status">${escapeHtml(e.status_label)}</span>
        <span class="tl-time">${escapeHtml(relativeTime(e.created_at))}</span>
        <span class="tl-source">${escapeHtml(e.source)}</span>
        ${e.message ? `<div style="margin-top:2px">${escapeHtml(e.message)}</div>` : ""}
      </li>`).join("");
  } catch (err) {
    el.innerHTML = `<li>Could not load history: ${escapeHtml(err.message)}</li>`;
  }
}

async function loadActivity() {
  try {
    const { events } = await api("/api/events?limit=12");
    const el = $("activity-list");
    if (!events.length) { el.innerHTML = "<li>No activity yet.</li>"; return; }
    el.innerHTML = events.map((e) => `
      <li>
        <span class="activity-name">${escapeHtml(e.package_name)}</span>
        <span style="color:var(--text-dim)">${escapeHtml(e.status_label)}</span>
        <span class="activity-time">${escapeHtml(relativeTime(e.created_at))}</span>
      </li>`).join("");
  } catch { /* activity is non-critical */ }
}

/* ------------------------------------------------------------------- loading */

async function loadAll({ silent = false } = {}) {
  try {
    const [pkgRes, statsRes] = await Promise.all([
      api(`/api/packages?q=${encodeURIComponent(state.filters.q)}&status=${encodeURIComponent(state.filters.status)}&carrier=${encodeURIComponent(state.filters.carrier)}`),
      api("/api/stats"),
    ]);
    state.packages = pkgRes.packages;
    state.stats = statsRes;
    renderStats();
    renderList();
    loadActivity();
    if (!silent) setConnection("ok", "connected");
  } catch (err) {
    setConnection("bad", "offline");
    if (!silent) toast(`Failed to load: ${err.message}`, "err");
  }
}

function setConnection(kind, text) {
  const pill = $("connection-pill");
  pill.className = `pill pill-${
    kind === "ok" ? "ok" : kind === "warn" ? "warn" : kind === "bad" ? "bad" : "muted"
  }`;
  pill.textContent = text;
}

/* ------------------------------------------------------------------- actions */

async function refreshPackage(id, { force = true } = {}) {
  state.busy.add(id);
  renderList();
  try {
    const result = await api(`/api/packages/${id}/refresh?force=${force}&notify=true`, { method: "POST" });
    if (result.ok) {
      if (result.changed) {
        const pkg = state.packages.find((p) => p.id === id);
        toast(`${pkg?.name || "Package"} → ${result.status.replace(/_/g, " ")}${result.notified ? " (Telegram sent)" : ""}`, "ok");
      } else {
        toast("Checked — no status change.", "", 2600);
      }
      if (result.warnings?.length && !result.confident) {
        toast(`Scrape inconclusive: ${result.warnings[0]}`, "warn", 6000);
      }
    } else {
      toast(result.error || "Check failed", "err", 6000);
    }
  } catch (err) {
    toast(`Check failed: ${err.message}`, "err");
  } finally {
    state.busy.delete(id);
    await loadAll({ silent: true });
  }
}

async function changeStatus(id, status) {
  state.busy.add(id);
  try {
    await api(`/api/packages/${id}/status`, {
      method: "POST",
      body: { status, send_notification: true },
    });
    const label = state.config?.statuses.find((s) => s.value === status)?.label || status;
    toast(`Status set to ${label}`, "ok", 2600);
  } catch (err) {
    toast(`Update failed: ${err.message}`, "err");
  } finally {
    state.busy.delete(id);
    await loadAll({ silent: true });
  }
}

async function deletePackage(id) {
  const pkg = state.packages.find((p) => p.id === id);
  if (!confirm(`Delete "${pkg?.name || "this package"}"? This cannot be undone.`)) return;
  try {
    await api(`/api/packages/${id}`, { method: "DELETE" });
    state.expanded.delete(id);
    toast("Package deleted", "", 2600);
    await loadAll({ silent: true });
  } catch (err) {
    toast(`Delete failed: ${err.message}`, "err");
  }
}

async function refreshAll() {
  const btn = $("btn-refresh-all");
  btn.disabled = true;
  btn.classList.add("is-busy");
  btn.innerHTML = '<span class="spinner"></span> Checking…';
  setConnection("warn", "refreshing…");
  try {
    const result = await api("/api/refresh?force=false", { method: "POST" });
    toast(`Checked ${result.checked} package${result.checked === 1 ? "" : "s"}, ${result.changed} updated.`, "ok", 5000);
    setConnection("ok", "connected");
  } catch (err) {
    toast(`Refresh failed: ${err.message}`, "err");
    setConnection("bad", "error");
  } finally {
    btn.disabled = false;
    btn.classList.remove("is-busy");
    btn.innerHTML = "↻ Refresh all";
    await loadAll({ silent: true });
  }
}

/* -------------------------------------------------------------------- dialog */

function openPackageDialog(pkg = null) {
  $("dialog-title").textContent = pkg ? "Edit package" : "Add package";
  $("field-id").value = pkg ? pkg.id : "";
  $("field-name").value = pkg?.name || "";
  $("field-tracking").value = pkg?.tracking_number || "";
  $("field-carrier").value = pkg?.carrier_id || "other";
  $("field-status").value = pkg?.status || "ordered";
  $("field-eta").value = pkg?.expected_date || "";
  $("field-postal").value = pkg?.postal_code || "";
  $("field-notes").value = pkg?.notes || "";
  togglePostalField();
  $("dialog-package").showModal();
  setTimeout(() => $("field-name").focus(), 50);
}

function togglePostalField() {
  const carrier = $("field-carrier").value;
  // Carriers whose tracking pages often require a destination postal code.
  $("postal-field").hidden = !["canada_post", "royal_mail", "postnl", "dhl_de"].includes(carrier);
}

async function savePackage(event) {
  event.preventDefault();
  const id = $("field-id").value;
  const payload = {
    name: $("field-name").value.trim(),
    tracking_number: $("field-tracking").value.trim(),
    carrier: $("field-carrier").value,
    status: $("field-status").value,
    expected_date: $("field-eta").value || null,
    postal_code: $("field-postal").value.trim(),
    notes: $("field-notes").value.trim(),
  };
  if (!payload.name) { toast("Please enter a package name.", "warn"); return; }

  const btn = $("btn-save");
  btn.disabled = true;
  try {
    if (id) {
      await api(`/api/packages/${id}`, {
        method: "PATCH",
        body: { ...payload, send_notification: $("field-notify").checked },
      });
      toast("Package updated", "ok", 2600);
    } else {
      await api("/api/packages", { method: "POST", body: payload });
      toast("Package added", "ok", 2600);
    }
    $("dialog-package").close();
    await loadAll({ silent: true });
  } catch (err) {
    toast(`Save failed: ${err.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- diagnostics */

async function openSettings() {
  const dialog = $("dialog-settings");
  const cfg = state.config;
  if (cfg) {
    $("settings-note").textContent =
      `MCP: ${cfg.mcp_url} · Telegram: ${cfg.telegram_enabled ? "enabled" : "not configured"} · ` +
      `Auto-poll: ${cfg.scrape_enabled ? `every ${Math.round(cfg.poll_interval_seconds / 60)} min` : "disabled"}`;
  }
  dialog.showModal();

  const mcpPill = $("diag-mcp");
  const tgPill = $("diag-telegram");
  mcpPill.className = "pill pill-muted"; mcpPill.textContent = "checking…";
  tgPill.className = "pill pill-muted"; tgPill.textContent = "checking…";
  $("diag-mcp-detail").textContent = "";
  $("diag-telegram-detail").textContent = "";

  try {
    const res = await api("/api/diagnostics/mcp");
    mcpPill.className = `pill pill-${res.ok ? "ok" : "bad"}`;
    mcpPill.textContent = res.ok ? `reachable · ${res.tool_count} tools` : "unreachable";
    $("diag-mcp-detail").textContent = res.ok
      ? `${res.url}\n\nTools:\n${(res.tools || []).join("\n")}`
      : `${res.url}\n\n${res.error}`;
  } catch (err) {
    mcpPill.className = "pill pill-bad"; mcpPill.textContent = "error";
    $("diag-mcp-detail").textContent = err.message;
  }

  try {
    const res = await api("/api/diagnostics/telegram");
    tgPill.className = `pill pill-${res.ok ? "ok" : "bad"}`;
    tgPill.textContent = res.ok ? `@${res.bot}` : "not working";
    $("diag-telegram-detail").textContent = res.ok
      ? `Bot: @${res.bot}${res.chat ? `\nChat: ${res.chat}` : "\nNo chat id configured"}`
      : (res.error || "Telegram not configured");
  } catch (err) {
    tgPill.className = "pill pill-bad"; tgPill.textContent = "not configured";
    $("diag-telegram-detail").textContent = err.message;
  }
}

async function testTelegram() {
  const btn = $("btn-telegram-test");
  btn.disabled = true;
  try {
    await api("/api/diagnostics/telegram/test", { method: "POST" });
    toast("Test message sent to Telegram.", "ok");
  } catch (err) {
    toast(`Telegram test failed: ${err.message}`, "err", 6000);
  } finally {
    btn.disabled = false;
  }
}

async function exportData() {
  try {
    const res = await fetch("/api/export");
    if (!res.ok) throw new Error(`Export failed (${res.status})`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `packages-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast("Exported packages.json", "ok");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function importData(file) {
  try {
    const text = await file.text();
    const parsed = JSON.parse(text);
    if (!parsed || !Array.isArray(parsed.packages)) {
      throw new Error("File must contain a 'packages' array");
    }
    const replace = confirm(
      `Import ${parsed.packages.length} package(s).\n\n` +
      "OK = replace all existing packages\nCancel = add to existing packages"
    );
    const result = await api("/api/import/raw", {
      method: "POST",
      body: { packages: parsed.packages, replace },
    });
    toast(`Imported ${result.imported} package(s)${result.skipped?.length ? `, skipped ${result.skipped.length}` : ""}.`, "ok", 5000);
    await loadAll({ silent: true });
  } catch (err) {
    toast(`Import failed: ${err.message}`, "err", 6000);
  }
}

/* ---------------------------------------------------------------- bootstrap */

function buildSelects() {
  const carrierSel = $("field-carrier");
  carrierSel.innerHTML = (state.config?.carriers || [])
    .map((c) => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("");

  const statusSel = $("field-status");
  statusSel.innerHTML = statusOptions("ordered");

  $("filter-status").innerHTML = `<option value="all">All statuses</option>` +
    (state.config?.statuses || []).map((s) => `<option value="${s.value}">${escapeHtml(s.label)}</option>`).join("");

  $("filter-carrier").innerHTML = `<option value="all">All carriers</option>` +
    (state.config?.carriers || []).map((c) => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("");
}

function wireEvents() {
  $("btn-add").addEventListener("click", () => openPackageDialog());
  $("btn-refresh-all").addEventListener("click", refreshAll);
  $("btn-settings").addEventListener("click", openSettings);

  $("btn-theme").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("pt-theme", next);
  });

  // Debounced search.
  let searchTimer;
  $("search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const value = e.target.value;
    searchTimer = setTimeout(() => {
      state.filters.q = value;
      renderList();
    }, 180);
  });

  $("filter-status").addEventListener("change", (e) => {
    state.filters.status = e.target.value;
    renderList();
  });
  $("filter-carrier").addEventListener("change", (e) => {
    state.filters.carrier = e.target.value;
    renderList();
  });
  $("sort").addEventListener("change", (e) => {
    state.filters.sort = e.target.value;
    renderList();
  });

  // Stat cards double as quick filters.
  document.querySelectorAll(".stat-card").forEach((card) => {
    card.addEventListener("click", () => {
      const map = {
        in_flight: "in_transit", out_for_delivery: "out_for_delivery",
        delivered: "delivered", problems: "exception", total: "all",
      };
      const target = map[card.dataset.stat] || "all";
      state.filters.status = target === "all" ? "all" : target;
      $("filter-status").value = state.filters.status;
      if (target === "exception") {
        // "Needs attention" spans two statuses; approximate with the filter UI.
        state.filters.status = "all";
        $("filter-status").value = "all";
        const problems = state.packages.filter((p) => p.status_is_problem);
        $("search").value = "";
        state.filters.q = "";
        renderList();
        toast(`${problems.length} package(s) need attention.`, problems.length ? "warn" : "", 3200);
        return;
      }
      renderList();
    });
  });

  // Delegated actions inside the list.
  $("package-list").addEventListener("click", (e) => {
    const card = e.target.closest(".package-card");
    if (!card) return;
    const id = Number(card.dataset.id);
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (action === "refresh") refreshPackage(id);
    else if (action === "edit") openPackageDialog(state.packages.find((p) => p.id === id));
    else if (action === "delete") deletePackage(id);
    else if (action === "history") {
      if (state.expanded.has(id)) state.expanded.delete(id);
      else state.expanded.add(id);
      renderList();
    }
  });

  $("package-list").addEventListener("change", (e) => {
    if (e.target.dataset.action === "status") {
      const card = e.target.closest(".package-card");
      changeStatus(Number(card.dataset.id), e.target.value);
    }
  });

  // Dialogs.
  $("form-package").addEventListener("submit", savePackage);
  $("field-carrier").addEventListener("change", togglePostalField);
  document.querySelector("[data-action='cancel']").addEventListener("click", () => $("dialog-package").close());
  document.querySelector("[data-action='close-settings']").addEventListener("click", () => $("dialog-settings").close());
  $("btn-telegram-test").addEventListener("click", testTelegram);
  $("btn-export").addEventListener("click", exportData);
  $("btn-import").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", (e) => {
    if (e.target.files?.[0]) importData(e.target.files[0]);
    e.target.value = "";
  });

  // Empty-state add button.
  $("empty-state").addEventListener("click", (e) => {
    if (e.target.closest("[data-action='add']")) openPackageDialog();
  });

  // Keyboard: "n" focuses the search box, Escape closes dialogs.
  document.addEventListener("keydown", (e) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
    if (e.key === "n" && !typing && !document.querySelector("dialog[open]")) {
      e.preventDefault();
      openPackageDialog();
    }
  });
}

async function boot() {
  const saved = localStorage.getItem("pt-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  else if (window.matchMedia("(prefers-color-scheme: light)").matches) {
    document.documentElement.dataset.theme = "light";
  }

  try {
    state.config = await api("/api/config");
    buildSelects();
  } catch (err) {
    setConnection("bad", "offline");
    toast(`Cannot reach backend: ${err.message}`, "err", 8000);
    return;
  }

  wireEvents();
  await loadAll();

  // Keep the dashboard fresh without hammering the server.
  setInterval(() => loadAll({ silent: true }), 60000);
}

document.addEventListener("DOMContentLoaded", boot);
