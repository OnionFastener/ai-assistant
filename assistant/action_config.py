"""Editable global action-agent config: `config/action.md`.

Plain markdown: the "system profile" every action-agent run starts with, before
the per-path behavior is layered on. Loaded fresh on every use so edits apply
immediately.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import REPO_ROOT

ACTION_CONFIG_PATH = REPO_ROOT / "config" / "action.md"

DEFAULT_ACTION_INSTRUCT = (
    "You are an AI engineer acting on a Jira ticket for an approved triage path.\n"
    "You work in a sandbox clone of the target repository. Bring the task to a "
    "reviewable end state: reproduce the issue, make the minimal change, and verify "
    "it - then emit the action plan for a human to approve. Nothing is committed, "
    "pushed, or merged until after approval."
)


@dataclass
class ActionConfig:
    instruct: str = ""

    def to_dict(self) -> dict:
        return {"instruct": self.instruct}


def load_action_config(path: Path | None = None) -> ActionConfig:
    p = Path(path or os.getenv("ASST_ACTION_CONFIG", ACTION_CONFIG_PATH))
    if not p.exists():
        return ActionConfig(instruct=DEFAULT_ACTION_INSTRUCT)
    try:
        return ActionConfig(instruct=p.read_text().strip())
    except OSError:
        return ActionConfig(instruct=DEFAULT_ACTION_INSTRUCT)


def save_action_config(cfg: ActionConfig, path: Path | None = None) -> None:
    p = Path(path or os.getenv("ASST_ACTION_CONFIG", ACTION_CONFIG_PATH))
    p.write_text(f"{cfg.instruct.strip()}\n")