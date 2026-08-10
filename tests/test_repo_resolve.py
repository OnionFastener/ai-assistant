"""Per-ticket repo resolution ordering tests."""
from assistant.repo_resolve import repo_from_ticket_text, resolve_repo


def test_repo_from_ticket_url():
    assert repo_from_ticket_text(["See https://github.com/acme/shop/issues/3"]) == "acme/shop"


def test_repo_from_ticket_ignores_confusing_segments():
    assert repo_from_ticket_text(["https://github.com/owner/issues/3"]) == ""
    assert repo_from_ticket_text(["https://github.com/owner/pulls/3"]) == ""


def test_repo_from_ticket_strips_git_suffix():
    assert repo_from_ticket_text(["git@github.com:acme/shop.git"]) == "acme/shop"


def test_repo_url_takes_precedence():
    r = resolve_repo("X-1", "PROJ", "fix in https://github.com/acme/shop/issues/1", "no",
                     [], [], repo_map={"X-1": "acme/shop"})
    assert r == "acme/shop"


def test_repo_map_key_override():
    r = resolve_repo("X-1", "PROJ", "s", "d", [], ["first/repo"],
                     repo_map={"X-1": "mapped/repo"})
    assert r == "mapped/repo"


def test_repo_map_default():
    r = resolve_repo("X-1", "PROJ", "s", "d", [], ["first/repo"],
                     repo_map={"default": "fallback/repo"})
    assert r == "fallback/repo"


def test_project_map():
    r = resolve_repo("X-1", "PROJ", "s", "d", [], ["first/repo"],
                     project_map={"PROJ": "proj/repo"})
    assert r == "proj/repo"


def test_linked_commit_evidence():
    links = [{"source": "GitHub", "repo": "a/repo", "kind": "commit"},
             {"source": "GitHub", "repo": "b/repo", "kind": "commit"},
             {"source": "GitHub", "repo": "a/repo", "kind": "pr"}]
    r = resolve_repo("X-1", "PROJ", "s", "d", links, ["first/repo"], project_map={})
    assert r == "a/repo"


def test_first_configured_repo_fallback():
    assert resolve_repo("X-1", "PROJ", "s", "d", [], ["first/repo", "second/repo"]) == "first/repo"


def test_no_repos_empty():
    assert resolve_repo("X-1", "PROJ", "s", "d", [], []) == ""


def test_non_github_links_ignored():
    links = [{"source": "Jira-dev", "repo": "x/repo", "kind": "commit"}]
    assert resolve_repo("X-1", "PROJ", "s", "d", links, ["first/repo"]) == "first/repo"