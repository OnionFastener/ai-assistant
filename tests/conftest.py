"""Shared test harness.

Everything lives in a session-scoped temp dir: workspace, SQLite DB, a throwaway
copy of paths/, and a throwaway repo_map. The real config/settings.json and
config/repo_map.json are only ever read, never written. Env vars are set here
BEFORE assistant.* is imported so the module-level settings/db singletons bind
to test locations.
"""
import copy
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="asst-test-"))
os.environ.setdefault("ASST_MOCK", "1")
os.environ["ASST_WORKSPACE"] = str(_SESSION_TMP / "workspace")
os.environ["ASST_DB_PATH"] = str(_SESSION_TMP / "test.db")

import pytest  # noqa: E402

from assistant import auth  # noqa: E402
from assistant import db as dbmod  # noqa: E402
from assistant.config import settings  # noqa: E402

# Throwaway copies so path/repo-map CRUD never touches the real repo.
_TEST_PATHS_DIR = _SESSION_TMP / "paths"
if _TEST_PATHS_DIR.exists():
    shutil.rmtree(_TEST_PATHS_DIR)
shutil.copytree(REPO_ROOT / "paths", _TEST_PATHS_DIR)
settings.paths_dir = _TEST_PATHS_DIR

_TEST_REPO_MAP = _SESSION_TMP / "repo_map.json"
_TEST_REPO_MAP.write_text("{}\n")

# Real config/settings.json caps max_tickets_per_run=3; tests want all 5 DEMO tickets.
settings.max_tickets_per_run = 20
# Pin mock repos so repo resolution is deterministic (real config's repos would
# otherwise resolve DEMO-1 to the first configured OnionFastener repo).
settings.github_repos = ["owner/demo-repo-a", "owner/demo-repo-b"]
settings.github_repo = "owner/demo-repo-a"
settings.scan_jira = True
settings.scan_github_issues = False
settings.github_issue_repos = ["demo/mock-repo"]


@pytest.fixture(autouse=True)
def _reset_db_and_auth():
    from assistant.models import Base as ModelsBase

    ModelsBase.metadata.drop_all(dbmod.engine)
    ModelsBase.metadata.create_all(dbmod.engine)
    auth._sessions.clear()
    yield


@pytest.fixture(autouse=True)
def _restore_settings():
    """Restore in-memory settings after each test (PUT /api/config mutates the
    singleton AND writes the settings file, so back that up too)."""
    before = {f: copy.deepcopy(getattr(settings, f)) for f in settings.__dataclass_fields__}
    settings_file = settings.settings_path
    file_backup = settings_file.read_bytes() if settings_file.exists() else None
    yield
    for field, value in before.items():
        setattr(settings, field, value)
    if file_backup is not None:
        settings_file.write_bytes(file_backup)


@pytest.fixture(autouse=True)
def _repo_map_redirect(monkeypatch):
    monkeypatch.setattr("assistant.config.REPO_MAP_PATH", _TEST_REPO_MAP)
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from assistant.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def authed(client):
    r = client.post("/api/login", json={"password": "change-me"})
    assert r.status_code == 200, r.text
    return {"client": client, "csrf": r.json()["csrf"]}


def wait_until(predicate, timeout=15.0, interval=0.1):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
