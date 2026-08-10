# AI Assistant — Design Plan (v1)

A personal AI assistant that — every night and on demand — scans Jira tickets, AI-triages each
one into a configurable "path", has an action agent produce a concrete proposal, waits for the
user to approve per ticket, then deterministically executes the approved actions against Jira,
GitHub, and git.

**Decisions locked in:**
- Python stack (FastAPI), SQLite, APScheduler
- Per-ticket approval — user reviews/approves each ticket's action plan individually
- Triage considers linked commits and PRs (GitHub)
- Single user; GitHub is the code host (branches + PRs)
- opencode is the AI runtime; web console controls everything

---

## 1. Goals & Non-Goals

### Goals
- Reduce daily Jira triage to a review-and-approve exercise.
- Fully configurable behavior without code changes: JQL queries, triage paths, agent
  instructions, schedule, action permissions.
- AI never mutates external systems; it only *proposes*. All mutations go through a
  deterministic executor after explicit human approval.
- Safe by default: bug-fix agents work in a throwaway sandbox; diffs + PRs are reviewed.

### Non-Goals (v1)
- Multi-user RBAC / team workflows.
- Non-GitHub code hosts (interface left open behind an ABC).
- Non-Jira trackers (interface left open behind an ABC).
- Fully autonomous execution without approval (deliberately excluded).

---

## 2. High-Level Architecture

```
                       ┌──────────────────────────────────────────┐
   Browser             │              WEB CONSOLE (SPA)           │
                       │  Dashboard · Approvals · Runs · Config   │
                       └──────────────────┬───────────────────────┘
                                          │ REST (JSON) + session auth
                       ┌──────────────────▼───────────────────────┐
                       │        ORCHESTRATOR (FastAPI)            │
                       │  runner.py     → pipeline state machine  │
                       │  scheduler.py  → APScheduler (cron)      │
                       │  paths.py      → triage-path registry    │
                       │  executor.py   → deterministic actions   │
                       │  db/ | models | schemas | auth           │
                       └───────┬─────────────┬─────────────┬──────┘
                               │             │             │
                     ┌─────────▼──┐   ┌──────▼──────┐   ┌───▼─────────────────┐
                     │ Jira Cloud │   │ GitHub API  │   │  opencode CLI       │
                     │  REST      │   │  + git      │   │  triage + action    │
                     └────────────┘   └─────────────┘   │  agents (headless)  │
                                                        └─────────────────────┘
            sandbox workspace: /tmp/assistant/<run>/<ticket>/<path>/ (temp clone)
```

**Two rails principle.** There are exactly two kinds of code in the system:

1. **AI rail** — opencode agents that read data and *produce proposals* (triage + action plans
   with previews). No outbound mutations; each agent gets a firewalled workspace.
2. **Deterministic rail** — the executor: small, tested, idempotent functions that turn an
   approved action plan into real API calls. No models in this rail, so behavior is
   reproducible and auditable.

---

## 3. Core Abstractions & Data Model

### 3.1 Entities

| Entity       | Description                                                              |
|--------------|--------------------------------------------------------------------------|
| `Run`        | One pipeline execution (scheduled or manual). Groups tickets processed.  |
| `Ticket`     | A Jira issue captured during a run, with fetched context.                |
| `Link`       | Context a ticket is enriched with: linked GitHub PR / commit / Jira dev-info. |
| `Path`       | A configurable triage destination (bug-fix, new-feature, ...). Files on disk. |
| `ActionPlan` | The AI-produced proposal for one ticket: narrative + ordered `Action`s.  |
| `Action`     | A single atomic, deterministic operation (comment / transition / assign / push_PR / ...). |

### 3.2 State Machines

**Run:** `queued → fetching → triaging → proposing → awaiting_approval → executing → completed`
(terminal: `completed`, `partial`, `failed`). A run is `executing` only while its wholly-approved
plans are being replayed.

**ActionPlan (per ticket):** `proposed → pending_review → approved | rejected | (edited→approved)`
terminal: `executed | failed`. One plan per ticket per run; a new run supersedes previous pending
plans of the same ticket (marked `superseded`).

**Ticket pipeline stage:** `incoming → triaged → proposed → awaiting_approval → executing → done`.

