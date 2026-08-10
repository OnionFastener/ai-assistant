# AGENTS.md — AI Assistant

Personal, single-user assistant that nightly scans Jira tickets, AI-triages them into
configurable "paths" via opencode, drafts an Action Plan per ticket, waits for human
approval in a web console, then deterministically executes approved actions against
Jira and GitHub.

## Stack
- Python 3.12, FastAPI + SQLAlchemy (SQLite) + APScheduler, vanilla-JS SPA (no build step).
- opencode 1.18.16 (`~/.opencode/bin/opencode`) is the AI runtime; agents in `.opencode/agent/`.
- Env: no sudo / venv / ensurepip. Deps live in user site-packages (`pip install --user --break-system-packages`).
  Install a dep with: `python3 -m pip install --user --break-system-packages <pkg>`.

## Quickstart
Mock mode needs zero credentials:
```
set -a; . ./.env; set +a          # ASST_MOCK=1 default in config, but .env says 0 — edit first
python3 -m uvicorn assistant.main:app --port 8010
```
Then open http://127.0.0.1:8010 (login: `change-me`). Mock = 5 DEMO tickets + a real
local bare-git remote so clone→patch→push→PR work with no network.

Live mode: `ASST_MOCK=0` plus real creds in `.env` (Jira base/email/api-token,
`ASST_GITHUB_REPOS` + `ASST_GITHUB_TOKEN`). The app does **not** auto-load `.env` —
export it first or use `set -a; . ./.env; set +a`.

## Verify / test
- Syntax gate: `python3 -m compileall -q assistant/`
- Read-only live health check: `set -a; . ./.env; set +a; python3 scripts/smoke_live.py`
  (`--write` pushes a branch + opens/closes a PR; `--jira-key KEY` adds then removes a comment)
- Full mock E2E lives OUTSIDE the repo: `/tmp/opencode-asst/e2e.py` (chat flows + config/paths)
  and `/tmp/opencode-asst/e2e_v2.py` (bug-fix sandbox → push → PR). Both run headless via
  FastAPI `TestClient` (no server needed). They remove `paths/copy-request`, `assistant.db`,
  and `/tmp/assistant/run-*` — re-clean those after running.
- The opencode CLI is exercised directly: `opencode run --agent triage --format json --dir <cwd> '<prompt>'`.

## Architecture map
- `assistant/main.py` — FastAPI app + all REST endpoints (login/logout, runs, approvals incl
  `GET/PUT /api/approvals/{id}`, approve/reject, config, paths, `GET/PUT /api/repo-map`, `/api/session`).
  SPA served at `/` from `web/static/`.
- `assistant/runner.py` — pipeline: fetch JQL → context (links across repos) → per-ticket repo
  resolution → triage → plan. In-process; run in a daemon thread.
- `assistant/repo_resolve.py` — per-ticket GitHub repo selection, in order: GitHub URL found in the
  ticket body → `config/repo_map.json` override (key or `default`) → project→repo map → repo where
  linked commits/PRs live → first configured repo.
- `assistant/triage.py` — prompt builder + opencode invoke; live failures escalate to `need-my-input`.
- `assistant/action_agent.py` — code path: sandbox clone of the resolved repo (mock = per-repo local
  bare remote), action-worker or mock fix, captures `git diff` as the plan's `patch`.
- `assistant/executor.py` — deterministic handlers (comment/transition/assign/push_branch/create_pr).
  Patch hash re-verified at execute time (`apply_patch` checks applied diff == stored patch).
- `assistant/opencode_runner.py` — `run_agent` + JSON event-stream text extractor.
- `assistant/integrations/gitutil.py` — git plumbing (see gotchas).
- `assistant/integrations/jira.py` / `github.py` — live clients + mock variants.
- `paths/<id>/` — `instruct.md` + `schema.json` per triage path; `allowed_actions` whitelist.
- `config/settings.json` — JQL, schedule, tokens' non-secret parts. `config/repo_map.json` — per-ticket
  repo overrides. Secrets live only in `.env`.

## Critical gotchas (read before editing)
- `gitutil.run_git` strips stdout by default. Diffs MUST be captured with `strip=False` or
  `git apply` fails with "corrupt patch at line 9" (trailing newline chopped).
- Mock remotes are per-repo and idempotent: `setup_mock_repo(base, remote_name(repo))` →
  `base/<name>.git`. The executor locates them via `remote_name(params["repo"])`. Mock default repos:
  `["owner/demo-repo-a", "owner/demo-repo-b"]`.
- Jira Cloud rejects plain-string comment bodies — always send ADF via `_to_adf()`.
  Jira search must hit `POST /rest/api/3/search/jql` (old `/search` is removed).
- opencode `--format json` streams `step_start`/`text`/`step_finish` events. The final assistant
  text lives in **`text`** events (handled in `extract_text`); missing it silently escalates every
  ticket to `need-my-input`.
- `gitutil.default_branch` resolves the remote default via `refs/remotes/origin/HEAD` — never the
  current branch (callers check out feature branches before asking).
- Auth: password → in-memory session token cookie + CSRF echo on mutations. Broken-cookie-on-
  discarded-response was a past bug: `auth.set_session_cookie` mutates the returned JSONResponse.
- SPA hash routing: parse with `location.hash.replace(/^#\/?/, "")`; naive `.slice(1).split("/")`
  yields an empty route and silently renders the dashboard for every path (fixed, don't regress).
- The bash tool here hangs when "backgrounding" uvicorn; detach with
  `setsid nohup … & disown` + redirect + `</dev/null` (still flaky — verify the PID separately),
  or drive the app via FastAPI `TestClient` in-process.
- Large `write`/`edit` payloads truncate around ~8 KB — write files in chunks if > that.

## Conventions
- Code: no new comments unless asked; match surrounding style; keep handlers deterministic
  (execution must NEVER call a model).
- Never commit secrets. `.env` is gitignored (also why it's hidden in VSCode explorer).
- Env vars are `ASST_*` and override `config/settings.json`; secrets' existence is reported as
  `token_set` booleans, never echoed.