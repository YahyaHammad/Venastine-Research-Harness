# Pass 6a — Revise

You will receive every currently-flagged claim in a single batch, each paired with the specific feedback that got it flagged: its critic findings (fallacies/contradictions), its grounding results, and any assumption-audit flags.

For each flagged claim, rewrite it to actually address the specific issue(s) raised — not just soften the language. If a claim was flagged as ungrounded, either revise it to something the available evidence actually supports, or explicitly narrow/qualify it to what can be defended. If a claim was flagged for a logical fallacy or contradiction, fix the actual reasoning, don't just hedge around it. If a claim was flagged for a hidden assumption, either state the assumption explicitly or revise the claim to not depend on it.

Do not revise every claim identically with generic hedge language ("some evidence suggests," "it is possible that") — that would defeat the purpose of this pass. Each rewrite should reflect what the SPECIFIC feedback for that claim actually said was wrong.

Respond with ONLY this JSON structure — one entry per flagged claim you were given:

```json
[
  {"claim_id": "c001", "revised_text": "the corrected/rewritten claim text"}
]
```
