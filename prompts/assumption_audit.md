# Pass 5 — Assumption Audit

You will receive: the original query, every extracted claim (both those that went through source grounding/critique and those that didn't, since this pass covers ALL claims regardless of type), the grounding results, the critic findings, and the completeness findings.

This pass runs deliberately BEFORE confidence tiering — a hidden assumption or framing issue you identify here should be able to downgrade a claim's eventual confidence tier, which would be impossible if tiering had already happened.

Check for:
- **Hidden premises**: unstated assumptions baked into the original query itself, or into how the response answered it, that aren't obviously true.
- **Framing issues**: ways the query or the response's framing could bias the answer toward a particular conclusion, or presupposes a contested position as settled.
- **Domain gaps**: places where answering well would require domain knowledge or context that doesn't appear to have been brought to bear (use the completeness findings to help spot these).

For claims specifically, flag any that rest on an assumption you identified, reference a contested framing, or fall into an identified domain gap.

Respond with ONLY this JSON structure:

```json
{
  "hidden_premises": ["..."],
  "framing_issues": ["..."],
  "domain_gaps": ["..."],
  "per_claim_flags": {
    "c001": ["short description of the specific assumption/framing issue affecting this claim"]
  }
}
```

Only include claim ids in `per_claim_flags` that actually have a flag — omit claims with no issues rather than including them with an empty list.
