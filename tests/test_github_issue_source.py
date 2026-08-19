from conftest import wait_until

from assistant.integrations.github import GitHubClient


def test_live_github_issue_source_uses_issue_only_search(monkeypatch):
    calls = []

    def fake_get(self, path, params=None):
        calls.append((path, params))
        return {"items": [{"number": 7, "title": "A real issue", "body": "details",
                           "state": "open", "html_url": "https://example.test/issues/7",
                           "labels": [{"name": "bug"}]}]}

    monkeypatch.setattr(GitHubClient, "_get", fake_get)
    issues = GitHubClient("usestrix/strix").open_issues(5)

    assert calls == [("/search/issues", {
        "q": "repo:usestrix/strix is:issue is:open", "sort": "updated", "order": "desc", "per_page": 5,
    })]
    assert issues[0]["key"] == "GH:usestrix/strix#7"


def test_github_issue_source_can_run_without_jira(authed):
    client = authed["client"]
    config = client.get("/api/config").json()
    config["sources"] = {
        "jira": {"enabled": False},
        "github_issues": {"enabled": True, "repos": ["demo/mock-repo"]},
    }
    saved = client.put("/api/config", json=config, headers={"X-CSRF": authed["csrf"]})
    assert saved.status_code == 200

    run_id = client.post("/api/runs", json={}, headers={"X-CSRF": authed["csrf"]}).json()["run_id"]
    assert wait_until(lambda: next(run for run in client.get("/api/runs").json() if run["id"] == run_id)["status"] in ("completed", "partial", "failed"))

    tickets = client.get(f"/api/runs/{run_id}/tickets").json()
    assert [ticket["key"] for ticket in tickets] == [f"GH:demo/mock-repo#{number}" for number in range(1, 5)]
    assert tickets[0]["repo"] == "demo/mock-repo"
    config["sources"] = {"jira": {"enabled": True}, "github_issues": {"enabled": False, "repos": []}}
    assert client.put("/api/config", json=config, headers={"X-CSRF": authed["csrf"]}).status_code == 200
