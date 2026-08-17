# AI Assistant

Personal, single-user assistant that scans selected work sources, AI-triages each item
into configurable paths, drafts an Action Plan, waits for human approval in a web console,
and deterministically executes approved actions.

It supports Jira tickets and public GitHub Issues as independent, selectable inbound sources.

## How it works

1. **Select sources** — in **Config**, enable Jira, public GitHub Issues, or both. GitHub
   Issue repositories are configured separately from repositories used for code context and PRs.
2. **Scan** — runs configured JQL queries and/or scans open issues in the selected public
   GitHub repositories, on a schedule or on demand.
3. **Triage** — every inbound item follows the same opencode triage paths (`bug-fix`,
   `new-feature`, `need-more-info`, …), each with its own instructions and action allowlist.
4. **Plan** — for code paths, an action agent clones the linked repository into a sandbox,
   captures its proposed patch, and drafts a reviewable Action Plan.
5. **Approve and execute** — approved actions run deterministically. GitHub-origin items
   comment on GitHub when approved; Jira-only assignment and transition actions are blocked
   for GitHub Issues. Patch hashes are re-verified before application.

## Source configuration

Use the Config page to toggle **Scan Jira issues** and **Scan public GitHub Issues**. Enter
one public GitHub repository (`owner/name`) per line for GitHub Issue scanning. In live mode,
`ASST_GITHUB_TOKEN` raises the GitHub API rate limit; it needs public repository read access
for scanning. Environment equivalents are `ASST_SCAN_JIRA`, `ASST_SCAN_GITHUB_ISSUES`, and
`ASST_GITHUB_ISSUE_REPOS` (comma-separated).

## Screenshots

| Login | Console (after a run) |
|---|---|
| ![Login](img/login.png) | ![Dashboard](img/dashboard.png) |

## Quickstart (mock mode, zero credentials)

```bash
set -a; . ./.env; set +a          # set ASST_MOCK=1 first (edit .env)
python3 -m uvicorn assistant.main:app --port 8010
```

Open http://127.0.0.1:8010 — login with the password in `config/settings.json` / `.env`.
Mock mode ships the Jira demo tickets, a mock public GitHub Issue when that source is enabled,
and local bare-git remotes so clone → patch → push → PR workflows run without network access.

**Live mode:** set `ASST_MOCK=0` and provide credentials only for enabled sources. Jira needs
its base URL, email, and API token; GitHub Issue scanning needs `ASST_GITHUB_TOKEN` for practical
rate limits. GitHub code actions and PRs also use `ASST_GITHUB_REPOS` and `ASST_GITHUB_TOKEN`.
The app does **not** auto-load `.env`; export it first, e.g. `set -a; . ./.env; set +a`.

## Verify / test

```bash
python3 -m compileall -q assistant/                              # syntax gate
set -a; . ./.env; set +a; python3 scripts/smoke_live.py          # read-only health check
```

Full mock E2E lives outside the repo at `/tmp/opencode-asst/e2e.py` and `e2e_v2.py`
(headless via FastAPI `TestClient`).

## Layout

- `assistant/` — FastAPI app, runner pipeline, triage + action agents, deterministic executor,
  integrations (Jira, GitHub, git).
- `paths/<id>/` — per-path `instruct.md` + `schema.json`, `allowed_actions` whitelist.
- `config/` — `settings.json` (JQL, schedule, non-secret settings) + `repo_map.json` (repo overrides).
- `web/static/` — vanilla-JS SPA served at `/`.
- `.opencode/agent/` — opencode agents (`triage`, `action-worker`).

## Conventions

- Deterministic execution — the executor must never call a model; AI is used only for
  triage and fixing, before human approval.
- Secrets live only in `.env` (gitignored); env vars are `ASST_*` and override
  `config/settings.json`.
- See `AGENTS.md` for detailed architecture notes and gotchas.
