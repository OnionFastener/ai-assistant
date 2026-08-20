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
  const url = path.startsWith("/api/")
    ? "/project2" + path
    : path;
  const res = await fetch(url, opts);
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
    triage: () => renderTriage(app),
    paths: () => renderPaths(app),
  };
  const fn = pages[route] || renderDashboard;
  document.querySelectorAll("#nav a").forEach((link) => {
    link.classList.toggle("active", link.getAttribute("href") === "#/" + route || (route === "run" && link.getAttribute("href") === "#/runs"));
  });
  fn.call(null, app, param);
}

async function boot() {
  let session = { authed: false };
  try {
    session = await api("/api/session");
  } catch {}
  const authed = !!session.authed;
  state.csrf = session.csrf || null;
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
        <p class="small muted">Use the console password configured in your environment.</p>
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
  document.getElementById("go").addEventListener("click", doLogin);
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
    const completed = runs.filter(r => r.status === "completed").length;
    const ticketTotal = runs.reduce((total, r) => total + (r.ticket_count || 0), 0);
    app.innerHTML = `
      <div class="hero"><div><div class="eyebrow">Your AI operations desk</div><h1>Good work starts with a clear queue.</h1><p class="page-intro">Review concrete AI proposals, then send only the work you trust into your tools.</p></div><div class="presence"><i></i> Review before execution</div></div>
      <div class="dashboard-grid">
        <section>
          <div class="panel metrics">
            <div class="metric"><div class="metric-label">Review queue</div><div class="metric-value">${approvals.length}</div><div class="metric-note">plans ready for your call</div></div>
            <div class="metric"><div class="metric-label">Latest scan</div><div class="metric-value">${latest ? latest.ticket_count : 0}</div><div class="metric-note">${latest ? `${esc(latest.status)} · ${fmt(latest.started_at)}` : "no scans yet"}</div></div>
            <div class="metric"><div class="metric-label">Tickets assessed</div><div class="metric-value">${ticketTotal}</div><div class="metric-note">across ${runs.length} recorded runs</div></div>
          </div>
          <div class="actions overview-actions"><button id="run-config">Run triage now</button><button id="run-custom" class="ghost">Custom query</button>${latest && ["queued", "fetching", "triaging", "stopping"].includes(latest.status) ? `<button id="stop-run" class="ghost">Stop triage</button>` : ""}</div>
          <div class="section-head"><h2>Ready for review</h2><a class="link small" href="#/approvals">Open queue →</a></div>
          ${approvals.length ? `<div class="panel review-callout"><strong>${approvals.length} proposal${approvals.length === 1 ? "" : "s"} need your decision.</strong><span class="small muted">Every action can be reviewed and edited before execution.</span></div>` : `<div class="queue-empty">Your review queue is clear. Run a scan when you’re ready to pick up new work.</div>`}
        </section>
        <aside class="panel work-report"><div class="eyebrow">Work report</div><h2>Operational pulse</h2><div class="report-line"><span>System mode</span><strong>${health.mock ? "DEMO" : "LIVE"}</strong></div><div class="report-line"><span>Runs completed</span><strong>${completed}</strong></div><div class="report-line"><span>Tickets assessed</span><strong>${ticketTotal}</strong></div><p class="small muted">${esc(health.warnings || "All connected services look configured.")}</p></aside>
      </div>`;
    $("#run-config").addEventListener("click", async () => {
      await api("/api/runs", { method: "POST", body: {} });
      toast("Run queued — refreshing in 2s"); setTimeout(() => renderDashboard(app), 2000);
    });
    const stopButton = $("#stop-run");
    if (stopButton) stopButton.addEventListener("click", async () => {
      stopButton.disabled = true;
      try {
        const stopped = await api(`/api/runs/${latest.id}/stop`, { method: "POST" });
        toast(`Stopped run — ${stopped.pending_plans} finished ticket(s) are ready for review`);
        setTimeout(() => renderDashboard(app), 500);
      } catch (e) { toast(e.message); stopButton.disabled = false; }
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
  <div class="card" data-plan-card="${p.id}">
    <div class="head">
      <h3>${esc(t.key)} · ${esc(t.summary)}</h3>
      <span class="chip">${esc(p.path_id)}</span>
      ${p.review_status === "preparing" ? '<span class="chip warn">preparing patch</span>' : ""}
      ${p.patch_preparation_failed ? '<span class="chip err">patch prep timed out</span>' : ""}
      ${p.scope_review_required ? '<span class="chip warn">scope review required</span>' : ""}
      ${t.repo ? `<span class="chip">${esc(t.repo)}</span>` : ""}
      <span class="chip warn">conf ${(t.triage_confidence * 100).toFixed(0)}%</span>
      ${t.need_my_input ? '<span class="chip err">needs your input</span>' : ""}
    </div>
    <div class="summary small muted">${esc(t.triage_reason)}</div>
    ${p.patch_preparation_failed ? `<div class="plan-error"><div class="plan-error-icon">!</div><div><strong>Patch preparation timed out</strong><span>${esc(p.error)}</span><a href="#/run/${p.run_id}">View diagnostics →</a></div></div>` : ""}
    ${p.scope_review_required ? `<div class="scope-review"><strong>Review scope before spending agent time.</strong><span>${esc(p.narrative)}</span></div>` : ""}
    <div class="small muted" style="margin-top:6px">${linkList(t.links)}</div>
    <details style="margin-top:6px"><summary>narrative</summary>
      <pre class="diff">${esc(p.narrative)}</pre></details>
    <table style="margin-top:8px">
      <thead><tr><th>#</th><th>kind</th><th>preview / editable content</th><th>on</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="actions">
      ${p.review_status === "preparing" ? `<button class="warn" data-cancel-patch="${p.id}">Cancel patch preparation</button>` : p.scope_review_required ? `<button class="warn" data-confirm-scope="${p.id}">Confirm scope</button>` : p.is_code_proposal ? `<button class="ok" data-prepare="${p.id}">${p.patch_preparation_failed ? "Retry patch preparation" : "Prepare patch"}</button>` : `<button class="ok" data-approve="${p.id}">Approve &amp; execute</button>`}
      ${p.review_status === "preparing" ? "" : `<button class="ghost" data-reject="${p.id}">Reject</button><button class="ghost" data-save="${p.id}">Save edits</button>`}
    </div>
  </div>`;
}

function wirePlans() {
  document.querySelectorAll("[data-confirm-scope]").forEach(b => b.addEventListener("click", async () => { try { await api(`/api/approvals/${b.dataset.confirmScope}/confirm-scope`, { method: "POST" }); toast("Scope confirmed — patch preparation is now available"); renderApprovals($("#app")); } catch (e) { toast(e.message); } }));
  document.querySelectorAll("[data-cancel-patch]").forEach(b => b.addEventListener("click", async () => { try { await api(`/api/approvals/${b.dataset.cancelPatch}/cancel-patch`, { method: "POST" }); toast("Patch preparation cancelled"); renderApprovals($("#app")); } catch (e) { toast(e.message); } }));
  document.querySelectorAll("[data-prepare]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await api(`/api/approvals/${b.dataset.prepare}/prepare-patch`, { method: "POST" }); toast("Preparing patch — the proposal will return as a diff review."); }
    catch (e) { toast(e.message); b.disabled = false; }
    renderApprovals($("#app"));
  }));
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
    if (run.logs?.length) {
      const diagnostics = run.logs.map((log) =>
        `${fmt(log.ts)} [${log.level}]${log.ticket_key ? ` ${log.ticket_key}` : ""} — ${log.message}`
      ).join("\n");
      app.insertAdjacentHTML("beforeend", `<details class="panel"><summary>Run diagnostics (${run.logs.length} events)</summary><pre class="diff">${esc(diagnostics)}</pre></details>`);
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
        <div class="row">
          <div><label><input id="cfg-source-jira" type="checkbox" ${(c.sources?.jira?.enabled ?? true) ? "checked" : ""}> Scan Jira issues</label></div>
          <div style="flex:2"><label><input id="cfg-source-github" type="checkbox" ${c.sources?.github_issues?.enabled ? "checked" : ""}> Scan public GitHub Issues${c.mock ? " (mock demo)" : ""}</label>
            <textarea id="cfg-gissue-repos" rows="3" placeholder="owner/repo, one per line">${esc((c.sources?.github_issues?.repos || []).join("\n"))}</textarea></div>
        </div>
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
        sources: { jira: { enabled: $("#cfg-source-jira").checked }, github_issues: { enabled: $("#cfg-source-github").checked, repos: $("#cfg-gissue-repos").value.split("\n").map(s => s.trim()).filter(Boolean) } },
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

// ---------- triage config ----------
function joinLines(arr) { return (arr || []).join("\n"); }
function splitLines(v) { return v.split("\n").map(s => s.trim()).filter(Boolean); }
async function renderTriage(app) {
  app.innerHTML = `<h1>Triage instructions</h1><div class="muted">Loading…</div>`;
  try {
    const [cfg, action] = await Promise.all([api("/api/triage-config"), api("/api/action-config")]);
    const cl = cfg.classify || {};
    app.innerHTML = `<h1>Triage instructions <span class="muted small">← <span class="mono">config/triage.md</span> — used on every run</span></h1>
      <div class="panel" style="max-width:760px">
        <p class="small muted">How tickets get classified. Word signals below feed the keyword
          classifier (mock mode) and are echoed to the triage agent as "classification signals".
          Per-path criteria live on the <a class="link" href="#/paths">Paths</a> page.</p>
        <label>Ticket context fields (one per line)</label>
        <textarea id="tc-fields" rows="5">${esc(joinLines(cfg.context_fields))}</textarea>
        <label>Bug signals (words that suggest a bug — one per line)</label>
        <textarea id="tc-bug" rows="5">${esc(joinLines(cl.bug_words))}</textarea>
        <label>Feature signals (words that suggest a feature request)</label>
        <textarea id="tc-feature" rows="5">${esc(joinLines(cl.feature_words))}</textarea>
        <label>Needs-human signals (words that suggest a human decision)</label>
        <textarea id="tc-decision" rows="4">${esc(joinLines(cl.decision_words))}</textarea>
        <label>Needs-more-info signals (short or vague tickets)</label>
        <textarea id="tc-moreinfo" rows="4">${esc(joinLines(cl.more_info_words))}</textarea>
        <div class="row">
          <div><label>Issue-type tie-break boosts</label></div>
          <div><span class="small muted">bug-type boost</span>
            <input id="tc-boost-bug" type="number" step="0.1" value="${esc(cl.type_boost && cl.type_boost["bug-fix"])}" style="width:80px"></div>
          <div><span class="small muted">feature-type boost</span>
            <input id="tc-boost-feature" type="number" step="0.1" value="${esc(cl.type_boost && cl.type_boost["new-feature"])}" style="width:80px"></div>
        </div>
        <label>Triage instructions (markdown, sent to the triage agent)</label>
        <textarea id="tc-instruct" rows="12">${esc(cfg.instruct)}</textarea>
        <div class="actions">
          <button id="tc-save">Save triage</button>
        </div>
      </div>
      <div class="panel" style="max-width:760px">
        <h2>Action agent</h2>
        <p class="small muted">The system profile the plan-building agent starts with. Per-path
          behavior (how it handles a bug vs a feature) is edited on the
          <a class="link" href="#/paths">Paths</a> page —
          <span class="mono">behavior.md</span> &mdash; and layered on top of this. Stored in
          <span class="mono">config/action.md</span>.</p>
        <label>Action-agent instructions (markdown)</label>
        <textarea id="ac-instruct" rows="12">${esc(action.instruct)}</textarea>
        <div class="actions">
          <button id="ac-save">Save action profile</button>
          <span class="small muted" style="align-self:center">Next run uses it; no restart needed.</span>
        </div>
      </div>`;
    $("#tc-save").addEventListener("click", async () => {
      const body = {
        context_fields: splitLines($("#tc-fields").value),
        classify: {
          bug_words: splitLines($("#tc-bug").value),
          feature_words: splitLines($("#tc-feature").value),
          decision_words: splitLines($("#tc-decision").value),
          more_info_words: splitLines($("#tc-moreinfo").value),
          type_boost: {
            "bug-fix": +$("#tc-boost-bug").value || (cl.type_boost && cl.type_boost["bug-fix"]) || 1.0,
            "new-feature": +$("#tc-boost-feature").value || (cl.type_boost && cl.type_boost["new-feature"]) || 0.4,
          },
        },
        instruct: $("#tc-instruct").value,
      };
      try { await api("/api/triage-config", { method: "PUT", body }); toast("Triage config saved — next run uses it"); renderTriage(app); }
      catch (e) { toast(e.message); }
    });
    $("#ac-save").addEventListener("click", async () => {
      try {
        await api("/api/action-config", { method: "PUT", body: { instruct: $("#ac-instruct").value } });
        toast("Action profile saved — next run uses it"); renderTriage(app);
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
        <label>Action-agent timeout (seconds)</label>
        <input data-path-timeout="${esc(p.id)}" type="number" min="30" max="3600" value="${esc(p.work?.action_timeout_seconds || 300)}">
        <div class="small muted">Used only for code-backed paths; lightweight paths do not invoke a code agent.</div>
        <label>instruct.md</label>
        <textarea data-path-instruct="${esc(p.id)}" rows="7">${esc(p.instruct)}</textarea>
        <label>behavior.md <span class="small muted">(action-agent guidance; blank = reuse instruct.md)</span></label>
        <textarea data-path-behavior="${esc(p.id)}" rows="5">${esc(p.behavior === p.instruct ? "" : p.behavior)}</textarea>
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
      const behavior = b.parentElement.parentElement.querySelector(`[data-path-behavior="${id}"]`).value;
      const timeout = +b.parentElement.parentElement.querySelector(`[data-path-timeout="${id}"]`).value;
      if (timeout) schema.work = { ...(schema.work || {}), action_timeout_seconds: timeout };
      const body = { id, name: schema.name, enabled: schema.enabled ?? true,
        allowed_actions: schema.allowed_actions || [], required_backend: schema.required_backend || null,
        work: schema.work || {}, approval: schema.approval || {}, default_actions: schema.default_actions || [],
        instruct, behavior };
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