### 3.3 SQLite Schema (SQLAlchemy)

```
runs          id PK, trigger(text: manual|scheduled), status, jql, started_at, finished_at
tickets       id PK, run_id FK, key, project, summary, description, issue_type,
              status_name, link_counts, triage_path_id, triage_reason,
              triage_confidence, stage, created_at
links         id PK, ticket_id FK, kind(commit|pr|devinfo), source(GitHub|Jira-dev),
              url, title, sha, pr_state, meta_json
action_plans  id PK, ticket_id FK, run_id FK, path_id, summary, narrative,
              review_status, approved_at, executed_at, superseded_by
actions       id PK, plan_id FK, seq, kind, params_json, preview_json,
              exec_status(pending|ok|skipped|failed), exec_result_json
runs_config   key PK, value_json            -- app/UI settings (mirrored to files)
run_logs      id PK, run_id FK, level, ts, message, ticket_id nullable
```

Timeline of actions is preserved even when tickets are later edited, and `exec_result_json`
tracks Jira issue IDs / GitHub PR numbers so approvals are fully auditable.

---

## 4. Runtime Components (module inventory)

```
assistant/
  main.py            FastAPI app factory, middleware, startup (scheduler + path registry)
  db.py              engine, session, migrations (SQLAlchemy + Alembic-ready)
  models.py          ORM models
  schemas.py         Pydantic: API DTOs + AI output validation models
  auth.py            single-user login, session cookie, CSRF token, one-time setup token
  config.py          app settings, secrets loader, .env
  paths.py           triage-path registry (load/validate/reload)
  runner.py          pipeline orchestration, per-ticket state transitions
  triage.py          builds triage prompt, invokes opencode, validates JSON
  action_agent.py    builds action-agent prompt, workspace setup, validates plan
  executor.py        action-type → deterministic handler dispatch, retries
  scheduler.py       APScheduler jobs (cron nightly + on-run cleanup)
  integrations/
    jira.py          Jira Cloud REST client (search, get, comment, transition, assign, devinfo)
    github.py        GitHub REST (+ git) client (search, PR create, branch push)
    gitutil.py       temp-clone / branch / commit / diff helpers
web/
  static/index.html  SPA: vanilla JS + small CSS; diff viewer; no build step
paths/               triage-path plugins (loaded dynamically at runtime, see §5)
  bug-fix/           instruct.md, schema.json
  new-feature/       instruct.md, schema.json
  need-more-info/    instruct.md, schema.json
  need-my-input/     instruct.md, schema.json
config/
  settings.json      JQL list, schedule, integration endpoints/custom-fields
.gitignore           excludes .env, sqlite files, workspaces
```

No build step for the web UI keeps setup trivial for a single user; the SPA is served as
static files by FastAPI.

---

## 5. Configurable Triage Paths (the plugin system)

This is the heart of configurable behavior. A **path** is a self-describing folder on disk.
Everything the triage and action agents need — plus the executor's guardrails — comes from that
folder. Adding a new path = dropping a folder (or using the console editor). The next run loads it
with **no restart and no code change**.

### 5.1 Layout

```
paths/<path-id>/
  instruct.md     ← markdown guide; injected INTO triage prompt AND action-agent prompt
  schema.json     ← machine-readable contract for the executor + console
```

`path-id` is lowercase-kebab. Both files are validated on load; a folder failing validation is
skipped and reported in the run log (fail-open so one bad path never blocks the run).

### 5.2 instruct.md (authoring contract)

Structure conventions the agents rely on (framed as sections, not a DSL):

```markdown
# <Path display name>

## Purpose
When this path fits, in ~50 words.

## Triage criteria (bullet list)
- Strong signal: ...
- Weak signal: ...
- Anti-signal / when NOT to choose this path: ...

## Action guidance
How to approach the work once routed here (step-by-step for the action agent).

## Output requirements
Any format notes, quality bar, e.g. "explain the root cause in the PR body".
```

The console provides a template and a live "triage coverage check" — it runs an LLM to spot
overlapping/ambiguous criteria across paths and warns about gaps.

### 5.3 schema.json (contract)

