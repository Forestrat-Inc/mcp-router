"use strict";

/**
 * MCP Registry Service UI — single-page vanilla JS. Every state transition
 * is explicit; no reactive framework. Auth is an X-API-Key kept in
 * sessionStorage. On 401 we clear it and fall back to the sign-in card.
 */

const API_BASE = "/api/mcp";
const KEY_STORAGE = "mcp_router_api_key";
const SESS_HINT   = "mcp_router_key_hint";

// ── State ─────────────────────────────────────────────────────────
let servers = [];       // array of MCPServerResponse
let editing = null;     // application_id being edited, or null
let confirmCallback = null;

// ── DOM helpers ───────────────────────────────────────────────────
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const show = (el) => el.classList.remove("hidden");
const hide = (el) => el.classList.add("hidden");
const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

// ── Auth ──────────────────────────────────────────────────────────
function getApiKey() {
  try { return sessionStorage.getItem(KEY_STORAGE) || ""; } catch { return ""; }
}
function setApiKey(k) {
  try {
    if (k) {
      sessionStorage.setItem(KEY_STORAGE, k);
      sessionStorage.setItem(SESS_HINT, k.slice(0, 8) + "…");
    } else {
      sessionStorage.removeItem(KEY_STORAGE);
      sessionStorage.removeItem(SESS_HINT);
    }
  } catch { /* noop */ }
}

async function signIn() {
  const key = $("#api-key-input").value.trim();
  const errBox = $("#auth-error");
  hide(errBox);
  if (!key) return;
  // Validate the key with a cheap read.
  const resp = await fetch(`${API_BASE}/apps`, { headers: { "X-API-Key": key } });
  if (resp.status === 401 || resp.status === 403) {
    errBox.textContent = "That key was rejected by the router.";
    show(errBox);
    return;
  }
  if (!resp.ok) {
    errBox.textContent = `Router returned HTTP ${resp.status}. Try again in a moment.`;
    show(errBox);
    return;
  }
  setApiKey(key);
  await enterApp();
}

function signOut() {
  setApiKey("");
  servers = [];
  hide($("#app"));
  show($("#auth-gate"));
  $("#api-key-input").value = "";
  $("#api-key-input").focus();
}

