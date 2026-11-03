# Pass 6a — Revise

You will receive every currently-flagged claim in a single batch, each paired with the specific feedback that got it flagged: its critic findings (fallacies/contradictions), its grounding results, and any assumption-audit flags.

For each flagged claim, rewrite it to actually address the specific issue(s) raised — not just soften the language. If a claim was flagged as ungrounded, either revise it to something the available evidence actually supports, or explicitly narrow/qualify it to what can be defended. If a claim was flagged for a logical fallacy or contradiction, fix the actual reasoning, don't just hedge around it. If a claim was flagged for a hidden assumption, either state the assumption explicitly or revise the claim to not depend on it.

Do not revise every claim identically with generic hedge language ("some evidence suggests," "it is possible that") — that would defeat the purpose of this pass. Each rewrite should reflect what the SPECIFIC feedback for that claim actually said was wrong.

Some claims also carry `disagreement_feedback`: several models answered the same query independently, and only `asserted_by` of the `of` answers made this claim. That is a reason to check the claim, not evidence that it is wrong, and it may be the only reason the claim is here. Check it against its grounding results. If they support the claim as written, return its text unchanged. If they do not, narrow it to what they do support. Do not mention the disagreement in the claim itself ("not all sources agree that…"): the run already records it, and hedging is not checking.

Respond with ONLY this JSON structure — one entry per flagged claim you were given:

```json
[
  {"claim_id": "c001", "revised_text": "the corrected/rewritten claim text"}
]
```
