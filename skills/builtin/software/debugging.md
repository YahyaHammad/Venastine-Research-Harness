---
name: debugging
description: Locating a defect by narrowing rather than guessing, and fixing it where the wrong value is produced
additional_tools: [read, shell]
---

## Debugging methodology

### Reproduce it before theorising about it

A defect you cannot reproduce is one you cannot confirm you fixed. Get to
a command, an input or a sequence that fails reliably, and write it down.
If it fails intermittently, say how often — one in two and one in a
thousand are different problems with different causes.

Where reproduction is genuinely impossible, say so explicitly and treat
everything downstream as a hypothesis, the fix included.

### Shrink the case until nothing can be removed

Cut the input, the configuration and the code path until removing anything
else makes the failure disappear. What remains is usually close to the
cause, and it is what makes a regression test that discriminates. Most of
the value of debugging comes from this step, and most of the time is spent
avoiding it.

### Read the whole error

The first line of a traceback names the symptom; the cause is usually
further down, or in the frame above. An exception arriving three layers
from where it was created has already told you something about the
boundary it crossed. Do not start editing until you can say which line
produced the wrong value, as opposed to which line noticed it.

### Bisect in whichever dimension is cheapest

Time (which commit), space (which module), input (which field), or
configuration (which setting). Each halving is worth more than an hour of
staring, and `git bisect` is only the most familiar instance of the move.

### Confirm the mechanism; correlation is not a diagnosis

"It works when I change this" does not identify a cause. Before fixing, be
able to say why the broken version produced the wrong result, in a
sentence with a subject and a verb. Without one you cannot tell a fix from
a coincidence that reorders something, and the defect returns later under
a different symptom.

### Fix at the producer

Correct the place that creates the bad value, not each place that copes
with it. A guard at the consumer leaves the wrong value in the system,
moves the failure somewhere less obvious, and has to be repeated at every
future consumer. When you deliberately fix at the consumer instead —
because the producer is out of reach — say that you did, and why.

### Leave the test behind, and check that it discriminates

The regression test should fail against the code as it was. Verify that
rather than assuming it: a test written after the fix, from the fixed
code, frequently passes both ways and pins nothing.
