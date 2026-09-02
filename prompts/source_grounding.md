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
      {
        "url": "...",
        "quote": "...",
        "similarity_score": 0.0,
        "authority_adjustment": 0.0,
        "authority_reason": "..."
      }
    ],
    "status": "grounded"
  }
]
```

If status is `ungrounded`, `sources` may be an empty list.

## `quote` — what the source actually says

The verbatim span from that source which supports the claim: copy it exactly as it appeared, do not paraphrase it, do not correct its spelling, and do not stitch together text from two places. One to three sentences is the right length. This is the passage your `similarity_score` is about.

If you cannot quote the source — you are citing it from a search result you did not open, or the page had nothing quotable — use an empty string rather than reconstructing what you believe it said. An invented quote is worse than an absent one: quotes are checked against the retrieved text.

## `similarity_score` (0–1) — how closely that passage matches this claim

Score the passage you quoted. Not the page's title, not the search result's summary, and not your own memory of what this source says.

| score | the passage… |
|---|---|
| 0.9–1.0 | states the claim explicitly, including its specific number, date, name or quantity |
| 0.7–0.9 | states the claim, but with a different scope or qualifier (a range where the claim gives a point, one country where the claim says a region) |
| 0.5–0.7 | supports the general assertion but not the specific detail the claim turns on |
| 0.3–0.5 | is on the same topic and does not address the assertion |
| 0.0–0.3 | mentions the entity only, or is off-topic |

A source that is authoritative but does not discuss this specific claim scores LOW here. Authority and relevance are separate questions and this is the relevance one.

## `authority_adjustment` (−0.15 to +0.15) — a correction, not a score

How authoritative a source is, is computed in code from its domain and — for published research — its venue, citation standing and retraction status. You are not asked for that number. You are asked only whether the computed one is wrong about *this specific page*, in a way a domain cannot see.

Use `0.0` and an empty `authority_reason` unless one of these applies. This is the normal answer.

Raise it when the page is a primary source that its domain does not signal: an organisation's own filing, dataset, standard or specification; a manufacturer's own technical documentation for its own product; an author's own publication of their own results.

Lower it when the page is weaker than its domain suggests: an opinion column, sponsored or promotional content, a user-editable page, a press release restating someone else's finding, or a page that is clearly outdated for a claim about the present.

`authority_reason` must name which of these it is, in a few words, whenever the adjustment is not `0.0`. An adjustment with no reason is discarded.
