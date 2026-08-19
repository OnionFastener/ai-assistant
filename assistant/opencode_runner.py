"""Thin wrapper around `opencode run` (headless). Extracts final assistant text."""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import re
import shutil
import subprocess
import time
from collections.abc import Callable

log = logging.getLogger("assistant.opencode")

FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")
ACTIVE: dict[str, subprocess.Popen] = {}
ACTIVE_LOCK = threading.Lock()

def cancel_agent(cwd: str | os.PathLike) -> bool:
    with ACTIVE_LOCK:
        proc = ACTIVE.get(str(cwd))
    return bool(proc and proc.poll() is None and terminate_process(proc.pid))


def process_start(pid: int) -> str:
    try:
        return open(f"/proc/{pid}/stat").read().split(") ", 1)[1].split()[19]
    except (FileNotFoundError, IndexError, OSError):
        return ""


def process_running(pid: int | None, started: str = "") -> bool:
    if not pid or not os.path.exists(f"/proc/{pid}"):
        return False
    return not started or process_start(pid) == started


def terminate_process(pid: int | None, started: str = "") -> bool:
    if not process_running(pid, started):
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


def _tail(value: str | bytes | None, limit: int = 1600) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "").strip()[-limit:].replace("\x00", "")


def run_agent(
    prompt: str,
    *,
    agent: str,
    cwd: str | os.PathLike,
    model: str = "",
    timeout: int = 900,
    extra_env: dict | None = None,
    on_started: Callable[[int, str], None] | None = None,
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
    started = time.monotonic()
    log.info("event=agent.start agent=%s cwd=%s timeout_seconds=%s model=%s", agent, cwd, timeout, model or "default")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(cwd), start_new_session=True)
        with ACTIVE_LOCK: ACTIVE[str(cwd)] = proc
        if on_started:
            on_started(proc.pid, process_start(proc.pid))
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM); stdout, stderr = proc.communicate()
        elapsed = round(time.monotonic() - started, 3)
        raise RuntimeError(f"agent={agent} timeout_seconds={timeout} elapsed_seconds={elapsed} stdout_tail={_tail(stdout)!r} stderr_tail={_tail(stderr)!r}") from None
    finally:
        with ACTIVE_LOCK: ACTIVE.pop(str(cwd), None)
    elapsed = round(time.monotonic() - started, 3)
    proc.stdout, proc.stderr = stdout, stderr
    log.info("event=agent.finish agent=%s elapsed_seconds=%s exit_code=%s stdout_bytes=%s stderr_bytes=%s", agent, elapsed, proc.returncode, len(stdout), len(stderr))
    if proc.returncode != 0 and not stdout.strip():
        raise RuntimeError(f"opencode exited {proc.returncode}: {stderr[:500]}")
    text = extract_text(stdout) or stdout
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
            msg_role = (evt.get("message") or {}).get("role")
            _collect(evt.get("part") or {}, candidates, msg_role)
        elif etype == "text":
            msg_role = (evt.get("message") or {}).get("role")
            _collect(evt.get("part") or {}, candidates, msg_role)
        elif etype == "message":
            _collect(evt.get("message") or {}, candidates)
    for c in reversed(candidates):
        if c.strip():
            return c.strip()
    return ""


def _collect(part: dict, out: list[str], msg_role: str | None = None) -> None:
    role = part.get("role") or msg_role
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