# Pass 2 — Claim Extraction & Independent Classification

You will receive a raw response text. Break it down into atomic, self-contained claims, and classify each one's type from its content alone.

Important: classify each claim independently, based purely on what it asserts — not based on any hedge language, caveats, or framing the original text used. The text you're reading was written without self-tagging (by design), so you cannot rely on phrases like "it's possible that" to identify speculation; you must judge each claim on its actual substance.

Claim types:
- `factual`: a checkable assertion about the world (a date, a mechanism, a statistic, a causal claim, a definition).
- `synthesis`: a claim that combines or reasons over multiple facts, valid as an inference but not itself a single checkable fact.
- `speculative`: an extrapolation, prediction, or claim that goes meaningfully beyond what current evidence could directly establish.

For each claim, identify the key entities or subjects it references (proper nouns, named systems, specific concepts) — this will be used later to batch fact-checking efficiently, so be precise and use consistent naming for the same entity across claims (e.g. always "the Treaty of Westphalia," not sometimes "the treaty" and sometimes "Westphalia").

Respond with ONLY this JSON structure — a flat list, one entry per claim:

```json
[
  {
    "id": "c001",
    "text": "the atomic, self-contained claim, in your own words",
    "type": "factual",
    "entities": ["..."],
    "source_span": "approximate location or quote from the raw response this claim came from"
  }
]
```

Use sequential zero-padded ids (c001, c002, ...). Extract every distinct claim — don't merge unrelated assertions to save space, and don't split one claim into fragments that aren't independently meaningful.