```json
{
  "id": "bug-fix",
  "name": "Bug Fix",
  "enabled": true,
  "allowed_actions": ["comment", "transition", "assign", "create_pr", "push_branch"],
  "required_backend": "github",           // null = no code work needed
  "work": { "clone": true, "working_dir": "./repo", "max_new_files": 50 },
  "approval": { "require_pr_diff": true, "editable": ["comment", "assignee"] },
  "default_actions": [
    { "kind": "create_pr", "params": { "target_branch": "main" } }
  ]
}
```

- `allowed_actions` — whitelist; the executor rejects anything a path isn't allowed to emit,
  and the action agent is told this list so it stays in bounds.
- `required_backend` — null for chat-only paths (need-more-info); `github` reserves a
  sandbox clone for code paths.
- `approval.require_pr_diff` — forces the plan to include a diff preview before it can be
  shown as approvable.
- `default_actions` — seed actions the agent starts from (e.g. always propose a PR to `main`).

### 5.4 Built-in paths (v1)

| Path             | Chosen when                                                     | Typical proposed actions                |
|------------------|-----------------------------------------------------------------|-----------------------------------------|
| `bug-fix`        | Repro steps / stack trace / "does not work"; matches a recent commit | comment explaining root cause, transition, assign, push_branch + create_pr |
| `new-feature`    | Explicit feature/enhancement request, acceptance criteria       | comment w/ scoped proposal or clarification, transition, assign |
| `need-more-info` | Ticket too vague to act; questions unanswered                    | comment requesting info, transition back, assign |
| `need-my-input`  | Design/product decision, conflicting requirements, sensitive change | comment framing the decision, assign to me, transition |
| `out-of-scope`   | Non-actionable / done / no-op                                    | comment only (optional), close/skip       |

The registry (`paths.py`) loads folders fresh **every run**, so edits via console or editor apply
immediately. The console UI reads `schema.json` to render the right form controls per path.

---

## 6. End-to-End Workflow (per run)

### Stage 1 — Trigger & fetch
- **Scheduled**: APScheduler cron defined in config (default 02:00) — can be multiple JQL
  queries, each becomes its own sub-run or single run with grouped tickets.
- **Manual**: console button → POST `/api/runs` (optional JQL override).
- Runner executes each configured JQL via Jira `POST /rest/api/3/search/jql`. Tickets already
  handled successfully (closed, PR merged, or previously `done`) are skipped by JQL filters and
  a local dedupe on `key` from pending plans.
- A `Run` row with `status=queued` is created first; each fetch appends `tickets` rows and
  `run_logs` entries.

### Stage 2 — Context assembly (linked commits & PRs)
This makes triage effective for code-centric tickets:

- **Jira dev info**: pull the GitHub-integration field (configurable custom field id, default
  `customfield_10031` or empty if license lacks it) → normalized *devinfo* links.
- **GitHub reverse lookup**: for each ticket key, search GitHub commits
  `GET /search/commits?q=<KEY> in:commits` and PRs `GET /search/issues?q=<KEY> in:title` →
  *commit* / *pr* links. This works even without the Jira-GitHub app.
- Store links with meta (sha, pr_state: open/merged/closed, url, title) → `links` table, and
  add a compact digest to the prompt (see §7).

### Stage 3 — AI Triage (opencode → JSON)
- Build the triage prompt from: ticket summary/description/comments/status/links digest, plus
  the `instruct.md` + criteria of **every enabled path**.
- Invoke: `opencode run --agent triage --format json --dir <run-workspace> "<prompt>"`.
- Expected output (validated strictly):

```json
{ "path_id": "bug-fix",
  "confidence": 0.87,
  "reason": "Stack trace + repro steps; recent commit <sha> introduced the toucher.",
  "need_my_input": false }
```

- Each path is also scored (`candidate_paths: [{path_id, score}]`) so a low-scoring top pick
  or a near-tie between paths is flagged to the user rather than silently forced.
- If the triage output fails schema validation, retry once with the error appended, then mark
  `need-my-input` (human decides manual at the console; this is a feature, not a fallback only).
- `need_my_input=true` short-circuits: the plan narrative = the triage question, awaiting
  user decision (still fully approvable/editable).

