"""Tests for opencode_run.extract_text and run_agent, the riskiest parser in the app.

Frozen, realistic `--format json` event streams. If the parser regresses here,
every live ticket silently escalates to need-my-input (see AGENTS.md).
"""
import json
import subprocess

import pytest

from assistant import opencode_runner as op


def _line(obj: dict) -> str:
    return json.dumps(obj)


# ---- extract_text against frozen streams ----


def test_extract_text_prefers_latest_assistant_text_in_text_events():
    stream = "\n".join([
        _line({"type": "step_start", "step": {"title": "Planning"}, "session_id": "s1"}),
        _line({"type": "session.updated", "session": {"id": "s1", "parentID": None, "createdAt": 1},
               "sessionID": "s1"}),
        _line({"type": "message.part.updated",
               "session": {"id": "s1"}, "message": {"id": "m1", "role": "assistant"},
               "part": {"type": "text", "text": "Let me think... my final answer is coming."}}),
        _line({"type": "text",
               "session": {"id": "s1"}, "message": {"id": "m1", "role": "assistant"},
               "part": {"type": "text", "text": "{\"path_id\": \"bug-fix\", \"confidence\": 0.9}"}}),
        _line({"type": "step_finish", "step": {"title": "Planning"}, "session_id": "s1"}),
    ])
    assert op.extract_text(stream) == '{"path_id": "bug-fix", "confidence": 0.9}'


def test_extract_text_uses_text_content_when_no_text_field():
    stream = "\n".join([
        _line({"type": "message.part.updated",
               "session": {"id": "s1"}, "message": {"id": "m1", "role": "assistant"},
               "part": {"type": "text", "content": [{"type": "text", "text": "answer from content"}]}}),
    ])
    assert op.extract_text(stream) == "answer from content"


def test_extract_text_ignores_user_and_empty_parts():
    stream = "\n".join([
        _line({"type": "text", "session": {"id": "s1"}, "message": {"id": "m1", "role": "user"},
               "part": {"type": "text", "text": "user prompt"}}),
        _line({"type": "text", "session": {"id": "s1"}, "message": {"id": "m1", "role": "assistant"},
               "part": {"type": "text", "text": ""}}),
    ])
    assert op.extract_text(stream) == ""


def test_extract_text_handles_non_json_lines():
    stream = "# some cosmetic\n***\n" + _line({"type": "text",
            "session": {"id": "s1"}, "message": {"id": "m1", "role": "assistant"},
            "part": {"type": "text", "text": "clean answer"}})
    assert op.extract_text(stream) == "clean answer"


def test_extract_text_returns_last_nonempty_candidate():
    stream = "\n".join([
        _line({"type": "message", "message": {"id": "m1", "role": "assistant", "content": "first draft"}}),
        _line({"type": "text", "session": {"id": "s1"}, "message": {"id": "m2", "role": "assistant"},
               "part": {"type": "text", "text": "revised answer"}}),
    ])
    # reversed(candidates) → newest non-empty wins
    assert op.extract_text(stream) == "revised answer"


def test_extract_text_empty_and_garbage():
    assert op.extract_text("") == ""
    assert op.extract_text("not json at all\n\nstill not json\n") == ""


# ---- run_agent wrapper (subprocess is stubbed; git/opencode never invoked) ----


class _FakeProc:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.stderr = "some stderr"
        self.returncode = returncode


def test_run_agent_returns_flag_stripped_text(monkeypatch, tmp_path):
    body = '{\n  "path_id": "need-more-info",\n  "confidence": 0.6\n}\n'
    stdout = "\n".join([
        _line({"type": "step_start", "step": {"title": "Triage"}, "session_id": "x"}),
        _line({"type": "text", "session": {"id": "x"}, "message": {"id": "m1", "role": "assistant"},
               "part": {"type": "text", "text": f"```json\n{body}```"}}),
    ])
    monkeypatch.setattr(op.shutil, "which", lambda name: "/fake/bin/opencode")
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _FakeProc(stdout))
    result = op.run_agent("triage me", agent="triage", cwd=str(tmp_path))
    assert result.startswith("{")
    assert result.endswith("}")
    assert "```" not in result


def test_run_agent_raises_when_binary_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(op.shutil, "which", lambda name: "")
    with pytest.raises(FileNotFoundError):
        op.run_agent("hi", agent="triage", cwd=str(tmp_path))


def test_run_agent_raises_on_bad_exit_with_no_stdout(monkeypatch, tmp_path):
    monkeypatch.setattr(op.shutil, "which", lambda name: "/fake/bin/opencode")
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _FakeProc("", returncode=1))
    with pytest.raises(RuntimeError, match="exited 1"):
        op.run_agent("hi", agent="triage", cwd=str(tmp_path))