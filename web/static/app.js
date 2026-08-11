"use strict";

const state = { csrf: null, token: null };

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmt(iso) { return iso ? new Date(iso).toLocaleString() : "—"; }
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 3200);
}

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, credentials: "include", headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-CSRF"] = state.csrf || "";
    opts.body = JSON.stringify(body);
  } else if (method !== "GET") {
    opts.headers["X-CSRF"] = state.csrf || "";
  }
  const res = await fetch(path, opts);
  if (res.status === 401) { setChrome(false); location.hash = "#/login"; throw new Error("Not authenticated"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function chip(status) {
  const cls = status === "completed" || status === "executed" || status === "ok" ? "ok"
    : status === "partial" || status === "pending" || status === "failed" ? "err"
    : "warn";
  return `<span class="chip ${cls}">${esc(status)}</span>`;
}

// ---------- app chrome ----------
function setChrome(show) {
  const bar = $("#topbar");
  if (bar) bar.style.display = show ? "" : "none";
}

// ---------- routing ----------
function navigate() {
  const path = (location.hash || "#/dashboard").replace(/^#\/?/, "");
  const [route, param] = path.split("/");
  const app = $("#app");
  if (route !== "login") setChrome(true);
  const pages = {
    login: renderLogin,
    dashboard: () => renderDashboard(app),
    approvals: () => renderApprovals(app),
    runs: () => renderRuns(app),
    run: () => renderRunDetail(app, param),
    config: () => renderConfig(app),
    paths: () => renderPaths(app),
  };
  const fn = pages[route] || renderDashboard;
  fn.call(null, app, param);
}

async function boot() {
  let authed = false;
  try {
    authed = !!(await api("/api/session")).authed;
  } catch {}
  if (!authed) location.hash = "#/login";
  $("#logout").addEventListener("click", async () => {
    try { await api("/api/logout", { method: "POST" }); } catch {}
    state.token = null; location.hash = "#/login";
  });
  navigate();
}

// ---------- login ----------
function renderLogin(app) {
  setChrome(false);
  app.innerHTML = `
    <div class="login-wrap">
      <div class="panel">
        <h1>AI Assistant</h1>
        <label for="pw">Password</label>
        <input id="pw" type="password" autocomplete="current-password">
        <div class="actions"><button id="go">Sign in</button></div>
        <p class="small muted">Set <span class="mono">ASST_ADMIN_PASSWORD</span> in <span class="mono">.env</span>.</p>
      </div>
    </div>`;
  const doLogin = async () => {
    try {
      const data = await api("/api/login", { method: "POST", body: { password: $("#pw").value } });
      state.csrf = data.csrf; state.token = "ok";
      setChrome(true);
      location.hash = "#/dashboard";
    } catch (e) { toast(e.message); }
  };
  $("#go").addEventListener("click", doLogin);
  $("#pw").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
  $("#pw").focus();
}

// ---------- dashboard ----------
async function renderDashboard(app) {
  app.innerHTML = `<h1>Dashboard</h1><div class="muted">Loading…</div>`;
  try {
    const [health, runs, approvals] = await Promise.all([
      api("/api/health"), api("/api/runs"), api("/api/approvals"),
    ]);
    const latest = runs[0] || null;
    app.innerHTML = `
      <h1>Dashboard</h1>
      <div class="panel">
        <div class="grid">
          <div><h2>Mode</h2><div>${health.mock ? '<span class="chip warn">mock</span>' : '<span class="chip ok">live</span>'}</div>
            <div class="small muted">${health.warnings || "all configured"}</div></div>
          <div><h2>Latest run</h2><div>${latest ? chip(latest.status) : chip("none")}</div>
            <div class="small muted">${latest ? fmt(latest.started_at) + " · " + latest.ticket_count + " tickets" : ""}</div></div>
          <div><h2>Pending approvals</h2><div><span class="chip warn">${approvals.length}</span></div></div>
        </div>
        <div class="actions">
          <button id="run-config">Run now (configured JQL)</button>
          <button id="run-custom" class="ghost">Run with custom JQL…</button>
        </div>
      </div>
      <h2>Ready for review</h2>
      <div class="muted small">See the <a class="link" href="#/approvals">Approvals</a> page for per-ticket plans.</div>`;
    $("#run-config").addEventListener("click", async () => {
      await api("/api/runs", { method: "POST", body: {} });
      toast("Run queued — refreshing in 2s"); setTimeout(() => renderDashboard(app), 2000);
    });
    $("#run-custom").addEventListener("click", async () => {
      const jql = prompt("JQL (single query):");
      if (!jql) return;
      await api("/api/runs", { method: "POST", body: { jql } });
      toast("Run queued"); setTimeout(() => renderDashboard(app), 2000);
    });
  } catch (e) { app.innerHTML = `<div class="panel">Error: ${esc(e.message)}</div>`; }
}

// ---------- approvals ----------
async function renderApprovals(app) {
  app.innerHTML = `<h1>Approvals</h1><div class="muted">Loading…</div>`;
  try {
    const items = await api("/api/approvals");
    if (!items.length) {
      app.innerHTML = `<h1>Approvals</h1><div class="panel">No pending plans. Trigger a run from the dashboard.</div>`;
      return;
    }
    app.innerHTML = `<h1>Approvals <span class="chip warn">${items.length}</span></h1>`;
    for (const it of items) { app.insertAdjacentHTML("beforeend", planCard(it)); }
    wirePlans();
  } catch (e) { app.innerHTML = `<div class="panel">Error: ${esc(e.message)}</div>`; }
}

function linkList(links) {
  if (!links || !links.length) return `<span class="muted small">no linked commits/PRs</span>`;
  return links.map(l =>
    `<a class="link" href="${esc(l.url)}" target="_blank" rel="noreferrer">` +
    `${esc(l.kind)}${l.sha ? " " + esc(l.sha) : ""}${l.pr_state ? " · " + esc(l.pr_state) : ""}` +
    ` — ${esc((l.title || "").slice(0, 70))}</a>`
  ).join("<br>");
}

function planCard(it) {
  const p = it.plan, t = it.ticket;
  const rows = p.actions.map((a) => {
    const meta = esc(JSON.stringify({ kind: a.kind, params: a.params, preview: a.preview }));
    let field = "";
    if (a.kind === "comment") {
      field = `<textarea data-edit-comment="${a.id}">${esc(a.params.body || "")}</textarea>`;
    } else if (a.kind === "create_pr") {
      field = `
        <label class="small">PR title</label>
        <textarea data-pr-title="${a.id}" rows="1">${esc(a.params.title || "")}</textarea>
        <label class="small">PR body</label>
        <textarea data-pr-body="${a.id}" rows="3">${esc(a.params.body || "")}</textarea>`;
    } else if (a.kind === "transition" || a.kind === "assign") {
      field = `<div class="mono small">${esc(a.preview || JSON.stringify(a.params))}</div>`;
    } else {
      field = `<pre class="diff">${esc(a.preview)}</pre>`;
    }
    return `
      <tr data-action-row data-kind="${esc(a.kind)}">
        <td>${a.seq}</td>
        <td><span class="chip">${esc(a.kind)}</span></td>
        <td style="width:42%">${field}
          <textarea data-meta class="hidden">${meta}</textarea></td>
        <td><input type="checkbox" data-toggle="${a.id}" ${a.enabled ? "checked" : ""}></td>
      </tr>`;
  }).join("");
  return `
  <div class="card approval-card" data-plan-card="${p.id}">
    <div class="head">
      <h3>${esc(t.key)} · ${esc(t.summary)}</h3>
      <span class="chip">${esc(p.path_id)}</span>
      ${t.repo ? `<span class="chip">${esc(t.repo)}</span>` : ""}
      <span class="chip warn">conf ${(t.triage_confidence * 100).toFixed(0)}%</span>
      ${t.need_my_input ? '<span class="chip err">needs your input</span>' : ""}
    </div>
    <div class="summary small muted">${esc(t.triage_reason)}</div>
    <div class="small muted" style="margin-top:6px">${linkList(t.links)}</div>
    <details style="margin-top:6px"><summary>narrative</summary>
      <pre class="diff">${esc(p.narrative)}</pre></details>
    <table style="margin-top:8px">
      <thead><tr><th>#</th><th>kind</th><th>preview / editable content</th><th>on</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="actions">
      <button class="ok" data-approve="${p.id}">Approve &amp; execute</button>
      <button class="ghost" data-reject="${p.id}">Reject</button>
      <button class="ghost" data-save="${p.id}">Save edits</button>
    </div>
  </div>`;
}

function wirePlans() {
  document.querySelectorAll("[data-approve]").forEach(b => b.addEventListener("click", async () => {
    try { await api(`/api/approvals/${b.dataset.approve}/approve`, { method: "POST" }); } catch (e) { toast(e.message); }
    renderApprovals($("#app"));
  }));
  document.querySelectorAll("[data-reject]").forEach(b => b.addEventListener("click", async () => {
    try { await api(`/api/approvals/${b.dataset.reject}/reject`, { method: "POST" }); } catch (e) { toast(e.message); }
    renderApprovals($("#app"));
  }));
  document.querySelectorAll("[data-save]").forEach(b => b.addEventListener("click", async () => {
    const card = b.closest("[data-plan-card]");
    const planId = b.dataset.save;
    try {
      const plan = (await api(`/api/approvals/${planId}`)).plan;
      const acts = [];
      [...card.querySelectorAll("tr[data-action-row]")].forEach(tr => {
        const orig = JSON.parse(tr.querySelector("[data-meta]").value || "{}");
        const kind = tr.dataset.kind;
        let params = { ...(orig.params || {}) };
        const toggle = tr.querySelector("[data-toggle]");
        if (kind === "comment") {
          const ta = tr.querySelector("[data-edit-comment]");
          if (ta) params.body = ta.value;
        } else if (kind === "create_pr") {
          const title = tr.querySelector("[data-pr-title]");
          const body = tr.querySelector("[data-pr-body]");
          if (title) params.title = title.value;
          if (body) params.body = body.value;
        }
        acts.push({ kind, params, enabled: toggle ? toggle.checked : true });
      });
      await api(`/api/approvals/${planId}`, {
        method: "PUT",
        body: { summary: plan.summary, narrative: plan.narrative, actions: acts },
      });
      toast("Saved");
    } catch (e) { toast(e.message); }
  }));
}

// ---------- runs ----------
async function renderRuns(app) {
  app.innerHTML = `<h1>Runs</h1><div class="muted">Loading…</div>`;
  try {
    const runs = await api("/api/runs");
    app.innerHTML = `<h1>Runs</h1>
      <div class="panel"><table>
        <thead><tr><th>#</th><th>trigger</th><th>status</th><th>jql</th><th>started</th><th>tickets</th><th>pending</th></tr></thead>
        <tbody>${runs.map(r => `<tr class="click" data-link="#/run/${r.id}" style="cursor:pointer">
          <td>${r.id}</td><td>${esc(r.trigger)}</td><td>${chip(r.status)}</td><td class="small">${esc(r.jql_label)}</td>
          <td class="small">${fmt(r.started_at)}</td><td>${r.ticket_count}</td><td>${r.pending_plans}</td></tr>`).join("")}
        </tbody></table></div>`;
    app.querySelectorAll("tr.click").forEach(tr => tr.addEventListener("click", () => location.hash = tr.dataset.link));
  } catch (e) { app.innerHTML = `<div class="panel">Error: ${esc(e.message)}</div>`; }
}

async function renderRunDetail(app, id) {
  app.innerHTML = `<h1>Run #${esc(id)}</h1><div class="muted">Loading…</div>`;
  try {
    const run = await api(`/api/runs/${id}`);
    app.innerHTML = `<h1>Run #${id} ${chip(run.status)}</h1>
      <div class="panel small muted">${esc(run.jql_label)} · ${fmt(run.started_at)} · error: ${esc(run.error || "none")}</div>`;
    for (const t of run.tickets) {
      app.insertAdjacentHTML("beforeend", `
      <div class="card">
        <div class="head"><h3>${esc(t.key)} · ${esc(t.summary)}</h3>
          <span class="chip">${esc(t.stage)}</span>
          <span class="chip">${esc(t.triage_path_id || "—")}</span>
          ${t.repo ? `<span class="chip">${esc(t.repo)}</span>` : ""}</div>
        <div class="summary small muted">${esc(t.triage_reason || t.error || "")}</div>
        <div class="small muted" style="margin:6px 0">${linkList(t.links)}</div>
        ${(t.plans || []).map(p => `
          <div class="small">Plan #${p.id} ${chip(p.review_status)} · ${esc(p.error || "")}</div>
          ${p.actions.map(a => `<pre class="diff">${esc(a.kind)}: ${esc(a.exec_status)} — ${esc(a.exec_result || a.preview || "")}</pre>`).join("")}
        `).join("")}
      </div>`);
    }
  } catch (e) { app.innerHTML = `<div class="panel">Error: ${esc(e.message)}</div>`; }
}

// ---------- config ----------
async function renderConfig(app) {
  app.innerHTML = `<h1>Config</h1><div class="muted">Loading…</div>`;
  try {
    const [c, repoMap] = await Promise.all([api("/api/config"), api("/api/repo-map")]);
    app.innerHTML = `<h1>Config</h1>
      <div class="panel" style="max-width:760px">
        <label>JQL queries (one per line — <span class="mono">name | jql</span>)</label>
        <textarea id="cfg-jql" rows="4">${esc((c.jql_queries || []).map(q => `${q.name} | ${q.jql}`).join("\n"))}</textarea>
        <div class="row">
          <div><label>Schedule enabled</label><input id="cfg-sched-on" type="checkbox" ${c.schedule.enabled ? "checked" : ""}></div>
          <div><label>Hour (24h)</label><input id="cfg-hour" type="number" value="${c.schedule.hour}"></div>
          <div><label>Minute</label><input id="cfg-minute" type="number" value="${c.schedule.minute}"></div>
          <div><label>Max tickets/run</label><input id="cfg-max" type="number" value="${c.run.max_tickets_per_run}"></div>
        </div>
        <div class="row">
          <div><label>Jira base URL</label><input id="cfg-jbase" placeholder="https://acme.atlassian.net" value="${esc(c.jira.base_url)}"></div>
          <div><label>Jira email</label><input id="cfg-jemail" value="${esc(c.jira.email)}"></div>
          <div><label>Jira account id (empty = resolve via token)</label><input id="cfg-jacc" value="${esc(c.jira.account_id)}"></div>
          <div><label>Jira dev-info field</label><input id="cfg-jdev" value="${esc(c.jira.devinfo_field)}"></div>
        </div>
        <div class="row">
          <div style="flex:2"><label>GitHub repos (owner/name, one per line)</label>
            <textarea id="cfg-grepos" rows="3">${esc((c.github.repos || []).join("\n"))}</textarea></div>
          <div style="flex:1"><label>Jira project → repo map (one per line, e.g. <span class="mono">PROJ=owner/repo</span>)</label>
            <textarea id="cfg-gmap" rows="3">${esc(Object.entries(c.github.project_repo_map || {}).map(([k, v]) => `${k}=${v}`).join("\n"))}</textarea></div>
          <div><label>Triage model (opencode -m)</label><input id="cfg-mtriage" value="${esc(c.models.triage)}"></div>
          <div><label>Action model</label><input id="cfg-maction" value="${esc(c.models.action)}"></div>
        </div>
        <div class="row">
          <div style="flex:3"><label>Per-ticket repo overrides (<span class="mono">KEY=owner/repo</span> when no URL in the ticket; <span class="mono">default=owner/repo</span> allowed) → <span class="mono">config/repo_map.json</span></label>
            <textarea id="cfg-overrides" rows="3">${esc(Object.entries(repoMap || {}).map(([k, v]) => `${k}=${v}`).join("\n"))}</textarea></div>
        </div>
        <div class="actions">
          <button id="cfg-save">Save</button>
          <span class="small muted" style="align-self:center">Tokens &amp; admin password come from <span class="mono">.env</span>; mock mode: ${c.mock ? "on" : "off"}</span>
        </div>
      </div>`;
    $("#cfg-save").addEventListener("click", async () => {
      const parseJql = $("#cfg-jql").value.split("\n").filter(Boolean).map(line => {
        const i = line.indexOf("|");
        return i >= 0 ? { name: line.slice(0, i).trim(), jql: line.slice(i + 1).trim() }
                      : { name: "query", jql: line.trim() };
      });
      const parseOverrides = () => Object.fromEntries(
        $("#cfg-overrides").value.split("\n").map(s => s.trim()).filter(Boolean)
          .map(line => { const i = line.indexOf("="); return [line.slice(0, i).trim(), line.slice(i + 1).trim()]; })
          .filter(([k, v]) => k && v));
      const body = {
        jql_queries: parseJql,
        schedule: { enabled: $("#cfg-sched-on").checked, hour: +$("#cfg-hour").value, minute: +$("#cfg-minute").value },
        jira: { base_url: $("#cfg-jbase").value, email: $("#cfg-jemail").value,
                account_id: $("#cfg-jacc").value, devinfo_field: $("#cfg-jdev").value },
        github: { repo: (c.github.repos || [])[0] || "",
                 repos: $("#cfg-grepos").value.split("\n").map(s => s.trim()).filter(Boolean),
                 project_repo_map: Object.fromEntries(
                   $("#cfg-gmap").value.split("\n").map(s => s.trim()).filter(Boolean)
                     .map(line => { const i = line.indexOf("="); return [line.slice(0, i).trim(), line.slice(i + 1).trim()]; })
                     .filter(([k, v]) => k && v)) },
        models: { triage: $("#cfg-mtriage").value, action: $("#cfg-maction").value },
        run: { max_tickets_per_run: +$("#cfg-max").value || 20 },
      };
      try {
        await api("/api/config", { method: "PUT", body });
        await api("/api/repo-map", { method: "PUT", body: parseOverrides() });
        toast("Config saved"); renderConfig(app);
      }
      catch (e) { toast(e.message); }
    });
  } catch (e) { app.innerHTML = `<div class="panel">Error: ${esc(e.message)}</div>`; }
}

// ---------- paths ----------
async function renderPaths(app) {
  app.innerHTML = `<h1>Paths</h1><div class="muted">Loading…</div>`;
  try {
    const paths = await api("/api/paths");
    app.innerHTML = `<h1>Paths <span class="muted small">${paths.length} loaded from <span class="mono">paths/</span></span></h1>`;
    for (const p of paths) {
      app.insertAdjacentHTML("beforeend", `
      <div class="card" data-path="${esc(p.id)}">
        <div class="head"><h3>${esc(p.id)}</h3>
          <span class="chip">${esc(p.name)}</span>
          ${p.enabled ? '<span class="chip ok">enabled</span>' : '<span class="chip err">disabled</span>'}
          ${p.valid ? "" : `<span class="chip err">${esc(p.error)}</span>`}
          <span class="chip">actions: ${esc((p.allowed_actions || []).join(", "))}</span></div>
        <label>instruct.md</label>
        <textarea data-path-instruct="${esc(p.id)}" rows="7">${esc(p.instruct)}</textarea>
        <label>schema.json</label>
        <textarea data-path-schema="${esc(p.id)}" rows="5">${esc(JSON.stringify({
          name: p.name, enabled: p.enabled, allowed_actions: p.allowed_actions,
          required_backend: p.required_backend, work: p.work, approval: p.approval,
          default_actions: p.default_actions }, null, 2))}</textarea>
        <div class="actions"><button data-path-save="${esc(p.id)}">Save path</button></div>
      </div>`);
    }
    app.insertAdjacentHTML("beforeend", `
      <div class="panel"><h2>New path</h2>
        <div class="row">
          <div><label>id (kebab-case)</label><input id="np-id" placeholder="e.g. copy-ticket"></div>
          <div><label>name</label><input id="np-name" placeholder="Copy request"></div>
        </div>
        <div class="actions"><button id="np-create">Create path from template</button></div>
      </div>`);
    document.querySelectorAll("[data-path-save]").forEach(b => b.addEventListener("click", async () => {
      const id = b.dataset.pathSave;
      const schema = JSON.parse(b.parentElement.parentElement.querySelector(`[data-path-schema="${id}"]`).value);
      const instruct = b.parentElement.parentElement.querySelector(`[data-path-instruct="${id}"]`).value;
      const body = { id, name: schema.name, enabled: schema.enabled ?? true,
        allowed_actions: schema.allowed_actions || [], required_backend: schema.required_backend || null,
        work: schema.work || {}, approval: schema.approval || {}, default_actions: schema.default_actions || [],
        instruct };
      try { await api(`/api/paths/${id}`, { method: "PUT", body }); toast("Path saved — next run uses it"); renderPaths(app); }
      catch (e) { toast(e.message); }
    }));
    $("#np-create").addEventListener("click", async () => {
      const body = { id: $("#np-id").value.trim(), name: $("#np-name").value.trim(),
        enabled: true, allowed_actions: ["comment"], instruct: "" };
      try { await api("/api/paths", { method: "POST", body }); toast("Created"); renderPaths(app); }
      catch (e) { toast(e.message); }
    });
  } catch (e) { app.innerHTML = `<div class="panel">Error: ${esc(e.message)}</div>`; }
}

window.addEventListener("hashchange", navigate);
document.addEventListener("DOMContentLoaded", boot);