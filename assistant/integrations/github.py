"""GitHub REST lookups (context enrichment). Mock variant for dev runs."""
from __future__ import annotations

import logging

import requests

REQUEST_TIMEOUT = 30
log = logging.getLogger("assistant.github")


class GitHubError(Exception):
    pass


class GitHubClient:
    def __init__(self, repo: str, token: str = ""):
        self.repo = repo.strip().lstrip("/")
        if not self.repo:
            raise GitHubError("GitHub repo 'owner/name' is required (ASST_GITHUB_REPO).")
        self.headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = requests.get(f"https://api.github.com{path}", headers=self.headers, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code >= 400:
            raise GitHubError(f"GitHub {r.status_code}: {r.text[:400]}")
        return r.json()

    def search_commits(self, key: str, limit: int = 10) -> list[dict]:
        """Commits mentioning the Jira key, in this repo."""
        if not self.repo:
            return []
        q = f"repo:{self.repo} type:commits {key}"
        data = self._get("/search/commits", {"q": q, "per_page": limit})
        out = []
        for item in data.get("items", []):
            commit = item.get("commit", {})
            out.append({
                "kind": "commit", "source": "GitHub",
                "url": item.get("html_url", ""),
                "sha": (item.get("sha", "") or "")[:12],
                "title": (commit.get("message", "") or "").splitlines()[0][:200],
            })
        return out

    def search_prs(self, key: str, limit: int = 10) -> list[dict]:
        """PRs mentioning the Jira key in title or body, in this repo."""
        if not self.repo:
            return []
        q = f"repo:{self.repo} type:pr {key} in:title,body"
        data = self._get("/search/issues", {"q": q, "per_page": limit})
        out = []
        for item in data.get("items", []):
            out.append({
                "kind": "pr", "source": "GitHub",
                "url": item.get("html_url", ""),
                "title": item.get("title", ""),
                "pr_state": item.get("state", ""),
                "meta": {"number": item.get("number")},
            })
        return out

    def open_issues(self, limit: int = 50) -> list[dict]:
        data = self._get("/search/issues", {
            "q": f"repo:{self.repo} is:issue is:open",
            "sort": "updated",
            "order": "desc",
            "per_page": min(limit, 100),
        })
        out = []
        for item in data.get("items", []):
            out.append({"key": f"GH:{self.repo}#{item.get('number')}", "project": self.repo, "repo": self.repo, "summary": item.get("title", ""), "description": item.get("body") or "", "issue_type": "GitHub issue", "status_name": item.get("state", "open"), "number": item.get("number"), "url": item.get("html_url", ""), "labels": [x.get("name", "") for x in item.get("labels", [])]})
        return out

    def add_issue_comment(self, number: int, body: str) -> str:
        r = requests.post(f"https://api.github.com/repos/{self.repo}/issues/{number}/comments", headers=self.headers, json={"body": body}, timeout=REQUEST_TIMEOUT)
        if r.status_code >= 400:
            raise GitHubError(f"add_issue_comment {r.status_code}: {_gh_error(r)}")
        return r.json().get("html_url", "")

    def create_pr(self, head: str, base: str, title: str, body: str = "") -> dict:
        """Create a pull request. Requires `head` branch to already be pushed."""
        payload = {"title": title, "head": head, "base": base, "body": body or ""}
        r = requests.post(f"https://api.github.com/repos/{self.repo}/pulls",
                          headers=self.headers, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code >= 400:
            raise GitHubError(f"create_pr {r.status_code}: {_gh_error(r)}")
        data = r.json()
        return {"number": data.get("number"), "html_url": data.get("html_url")}

    def close_pr(self, number: int) -> None:
        r = requests.patch(f"https://api.github.com/repos/{self.repo}/pulls/{number}",
                           headers=self.headers, json={"state": "closed"}, timeout=REQUEST_TIMEOUT)
        if r.status_code >= 400:
            raise GitHubError(f"close_pr {r.status_code}: {_gh_error(r)}")

    def delete_branch(self, branch: str) -> None:
        """Delete a remote branch via the git refs API (leaves the PR/commits intact)."""
        r = requests.delete(f"https://api.github.com/repos/{self.repo}/git/refs/heads/{branch}",
                            headers=self.headers, timeout=REQUEST_TIMEOUT)
        if r.status_code not in (204, 404):
            raise GitHubError(f"delete_branch {r.status_code}: {_gh_error(r)}")


class MockGitHubClient:
    def __init__(self, repo: str = ""):
        self.repo = repo
        self.pushed: list[tuple[str, str]] = []
        self.prs: list[dict] = []
        self.issue_comments: list[tuple[int, str]] = []

    def open_issues(self, limit: int = 50) -> list[dict]:
        issues = [
            {"number": 1, "summary": "Fix the empty search state on the demo page", "description": "When a search has no matches, the results panel stays blank instead of showing the empty-state message. Reproduction steps and the affected component are included in the issue.", "labels": ["good first issue", "bug", "javascript"]},
            {"number": 2, "summary": "Document the local development workflow", "description": "Add concise setup and test instructions for first-time contributors, including the mock-mode workflow.", "labels": ["documentation", "good first issue"]},
            {"number": 3, "summary": "Add CSV export for filtered results", "description": "Users need to export the currently filtered result set as CSV. The issue describes expected columns and acceptance criteria.", "labels": ["enhancement", "help wanted"]},
            {"number": 4, "summary": "Clarify the desired mobile navigation behavior", "description": "The issue reports that the mobile navigation feels confusing but does not yet define the intended interaction or design constraints.", "labels": ["needs discussion", "ux"]},
        ]
        return [
            {"key": f"GH:{self.repo}#{issue['number']}", "project": self.repo, "repo": self.repo,
             "summary": issue["summary"], "description": issue["description"], "issue_type": "GitHub issue",
             "status_name": "open", "number": issue["number"],
             "url": f"https://github.com/{self.repo}/issues/{issue['number']}", "labels": issue["labels"]}
            for issue in issues[:limit]
        ]

    def add_issue_comment(self, number: int, body: str) -> str:
        self.issue_comments.append((number, body))
        return f"https://github.com/{self.repo}/issues/{number}#issuecomment-{len(self.issue_comments)}"
    def search_commits(self, key: str, limit: int = 10) -> list[dict]:
        if key == "DEMO-1":
            return [{"kind": "commit", "source": "GitHub", "url": "https://github.com/x/y/commit/abc", "sha": "abc123456789",
                     "title": "feat(checkout): add free shipping coupon"}]
        return []

    def search_prs(self, key: str, limit: int = 10) -> list[dict]:
        if key == "DEMO-2":
            return [{"kind": "pr", "source": "GitHub", "url": "https://github.com/x/y/pull/12", "title": "WIP: CSV export",
                     "pr_state": "open", "meta": {"number": 12}}]
        return []

    def create_pr(self, head: str, base: str, title: str, body: str = "") -> dict:
        n = len(self.prs) + 1
        self.prs.append({"head": head, "base": base, "title": title, "body": body, "number": n})
        return {"number": n, "html_url": f"https://github.com/{self.repo}/pull/{n}"}


def build_github(settings, repo: str | None = None) -> GitHubClient | MockGitHubClient:
    repo = repo or settings.github_repo
    if settings.mock:
        return MockGitHubClient(repo)
    return GitHubClient(repo, settings.github_token)


def configured_repos(settings) -> list[str]:
    repos = list(settings.github_repos or [])
    if not repos and settings.github_repo:
        repos = [settings.github_repo]
    if settings.mock and not repos:
        repos = ["owner/demo-repo-a", "owner/demo-repo-b"]
    return repos


def search_context(settings, key: str, limit: int = 10) -> list[dict]:
    """Search commits+PRs mentioning `key` across all configured repos.

    Each returned link is tagged with the repo it was found in ("repo").
    """
    repos = configured_repos(settings)
    if settings.mock:
        return _mock_search_context(settings, key, repos)
    out: list[dict] = []
    for repo in repos:
        try:
            c = build_github(settings, repo)
            for link in c.search_commits(key, limit):
                link["repo"] = repo
                out.append(link)
            for link in c.search_prs(key, limit):
                link["repo"] = repo
                out.append(link)
        except GitHubError as e:
            log.warning("repo search %s: %s", repo, e)
    return out


def _mock_search_context(settings, key: str, repos: list[str]) -> list[dict]:
    repo_a = repos[0] if repos else "owner/demo-repo-a"
    repo_b = repos[1] if len(repos) > 1 else repo_a
    if key == "DEMO-1":
        return [{"kind": "commit", "source": "GitHub", "repo": repo_a,
                 "url": f"https://github.com/{repo_a}/commit/abc123456789", "sha": "abc123456789",
                 "title": "feat(checkout): add free shipping coupon"}]
    if key == "DEMO-2":
        return [{"kind": "pr", "source": "GitHub", "repo": repo_b,
                 "url": f"https://github.com/{repo_b}/pull/12", "title": "WIP: CSV export",
                 "pr_state": "open", "meta": {"number": 12}}]
    return []


def _gh_error(r: requests.Response) -> str:
    try:
        j = r.json()
        msg = j.get("message", "")
        errors = j.get("errors")
        if errors:
            parts = [f"{e.get('field', '')}: {e.get('message', e)}".strip(": ") for e in errors]
            if parts:
                msg = f"{msg} ({'; '.join(parts)})"
        return msg[:300]
    except ValueError:
        return r.text[:300]
