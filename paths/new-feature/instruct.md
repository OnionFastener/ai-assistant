# New Feature

## Purpose
This path fits when the ticket requests net-new behavior or capabilities: new endpoints, new
exports, UI features, integrations. The agent implements the feature in the sandbox clone and
proposes the change as a reviewed PR, like a bug fix.

## Triage criteria (bullet list)
- Strong signal: "add / support / implement / new" capability words; acceptance criteria;
  a linked feature-branch PR; or requirements that clearly define new behavior.
- Weak signal: "would be nice to have", "improve UX".
- Anti-signal: bug complaints (→ bug-fix); unclear scope with no acceptance criteria (→ need-more-info).

## Action guidance
1. Restate scope in one paragraph; flag missing acceptance criteria first.
2. If the request has no target repo/area, propose a clarifying comment instead of code.
3. If well-scoped: implement the feature in the sandbox following existing patterns, add or
   extend tests, and run the relevant test suite. Leave changes in the working tree.
4. Propose: comment summarizing the change, push_branch + create_pr, transition.

## Output requirements
- Implement only what the ticket asks; match surrounding style and conventions.
- New public surface (endpoints, functions, config) needs tests and a usage note in the PR body.
- Never overwrite unrelated code; keep the diff reviewable.