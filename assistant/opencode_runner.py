"""Thin wrapper around `opencode run` (headless). Extracts final assistant text."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("assistant.opencode")

FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


def run_agent(
    prompt: str,
    *,
    agent: str,
    cwd: str | os.PathLike,
    model: str = "",
    timeout: int = 900,
    extra_env: dict | None = None,
) -> str:
    """Run one opencode agent step; return the final assistant text (stripped)."""
    exe = shutil.which("opencode")
    if not exe:
        raise FileNotFoundError("opencode CLI not found on PATH")
    cmd = [exe, "run", "--agent", agent, "--format", "json", "--log-level", "ERROR", "--dir", str(cwd)]
    if model:
        cmd += ["-m", model]
    cmd.append(prompt)

    env = dict(os.environ)
    env.update(extra_env or {})
    log.debug("opencode: %s %s", agent, cwd)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(cwd)
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"opencode exited {proc.returncode}: {proc.stderr[:500]}")
    text = extract_text(proc.stdout) or proc.stdout
    return FENCE_RE.sub("", text or "").strip()


def extract_text(stdout: str) -> str:
    """Best-effort parser for `--format json` event stream -> final assistant text."""
    candidates: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        if etype == "session.updated":
            for part in (evt.get("session") or {}).get("parts", []) or []:
                _collect(part, candidates)
        elif etype == "message.part.updated":
            _collect(evt.get("part") or {}, candidates)
        elif etype == "text":
            _collect(evt.get("part") or {}, candidates)
        elif etype == "message":
            _collect(evt.get("message") or {}, candidates)
    for c in reversed(candidates):
        if c.strip():
            return c.strip()
    return ""


def _collect(part: dict, out: list[str]) -> None:
    role = part.get("role")
    if role and role != "assistant":
        return
    if isinstance(part.get("text"), str) and part["text"].strip():
        out.append(part["text"])
    elif isinstance(part.get("content"), str) and part["content"].strip():
        out.append(part["content"])
    elif isinstance(part.get("content"), list):
        for block in part["content"]:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))