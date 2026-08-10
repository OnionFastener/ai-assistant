# Need More Information

## Purpose
This path fits when the ticket is too vague to act on: missing repro steps, missing versions,
unclear acceptance criteria, or half-written descriptions. The assistant drafts a precise list
of the missing facts and asks the reporter to supply them.

## Triage criteria (bullet list)
- Strong signal: empty/minimal description, open questions, "sometimes it fails", no version or
  repro steps, no error message.
- Weak signal: description present but environment/facts missing.
- Anti-signal: completeness of description does not matter (→ need-my-input when the blocker is
  actually a decision, not missing facts).

## Action guidance
1. List exactly what is missing (env, steps, expected/actual, logs/screenshots).
2. Ask focused questions — one numbered list, no essay.
3. Propose moving the ticket back to a needs-info state.

## Output requirements
- Comment = numbered questions only. Prefer asking for the single most diagnostic piece of
  evidence first.