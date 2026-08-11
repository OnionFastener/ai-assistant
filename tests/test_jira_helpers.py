"""Jira integration: ADF clean/flatten/rebuild round-trips, mock client behavior."""
import pytest

from assistant.integrations import jira as jira_mod
from assistant.integrations.jira import MockJiraClient, _adf_to_text, _clean, _to_adf


def test_clean_flattens_adf_paragraphs():
    adf = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Bug: total crashes. "},
            {"type": "text", "text": "Traceback below."},
        ]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Expected: subtotal shown."}]},
    ]}
    assert _clean(adf).startswith("Bug: total crashes")


def test_clean_preserves_newlines_for_headings_and_lists():
    adf = {"type": "doc", "version": 1, "content": [
        {"type": "heading", "content": [{"type": "text", "text": "Steps"}]},
        {"type": "listItem", "content": [{"type": "text", "text": "one"}]},
        {"type": "listItem", "content": [{"type": "text", "text": "two"}]},
    ]}
    out = _clean(adf)
    assert "Steps" in out
    assert "one" in out and "two" in out


def test_clean_handles_plain_string_and_empty():
    assert _clean("plain text") == "plain text"
    assert _clean(None) == ""
    assert _clean({}) == ""


def test_clean_handles_hardbreak():
    adf = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "line1"},
            {"type": "hardBreak"},
            {"type": "text", "text": "line2"},
        ]},
    ]}
    assert _clean(adf) == "line1\nline2"


def test_clean_preserves_smartlink_url():
    adf = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "project repo is "},
            {"type": "inlineCard", "attrs": {"url": "https://github.com/OnionFastener/ai-assistant"}},
        ]},
    ]}
    out = _clean(adf)
    assert "https://github.com/OnionFastener/ai-assistant" in out


def test_clean_flattens_mention_media_and_tables():
    adf = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"text": "@owen", "id": "abc"}},
            {"type": "text", "text": " see "},
            {"type": "inlineCard", "attrs": {"url": "https://example.com/item"}},
        ]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "text", "text": "one"}]},
            {"type": "listItem", "content": [{"type": "text", "text": "two"}]},
        ]},
        {"type": "table", "content": [
            {"type": "tableRow", "content": [
                {"type": "tableHeader", "content": [{"type": "text", "text": "a"}]},
                {"type": "tableHeader", "content": [{"type": "text", "text": "b"}]},
            ]},
            {"type": "tableRow", "content": [
                {"type": "tableCell", "content": [{"type": "text", "text": "1"}]},
                {"type": "tableCell", "content": [{"type": "text", "text": "2"}]},
            ]},
        ]},
    ]}
    out = _clean(adf)
    assert "@owen" in out
    assert "https://example.com/item" in out
    assert "- one" in out and "- two" in out
    assert "a | b" in out and "1 | 2" in out


def test_clean_keeps_contentless_nodes_from_dropping_siblings():
    adf = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "before "},
            {"type": "emoji", "attrs": {"shortName": ":smile:"}},
            {"type": "text", "text": "after"},
        ]},
    ]}
    out = _clean(adf)
    assert "before" in out and "after" in out
    assert ":smile:" in out


def test_to_adf_produces_doc_with_paragraphs():
    a = _to_adf("hello world")
    assert a["type"] == "doc"
    paras = [b for b in a["content"] if b["type"] == "paragraph"]
    assert len(paras) == 1
    text = "".join(n.get("text", "") for n in paras[0]["content"])
    assert text == "hello world"


def test_to_adf_bold_marks():
    a = _to_adf("**Root cause** fixed")
    para = a["content"][0]
    bold = [n for n in para["content"] if n.get("marks")]
    assert bold and bold[0]["text"] == "Root cause"


def test_to_adf_multi_paragraph_split():
    a = _to_adf("first para\n\nsecond para")
    paras = [b for b in a["content"] if b["type"] == "paragraph"]
    assert len(paras) == 2


def test_to_adf_empty_gives_empty_paragraph():
    a = _to_adf("")
    assert len(a["content"]) == 1


def test_adf_round_trip_text():
    body = "Root cause: **bad math**\n\nExpected subtotal."
    assert _adf_to_text(_to_adf(body)).replace("\n", " ") in ("Root cause: bad math Expected subtotal.",)


def test_mock_jira_records_writes():
    m = MockJiraClient()
    assert m.current_account_id() == "mock-user"
    cid = m.add_comment("DEMO-1", "hello")
    assert cid == "cmt-1"
    assert ("DEMO-1", "hello") in m.comments
    m.transition("DEMO-1", "In Review")
    assert ("DEMO-1", "In Review") in m.transitions
    m.assign("DEMO-1", "me")
    assert ("DEMO-1", "me") in m.assignees


def test_mock_jira_rejects_unknown_transition():
    m = MockJiraClient()
    with pytest.raises(jira_mod.JiraError):
        m.transition("DEMO-1", "Does Not Exist")


def test_mock_jira_search_returns_default_tickets():
    m = MockJiraClient()
    out = m.search("ANY JQL")
    assert [t["key"] for t in out] == ["DEMO-1", "DEMO-2", "DEMO-3", "DEMO-4", "DEMO-5"]
    assert out[0]["project"] == "DEMO"
    assert out[0]["summary"] == "Order total crashes to $0 for free-shipping orders"


def test_mock_jira_comments_empty():
    m = MockJiraClient()
    assert m.get_comments("DEMO-1") == []


def test_parse_devinfo():
    dv = {"instances": [{"repositories": [{
        "pullRequests": [{"url": "http://gh/pr/1", "name": "PR 1", "status": {"state": "OPEN"}}],
        "commits": [{"url": "http://gh/c/1", "message": "fix", "id": "abc123"}],
    }]}]}
    links = jira_mod._parse_devinfo(dv)
    assert links[0]["kind"] == "pr"
    assert links[1]["kind"] == "commit"
    assert links[1]["sha"] == "abc123"