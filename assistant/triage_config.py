"""Editable global triage config: `config/triage.md`.

Format: an optional JSON front-matter block (`{ "context_fields": [...],
"classify": {...} }`) followed by the triage agent's instructions as markdown.
Loaded fresh on every use (and every API read) so edits apply immediately -
same contract as paths/.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import REPO_ROOT

TRIAGE_CONFIG_PATH = REPO_ROOT / "config" / "triage.md"

DEFAULT_CONTEXT_FIELDS = ["key", "project", "summary", "issue_type", "status_name", "description", "labels"]

DEFAULT_CLASSIFY = {
    "bug_words": ["error", "exception", "crash", "traceback", "stack trace", "wrong", "false", "fails",
                  "broken", "bug", "cannot", "doesn't work", "not working", "regression", "typeerror"],
    "feature_words": ["feature", "enhance", "export", "support", "ability to", "add ", "new ", "improve"],
    "decision_words": ["decision", "we should", "drop support", "policy", "strategy", "deprecat"],
    "more_info_words": ["no repro", "no steps", "missing", "not provided", "sometimes",
                        "unclear", "vague", "random"],
    "bug_type_hints": ["bug", "defect", "incident", "problem"],
    "feature_type_hints": ["story", "feature", "enhancement", "epic"],
    "type_boost": {"bug-fix": 1.0, "new-feature": 0.4},
    "short_desc_threshold": 60,
    "short_desc_bonus": 0.5,
    "hit_score": 0.33,
    "question_bonus": 1.0,
}


@dataclass
class TriageConfig:
    context_fields: list[str] = field(default_factory=lambda: list(DEFAULT_CONTEXT_FIELDS))
    classify: dict = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_CLASSIFY)))
    instruct: str = ""

    def to_dict(self) -> dict:
        return {"context_fields": self.context_fields, "classify": self.classify, "instruct": self.instruct}


def _parse(text: str) -> TriageConfig:
    cfg = TriageConfig()
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            _, meta_raw, body = parts
            try:
                meta = json.loads(meta_raw)
            except (json.JSONDecodeError, ValueError):
                meta = {}
            fields = meta.get("context_fields")
            if isinstance(fields, list) and fields:
                cfg.context_fields = [str(f) for f in fields]
            classify = meta.get("classify")
            if isinstance(classify, dict) and classify:
                merged = {**DEFAULT_CLASSIFY, **classify}
                merged["type_boost"] = {**DEFAULT_CLASSIFY["type_boost"], **(classify.get("type_boost") or {})}
                cfg.classify = merged
            cfg.instruct = body.strip()
            return cfg
    cfg.instruct = text.strip()
    return cfg


def load_triage_config(path: Path | None = None) -> TriageConfig:
    p = Path(path or TRIAGE_CONFIG_PATH)
    if not p.exists():
        return TriageConfig()
    try:
        return _parse(p.read_text())
    except OSError:
        return TriageConfig()


def _serialize(cfg: TriageConfig) -> str:
    meta = json.dumps({"context_fields": cfg.context_fields, "classify": cfg.classify}, indent=2)
    return f"---\n{meta}\n---\n{cfg.instruct.strip()}\n"


def save_triage_config(cfg: TriageConfig, path: Path | None = None) -> None:
    p = Path(path or TRIAGE_CONFIG_PATH)
    p.write_text(_serialize(cfg))