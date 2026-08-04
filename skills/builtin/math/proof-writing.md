---
name: proof-writing
description: Structuring and checking a formal mathematical argument
additional_tools: [symbolic_math, linear_algebra, discrete_math, logic]
---

## Proof writing methodology

### Fix the statement before proving anything

Write the claim as a single sentence with every quantifier explicit and
every symbol typed. Most flawed proofs are proofs of a subtly different
statement — one where a "for all" quietly became a "there exists", or a
hypothesis that was used but never stated.

List the hypotheses separately from the conclusion. If a hypothesis is
never used in the proof, either the proof is wrong or the statement is
weaker than claimed; say which.

### Choose the shape deliberately

Name the proof technique before writing: direct, contrapositive,
contradiction, induction, construction, exhaustion of cases. If it is
induction, state the induction variable, the base case, and the induction
hypothesis explicitly — a proof that says "by induction" and then argues
informally is where off-by-one errors live. If it is case analysis, show
that the cases are exhaustive.

### One step, one justification

Every non-trivial step cites the definition, lemma or theorem it uses. A
step that cannot be justified is the interesting part of the problem, not
a detail to move past — flag it rather than smoothing over it with
"clearly" or "it follows that".

Reserve "without loss of generality" for cases where you can say what the
symmetry is. It is frequently used to skip a case that is not actually
symmetric.

### Check the algebra with the tools, not from memory

Use `symbolic_math` for identities and manipulations, `linear_algebra` for
matrix claims, `discrete_math` for counting and number-theoretic steps,
and `logic` for checking that an implication chain is valid. An
algebraically wrong line invalidates everything after it, and it is the
cheapest class of error to eliminate.

### Attack your own argument before presenting it

Try the boundary cases: empty set, zero, one, equality in a strict
inequality, the degenerate configuration. Try to construct a
counterexample for ten seconds before concluding. If the proof would still
"work" with a hypothesis removed, the proof is probably wrong — that is
the sharpest self-check available.

### Report honestly

If a step is unproven, say which step, and state what would close it. A
proof with one clearly marked gap is a useful contribution; a proof with
one unmarked gap is worse than no proof, because it spends the reader's
trust.
