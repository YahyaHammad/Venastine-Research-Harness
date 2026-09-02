---
name: formal-specification
description: Saying precisely what a system must do before building it — preconditions, invariants, and the states the specification forbids
additional_tools: [logic, discrete_math, symbolic_math]
---

## Formal specification methodology

### Specify the what, never the how

A specification that describes an algorithm has ruled out every other
correct implementation and cannot be used to judge the one that was
written. Say what must be true of the result. If the method genuinely
matters — because a caller depends on its complexity or its ordering —
that is itself a property to state, not a reason to describe the code.

### Write the contract as three parts

- **Precondition** — what must hold when this is called. Whose job is it
  to establish it, the caller's or the callee's? Say which; unowned
  preconditions are checked twice or not at all.
- **Postcondition** — what is true when it returns, related to the inputs.
- **Invariant** — what is true before and after, which the operation may
  break in the middle and must restore.

The frame matters as much as the three: say what the operation does *not*
change. Most specifications are silent about it and most defects live
there.

### Quantifiers, explicitly, with their types

"All items are valid" is not a specification until you say all items in
what, valid by which predicate, and what happens for the empty collection.
A "for all" quietly becoming a "there exists" is the single most common
way a specification and its implementation come to disagree while both
look right.

The empty case, the singleton case and the duplicate case should each be
answered by the text.

### Say what is forbidden, not only what is required

The reachable-state question is usually the interesting one: which
combinations of values must never occur together? Those are the assertions
worth checking at runtime, and they are what a reviewer needs in order to
find the path that produces one.

### Make the failure behaviour part of the specification

Under what conditions does this refuse, and what does it leave behind when
it does? "Undefined behaviour on bad input" is a decision with
consequences, so make it explicitly — and if partial effects are possible
on failure, say exactly which, because a caller cannot write recovery
against silence.

### Prefer a testable statement to an elegant one

If a property cannot be checked — by a test, an assertion, a type, or a
proof — it will drift from the implementation without anyone noticing.
When a property genuinely resists checking, mark it as an assumption the
design rests on rather than as something the system guarantees.

### Version the specification with the interface

A specification that no longer matches the code is worse than none,
because it is believed. When behaviour changes, the specification changes
in the same commit, and if it does not, the change is incomplete.
