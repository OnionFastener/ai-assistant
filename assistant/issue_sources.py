"""Inbound Jira and GitHub Issue source collection."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .integrations import build_github


def fetch_tickets(settings, jira, jql_override: str | None, emit) -> dict[str, dict]:
    tickets: dict[str, dict] = {}
    if settings.scan_jira:
        if jira is None:
            raise RuntimeError("Jira source is enabled but unavailable")
        queries = ([{"name": "override", "jql": jql_override}] if jql_override else settings.jql_queries or [])
        for query in queries:
            label, jql = query.get("name", "?"), query.get("jql", "")
            if not jql:
                continue
            emit(f"searching JQL '{label}'")
            for ticket in jira.search(jql, max_results=settings.max_tickets_per_run + 50):
                tickets.setdefault(ticket["key"], ticket)
    if settings.scan_github_issues:
        repos = list(settings.github_issue_repos)
        for repo in repos:
            emit(f"searching GitHub issues in '{repo}'")
        with ThreadPoolExecutor(max_workers=min(4, len(repos) or 1)) as pool:
            searches = {
                repo: pool.submit(build_github(settings, repo).open_issues, settings.max_tickets_per_run + 50)
                for repo in repos
            }
            for repo in repos:
                for ticket in searches[repo].result():
                    tickets.setdefault(ticket["key"], ticket)
    if not tickets and not (settings.scan_jira or settings.scan_github_issues):
        raise RuntimeError("No issue source is enabled")
    return tickets
