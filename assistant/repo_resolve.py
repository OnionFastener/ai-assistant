"""Per-ticket GitHub repo resolution.

Order (first match wins):
  1. A github.com URL mentioned in the ticket summary/description.
  2. Manual override from config/repo_map.json (keyed by ticket key, or "default").
  3. Jira project -> repo mapping from config.
  4. The repo where the ticket's linked commits/PRs were found.
  5. The first configured repo.
"""
from __future__ import annotations

import re

_REPO_FROM_URL = re.compile(
    r"(?:https?://|git@)?(?:www\.)?github(?:\.\w+)?[.:/]"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"
    r"(?:\.git)?(?:\b|/|#|\?|$)"
)

_CONFUSING = {"issues", "pull", "pulls", "tree", "blob", "releases", "commits", "raw", "archive"}


def repo_from_ticket_text(parts: list[str]) -> str:
    """Find the first GitHub 'owner/name' referenced in free-form ticket text."""
    for part in parts:
        if not part:
            continue
        for m in _REPO_FROM_URL.finditer(part):
            owner, name = m.group(1), m.group(2)
            if owner in _CONFUSING or name in _CONFUSING:
                continue
            name = name[:-4] if name.endswith(".git") else name
            repo = f"{owner}/{name}"
            return repo
    return ""


def resolve_repo(key: str, project: str, summary: str, description: str,
                 links: list[dict] | None, repos: list[str],
                 repo_map: dict | None = None, project_map: dict | None = None) -> str:
    """Pick the GitHub repo a ticket belongs to, per the order in the docstring."""
    sum_desc = sorted((summary or "", description or ""), key=len, reverse=True)

    # 1. GitHub URL written in the ticket itself.
    url_repo = repo_from_ticket_text(sum_desc)
    if url_repo:
        return url_repo

    # 2. Manual per-ticket / default override (context file).
    repo_map = repo_map or {}
    if key in repo_map:
        return str(repo_map[key])
    if repo_map.get("default"):
        return str(repo_map["default"])

    # 3. Jira project -> repo mapping.
    project_map = project_map or {}
    if project and project_map.get(project):
        return str(project_map[project])

    # 4. Evidence from linked commits/PRs.
    counts: dict[str, int] = {}
    for link in links or []:
        if link.get("source") == "GitHub" and link.get("repo"):
            counts[str(link["repo"])] = counts.get(str(link["repo"]), 0) + 1
    if counts:
        return max(counts, key=counts.get)

    # 5. First configured repo.
    return repos[0] if repos else ""