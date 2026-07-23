# Pass 3c — Completeness

You will receive the ORIGINAL query and the preliminary plan sketch from Pass 0. You will NOT receive the actual generated response — this is deliberate. Form your own independent picture of what a complete answer to this query should cover BEFORE seeing what was actually produced, so your assessment isn't anchored to (or limited by) the structure of an answer you haven't seen.

Think through the query fresh: what would a thorough, complete answer need to address? Use the preliminary plan as a starting point, but reason past it if you can identify things it missed too.

Respond with ONLY this JSON structure:

```json
{
  "gaps": ["specific topic, angle, or sub-question a complete answer would need to cover"],
  "coverage_score": 0.0
}
```

`gaps` should be specific enough to act on later (not "more detail needed" but "the query asks about long-term effects, which requires addressing X specifically"). `coverage_score` is your own estimate (0-1) of how much of a genuinely complete answer you'd expect a well-covered response to represent — this is a baseline expectation, not a score of anything you've seen.

Note: since you have not seen the actual response, `gaps` here represents the FULL set of things a complete answer needs, not a diff against a specific text. That comparison happens downstream, using this output.
