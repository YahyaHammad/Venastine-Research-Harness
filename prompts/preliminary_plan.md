# Pass 0 — Preliminary Plan

You will receive only the original query or task. Do not attempt to answer it.

Your job is to sketch, in advance, what a complete and correct answer should probably contain. This is a forward-looking prediction, not a constraint — later passes may deviate from it freely, and the eventual answer is not required to match this sketch. Its only purpose is to give later passes (specifically completeness-checking and assumption-auditing) something anchored to the original query to compare against, independent of whatever the actual generated answer turns out to contain.

Think about:
- What TYPES of claims a thorough answer would need to make (factual assertions, synthesized reasoning, genuinely speculative extrapolation).
- What entities, subjects, systems, or named things are central to this query.
- What kinds of sources would plausibly ground this topic (academic papers, official documentation, news, statistics, primary sources — whatever fits).
- What gaps or edge cases an incomplete answer would likely miss — the parts of this query that are easy to overlook.

Respond with ONLY this JSON structure:

```json
{
  "expected_claim_types": ["..."],
  "key_entities_or_subjects": ["..."],
  "expected_source_domains": ["..."],
  "anticipated_gaps_or_edge_cases": ["..."],
  "notes": "free text -- what a complete, correct answer should probably contain"
}
```
