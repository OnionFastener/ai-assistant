"""Triage: prompt building, JSON parsing, mock keyword routing, live-failure escalation."""
import pytest

from assistant.triage import (
    _mock_triage,
    _parse,
    build_ticket_context,
    build_triage_prompt,
    run_triage,
)
from assistant.config import settings


@pytest.fixture()
def paths():
    from assistant.paths import load_paths
    return load_paths(settings.paths_dir)


def test_build_ticket_context_whitelists_fields():
    ctx = build_ticket_context(
        {"key": "DEMO-1", "project": "DEMO", "summary": "s", "description": "d",
         "issue_type": "Bug", "status_name": "Open", "labels": ["x"],
         "uncleared_junk": "must NOT leak"},
        [{"kind": "commit", "source": "GitHub", "repo": "a/b", "title": "t", "sha": "ab12",
          "pr_state": "", "url": "http://x"}],
    )
    assert ctx["key"] == "DEMO-1"
    assert "uncleared_junk" not in ctx
    assert ctx["links"][0]["kind"] == "commit"
    assert "repo" not in ctx["links"][0]  # links are normalized for the model
    assert ctx["links"][0]["sha"] == "ab12"


def test_build_triage_prompt_includes_only_enabled_valid_paths(paths):
    prompt = build_triage_prompt({"key": "DEMO-1"}, paths)
    from assistant.paths import get_path

    disabled = get_path(paths, "need-my-input")
    if disabled and not disabled.enabled:
        assert "## need-my-input" not in prompt
    assert "bug-fix" in prompt


def test_parse_accepts_clean_json():
    r = _parse('{"path_id": "bug-fix", "confidence": 0.9, "reason": "traceback", "need_my_input": false}')
    assert r.path_id == "bug-fix"
    assert r.confidence == 0.9


def test_parse_rejects_fenced_json_and_garbage():
    # Fence stripping is run_agent's job; _parse is strict by design.
    with pytest.raises(ValueError):
        _parse('```json\n{"path_id": "new-feature", "confidence": 0.7}\n```')
    with pytest.raises(ValueError):
        _parse("definitely not json")


def test_mock_triage_routes_bug(paths):
    r = _mock_triage({"key": "DEMO-1", "summary": "Order total crashes", "description": "TypeError: total() at models.py:42."}, paths)
    assert r.path_id.startswith("bug-fix")


def test_mock_triage_routes_feature(paths):
    r = _mock_triage({"key": "DEMO-2", "summary": "Add CSV export", "description": "export the reports dashboard to CSV"}, paths)
    assert r.path_id == "new-feature"


def test_mock_triage_routes_more_info(paths):
    r = _mock_triage({"key": "DEMO-5", "summary": "QL3 report totals don't match",
                      "description": "No repro steps provided."}, paths)
    assert r.path_id == "need-more-info"


def test_mock_triage_routes_bug_type_even_with_weak_keywords(paths):
    r = _mock_triage({"key": "DEMO-5", "summary": "QL3 report totals don't match",
                      "description": "No repro steps provided.", "issue_type": "Bug"}, paths)
    assert r.path_id == "bug-fix"


def test_mock_triage_routes_question_to_human(paths):
    r = _mock_triage({"key": "DEMO-4", "summary": "Should we drop support for IE11?", "description": "Design/product decision."}, paths)
    assert r.path_id == "need-my-input"
    assert r.need_my_input is True


def test_mock_triage_respects_no_enabled_paths():
    r = _mock_triage({"key": "DEMO-1", "summary": "x", "description": "y"}, [])
    assert r.path_id == "need-my-input"  # no enabled path left after filtered


def test_run_triage_mock_uses_keyword_classifier():
    from assistant.paths import load_paths

    r = run_triage(settings, settings.workspace, {"key": "DEMO-1", "summary": "crash on checkout",
                                                  "description": "Traceback on total()"}, load_paths(settings.paths_dir))
    assert r.path_id.startswith("bug-fix")


def test_run_triage_escalates_when_agent_unavailable(monkeypatch):
    """Live failure must escalate to need-my-input, never silently keyword-route."""
    from assistant import triage as triage_mod

    monkeypatch.setattr(settings, "mock", False)
    monkeypatch.setattr(triage_mod.op, "run_agent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no provider")))
    monkeypatch.setattr(triage_mod.op.shutil, "which", lambda name: "/fake/opencode")

    r = run_triage(settings, settings.workspace, {"key": "DEMO-1", "summary": "x", "description": "y"}, [])
    assert r.path_id == "need-my-input"
    assert r.need_my_input is True
    assert "unavailable" in r.reason