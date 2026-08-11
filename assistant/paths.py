"""Triage-path registry: each path is a folder `paths/<id>/{instruct.md,schema.json}`.

Loaded fresh on every run and on every /api/paths request, so edits apply immediately -
no restart, no code change. A folder failing validation is skipped (fail-open).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

VALID_ACTIONS = {"comment", "transition", "assign", "push_branch", "create_pr", "edit_ticket"}
PATH_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass
class TriagePath:
    id: str
    name: str
    enabled: bool = True
    allowed_actions: list[str] = field(default_factory=list)
    required_backend: str | None = None
    work: dict = field(default_factory=dict)
    approval: dict = field(default_factory=dict)
    default_actions: list[dict] = field(default_factory=list)
    instruct: str = ""
    behavior: str = ""
    schema: dict = field(default_factory=dict)
    valid: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "allowed_actions": self.allowed_actions,
            "required_backend": self.required_backend,
            "work": self.work,
            "approval": self.approval,
            "default_actions": self.default_actions,
            "instruct": self.instruct,
            "behavior": self.behavior,
            "valid": self.valid,
            "error": self.error,
        }


def load_paths(paths_dir: Path) -> list[TriagePath]:
    paths_dir = Path(paths_dir)
    paths: list[TriagePath] = []
    if not paths_dir.exists():
        return paths
    for folder in sorted(p for p in paths_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        paths.append(_load_one(folder))
    return paths


def _load_one(folder: Path) -> TriagePath:
    path_id = folder.name
    try:
        schema = json.loads((folder / "schema.json").read_text()) if (folder / "schema.json").exists() else {}
        instruct = (folder / "instruct.md").read_text() if (folder / "instruct.md").exists() else ""
        behavior_file = folder / "behavior.md"
        if behavior_file.exists():
            behavior = behavior_file.read_text().strip() or instruct
        else:
            behavior = instruct
        errors = _validate(path_id, schema)
        if errors:
            return TriagePath(id=path_id, name=path_id, valid=False, error="; ".join(errors))
        allowed = [a for a in schema.get("allowed_actions", []) if a in VALID_ACTIONS]
        return TriagePath(
            id=path_id,
            name=str(schema.get("name", path_id)),
            enabled=bool(schema.get("enabled", True)),
            allowed_actions=allowed,
            required_backend=schema.get("required_backend"),
            work=schema.get("work", {}) or {},
            approval=schema.get("approval", {}) or {},
            default_actions=schema.get("default_actions", []) or [],
            instruct=instruct,
            behavior=behavior,
            schema=schema,
        )
    except Exception as e:  # noqa: BLE001
        return TriagePath(id=path_id, name=path_id, valid=False, error=str(e))


def _validate(path_id: str, schema: dict) -> list[str]:
    errors = []
    if not PATH_ID_RE.match(path_id):
        errors.append(f"invalid path id '{path_id}' (must be kebab-case a-z0-9)")
    if not isinstance(schema, dict):
        errors.append("schema.json must be a JSON object")
        return errors
    for a in schema.get("allowed_actions", []):
        if a not in VALID_ACTIONS:
            errors.append(f"unknown action '{a}' (known: {sorted(VALID_ACTIONS)})")
    return errors


def get_allowed(paths: list[TriagePath], path_id: str) -> list[str]:
    for p in paths:
        if p.id == path_id:
            return p.allowed_actions
    return ["comment"]


def get_path(paths: list[TriagePath], path_id: str) -> TriagePath | None:
    return next((p for p in paths if p.id == path_id), None)