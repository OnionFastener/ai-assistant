"""Jira Cloud REST client (requests-based) + in-memory mock for dev."""
from __future__ import annotations

import json
import re
import time

import requests

REQUEST_TIMEOUT = 30


class JiraError(Exception):
    pass


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, account_id: str = ""):
        if not (base_url and api_token):
            raise JiraError("Jira base URL and API token are required (env ASST_*).")
        self.base_url = base_url.rstrip("/")
        # Basic auth wants a username; a PAT works as username for token-as-user setups.
        self._auth = (email if email else api_token, api_token)
        self.account_id = account_id or ""

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = requests.get(f"{self.base_url}{path}", auth=self._auth, params=params, timeout=REQUEST_TIMEOUT)
        return self._handle(r)

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(
            f"{self.base_url}{path}",
            auth=self._auth,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        return self._handle(r)

    def _put(self, path: str, body: dict) -> dict:
        r = requests.put(
            f"{self.base_url}{path}",
            auth=self._auth,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        return self._handle(r)

    def _handle(self, r: requests.Response) -> dict:
        if r.status_code >= 400:
            detail = r.text[:400]
            try:
                j = r.json()
                msgs = [e for m in j.get("errors", {}).values() or [] for e in ([m] if isinstance(m, str) else m)]
                detail = msgs[0] if msgs else detail
            except ValueError:
                pass
            raise JiraError(f"Jira {r.status_code}: {detail}")
        if not r.content:
            return {}
        return r.json()

    # ---- read side ----

    def current_account_id(self) -> str:
        me = self._get("/rest/api/3/myself")
        return me.get("accountId", "")

    def search(self, jql: str, max_results: int = 50) -> list[dict]:
        body = {
            "jql": jql,
            "maxResults": max_results,
            "fields": ["summary", "description", "status", "issuetype", "priority", "labels"],
        }
        data = self._post("/rest/api/3/search/jql", body)
        out = []
        for issue in data.get("issues", []):
            k = issue["key"]
            f = issue.get("fields", {})
            out.append({
                "key": k,
                "project": (k.split("-")[0] if "-" in k else ""),
                "summary": f.get("summary", "") or "",
                "description": _clean(f.get("description")),
                "issue_type": (f.get("issuetype") or {}).get("name", ""),
                "status_name": (f.get("status") or {}).get("name", ""),
                "labels": f.get("labels", []) or [],
            })
        return out

    def get_transitions(self, key: str) -> list[dict]:
        data = self._get(f"/rest/api/3/issue/{key}/transitions")
        return [{"id": t["id"], "name": t["name"]} for t in data.get("transitions", [])]

    def get_devinfo(self, key: str, devinfo_field: str = "") -> list[dict]:
        """Read the GitHub-for-Jira "Development" field if it exists."""
        if not devinfo_field:
            return []
        data = self._get(f"/rest/api/3/issue/{key}", params={"fields": devinfo_field})
        dv = data.get("fields", {}).get(devinfo_field)
        return _parse_devinfo(dv) if dv else []

    def get_comments(self, key: str, max_results: int = 50) -> list[dict]:
        """Recent issue comments flattened to text (paged so nothing is truncated)."""
        out: list[dict] = []
        start = 0
        while True:
            data = self._get(f"/rest/api/3/issue/{key}/comment",
                             params={"startAt": start, "maxResults": max_results, "orderBy": "created"})
            comments = data.get("comments", [])
            for c in comments:
                body = c.get("body")
                out.append({
                    "id": c.get("id", ""),
                    "author": (c.get("author") or {}).get("displayName", ""),
                    "created": c.get("created", ""),
                    "body": _adf_to_text(body) if isinstance(body, dict) else str(body or ""),
                })
            total = int(data.get("total", 0))
            start += len(comments)
            if not comments or start >= total:
                break
        return out

    # ---- write side (called only by the deterministic executor) ----

    def add_comment(self, key: str, body: str) -> str:
        created = self._post(f"/rest/api/3/issue/{key}/comment", {"body": _to_adf(body)})
        return str(created.get("id", ""))

    def delete_comment(self, key: str, comment_id: str) -> None:
        """Reversible write, used by the live smoke test to leave no trace."""
        r = requests.delete(
            f"{self.base_url}/rest/api/3/issue/{key}/comment/{comment_id}",
            auth=self._auth, timeout=REQUEST_TIMEOUT,
        )
        if r.status_code >= 400:
            raise JiraError(f"Jira {r.status_code}: {r.text[:400]}")

    def transition(self, key: str, to: str) -> str:
        """Transition to a status *name*. Resolves id first; raises if not available."""
        available = self.get_transitions(key)
        match = next((t for t in available if t["name"].lower() == to.strip().lower()), None)
        if not match:
            names = ", ".join(t["name"] for t in available)
            raise JiraError(f"Transition '{to}' not available for {key}. Available: {names}")
        self._post(f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": match["id"]}})
        return match["name"]

    def assign(self, key: str, account_id: str) -> str:
        self._put(f"/rest/api/3/issue/{key}/assignee", {"accountId": account_id})
        return account_id


class MockJiraClient:
    """Deterministic fake used when ASST_MOCK=1. Records writes for inspection."""

    def __init__(self, tickets: list[dict] | None = None):
        self.tickets = tickets or _default_mock_tickets()
        self.comments: list[tuple[str, str]] = []
        self.transitions: list[tuple[str, str]] = []
        self.assignees: list[tuple[str, str]] = []
        self.account_id = "mock-user"

    def current_account_id(self) -> str:
        return self.account_id

    def search(self, jql: str, max_results: int = 50) -> list[dict]:
        return [t for t in self.tickets][:max_results]

    def get_transitions(self, key: str) -> list[dict]:
        return [{"id": "11", "name": "In Progress"}, {"id": "21", "name": "In Review"},
                {"id": "31", "name": "Backlog"}, {"id": "41", "name": "Closed"}]

    def get_devinfo(self, key: str, devinfo_field: str = "") -> list[dict]:
        return []

    def get_comments(self, key: str, max_results: int = 50) -> list[dict]:
        return []

    def add_comment(self, key: str, body: str) -> str:
        self.comments.append((key, body))
        return f"cmt-{len(self.comments)}"

    def transition(self, key: str, to: str) -> str:
        available = {t["name"].lower() for t in self.get_transitions(key)}
        if to.strip().lower() not in available:
            raise JiraError(f"Transition '{to}' not available for {key}")
        self.transitions.append((key, to))
        return to

    def assign(self, key: str, account_id: str) -> str:
        self.assignees.append((key, account_id))
        return account_id


def build_jira(settings) -> JiraClient | MockJiraClient:
    if settings.mock:
        return MockJiraClient()
    client = JiraClient(settings.jira_base_url, settings.jira_email, settings.jira_api_token)
    client.account_id = settings.jira_account_id or client.current_account_id()
    return client


# ---- helpers ----

def _clean(desc) -> str:
    """Flatten Jira ADF content into plain text. String descriptions pass through;
    embedded objects (smart-link cards, mentions, media, tables) keep their URLs/text."""
    if isinstance(desc, str):
        return desc
    if not isinstance(desc, dict) or desc.get("type") != "doc":
        return ""
    return _flatten(desc)


def _node_text(node: dict | None) -> str:
    """Best-effort display text for a leaf node (smart-link URL, mention, media, …)."""
    if not isinstance(node, dict):
        return ""
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    attrs = node.get("attrs") or {}
    if t in ("inlineCard", "blockCard", "embedCard"):
        return attrs.get("url", "")
    if t in ("media", "mediaSingle", "mediaGroup"):
        return attrs.get("url", "") or attrs.get("alt", "")
    if t == "mention":
        return attrs.get("text", "") or attrs.get("id", "")
    if t == "emoji":
        return attrs.get("shortName", "")
    if t in ("status", "date", "label"):
        return attrs.get("text", "") or attrs.get("shortName", "") or attrs.get("value", "")
    if t in ("rule", "hardBreak"):
        return "\n"
    return ""


def _flatten(adf: dict) -> str:
    """Robust ADF → text: block-aware, preserves embedded objects and URLs."""
    lines: list[str] = []

    def walk_block(node: dict):
        t = node.get("type")
        if t == "text":
            return [node.get("text", "")]
        if t == "hardBreak":
            return ["\n"]
        if t in ("inlineCard", "blockCard", "embedCard", "media", "mention", "emoji",
                 "status", "date", "label", "rule"):
            text = _node_text(node)
            return [text] if text else []
        if t in ("paragraph", "blockquote", "panel"):
            return [_join_inline(node.get("content", []) or [])]
        if t == "heading":
            text = _join_inline(node.get("content", []) or [])
            return [f"## {text.strip()}"]
        if t == "bulletList":
            out: list[str] = []
            for item in node.get("content", []) or []:
                seg = " ".join(s for s in walk_block(item) if s != "\n")
                out.append(f"- {seg.strip()}")
            return out
        if t == "orderedList":
            numbered: list[str] = []
            for i, item in enumerate(node.get("content", []) or [], 1):
                seg = " ".join(s for s in walk_block(item) if s != "\n")
                numbered.append(f"{i}. {seg.strip()}")
            return numbered
        if t == "listItem":
            segs: list[str] = []
            for child in node.get("content", []) or []:
                segs.extend(walk_block(child))
            return segs
        if t == "table":
            rows: list[str] = []
            for row in node.get("content", []) or []:
                cells = []
                for cell in row.get("content", []) or []:
                    seg = " ".join(s for s in walk_block(cell) if s != "\n").strip()
                    cells.append(seg)
                rows.append(" | ".join(c for c in cells if c))
            return rows
        if t in ("tableRow", "tableCell", "tableHeader"):
            segs: list[str] = []
            for child in node.get("content", []) or []:
                segs.extend(walk_block(child))
            return segs
        if t == "codeBlock":
            return [_join_inline(node.get("content", []) or [])]
        # generic container
        segs: list[str] = []
        for child in node.get("content", []) or []:
            segs.extend(walk_block(child))
        return segs

    def _join_inline(children: list) -> str:
        out_parts: list[str] = []
        for child in children:
            for s in walk_block(child):
                out_parts.append(s)
        return "".join(out_parts)

    for block in adf.get("content", []) or []:
        for seg in walk_block(block):
            seg = seg.strip()
            if seg:
                lines.append(seg)
    return "\n".join(lines)


def _to_adf(text: str) -> dict:
    """Convert a plain-text/markdown-lite comment into Jira ADF (their API rejects plain strings)."""
    content = []
    for block in re.split(r"\n\s*\n", str(text or "")):
        block = block.strip()
        if not block:
            continue
        nodes = []
        first = True
        for line in block.split("\n"):
            if not first:
                nodes.append({"type": "hardBreak"})
            first = False
            for seg in re.split(r"(\*\*[^*]+\*\*)", line):
                if not seg:
                    continue
                if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
                    nodes.append({"type": "text", "text": seg[2:-2], "marks": [{"type": "strong"}]})
                else:
                    nodes.append({"type": "text", "text": seg})
        content.append({"type": "paragraph", "content": nodes})
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]
    return {"type": "doc", "version": 1, "content": content}


def _plain_string(v) -> str:
    return str(v or "")


def _adf_to_text(adf: dict) -> str:
    """Convert a Jira ADF document to plain text (newlines preserved)."""
    return _flatten(adf)


def _parse_devinfo(dv) -> list[dict]:
    """Normalize the GitHub-for-Jira 'Development' field into generic links."""
    links = []
    for inst in (dv or {}).get("instances", []):
        for rep in (inst or {}).get("repositories", []):
            for pr in (rep or {}).get("pullRequests", []) or []:
                links.append({"kind": "pr", "source": "Jira-dev", "url": pr.get("url", ""),
                              "title": pr.get("name", ""), "pr_state": (pr.get("status") or {}).get("state", "")})
            for commit in (rep or {}).get("commits", []) or []:
                links.append({"kind": "commit", "source": "Jira-dev", "url": commit.get("url", ""),
                              "title": commit.get("message", ""), "sha": commit.get("id", "")})
    return links


def _default_mock_tickets() -> list[dict]:
    return [
        {
            "key": "DEMO-1", "project": "DEMO",
            "summary": "Order total crashes to $0 for free-shipping orders",
            "description": "Bug: Order page shows 0.00 total for orders with free shipping.\n\n"
                           "Steps:\n1. Add free shipping coupon\n2. View order\n3. Total is 0.00 instead of subtotal.\n\n"
                           "Expected: subtotal shown. Traceback: TypeError in total() at models.py:42.",
            "issue_type": "Bug", "status_name": "Open",
        },
        {
            "key": "DEMO-2", "project": "DEMO",
            "summary": "Add CSV export for the reports dashboard",
            "description": "Feature request: Users want to export the reports dashboard table to CSV with a single click.",
            "issue_type": "Story", "status_name": "Open",
        },
        {
            "key": "DEMO-3", "project": "DEMO",
            "summary": "Can't log in after password reset",
            "description": "sometimes login fails",
            "issue_type": "Bug", "status_name": "Open",
        },
        {
            "key": "DEMO-4", "project": "DEMO",
            "summary": "Should we drop support for IE11?",
            "description": "Design/product decision. IE11 usage is <0.2%. Our JS is increasingly modern and polyfills are ugly.",
            "issue_type": "Task", "status_name": "Open",
        },
        {
            "key": "DEMO-5", "project": "DEMO",
            "summary": "QL3 report totals don't match the ledger",
            "description": "The QL3 report shows different totals than the ledger. No repro steps provided.",
            "issue_type": "Bug", "status_name": "Open",
        },
    ]