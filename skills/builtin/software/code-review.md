---
name: code-review
description: Reading a change for defects rather than for style, and reporting each one with the failure it produces
additional_tools: [read, shell, read_project_doc]
---

## Code review methodology

### Establish what the change claims before reading it

A diff on its own can only be read as itself. Read the commit message, the
issue or the docstring first, then read the change as an answer to that
claim. The most common real defect is a disagreement between the two — the
message says one thing was fixed, the diff does that and one other thing
nobody mentioned — and it is invisible to a reader who starts at line one
of the diff.

### Read the call graph, not the changed lines

A diff shows what moved. It does not show the caller three files away that
passes the argument the new code no longer tolerates, the subclass that
overrides the method whose contract just changed, or the second call site
of the helper that was fixed in one place. Find the callers before you
approve the change.

### A finding needs a failure, not a preference

For each item, name the input, state or sequence that produces the wrong
result. If you cannot construct one, say so and label the item a concern
rather than a defect. Both are worth reporting; reporting them in the same
voice makes the serious ones unfindable.

Rank by consequence: wrong answers first, then silently wrong answers
(worse than a crash, because nothing reports them), then security and data
loss, then anything that makes the next change harder. Style last, if at
all.

### Look hardest at what is absent

Most surviving defects are omissions rather than errors:

- The error path nobody wrote, or one that swallows the cause.
- The caller that was not updated.
- The invariant that used to hold and now holds only by accident.
- The case the new tests cannot distinguish from the old behaviour.

Ask what would have to be added for the change to be complete, not only
whether what is present is correct.

### Judge the tests by whether they could fail

A test that passes against the unfixed code is not testing the fix. When a
change ships with tests, check at least one against the behaviour it
claims to pin: would it go red if the fix were reverted? If the answer is
no, that is a finding about the test, and it is often the most valuable
thing in the review.

### Say when it is fine

If the change is sound, say so and stop. Manufacturing findings to justify
the time spent teaches the author to skim your reviews.
