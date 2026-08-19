"""gitutil tests. Real local git (no network) exercised against temp repos.

Covers the "corrupt patch at line 9" trailing-newline gotcha, patch hash
re-verification, mock remote idempotency, and the full clone→edit→diff→push path.
"""
import pytest

from assistant.integrations import gitutil
from assistant.integrations.gitutil import GitError


@pytest.fixture()
def mock_remote(tmp_path):
    return gitutil.setup_mock_repo(tmp_path, "demo-repo-a")


@pytest.fixture()
def workspace(tmp_path):
    return gitutil.setup_mock_repo(tmp_path, "default")


def test_setup_mock_repo_idempotent(tmp_path):
    r1 = gitutil.setup_mock_repo(tmp_path, "demo-repo-a")
    r2 = gitutil.setup_mock_repo(tmp_path, "demo-repo-a")
    assert r1 == r2
    refs = gitutil.run_git(tmp_path, "ls-remote", str(r1), "refs/heads/*")
    assert "refs/heads/main" in refs


def test_remote_name():
    assert gitutil.remote_name("owner/demo-repo-a") == "demo-repo-a"
    assert gitutil.remote_name("owner/demo-repo-a.git") == "demo-repo-a"
    assert gitutil.remote_name("") == "default"


def test_remote_name_keeps_normal_tail_characters():
    """Regression: .rstrip('.git') used to strip any trailing g/i/t char,
    mangling 'ai-assistant' → 'ai-assistan'."""
    assert gitutil.remote_name("OnionFastener/ai-assistant") == "ai-assistant"
    assert gitutil.remote_name("OnionFastener/ai-assistant.git") == "ai-assistant"
    assert gitutil.remote_name("owner/repo-with-t") == "repo-with-t"


def test_repo_url_preserves_repo_slug():
    from types import SimpleNamespace

    s = SimpleNamespace(github_host="github.com", github_repo="OnionFastener/ai-assistant",
                        github_token="", github_ssh_url="")
    assert gitutil.repo_url(s, repo="OnionFastener/ai-assistant").endswith("OnionFastener/ai-assistant.git")
    assert gitutil.repo_url(s, repo="OnionFastener/ai-assistant.git").endswith("OnionFastener/ai-assistant.git")


def test_clone_local_and_default_branch(mock_remote, tmp_path):
    dest = tmp_path / "clone"
    gitutil.clone_local(mock_remote, dest)
    assert (dest / "service.py").exists()
    assert gitutil.default_branch(None, dest) == "main"
    gitutil.run_git(dest, "branch", "-M", "dev")
    assert gitutil.default_branch(None, dest) == "main"  # origin/HEAD still main


def test_stage_and_diff_rejects_empty(mock_remote, tmp_path):
    dest = tmp_path / "clone"
    gitutil.clone_local(mock_remote, dest)
    with pytest.raises(GitError):
        gitutil.stage_and_diff(dest)


def test_diff_captures_trailing_newline(mock_remote, tmp_path):
    dest = tmp_path / "clone"
    gitutil.clone_local(mock_remote, dest)
    src = dest / "service.py"
    text = src.read_text()
    src.write_text(text.replace("shipping=0.0):", "shipping=0.0):\n    # guard\n"))
    diff = gitutil.stage_and_diff(dest)
    assert diff.startswith("diff --git")
    assert diff.endswith("\n"), "trailing newline must survive (git apply requires it)"


def test_patch_sha_is_stable_and_sensitive(mock_remote, tmp_path):
    p1 = "diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n"
    p2 = p1 + "\n"
    assert gitutil.patch_sha(p1) == gitutil.patch_sha(p1)
    assert gitutil.patch_sha(p1) != gitutil.patch_sha(p2)


def test_apply_patch_round_trip(mock_remote, tmp_path):
    dest = tmp_path / "clone"
    gitutil.clone_local(mock_remote, dest)
    src = dest / "service.py"
    text = src.read_text()
    src.write_text(text.replace("shipping=0.0):", "shipping=0.0):\n    # guard\n"))
    diff = gitutil.stage_and_diff(dest)

    # fresh clone + apply must reproduce the exact diff
    dest2 = tmp_path / "clone2"
    gitutil.clone_local(mock_remote, dest2)
    gitutil.apply_patch(dest2, diff)
    assert (dest2 / "service.py").read_text().count("# guard") == 1


def test_apply_patch_rejects_tampered_patch(mock_remote, tmp_path):
    dest = tmp_path / "clone"
    gitutil.clone_local(mock_remote, dest)
    src = dest / "service.py"
    src.write_text(src.read_text().replace("shipping=0.0):", "shipping=0.0):\n    # guard\n"))
    diff = gitutil.stage_and_diff(dest)
    tampered = diff.replace("# guard", "# DIFFERENT")

    dest2 = tmp_path / "clone2"
    gitutil.clone_local(mock_remote, dest2)
    with pytest.raises(GitError, match="patch verification failed"):
        gitutil.apply_patch(dest2, tampered)


def test_push_branch_reaches_remote(mock_remote, tmp_path):
    src = tmp_path / "src"
    gitutil.clone_local(mock_remote, src)
    sfile = src / "service.py"
    sfile.write_text(sfile.read_text().replace("shipping=0.0):", "shipping=0.0):\n    # guard\n"))
    diff = gitutil.stage_and_diff(src)

    dest = tmp_path / "clone"
    gitutil.clone_local(mock_remote, dest)
    gitutil.apply_patch(dest, diff)
    gitutil.commit_all(dest, "fix total")
    gitutil.push_branch(dest, "fix/demo-1")

    refs = gitutil.run_git(tmp_path, "ls-remote", str(mock_remote), "refs/heads/*")
    assert "refs/heads/fix/demo-1" in refs

def test_clone_cached_repo_refreshes_mirror_and_isolates_sandbox(mock_remote, tmp_path, monkeypatch):
    class Settings:
        github_repo = "owner/demo-repo"
        github_token = ""
        github_host = "github.com"
        github_ssh_url = ""

    monkeypatch.setattr(gitutil, "repo_url", lambda settings, token="", repo=None: str(mock_remote))
    cache_root = tmp_path / "cache"
    first = tmp_path / "first"
    gitutil.clone_cached_repo(Settings(), cache_root, first, "owner/demo-repo")
    assert (first / "service.py").exists()
    assert (cache_root / next(cache_root.iterdir()).name / "HEAD").exists()

    seed = tmp_path / "seed-update"
    gitutil.clone_local(mock_remote, seed)
    (seed / "cached.txt").write_text("fresh\n")
    gitutil.run_git(seed, "add", "cached.txt")
    gitutil.run_git(seed, "commit", "-m", "cache update")
    gitutil.run_git(seed, "push")
    second = tmp_path / "second"
    gitutil.clone_cached_repo(Settings(), cache_root, second, "owner/demo-repo")
    assert (second / "cached.txt").read_text() == "fresh\n"
    (second / "service.py").write_text("changed\n")
    assert (first / "service.py").read_text() != "changed\n"
