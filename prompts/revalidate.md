# Pass 6b — Re-validate

You will receive the revised claims from Pass 6a — ONLY the claims that were just rewritten, not the full original claim set.

Re-run the same checks Pass 3a (source grounding) and Pass 3b (critic) performed, but scoped to just this revised subset:

1. **Grounding**: for each revised claim, re-derive a search query from its (now possibly different) entity/subject and check it against available sources, the same way Pass 3a did — deduplicate entities across this batch first, search once per unique entity, map back to claims. Assign a fresh `grounded`/`partial`/`ungrounded` status based on the REVISED text, not the original.
2. **Critic**: re-check the revised claims for fallacies and contradictions, the same way Pass 3b did, including against the rest of the (unrevised) claim set if relevant context is available to you.

Respond with ONLY this JSON structure:

```json
{
  "grounding": [
    {"claim_id": "c001", "sources": [{"url": "...", "quote": "...", "similarity_score": 0.0, "authority_adjustment": 0.0, "authority_reason": "..."}], "status": "grounded"}
  ],
  "critic": [
    {"claim_id": "c001", "fallacies": ["..."], "contradictions": ["..."], "severity": 0.0}
  ]
}
```

Include an entry in both `grounding` and `critic` for every claim you were given, even if a section is empty (e.g. `"fallacies": []`).

The `sources` entries follow Pass 3a's rules exactly, and this is a re-check rather than a re-use: quote the passage verbatim from the source, score `similarity_score` against the passage you quoted and against the REVISED claim text, and leave `authority_adjustment` at `0.0` unless this specific page is stronger or weaker than its domain suggests. A source that supported the original wording may not support the revision, and carrying its old score across is the thing re-validation exists to catch.
