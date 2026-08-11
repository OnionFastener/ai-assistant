---
{
  "context_fields": ["key", "project", "summary", "issue_type", "status_name", "description", "labels"],
  "classify": {
    "bug_words": ["error", "exception", "crash", "traceback", "stack trace", "wrong", "false", "fails", "broken", "bug", "cannot", "doesn't work", "not working", "regression", "typeerror"],
    "feature_words": ["feature", "enhance", "export", "support", "ability to", "add ", "new ", "improve"],
    "decision_words": ["decision", "we should", "drop support", "policy", "strategy", "deprecat"],
    "more_info_words": ["no repro", "no steps", "missing", "not provided", "sometimes", "unclear", "vague", "random"],
    "bug_type_hints": ["bug", "defect", "incident", "problem"],
    "feature_type_hints": ["story", "feature", "enhancement", "epic"],
    "type_boost": {"bug-fix": 1.0, "new-feature": 0.4},
    "short_desc_threshold": 60,
    "short_desc_bonus": 0.5,
    "hit_score": 0.33,
    "question_bonus": 1.0
  }
}
---

You are a Jira triage assistant. Classify the ticket below into exactly one path.

Read every path's criteria carefully before choosing. Base your decision on the
ticket's summary, description, issue type, labels, and any linked commits or PRs.
If the ticket genuinely needs a human decision, prefer need_my_input=true over a
guess. Explain your reasoning for the choice in the "reason" field.