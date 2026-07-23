# Pass 3b — Critic Pass

You will receive the original raw response, the factual claims extracted from it, and their grounding results.

Check for:
- **Internal contradictions**: does any claim conflict with another claim elsewhere in the response?
- **Logical fallacies**: non-sequiturs, false dichotomies, correlation-causation conflation, circular reasoning, or other reasoning errors in how claims are connected or justified.

Weight severity by how well-grounded the claim already is: a contradiction involving a well-grounded (`grounded` status) claim is more serious than one involving an already-`ungrounded` guess, since undermining something otherwise-credible is a bigger problem than flagging an issue with something already known to be shaky. Use this weighting to inform the `severity` score below, not as a separate field.

Respond with ONLY this JSON structure — one entry per factual claim you were given (include claims with no issues, using empty lists and severity 0.0):

```json
[
  {
    "claim_id": "c001",
    "fallacies": ["..."],
    "contradictions": ["..."],
    "severity": 0.0
  }
]
```

`severity` is 0.0 (no issues) to 1.0 (severe — e.g. a flat contradiction undermining an otherwise well-grounded claim). Be specific in `fallacies`/`contradictions` — name the actual reasoning error or quote the conflicting claim, don't just say "there is a contradiction."
