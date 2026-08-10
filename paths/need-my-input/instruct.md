# Needs My Input

## Purpose
This path fits when only the human can decide: product/design choices, conflicting requirements,
sensitive or precedent-setting changes, or when triage itself is unsure. The assistant frames
the decision with options and escalates.

## Triage criteria (bullet list)
- Strong signal: "should we", "which approach", conflicting requirements, cost/scope trade-offs,
  deprecation/policy questions, near-tie triage confidence.
- Weak signal: ticket mentions a stakeholder decision or open question to the reporter's team.
- Anti-signal: the missing piece is just factual detail (→ need-more-info), or there is clear
  bug/feature evidence (→ those paths).

## Action guidance
1. Summarize the decision, options, and a recommendation with a short rationale.
2. Propose a comment to the reporter/team documenting the decision for the record.
3. Assign the ticket to me.

## Output requirements
- Comment must present options and a recommendation, not just ask an open question.