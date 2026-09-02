---
name: test-design
description: Writing tests that can actually fail — choosing what to pin, and checking that the assertion discriminates
additional_tools: [read, shell]
---

## Test design methodology

### A test's value is entirely in what makes it fail

Before writing an assertion, name the change to the production code that
this test would catch. If you cannot name one, the test is decoration: it
adds runtime, it adds the appearance of coverage, and it will later be
cited as evidence that the behaviour is protected.

The cheapest check is to break the code on purpose — revert the fix,
invert the condition, delete the line — and confirm the test goes red. Do
this once for every non-obvious test. It finds more than rereading does.

### Watch for the assertion that cannot discriminate

An assertion is vacuous when the environment guarantees it regardless of
the code. Sorting is the classic case: if the input already arrives in
sorted order, a test of the sort cannot tell it from a no-op. Construct
inputs that would come back wrong if the behaviour were absent —
reversed, out of range, colliding, duplicated.

Watch equally for a check whose passing condition is "found nothing".
Such a test cannot detect that it looked in fewer places, so pin the
domain in both directions: something that must be seen, and something
that must not.

### Test the boundary, not the middle

Off by one, empty, exactly one, exactly the limit, one over the limit, the
duplicate, the null. The interior of a range is where the code is already
correct.

### One reason to fail per test

When a test asserts five things, its failure names one and you learn
nothing about the other four. Split by the reason it would go red, not by
the function it calls.

### Name the test after the behaviour

`test_a_rejected_payment_leaves_the_cart_untouched` survives a rename and
tells a reader what broke. `test_process_payment_2` does neither. The name
is the first thing anyone reads when the suite goes red at an
inconvenient moment.

### Assert on the observable, not the implementation

Prefer the value a caller sees over the internal call that produced it: a
test asserting that a helper was called still passes when the helper is
called and its result thrown away. There is a real exception — sometimes
the arguments a decision was made with are exactly what needs pinning,
because an outcome check would pass for an implementation that read the
wrong field and happened to agree — but take it deliberately and say so in
the docstring.

### Hold the environment still

A test whose result depends on filesystem ordering, wall-clock time,
locale, network reachability, or which test ran before it is a source of
noise that eventually gets labelled flaky and ignored. Inject the clock,
pin the ordering, isolate the state.
