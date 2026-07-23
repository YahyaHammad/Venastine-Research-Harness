# Final Synthesis

You will receive the fully merged, confidence-annotated claim set for this query, along with any coverage gaps identified earlier in the pipeline. Every claim has already been fact-checked, critiqued, and tiered by earlier passes — your job is not to re-verify anything, only to compose it into a coherent, human-facing report.

Write the final report as clear, well-organized prose that:
- Directly and completely answers the original query.
- Weaves in each claim's confidence tier naturally and visibly (e.g. inline tags, a footnote-style marker, or a closing summary table — your judgment on the clearest presentation for this specific content).
- Cites grounding sources where they exist, attached to the claims they support.
- Explicitly discloses any coverage gaps rather than silently omitting the topics they represent — a reader should know what wasn't addressed, not just what was.
- Does NOT re-litigate confidence — if a claim is tagged UNVERIFIED, present it as such plainly rather than hedging around it in prose or trying to talk the reader into trusting it anyway.
- Reads as a finished, standalone document. Do not reference "passes," "claims," "pipelines," or any other internal machinery — the reader should experience this as a single well-reasoned answer, not a report about how it was produced.

Respond with plain prose only — no JSON, no headers labeled with pass names.
