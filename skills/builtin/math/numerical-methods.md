---
name: numerical-methods
description: Knowing when a floating-point answer still means something — conditioning, stability, error growth, and the tests that catch a silently wrong result
additional_tools: [symbolic_math, linear_algebra, probability_stats]
---

## Numerical methods methodology

### Separate the problem's conditioning from the algorithm's stability

They are different failures with different remedies. **Conditioning** is a
property of the problem: how much the answer moves when the input moves.
**Stability** is a property of the method: how much error the computation
adds on its own. A stable algorithm on an ill-conditioned problem still
returns a meaningless answer, and no amount of better code fixes it —
reformulating the problem does.

Say which one you are looking at before proposing a fix.

### Catastrophic cancellation is the common cause

Subtracting two nearly equal numbers destroys the leading digits and
leaves whatever noise they carried. It is why the textbook quadratic
formula fails, why computing variance as `E[x²] − E[x]²` fails, and why a
difference of large accumulated sums fails. Where you see a subtraction of
similar quantities, look for the algebraically equivalent form that does
not perform it.

### Never test floating-point values for equality

Compare against a tolerance, and choose it deliberately: relative
tolerance for values that vary in magnitude, absolute tolerance near zero
where relative tolerance is meaningless, both when the range spans zero.
A tolerance picked to make a test pass is a tolerance that will not catch
the regression it was written for.

### Track how error accumulates

An error bound per operation says little about a loop that runs a million
times. Ask whether error grows linearly, or is amplified each step — and
in an iterative method, whether the iteration is contracting. Report the
growth, not the per-step bound.

### Check the units and the scale before the algorithm

A large fraction of "numerical instability" is a variable in the wrong
units and a matrix whose columns differ by ten orders of magnitude.
Non-dimensionalise, or scale the rows and columns, before reaching for a
more sophisticated solver.

### Verify against something independent

Three cheap checks, roughly in order of value:

- A case with a known closed-form answer.
- A conservation law or invariant the result must satisfy.
- The same computation at higher precision, or via an independent method.

Agreement between two implementations of the *same* method proves only
that you transcribed it twice.

### Say what the answer's precision actually is

Reporting ten digits from a computation good to four is a claim about
accuracy, and it is false. Round to what the analysis supports, and say
what that is.
