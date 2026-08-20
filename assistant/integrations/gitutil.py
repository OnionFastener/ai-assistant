"""git plumbing for the bug-fix sandbox (subprocess only, deterministic).

Mock mode uses a local bare remote so clone → patch → commit → push works end to end
with no network. Real mode uses GitHub over HTTPS with the token embedded in the URL.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("assistant.gitutil")

CACHE_LOCKS: dict[str, threading.Lock] = {}
CACHE_LOCKS_GUARD = threading.Lock()

GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "AI Assistant",
    "GIT_AUTHOR_EMAIL": "assistant@localhost",
    "GIT_COMMITTER_NAME": "AI Assistant",
    "GIT_COMMITTER_EMAIL": "assistant@localhost",
}

_empty_env = dict(os.environ)
_empty_env.update(GIT_ENV)


class GitError(Exception):
    pass


def run_git(repo: Path, *args: str, check: bool = True, strip: bool = True) -> str:
    """Run git in `repo`, return stdout."""
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), env=_empty_env, capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip() if strip else proc.stdout


def _strip_git_suffix(name: str) -> str:
    if name.endswith(".git"):
        return name[:-4]
    return name


def repo_url(settings, token: str = "", repo: str | None = None) -> str:
    """Clone/push URL for a GitHub repo (defaults to the configured one)."""
    host = getattr(settings, "github_host", "github.com")
    repo = _strip_git_suffix((repo or settings.github_repo).strip())
    token = token or settings.github_token
    if token:
        return f"https://x-access-token:{token}@{host}/{repo}.git"
    ssh = getattr(settings, "github_ssh_url", "")
    if ssh:
        return ssh.rstrip("/") + ".git" if not ssh.endswith(".git") else ssh
    return f"https://{host}/{repo}.git"


def clone_repo(settings, dest: Path, repo: str | None = None) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    run_git(dest.parent, "clone", "--depth", "1", repo_url(settings, repo=repo), str(dest))
    return dest


def _cache_lock(path: Path) -> threading.Lock:
    with CACHE_LOCKS_GUARD:
        return CACHE_LOCKS.setdefault(str(path), threading.Lock())


def clone_cached_repo(settings, cache_root: Path, dest: Path, repo: str | None = None) -> Path:
    """Refresh a private bare mirror, then create an isolated local sandbox clone."""
    identity = (repo or settings.github_repo or "default").strip().lower()
    cache = Path(cache_root) / f"{hashlib.sha256(identity.encode()).hexdigest()[:20]}.git"
    cache.parent.mkdir(parents=True, exist_ok=True)
    source = repo_url(settings, repo=repo)
    public_source = repo_url(settings, token="", repo=repo)
    with _cache_lock(cache):
        if not cache.exists():
            run_git(cache.parent, "clone", "--mirror", source, str(cache))
            run_git(cache, "remote", "set-url", "origin", public_source)
        else:
            try:
                run_git(cache, "-c", f"remote.origin.url={source}", "fetch", "--prune", "origin")
            except GitError:
                log.warning("using existing repository cache after refresh failed: %s", cache.name)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_git(dest.parent, "clone", "--no-hardlinks", "--depth", "1", str(cache), str(dest))
    return dest


def default_branch(settings, dest: Path) -> str:
    """Best-effort name of the remote default branch (from origin/HEAD)."""
    try:
        ref = run_git(dest, "symbolic-ref", "refs/remotes/origin/HEAD")
        name = ref.rsplit("/", 1)[-1]
        if name:
            return name
    except GitError:
        pass
    try:
        current = run_git(dest, "branch", "--show-current")
        if current:
            return current
    except GitError:
        pass
    return "main"


def stage_and_diff(dest: Path) -> str:
    """Capture working-tree changes (incl. new files) as a unified diff vs HEAD."""
    run_git(dest, "add", "-A", "-N", ".")
    diff = run_git(dest, "diff", "HEAD", strip=False, check=True)
    if not diff.strip():
        raise GitError("no working-tree changes to propose (agent made no edits)")
    return diff


def patch_sha(patch: str) -> str:
    return hashlib.sha256(patch.encode()).hexdigest()


def apply_patch(dest: Path, patch: str, expected_sha: str = "") -> None:
    if expected_sha and patch_sha(patch) != expected_sha:
        raise GitError("patch hash does not match the approved patch")
    pf = dest.parent / "pending.patch"
    pf.write_text(patch)
    try:
        run_git(dest, "apply", "--whitespace=nowarn", str(pf))
    finally:
        pf.unlink(missing_ok=True)
    run_git(dest, "add", "-A", "-N", ".")
    applied = run_git(dest, "diff", "HEAD", strip=False)
    if applied != patch:
        raise GitError("patch verification failed: applied diff does not match the approved patch")


def commit_all(dest: Path, message: str) -> str:
    run_git(dest, "add", "-A")
    run_git(dest, "commit", "-m", message)
    return run_git(dest, "rev-parse", "--short", "HEAD")


def push_branch(dest: Path, branch: str) -> None:
    run_git(dest, "checkout", "-B", branch)
    run_git(dest, "push", "-u", "origin", branch)
    run_git(dest, "fetch", "origin", "--prune")


# ---- mock fixture: real local git so the whole flow is exercised ----

def setup_mock_remote(base: Path) -> Path:
    """Create a bare remote with one commit; returns the bare repo path. Idempotent."""
    return setup_mock_repo(base, "default")

def setup_mock_repo(base: Path, name: str) -> Path:
    """Create a named bare remote. Idempotent. Used to emulate multiple repos in mock mode."""
    base = Path(base)
    remote = base / f"{name}.git"
    if remote.exists():
        return remote
    work = base / f"seed-{name}"
    remote.mkdir(parents=True, exist_ok=True)
    Path(work).mkdir(parents=True, exist_ok=True)
    run_git(base, "init", "--bare", str(remote))
    run_git(work, "init")
    (work / "README.md").write_text(f"# {name}\n\nRepo for {name}.\n")
    (work / "service.py").write_text("def total(subtotal, shipping=0.0):\n    return subtotal + shipping\n")
    run_git(work, "add", "-A")
    run_git(work, "commit", "-m", "init")
    run_git(work, "remote", "add", "origin", str(remote))
    run_git(work, "branch", "-M", "main")
    run_git(work, "push", "-u", "origin", "main")
    run_git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote


def clone_local(remote: Path, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_git(dest.parent, "clone", str(remote), str(dest))
    return dest


def remote_name(repo: str) -> str:
    """Map an 'owner/name' (or bare name) to a safe mock-remote dir name."""
    name = _strip_git_suffix((repo or "").strip()).split("/")[-1]
    return name or "default"