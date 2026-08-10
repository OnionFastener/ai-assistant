# Bug Fix

## Purpose
This path fits when a Jira ticket reports broken behavior with evidence: repro steps, a stack
trace, a failing assertion, or a regression introduced by a recent commit. The goal is a
minimal, tested fix pushed as a PR.

## Triage criteria (bullet list)
- Strong signal: stack trace / exception / "does not work" / "broken" / regression report,
  plus a repro path or recent commit touching the failure area.
- Weak signal: "wrong value" / "incorrect result" without repro steps.
- Anti-signal / when NOT to choose this path:
  - vague complaints with no repro or version info (→ need-more-info);
  - feature requests or new behavior (→ new-feature);
  - pure design/product decisions (→ need-my-input).

## Action guidance
1. In the sandbox clone, find the code touching the error site from the traceback/failing test.
2. Reproduce: run the failing test or a quick repro script.
3. Make the smallest change that fixes the root cause; add or update a regression test.
4. Run the relevant test suite. Show the final diff.
5. Propose: comment with root-cause explanation, transition, assign, push_branch + create_pr.

## Output requirements
- PR body must state the root cause and how the change was verified.
- Never change unrelated code; keep the diff reviewable.