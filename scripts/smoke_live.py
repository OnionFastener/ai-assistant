#!/usr/bin/env python3
"""Live smoke test for AI Assistant GitHub + Jira integrations.

Read-only by default. Pass --write to exercise the full push→PR path and optionally
a reversible Jira comment. Never leaves artifacts: the smoke PR is closed and its
branch deleted; any Jira comment is removed after posting.

Usage:
  export ASST_MOCK=0
  export ASST_GITHUB_REPO=owner/name
  export ASST_GITHUB_TOKEN=...
  export ASST_JIRA_BASE_URL=...   # for the Jira steps
  export ASST_JIRA_EMAIL=...
  export ASST_JIRA_API_TOKEN=...
  python3 scripts/smoke_live.py                     # read-only (clone + search)
  python3 scripts/smoke_live.py --write             # + branch push + PR + cleanup
  python3 scripts/smoke_live.py --write --jira-key SMOKE-1   # + reversible comment
"""
import os
import sys
import time
import shutil
from pathlib import Path

os.environ["ASST_MOCK"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WRITE = "--write" in sys.argv
JIRA_KEY = None
if "--jira-key" in sys.argv:
    JIRA_KEY = sys.argv[sys.argv.index("--jira-key") + 1]

results = []


def step(name, fn, required=False):
    try:
        out = fn()
        results.append(("ok", name))
        if out:
            print(f"  ✔ {name}: {out}")
        else:
            print(f"  ✔ {name}")
    except Exception as e:  # noqa: BLE001
        results.append(("warn" if not required and not WRITE else "fail", name))
        print(f"  ✘ {name}: {type(e).__name__}: {e}")


def main() -> int:
    from assistant.config import settings
    from assistant.integrations import build_github, build_jira

    print("== AI Assistant live smoke (mock=%s, write=%s) ==" % (settings.mock, WRITE))
    if settings.mock:
        print("ERROR: ASST_MOCK=0 was not honoured (env must be exported before Python runs).")
        return 2
    if not settings.github_repo:
        print("\nMissing ASST_GITHUB_REPO (owner/name). Set it in .env / env and re-run.")
        return 2

    ws = Path("/tmp/assistant/smoke-live")
    shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True, exist_ok=True)
    dest = ws / "repo"
    github = build_github(settings)

    # read only
    step("GitHub: clone repo", lambda: _clone_desc(settings, dest))
    step("GitHub: search commits/PRs for SMOKE-LIVE", lambda: _search_desc(github))
    step("Jira: search configured JQL (first query)", lambda: _jira_search_desc(settings), required=False)

    if WRITE:
        if not settings.github_token:
            print("\n--write needs ASST_GITHUB_TOKEN with repo scope.")
            return 2
        step("GitHub: push branch + open PR + clean up",
             lambda: _write_flow(github, settings, dest), required=True)

        if JIRA_KEY:
            step(f"Jira: comment on {JIRA_KEY} (added then removed)",
                 lambda: _jira_comment_flow(settings, JIRA_KEY), required=True)
        else:
            print("  (skipping Jira write — pass --jira-key KEY to test a reversible comment)")

    bad = sum(1 for s, _ in results if s == "fail")
    bad += sum(1 for s, _ in results if s == "warn")
    print(f"\n== {len(results)} step(s), {bad} issue(s; warnings may be missing creds) ==")
    return 1 if any(s == "fail" for s, _ in results) else 0


def _clone_desc(settings, dest):
    from assistant.integrations import gitutil
    gitutil.clone_repo(settings, dest)
    branch = gitutil.default_branch(settings, dest)
    return f"cloned {settings.github_repo} → {dest} (on {branch})"


def _search_desc(github):
    commits = github.search_commits("SMOKE-LIVE")
    prs = github.search_prs("SMOKE-LIVE")
    return f"{len(commits)} commits, {len(prs)} PRs mention SMOKE-LIVE"


def _jira_search_desc(settings):
    from assistant.integrations import build_jira

    if not (settings.jira_base_url and settings.jira_api_token):
        raise RuntimeError("Jira not configured (ASST_JIRA_BASE_URL + ASST_JIRA_API_TOKEN)")
    jira = build_jira(settings)
    q = (settings.jql_queries or [{}])[0].get("jql", "")
    if not q:
        raise RuntimeError("no JQL configured in config/settings.json")
    issues = jira.search(q, max_results=3)
    return ", ".join(f"{i['key']} {i['summary'][:40]}" for i in issues) or "no issues"


def _write_flow(github, settings, dest):
    from assistant.integrations import gitutil
    ts = int(time.time())
    branch = f"ai-assistant/smoke-{ts}"
    sentinel = dest / f"smoke-{ts}.txt"
    sentinel.write_text(f"AI assistant live smoke test ({ts})\n"
                        "This branch is removed by the smoke script.\n")
    run_git = gitutil.run_git
    run_git(dest, "checkout", "-B", branch)
    run_git(dest, "add", "-A")
    run_git(dest, "commit", "-m", f"chore: AI assistant smoke test {ts}")
    gitutil.push_branch(dest, branch)

    base = gitutil.default_branch(settings, dest)
    pr = github.create_pr(head=branch, base=base,
                          title=f"AI Assistant smoke test {ts}",
                          body="Automated smoke test — opened, then closed and cleaned up.")
    github.close_pr(pr["number"])
    github.delete_branch(branch)
    sentinel.unlink(missing_ok=True)
    return f"PR #{pr['number']} {pr['html_url']} opened, then closed; branch '{branch}' deleted"


def _jira_comment_flow(settings, key):
    jira = build_jira(settings)
    cid = jira.add_comment(key, "AI Assistant live smoke test (added and removed automatically).")
    jira.delete_comment(key, cid)
    return f"comment {cid} posted and removed on {key}"


if __name__ == "__main__":
    raise SystemExit(main())