### Stage 4 — Action agent (path routing)
- Workspace per path: code paths get `git clone --depth 1` of the configured GitHub repo into
  `<run-workspace>/<ticket>/<path>/repo`; chat-only paths get an empty cwd.
- Prompt = ticket context + `instruct.md` + `allowed_actions` + output contract; the agent is
  told to actually draft the comment(s), perform the sandboxed fix with its edit/git tools,
  run tests if present, and inspect the resulting diff.
- It emits an **action plan**, then a *verification pass* runs (schema-check `actions` against
  `allowed_actions`, non-empty diff when `require_pr_diff`, comment text length limits).

```json
{ "summary": "Fix null-deref in order total; add regression test",
  "narrative": "Root cause ...",
  "actions": [
    { "kind": "comment", "params": {"body": "Diagnosis: ..."} },
    { "kind": "transition", "params": {"to": "In Review"} },
    { "kind": "assign", "params": {"assignee": "me"} },
    { "kind": "push_branch", "params": {"base": "main","branch_name": "fix/order-total-null"} },
    { "kind": "create_pr", "params": {"title":"...","body":"...","target_branch":"main"} }
  ] }
```

`ActionPlan` + `Action` rows are persisted; previews (diff text, rendered comment) are put on
`actions.preview_json` so the console renders without re-running anything. A synthetic
`TriagePathType` is created inline if a new path's id ever appears (defensive).

### Stage 5 — Per-ticket approval (console)
- Dashboard groups tickets: held by most recent run, unactioned tickets sorted by
  `need_my_input` first, then confidence.
- One card per ticket: triage reason + confidence, links, narrative, numbered action list with
  **Preview** for each (inline diff with syntax highlight, comment rendered as it will post).
- Buttons per ticket: **Approve & execute**, **Reject**, **Edit** (edit comment text; change
  assignee; toggle any individual action on/off; reorder). Editable fields are declared in
  `schema.json.approval.editable`.
