"""config load + DB wiring, isolation-focused.

Ensures Settings.load honours ASST_MOCK / ASST_DB_PATH / ASST_WORKSPACE and that
the engine/SessionLocal bind to the test DB, never the real assistant.db.
"""
import os
from pathlib import Path

from assistant import db as dbmod


def test_settings_mock_env_honoured():
    from assistant.config import settings
    assert settings.mock is True


def test_settings_db_and_workspace_redirected():
    from assistant.config import settings
    assert "asst-test-" in str(settings.db_path)
    assert "asst-test-" in str(settings.workspace)
    assert settings.load  # sanity: callable exists


def test_db_engine_points_at_test_sqlite():
    assert str(dbmod.engine.url) == f"sqlite:///{dbmod.settings.db_path}"


def test_session_writes_and_reads_back(tmp_path):
    from assistant.models import Run

    db = dbmod.SessionLocal()
    r = Run(trigger="manual", status="queued")
    db.add(r)
    db.commit()
    fresh = db.get(Run, r.id)
    assert fresh.trigger == "manual"
    db.close()


def test_env_load_respects_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ASST_DB_PATH", str(tmp_path / "custom.db"))
    monkeypatch.setenv("ASST_MOCK", "0")
    from assistant.config import Settings

    s = Settings.load()
    assert str(s.db_path).endswith("custom.db")
    assert s.mock is False