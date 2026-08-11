# Bug Fix — action behavior

This ticket is a confirmed bug. Work this way:

1. Reproduce first: run the failing test or a quick repro from the ticket.
2. Find the root cause by reading the code at the error site, not by guessing.
3. Make the smallest change that fixes the root cause.
4. Add or update a regression test that fails before the fix and passes after.
5. Run the relevant test suite. Show the final diff in your summary.

Never touch unrelated code. If the ticket turns out to be a feature request or a
question instead of a bug, say so in the summary instead of forcing a fix.