- Bulk approval only within a single ticket's actions — never "approve everything" globally,
  matching the per-ticket decision (bulk *dismiss* of a whole run's stale plans is allowed).

### Stage 6 — Deterministic execution
- Approved plan → executor replays its *enabled* actions **in order with dependency checks**
  (PR action requires the branch action to have succeeded).
- Each action maps to a small handler (see §9). If one action fails:
  - non-critical (comment): log + continue; mark plan `partial`.
  - critical (push/PR): stop remaining plan; mark `failed`; surface reason.
- Result is recorded on `actions.exec_result_json` (Jira issue IDs, PR numbers, transition
  ids) and the console shows a green/red execution timeline per ticket.
- Idempotency: every mutating GitHub/Jira call embeds a deterministic idempotency token
  (e.g. `assistant:<run>:<ticket>:<action-id>` in PR body/comment footer) so manual re-pushes
  never duplicate comments/PRs.

---

## 7. opencode as the AI Runtime

### 7.1 Invocation model

Agents run headless via the CLI, one fresh process per model step. This yields natural
sandboxing (tools scoped to a cwd) and crash isolation.

```bash
opencode run --agent triage \
  --dir /tmp/assistant/<run>/work \
  --format json --log-level ERROR \
  "Triage Jira ticket: <compact ticket JSON>"
```

- `--agent` selects a task-role agent (system prompt) defined in opencode config.
- `--format json` emits a structured event stream; the harness extracts the final assistant
  text, strips fences, and `json.loads` the payload.
- `--dir` scopes file/bash tools to the sandbox workspace. Secrets are passed only via env
  vars that integration clients read — never injected into prompts.
- `--log-level ERROR` keeps run output clean; `--print-logs` used in debug mode.
- Model per role is configurable (`triage.model`, `action.model`); default is cost-first:
  triage on a fast model, action agents on a stronger one.

### 7.2 Agent definitions (project `.opencode/agent/`)

| Agent | Role | Tools |
|-------|------|-------|
| `triage` | Classify a ticket into a path; return JSON only, no mutations | read, grep, webfetch (optional) |
| `action-worker` | Produce the Action Plan for a routed ticket | edit, write, bash (git/test), read, grep, glob |
| `path-editor` | Author/edit path `instruct.md` + `schema.json` from the console | read, write, bash (validate) |

Custom system prompts embed the pipeline output contract; per-path behavior comes from the
injected `instruct.md`, never from code. Because the worker fixes the same repos the assistant
lives in, path guides live in the repo and are editable from the console as artifacts.

### 7.3 Output contracts (Pydantic-validated)

- `TriageResult` and `ActionPlan` (both defined earlier) are the only shapes the harness
  accepts. Extraction: strip fences → `json.loads` → validate → on failure, retry once with
  the validation error message appended to the prompt, then fall back to `need-my-input`.
- Never trust token counts: plans are capped (`max_actions=20`), and every mutating action
  must carry `kind`, `params`, and a `preview`. Previews like diffs are generated
  **deterministically** by the harness (`git diff`), not by the model — what the user approves
  is byte-for-byte what later executes.

### 7.4 Cost & rate control

- Per run, model usage is logged; a budget guard (`max_agent_calls`, `max_tokens_estimate`)
  aborts a run early if exceeded.
- Jira/GitHub clients implement token-bucket rate limiting; a watchdog fails a plan stuck in
  `executing` past a configurable timeout and surfaces it on the dashboard.

---

## 8. Integration Layer (Jira + GitHub)

### 8.1 Jira (`integrations/jira.py`)

- Credentials via env: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`.
- Methods: `search(jql, fields)`, `get(key)`, `add_comment(key, body)`, `transition(key, to)`
  (discovered via GET `/transitions`), `assign(key, account_id)`, `devinfo(key)` reading the
  GitHub-for-Jira custom field (`config.jira.devinfo_field`, may be empty),
  `current_user()` to resolve `assignee: "me"`.
- Every call is logged with request/response ids. Transitions/assignments are gated by the
  path's `allowed_actions`. The final comment footer carries the run/ticket id + an approval
  marker so assistant activity is self-evident and attributable.

### 8.2 GitHub (`integrations/github.py` + `gitutil.py`)

- Token via `GITHUB_TOKEN`; read-only `search_commits(key)`, `search_prs(key)` for context
  assembly; `create_pr(owner, repo, ...)` for execution.
- Sandbox git flow (deterministic rail): `git clone --depth 1` → `git switch -c <branch>` →
  apply the same patch that was previewed (hash-verified) → `git push -u origin <branch>` →
  `create_pr`. The PR body is the approved `create_pr.params.body` verbatim, plus the
  idempotency marker so duplicate runs never create a second PR.

---

## 9. Deterministic Action Executor — action-type catalog

Each action kind maps to one small, unit-tested handler. Handlers never call a model; the
executor only ever receives an **approved** plan.

| kind          | Required params                     | Effect (deterministic)                     | Critical on failure |
|---------------|-------------------------------------|--------------------------------------------|---------------------|
| `comment`     | `body`                              | Jira `add_comment` (verbatim + footer)     | no                  |
| `transition`  | `to`                                | Jira transition if permitted               | no                  |
| `assign`      | `assignee` (`me` / account id)      | Jira assign                                | no                  |
| `push_branch` | `base`, `branch_name`               | push sandbox branch to GitHub              | yes                 |
| `create_pr`   | `title`, `body`, `target_branch`    | GitHub pull request                        | yes                 |

- Default order: `push_branch` → `create_pr`; other actions may run first.
- Re-validation right before execution: actions re-checked against the path whitelist and the
  patch re-hashed against the preview — closes the TOCTOU gap between review and execution.

---

## 10. Web Console (single user)

### 10.1 Pages (SPA, no build step)

| Route        | Purpose |
|--------------|---------|
| `/`          | Dashboard: active run, pending-approval count, error banners |
| `/approvals` | Per-ticket cards grouped by run; expand → plan + action previews; Approve / Reject / Edit |
| `/runs`      | Run history: status, tickets, triage + links, execution timeline per ticket |
| `/config`    | JQL list (+ "test search"), schedule (cron + manual run), integrations (host, token pointer, dev-info field), per-role model |
| `/paths`     | Path registry, editor for `instruct.md` / `schema.json` (form + JSON), validate & enable/disable |
| `/login`     | Single-user login (password from `ASST_ADMIN_PASSWORD`; first-run setup flow) |

### 10.2 REST API (excerpts)

```
POST /api/login  ·  POST /api/logout
POST /api/runs                      manual trigger (optional JQL override)
GET  /api/runs  ·  GET /api/runs/{id}  ·  GET /api/runs/{id}/tickets
GET  /api/approvals                 pending plans, one card per ticket
POST /api/approvals/{plan_id}/approve
POST /api/approvals/{plan_id}/reject
PUT  /api/approvals/{plan_id}       edit plan (only fields marked editable)
GET  /api/approvals/{plan_id}/diff  preview text/html
GET/PUT /api/config · GET/PUT /api/paths/{id} · POST /api/paths
GET  /api/health
```

All `/api/*` except `/api/login` and `/api/health` require the session cookie; state-changing
calls also validate a per-session CSRF token. Auth is a single bcrypt-hashed password (env or
first-run prompt), no third-party OIDC.

---

## 11. Security

| Area | Measure |
|------|---------|
| Secrets | Jira token, GitHub token, admin password live in the `.env` (gitignored) or a sealed env file; never in DB or prompts |
| Web console | Session cookie with `HttpOnly` + `SameSite=Lax`, CSRF token, rate-limited `/api/login` |
| AI isolation | Agents only get a sandbox dir (`--dir`); they cannot reach caller envs; API tokens are read by integration clients, not injected into agent context |
| Executor guardrails | `allowed_actions` whitelist per path, re-validated at execution time; patch hash re-checked: preview == execute |
| LLM output | Strict Pydantic validation + 1 retry; nothing mutates without a whitelisted, human-approved action |
| Audit | Every Jira/GitHub mutation logged with run/ticket/action ids; comment footer marks assistant postings |

---

## 12. Failure Handling & Observability

- **Per-ticket isolation**: a failing ticket never blocks the run; it's marked `failed` with
  the error and the run continues (status becomes `partial` if anything failed).
- **Retries**: idempotent API calls retry 3× with exponential backoff (418/429/5xx).
- **Watchdog**: stuck `executing` plans are flagged after a configurable timeout.
- **Logs**: `run_logs` per event with levels; a `Debug` toggle in the console streams the last
  run's raw agent output. Runs are kept indefinitely (small DB) and exportable as JSON.
- **Notifications (v2)**: optional email/Discord ping when a run completes with pending
  approvals or errors.

---

## 13. Extensibility

| Extension point | How |
|-----------------|-----|
| New triage path | Drop a folder in `paths/` (or console editor) → next run picks it up |
| New action type | Add a handler in `executor.py`, register kind, add default whitelist entry |
| New Jira field / status | Config entries (devinfo field, transitions map) via `/api/config` |
| New code host | Implement `integrations/base.py` ABC (`fetch_context`, `create_pr`, `push`) |
| Model preferences | Per-role model config; opencode provider config |
| UI flow | SPA pages own their own components; no build step to preview |

### Roadmap

- **v0.1** — Skeleton: FastAPI + SQLite + SPA with login, config, runs + manual trigger,
  hardcoded JQL path; triage agent wired to opencode; per-ticket approve/reject/execute for
  chat-only actions (comment/transition/assign).
- **v0.2** — Bug-fix path end to end: sandbox clone, diff preview UI, push_branch + create_pr,
  edit-plan interaction, run history timeline.
- **v0.3** — GitHub context enrichment (commits/PRs by key), scoring ties, budget guard,
  watchdog, path editor from console.
- **v1.0** — All four built-in paths, notifications, export, hardening.

---

## 14. Open Questions (to resolve during v0.1)

1. Which Jira transitions per project should the assistant be allowed to use (whitelist per
   project/issue-type)?
2. Jira-GitHub "Development" custom field availability — if absent, GitHub reverse lookup is
   the fallback; confirm the Jira plan exposes it or we rely on search.
3. Should `push_branch`/`create_pr` be a single combined `create_pr` action that also pushes,
   to reduce approval surface?
4. Where does the assistant's own GitHub repo live (it is also its first bug-fix subject)?
5. Who is `assignee: "me"` — bind to the Jira user whose token is configured (recommended).