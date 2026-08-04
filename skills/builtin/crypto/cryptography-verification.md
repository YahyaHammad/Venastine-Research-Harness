---
name: cryptography-verification
description: Checking cryptographic constructions, protocols and claimed security properties
additional_tools: [symbolic_math, probability_stats, arxiv_search]
---

## Cryptography verification methodology

### State the model before the claim

A security claim is meaningless without its setting. Before evaluating
anything, pin down:

- **What the adversary can do** — passive, chosen-plaintext, chosen-
  ciphertext, adaptive; online or offline; with or without side channels.
- **What "broken" would mean here** — distinguishing from random, key
  recovery, forgery, malleability, or a distinguisher with impractical
  data complexity.
- **What is assumed hard** — factoring, discrete log, LWE, a hash being a
  random oracle, a cipher being a PRP.

A construction is not "secure"; it is secure *against this adversary*
*under this assumption*. Most bad cryptography analysis is a true
statement in the wrong model.

### Attack the joints, not the primitives

AES and SHA-256 are almost never the problem. Look at:

- **Nonce and IV handling** — reuse under CTR/GCM is catastrophic and
  common. Ask where the nonce comes from and what happens after a restart.
- **Key derivation and separation** — one secret used for two purposes.
- **Padding and error behaviour** — anything that lets an attacker
  distinguish failure modes.
- **Ordering** — encrypt-then-MAC vs MAC-then-encrypt, and whether the MAC
  covers the associated data, the length, and the nonce.
- **Comparison** — non-constant-time equality on a tag or an HMAC.
- **Randomness source** — and whether it is seeded before first use.

### Do the arithmetic, do not assert it

When a claim rests on a number — a birthday bound, a collision
probability, a work factor, a data-complexity threshold — compute it.
Use `symbolic_math` or `probability_stats` rather than reciting a
remembered figure, and show the expression. Remembered constants are
where confidently wrong answers come from in this domain.

State work factors as base-2 logarithms and say what a bound assumes: a
2^128 figure that presumes single-target security is a different claim
from one that survives multi-target attacks.

### Never approve a hand-rolled construction

If the analysis is of a bespoke scheme, the finding is "use the standard
construction" plus the specific reason this one differs. Say what the
standard alternative is (AEAD instead of separate encrypt and MAC, a
password hash instead of a fast hash with a salt, an existing PAKE instead
of a custom handshake). Identifying that a bespoke design happens to
resist the one attack you thought of is not a security result.

### What you cannot conclude

You cannot conclude a scheme is secure. You can conclude that a specific
attack does not apply, that a claimed proof has a gap, or that a
construction deviates from a standard in a way with known consequences.
Say which of those you did.