// ── API calls ─────────────────────────────────────────────────────
async function api(path, options = {}) {
  const headers = { "X-API-Key": getApiKey(), "Content-Type": "application/json", ...(options.headers || {}) };
  const resp = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (resp.status === 401 || resp.status === 403) {
    signOut();
    throw new Error("Session expired — please sign in again.");
  }
  const text = await resp.text();
  let body;
  try { body = text ? JSON.parse(text) : null; }
  catch { body = { raw: text }; }
  if (!resp.ok) {
    const msg = body?.detail || body?.raw || `HTTP ${resp.status}`;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  return body;
}

const listApps    = () => api("/apps");
const getApp      = (id) => api(`/apps/${id}`);
const createApp   = (payload) => api("/apps", { method: "POST", body: JSON.stringify(payload) });
const patchApp    = (id, payload) => api(`/apps/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
const deleteApp   = (id) => api(`/apps/${id}`, { method: "DELETE" });
const invalidate  = (id) => api(`/admin/invalidate/${id}`, { method: "POST" });
const getHistory  = (id) => api(`/admin/history/${id}`);

// ── Render ────────────────────────────────────────────────────────
function statusBadge(status) {
  return `<span class="badge badge-${status}">${status}</span>`;
}

function renderServers() {
  const list = $("#servers-list");
  const filter = ($("#filter-input").value || "").trim().toLowerCase();
  const statusFilter = $("#status-filter").value;

  const filtered = servers.filter((s) => {
    if (statusFilter && s.status !== statusFilter) return false;
    if (!filter) return true;
    return (
      (s.name || "").toLowerCase().includes(filter) ||
      (s.application_id || "").toLowerCase().includes(filter)
    );
  });

  if (!filtered.length) {
    list.innerHTML = "";
    if (!servers.length) show($("#empty-state"));
    else {
      hide($("#empty-state"));
      list.innerHTML = `<div class="empty muted">No servers match your filter.</div>`;
    }
    $("#status-bar").textContent = `${servers.length} registered`;
    return;
  }
  hide($("#empty-state"));

  list.innerHTML = filtered.map((s) => {
    const handles = s.capabilities?.handles || {};
    const chips = [
      ...(handles.severities || []).map((v) => `<code>${escapeHtml(v)}</code>`),
      ...(handles.alert_types || []).map((v) => `<code>${escapeHtml(v)}</code>`),
    ].slice(0, 4).join(" ");
    return `
      <div class="card">
        <div class="card-head">
          <div class="card-name">${escapeHtml(s.name)}</div>
          ${statusBadge(s.status)}
        </div>
        <div class="card-body">
          <div><span class="key">app id</span><code>${escapeHtml(s.application_id)}</code></div>
          <div><span class="key">endpoint</span><code>${escapeHtml(s.endpoint_url)}</code></div>
          <div><span class="key">transport</span>${escapeHtml(s.transport)} · <span class="key">auth</span>${escapeHtml(s.auth_type)}</div>
          <div><span class="key">handles</span>${chips || `<span class="muted small">everything (no filter)</span>`}</div>
        </div>
        <div class="card-actions">
          <button data-action="view" data-id="${s.application_id}">View</button>
          <button data-action="edit" data-id="${s.application_id}">Edit</button>
          <button data-action="invalidate" data-id="${s.application_id}" class="ghost">Bust cache</button>
          <button data-action="delete" data-id="${s.application_id}" class="ghost">Delete</button>
        </div>
      </div>
    `;
  }).join("");

  $("#status-bar").textContent =
    `${filtered.length} shown · ${servers.length} registered`;

  // Wire up per-card buttons.
  $$("#servers-list button[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => onCardAction(btn.dataset.action, btn.dataset.id));
  });
}

async function refresh() {
  try {
    $("#status-bar").textContent = "Loading…";
    servers = await listApps();
    // Deterministic order: active first, then by name.
    servers.sort((a, b) => {
      if (a.status !== b.status) return a.status === "active" ? -1 : 1;
      return (a.name || "").localeCompare(b.name || "");
    });
    renderServers();
  } catch (e) {
    toast(e.message, "err");
    $("#status-bar").textContent = "Failed to load";
  }
}

// ── Card actions ──────────────────────────────────────────────────
async function onCardAction(action, id) {
  if (action === "view")       return openDetail(id);
  if (action === "edit")       return openEdit(id);
  if (action === "delete")     return confirmDelete(id);
  if (action === "invalidate") return doInvalidate(id);
}

async function doInvalidate(id) {
  try {
    await invalidate(id);
    toast("Cache invalidated for this app.", "ok");
  } catch (e) { toast(e.message, "err"); }
}

function confirmDelete(id) {
  const s = servers.find((x) => x.application_id === id);
  openConfirm(
    "Deprecate MCP server?",
    `This soft-deletes ${s?.name || id}. The row stays for audit; the router will 404 lookups.`,
    async () => {
      try { await deleteApp(id); toast("Deprecated.", "ok"); refresh(); }
      catch (e) { toast(e.message, "err"); }
    }
  );
}

// ── Modals ────────────────────────────────────────────────────────
function openConfirm(title, body, cb) {
  $("#confirm-title").textContent = title;
  $("#confirm-body").textContent = body;
  confirmCallback = cb;
  show($("#confirm-modal"));
}
$("#confirm-cancel").addEventListener("click", () => { hide($("#confirm-modal")); confirmCallback = null; });
$("#confirm-ok").addEventListener("click", async () => {
  const cb = confirmCallback;
  hide($("#confirm-modal"));
  confirmCallback = null;
  if (cb) await cb();
});

async function openDetail(id) {
  try {
    const [server, history] = await Promise.all([getApp(id), getHistory(id).catch(() => [])]);
    $("#detail-title").textContent = server.name;
    const caps = server.capabilities || {};
    const handles = caps.handles || {};
    $("#detail-body").innerHTML = `
      <h3>Identity</h3>
      <dl class="kv-grid">
        <dt>application_id</dt><dd>${escapeHtml(server.application_id)}</dd>
        <dt>name</dt><dd>${escapeHtml(server.name)}</dd>
        <dt>status</dt><dd>${statusBadge(server.status)}</dd>
        <dt>owner_email</dt><dd>${escapeHtml(server.owner_email || "—")}</dd>
        <dt>created_at</dt><dd>${escapeHtml(server.created_at || "—")}</dd>
        <dt>updated_at</dt><dd>${escapeHtml(server.updated_at || "—")}</dd>
      </dl>

      <h3>Endpoint</h3>
      <dl class="kv-grid">
        <dt>transport</dt><dd>${escapeHtml(server.transport)}</dd>
        <dt>endpoint_url</dt><dd>${escapeHtml(server.endpoint_url)}</dd>
        <dt>auth_type</dt><dd>${escapeHtml(server.auth_type)}</dd>
        <dt>auth_ref</dt><dd>${escapeHtml(server.auth_ref || "—")}</dd>
      </dl>

      <h3>Declared capabilities</h3>
      <dl class="kv-grid">
        <dt>protocol_version</dt><dd>${escapeHtml(caps.protocol_version || "?")}</dd>
        <dt>server_name</dt><dd>${escapeHtml(caps.server_name || "?")}</dd>
        <dt>server_version</dt><dd>${escapeHtml(caps.server_version || "?")}</dd>
        <dt>read_only_default</dt><dd>${caps.read_only_default === false ? "false ⚠︎" : "true"}</dd>
        <dt>max_response_ms</dt><dd>${escapeHtml(caps.max_response_ms || "?")}</dd>
        <dt>handles.severities</dt><dd>${(handles.severities || []).map(escapeHtml).join(", ") || "<span class='muted'>any</span>"}</dd>
        <dt>handles.alert_types</dt><dd>${(handles.alert_types || []).map(escapeHtml).join(", ") || "<span class='muted'>any</span>"}</dd>
        <dt>handles.metrics</dt><dd>${(handles.metric_patterns || []).map(escapeHtml).join(", ") || "<span class='muted'>any</span>"}</dd>
      </dl>

      <h3>Actions declared</h3>
      ${
        (caps.declared_actions || []).length === 0
          ? `<div class="muted small">(server declared no actions)</div>`
          : `<pre>${escapeHtml(JSON.stringify(caps.declared_actions, null, 2))}</pre>`
      }

      <h3>Change history (last ${history.length})</h3>
      ${
        history.length === 0
          ? `<div class="muted small">No changes yet.</div>`
          : history.map(h => `
              <div class="history-item op-${h.op}">
                <strong>${escapeHtml(h.op)}</strong>
                <span class="who">by ${escapeHtml(h.changed_by || "unknown")}</span>
                · ${escapeHtml(h.changed_at || "")}
              </div>
            `).join("")
      }
    `;
    show($("#detail-modal"));
  } catch (e) { toast(e.message, "err"); }
}
$("#detail-close").addEventListener("click", () => hide($("#detail-modal")));

function openEdit(id) {
  const s = servers.find((x) => x.application_id === id);
  if (!s) return;
  editing = id;
  $("#form-title").textContent = "Edit MCP server";
  $("#form-submit").textContent = "Save changes";

  const f = $("#server-form");
  f.application_id.value = s.application_id;
  f.application_id.readOnly = true;
  f.name.value = s.name;
  f.transport.value = s.transport;
  f.status.value = s.status;
  f.endpoint_url.value = s.endpoint_url;
  f.auth_type.value = s.auth_type;
  f.auth_ref.value = s.auth_ref || "";
  f.owner_email.value = s.owner_email || "";
  toggleAuthRefRequired();
  hide($("#write-capable-row"));
  hide($("#form-error"));
  show($("#form-modal"));
}

function openCreate() {
  editing = null;
  $("#form-title").textContent = "Register MCP server";
  $("#form-submit").textContent = "Register";
  const f = $("#server-form");
  f.reset();
  f.application_id.readOnly = false;
  f.transport.value = "http";
  f.auth_type.value = "none";
  f.status.value = "active";
  toggleAuthRefRequired();
  hide($("#write-capable-row"));
  hide($("#form-error"));
  show($("#form-modal"));
}
$("#new-btn").addEventListener("click", openCreate);
$("#form-close").addEventListener("click", () => hide($("#form-modal")));
$("#form-cancel").addEventListener("click", () => hide($("#form-modal")));

function toggleAuthRefRequired() {
  const type = $("#server-form").auth_type.value;
  const req  = $("#auth-ref-req");
  const input = $("#server-form").auth_ref;
  // Both bearer and api_key resolve their secret via auth_ref (KV pointer).
  if (type === "bearer" || type === "api_key") {
    show(req);
    input.required = true;
  } else {
    hide(req);
    input.required = false;
  }
}
$("#server-form").auth_type.addEventListener("change", toggleAuthRefRequired);

$("#server-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errBox = $("#form-error");
  hide(errBox);
  const f = $("#server-form");
  const payload = {
    application_id: f.application_id.value.trim(),
    name: f.name.value.trim(),
    transport: f.transport.value,
    endpoint_url: f.endpoint_url.value.trim(),
    auth_type: f.auth_type.value,
    auth_ref: f.auth_ref.value.trim() || null,
    owner_email: f.owner_email.value.trim() || null,
    status: f.status.value,
    i_accept_write_capable_server: f.i_accept_write_capable_server?.checked === true,
  };

  $("#form-submit").disabled = true;
  try {
    if (editing) {
      // PATCH — application_id + i_accept_write_capable_server are not accepted
      const { application_id, i_accept_write_capable_server, ...rest } = payload;
      await patchApp(editing, rest);
      toast("Server updated.", "ok");
    } else {
      try {
        await createApp(payload);
        toast("Server registered.", "ok");
      } catch (err) {
        // The router surfaces "server declares read_only_default=false" — pop
        // the confirmation checkbox and let the user try again.
        if (err.status === 400 && /read_only_default=false/i.test(err.message)) {
          show($("#write-capable-row"));
          errBox.textContent = err.message + " — tick the acknowledgement below and try again.";
          show(errBox);
          return;
        }
        throw err;
      }
    }
    hide($("#form-modal"));
    refresh();
  } catch (err) {
    errBox.textContent = err.message;
    show(errBox);
  } finally {
    $("#form-submit").disabled = false;
  }
});

// ── Toast ─────────────────────────────────────────────────────────
let toastTimer = null;
function toast(msg, kind = "ok") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast toast-" + kind;
  show(el);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => hide(el), 3500);
}

// ── Boot ──────────────────────────────────────────────────────────
async function enterApp() {
  hide($("#auth-gate"));
  show($("#app"));
  const hint = sessionStorage.getItem(SESS_HINT) || "";
  $("#user-hint").textContent = hint ? `key: ${hint}` : "";
  await refresh();
}

document.addEventListener("DOMContentLoaded", async () => {
  $("#auth-btn").addEventListener("click", signIn);
  $("#api-key-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") signIn();
  });
  $("#logout-btn").addEventListener("click", signOut);
  $("#refresh-btn").addEventListener("click", refresh);
  $("#filter-input").addEventListener("input", renderServers);
  $("#status-filter").addEventListener("change", renderServers);

  // Close modals on backdrop click.
  $$(".modal").forEach((m) => {
    m.addEventListener("click", (e) => { if (e.target === m) hide(m); });
  });

  if (getApiKey()) {
    // Valid-until-proven-otherwise; a 401 from any call clears it via signOut.
    await enterApp();
  } else {
    show($("#auth-gate"));
    $("#api-key-input").focus();
  }
});
