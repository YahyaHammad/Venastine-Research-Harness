# Pass 3a — Source Grounding

You will receive a list of factual claims, and a deduplicated list of the unique entities/subjects those claims collectively reference (already deduplicated in code — you do not need to do this yourself).

For each unique entity in the deduplicated list, search for it ONCE using the tools available to you (web search, arxiv search, or fetching a specific URL, whichever fits the entity). Do not search once per claim — search once per unique entity, then apply what you find to every claim that referenced it. If the first search result is ambiguous or clearly insufficient, you may run one additional, more specific search for that same entity — but no more than two searches per entity.

Critical: derive your search query from the entity/subject itself, not from the claim's exact wording. If a claim says "the treaty was signed in 1947," search for the treaty's actual signing date as a topic — do not search for confirmation of that literal sentence. Searching for validation of your own starting point is how unverified claims end up looking "confirmed."

For each claim, determine a grounding status:
- `grounded`: one or more credible sources directly support the claim.
- `partial`: sources exist and are relevant, but don't fully confirm the specific claim as stated (e.g. confirm the general topic but not the specific number/date/detail).
- `ungrounded`: no credible source found supporting the claim, or search results contradict it.

Respond with ONLY this JSON structure — one entry per factual claim you were given:

```json
[
  {
    "claim_id": "c001",
    "sources": [
      {"url": "...", "authority_score": 0.0, "similarity_score": 0.0}
    ],
    "status": "grounded"
  }
]
```

`authority_score` (0-1) reflects how credible/authoritative the source itself is (official/primary source and reputable outlets score higher than forums or unattributed content). `similarity_score` (0-1) reflects how closely the source's actual content matches the specific claim being checked, not just the general topic. If status is `ungrounded`, `sources` may be an empty list.
