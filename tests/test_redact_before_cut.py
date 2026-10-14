"""
test_redact_before_cut.py

ROADMAP_v3 §52 (RB1-RB4). `fetch_url` redacts the whole body BEFORE it is
cut into pages or held for paging, so a credential straddling a page
boundary is replaced while it is still one string.

WHAT THIS IS ABOUT, in one sentence: §51 introduced a cut, and the redactor
ran after it. A pattern only matches what it can see whole, so a token
spanning character 5,000 was offered to it as two fragments -- and PG7's
append put them back together in the corpus, which is written to
`sources/<sha256>.txt` and chunked into the windows sent to the embedding
provider.

WHAT WOULD MAKE THIS FILE VACUOUS, stated first, because the defect here is
unusually good at hiding from its own tests:

  * CALLING `redact_output_text` AND CHECKING IT REDACTS. That is Trap 14 in
    its purest form. The redactor was never broken; the ORDER of two calls
    was. A test that invokes the redactor directly cannot see the defect,
    because the defect is that something else ran first. Batch 105 shipped
    two tests with this exact shape and a mutation pass caught both. So the
    reproduction below drives `registry.dispatch` -- the real caller, in the
    real order -- and reads the real `SourceCorpus`.
  * ASSERTING THAT A PAGE CONTAINS `[REDACTED]`. A marker proves something
    was replaced, not that the secret is gone; the interesting failure leaves
    a marker AND the token's tail (measured: the open-quantifier patterns do
    exactly that). What is asserted is the absence of the SECRET, in the
    place it would be reassembled.
  * PLANTING THE SECRET SOMEWHERE COMFORTABLE. A secret entirely inside one
    page was always redacted and always will be. Every placement here
    straddles the boundary, and the sweep walks every interior cut position
    rather than one hand-picked offset.
  * TESTING ONE PATTERN. Measured (batch 106), the ten patterns do NOT
    behave alike: the fixed-length vendor tokens, the PEM literal and both
    credential shapes are defeated by EVERY interior cut, while `sk-`,
    `sk-ant-` and `xox` -- whose quantifiers are open-ended -- are defeated
    only by a cut inside their first 14 to 26 characters, because a long
    enough prefix still matches on its own. The §51 gap register said
    "neither half matches a pattern"; that is true of seven of the ten.

MEASURED END TO END BEFORE ANY OF THIS WAS WRITTEN (batch 106), which is
§51's own lesson about the corpus drop applied to its successor: a `ghp_`
token at characters 4,980-5,020 came back clean in both pages, was rejoined
by the corpus into one 10,000-character document, and appeared whole in
`artifact_entries()`.

AND NOW THE TWO SEARCH TOOLS (batch 156, ROADMAP_v3 §73 A59), which is
where §52's own gap register pointed and nobody had gone. `web_search` cut
its snippet to 300 characters and `arxiv_search` cut its abstract to 600
and its title to 300, and in all three places `check_output_policy`
redacted the piece that had been kept. Measured before the fix, through
the real `_normalize` and the real `_parse_atom_feed`: a `ghp_` token
straddling any of those cuts survived post-hoc redaction at 36 of the 39
offsets it could straddle, and redacting the whole value first caught
every one. The same batch added three more cuts (title, author name,
category term) that did not exist before it, so the ordering had to land
first or the repair would have manufactured its own straddle sites.

AND NOW A SECOND TOOL THAT CUTS (batch 148, S11-05 / ROADMAP_v3 §65 A23).
§52's gap register declined to solve this generally -- "a future tool that
returns a long body and slices it would have to make the same choice, and
nothing mechanical forces it to" -- and §49 had already shipped that tool
before the sentence was written: a background session's output is cut in
FOUR places, and the redactor ran after every one of them. Two of the four
are pure functions and are tested below; the producer's own tail cut and
`shell_output`'s paging need a live manager and are in
tests/test_shell_sessions.py, under
TestASessionsOutputIsRedactedBeforeItIsCut. The four vacuity traps above
apply unchanged, and the third one bit the batch's own first probe: a run of
"x" as filler leaves no word boundary before `password`, so the sweep
reported clean rows for a secret the redactor had never matched even
unsplit. `_straddling` below already says so, which is why it is reused
rather than re-derived.
"""

import logging

import pytest

import config
from core import session_view, session_wake
from core.reasoning.source_corpus import SourceCorpus
from core.shell_sessions import SessionRow, WakeEvent
from safety.policy_enforcement import REDACTION_MARKER
from tools.builtin import arxiv, fetch_url, web_search
from tools.registry import registry

URL = "https://example.com/doc"

#: A GitHub personal access token: `ghp_` plus exactly 36 characters. The
#: unambiguous case, and the one the batch-106 probe reproduced with -- the
#: pattern is fixed-length, so no prefix of it matches and no cut inside it
#: leaves anything for the redactor to find.
PAT = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

#: (label, the literal to plant, the substring whose survival is the leak).
#: The two differ for the credential SHAPES, where the pattern matches a
#: field-name-plus-value construct and only the value is the secret.
SECRETS = [
    ("sk_key", "sk-" + "A1b2C3d4E5f6G7h8i9J0k1L2m3N4", None),
    ("sk_ant_key", "sk-ant-api03-" + "A1b2C3d4E5f6G7h8" * 3, None),
    ("github_pat", PAT, None),
    ("aws_key_id", "AKIA" + "QWERTYUIOPASDFGH", None),
    ("google_api_key", "AIza" + "Sy" + "B" * 33, None),
    ("slack_token", "xoxb-" + "1234567890-ABCDEFghijkl", None),
    ("pem_header", "-----BEGIN RSA PRIVATE KEY-----", None),
    ("password_assignment", 'password = "hunter2hunter2"', "hunter2hunter2"),
    ("password_xml", "<password>hunter2hunter2</password>", "hunter2hunter2"),
    ("url_userinfo",
     "https://admin:s3cr3tp4ss@internal.example.com/x", "s3cr3tp4ss"),
]

PAGE = 5000


@pytest.fixture(autouse=True)
def _blocklist(monkeypatch):
    """The REAL `is_url_permitted` runs; only DNS is made deterministic.

    Lifted from `test_paging.py`, which lifted it from `test_fetch_url.py`,
    for the reason that file gives: stubbing the policy checker in a file
    about what `fetch_url` does to a body would leave the composition
    untested in the very place it changed.
    """
    import safety.policy_enforcement as policy
    monkeypatch.setattr(policy, "BLOCKED_DOMAINS", {"blocked.example"})
    monkeypatch.setattr(
        policy.socket, "getaddrinfo",
        lambda host, port, *a, **kw: [
            (None, None, None, "", ("93.184.216.34", 0))])


def _straddling(secret, cut_at):
    """A body in which `secret` crosses the page boundary, `cut_at`
    characters of it sitting in page 1.

    The filler is prose rather than a repeated character because two of the
    three credential shapes anchor on a word boundary -- and the last
    character before the plant is forced to a space for the same reason. A
    secret welded onto the end of a filler word does not match even unsplit,
    so a sweep built on one would be measuring its own fixture rather than
    the code.
    """
    head = ("some configuration prose here and there. "
            * 200)[:PAGE - cut_at - 1] + " "
    assert len(head) == PAGE - cut_at
    return head + secret + " and then more prose follows after it."


def _both_pages(http, body):
    """Page 1 and page 2 of `body`, through the real dispatch path.

    `registry.dispatch` and not `fetch_url.run`: the defect is an ORDERING
    between the tool's own slice and `check_output_policy`, so a test that
    skips the second half of that pair can only see one of the two orders.
    """
    fetch_url._body_cache.clear()
    http.respond(text=body, url=URL)
    first = registry.dispatch("fetch_url", {"url": URL})
    second = registry.dispatch("fetch_url", {"url": URL, "offset": PAGE})
    return first, second


def _corpus_text(first, second):
    corpus = SourceCorpus()
    corpus.add("fetch_url", first)
    corpus.add("fetch_url", second)
    return corpus.text_for(URL)


# ===========================================================================
# ---- RB1: the reproduction, end to end ------------------------------------
# ===========================================================================

class TestTheSplitCredentialIsNotRejoined:
    """The centre of the batch, and the test the mutation pass must not be
    able to kill by moving one statement."""

    def test_a_token_across_the_boundary_never_reaches_the_corpus(self, http):
        body = _straddling(PAT, cut_at=20)
        assert body.index(PAT) == PAGE - 20

        first, second = _both_pages(http, body)

        assert PAT not in _corpus_text(first, second), (
            "the two halves were rejoined by PG7's append -- the body was "
            "cut before it was redacted, which is the whole of RB1")

    def test_neither_half_of_it_survives_in_either_page(self, http):
        """Not the same assertion. The corpus is where the halves become
        adjacent, but each page is separately handed to the model, written
        to the transcript, and replayed -- so a page carrying 20 characters
        of a live token is a leak whether or not anything rejoins them."""
        body = _straddling(PAT, cut_at=20)

        first, second = _both_pages(http, body)

        tail = PAT[20:]
        assert PAT[:20] not in first["content"]
        assert tail not in second["content"]

    def test_it_reaches_the_artifact_and_the_embedding_window_clean(
            self, http):
        """`artifact_entries()` is what `output_writer._write_sources`
        writes to `sources/<sha256>.txt`, under a docstring calling it 'the
        text itself, redacted', and what `source_scoring.source_passages`
        chunks into the windows sent to the embedding provider. Those are
        the two places the rejoined token actually went."""
        body = _straddling(PAT, cut_at=20)
        first, second = _both_pages(http, body)

        corpus = SourceCorpus()
        corpus.add("fetch_url", first)
        corpus.add("fetch_url", second)
        entries = corpus.artifact_entries()

        assert entries, "the corpus held nothing, so this asserted nothing"
        assert not any(PAT in entry["text"] for entry in entries)

    def test_a_secret_wholly_inside_one_page_is_still_redacted(self, http):
        """The control. If this fails the fix broke the ordinary case, and
        if it passes vacuously -- because nothing is ever redacted -- the
        test above could not tell."""
        body = _straddling(PAT, cut_at=len(PAT) + 500)
        assert body.index(PAT) + len(PAT) < PAGE

        first, _ = _both_pages(http, body)

        assert PAT not in first["content"]
        assert REDACTION_MARKER in first["content"]

    def test_an_ordinary_document_is_returned_unaltered(self, http):
        """The other control, and the one that would catch a fix that
        redacted too much. A body with no credentials in it comes back
        exactly as it was served."""
        body = ("The quick brown fox jumps over the lazy dog. " * 400)[:12000]

        first, second = _both_pages(http, body)

        assert first["content"] + second["content"] == body[:10000]
        assert REDACTION_MARKER not in first["content"]
        assert _corpus_text(first, second) == body[:10000]


# ===========================================================================
# ---- RB7: every pattern, every cut position -------------------------------
# ===========================================================================

@pytest.mark.parametrize(
    "label,secret,needle", SECRETS, ids=[row[0] for row in SECRETS])
class TestEveryPatternAtEveryCut:

    def test_no_interior_cut_leaves_the_secret_recoverable(
            self, http, label, secret, needle):
        """The sweep. Every position the boundary could fall inside the
        token, not one hand-picked offset -- because the patterns differ in
        WHICH cuts defeat them and a single offset would test seven of the
        ten by accident and three of them not at all."""
        needle = needle or secret
        survived = []
        for cut_at in range(1, len(secret)):
            first, second = _both_pages(http, _straddling(secret, cut_at))
            if needle in _corpus_text(first, second):
                survived.append(cut_at)

        assert not survived, (
            f"{label} survived the boundary at offsets {survived} of "
            f"1..{len(secret) - 1}")

    def test_the_sweep_is_not_vacuous(self, http, label, secret, needle):
        """Each planted secret must actually BE a secret this deployment's
        redactor recognises. Without this, a typo in a pattern above makes
        the sweep above pass by testing a string nothing was ever going to
        match."""
        needle = needle or secret
        body = _straddling(secret, cut_at=len(secret) + 500)

        first, _ = _both_pages(http, body)

        assert needle not in first["content"], (
            f"{label} is not recognised by the redactor even unsplit, so "
            f"the sweep above proves nothing")


# ===========================================================================
# ---- RB2: the warning that would otherwise have gone silent ---------------
# ===========================================================================

class TestTheRedactionIsReported:
    """#49's rule is that nothing an output-policy pass does is silent.
    Moving the redaction into the tool moves that obligation with it:
    `check_output_policy` now finds nothing left to alter, so its warning
    stops firing for this tool and would take the observability with it."""

    def _warnings(self, caplog):
        return [r for r in caplog.records
                if r.levelno >= logging.WARNING
                and r.name == "tools.builtin.fetch_url"]

    def test_a_redacted_body_warns_once(self, http, caplog):
        with caplog.at_level(logging.WARNING):
            _both_pages(http, _straddling(PAT, cut_at=20))

        records = self._warnings(caplog)
        assert len(records) == 1, (
            "one fetch, one warning -- page 2 came from the held body and "
            "must not report a redaction it did not perform")
        assert "fetch_url" in records[0].getMessage()
        assert URL in records[0].getMessage()

    def test_the_warning_never_quotes_what_it_found(self, http, caplog):
        """`check_output_policy`'s own rule, and it has to survive the move:
        this exists because something credential-shaped was found, so saying
        what would put it in app.log and every sink a WARNING reaches."""
        with caplog.at_level(logging.WARNING):
            _both_pages(http, _straddling(PAT, cut_at=20))

        for record in self._warnings(caplog):
            assert PAT not in record.getMessage()
            assert PAT[:20] not in record.getMessage()

    def test_a_clean_body_says_nothing(self, http, caplog):
        with caplog.at_level(logging.WARNING):
            _both_pages(http, "nothing credential-shaped here. " * 400)

        assert not self._warnings(caplog)

    def test_two_fetches_warn_twice(self, http, caplog):
        """The count is per FETCH, not per process and not per page. A
        second fetch of the same URL redacts a second body and says so."""
        body = _straddling(PAT, cut_at=20)
        with caplog.at_level(logging.WARNING):
            fetch_url._body_cache.clear()
            http.respond(text=body, url=URL)
            http.respond(text=body, url=URL)
            registry.dispatch("fetch_url", {"url": URL})
            registry.dispatch("fetch_url", {"url": URL})

        assert len(self._warnings(caplog)) == 2


# ===========================================================================
# ---- RB3: the offsets are positions in the redacted body ------------------
# ===========================================================================

class TestTheOffsetsStayConsistent:

    def test_chars_available_counts_what_the_caller_can_reach(self, http):
        """Self-consistency is the whole contract: a caller only ever passes
        back an offset this tool named, so the numbers have to describe the
        text it will actually serve."""
        body = _straddling(PAT, cut_at=20)
        fetch_url._body_cache.clear()
        http.respond(text=body, url=URL)

        first = fetch_url.run({"url": URL})
        total = first["chars_available"]

        assert total < len(body), (
            "the redaction shortened the body, so the total must shrink "
            "with it or an offset will point past the end")

        # Offsets computed up front rather than accumulated in a `while`.
        # An offset of 0 does not read the cache -- it re-fetches, by PG5 --
        # so a loop that starts there would consume a second queued response,
        # get an empty body back, and never advance.
        rest = range(fetch_url.MAX_CONTENT_CHARS, total,
                     fetch_url.MAX_CONTENT_CHARS)
        pages = [first["content"]] + [
            fetch_url.run({"url": URL, "offset": off})["content"]
            for off in rest]

        assert len("".join(pages)) == total
        assert PAT not in "".join(pages)
        assert PAT[:20] not in "".join(pages)

    def test_the_named_offset_is_where_the_content_ended(self, http):
        """Batch 105's M12 row, re-pointed. `next_offset` differing from
        `offset + len(content)` shows up only where a page is short, and
        redaction is a new way for a page to be short."""
        body = _straddling(PAT, cut_at=20)
        fetch_url._body_cache.clear()
        http.respond(text=body, url=URL)

        result = fetch_url.run({"url": URL})

        assert result["offset"] + len(result["content"]) <= \
            result["chars_available"]
        assert str(len(result["content"])) in result["message"]

    def test_an_offset_past_the_shortened_end_is_not_an_error(self, http):
        body = _straddling(PAT, cut_at=20)
        fetch_url._body_cache.clear()
        http.respond(text=body, url=URL)

        total = fetch_url.run({"url": URL})["chars_available"]
        beyond = fetch_url.run({"url": URL, "offset": total + 10})

        assert "error" not in beyond
        assert beyond["content"] == ""
        assert "past the end" in beyond["message"].lower()


# ===========================================================================
# ---- The switch, which governs this as it governs every other pass --------
# ===========================================================================

class TestRedactionOffLeavesTheBodyAlone:
    """`REDACT_TOOL_OUTPUTS` / `VENASTINE_REDACT_OFF` govern pattern GUESSES
    about other people's credentials, and a user who turns them off has
    chosen to see those raw. Moving WHERE the guessing happens must not
    quietly move whether it can be turned off."""

    def test_the_body_is_untouched_and_the_offsets_are_raw(
            self, http, monkeypatch):
        import safety.policy_enforcement as policy
        monkeypatch.setattr(policy, "redaction_enabled", lambda: False)

        body = _straddling(PAT, cut_at=20)
        fetch_url._body_cache.clear()
        http.respond(text=body, url=URL)

        result = fetch_url.run({"url": URL})

        assert result["chars_available"] == len(body)
        assert PAT[:20] in result["content"]

    def test_it_says_nothing_when_it_changed_nothing(
            self, http, monkeypatch, caplog):
        import safety.policy_enforcement as policy
        monkeypatch.setattr(policy, "redaction_enabled", lambda: False)

        with caplog.at_level(logging.WARNING):
            fetch_url._body_cache.clear()
            http.respond(text=_straddling(PAT, cut_at=20), url=URL)
            fetch_url.run({"url": URL})

        assert not [r for r in caplog.records
                    if r.name == "tools.builtin.fetch_url"
                    and r.levelno >= logging.WARNING]


# ===========================================================================
# ---- RB4: the corpus was NOT the place this was fixed ---------------------
# ===========================================================================

class TestTheCorpusDidNotGainASecondScan:
    """The register proposed re-redacting the whole merged text on every
    append. RB1 made that unnecessary; this pins that it was not also done,
    because a second full scan per page is a cost with no coverage and the
    next reader has no way to tell a declined option from a forgotten one.
    """

    def test_a_page_arrives_already_clean(self, http):
        body = _straddling(PAT, cut_at=20)
        first, _ = _both_pages(http, body)

        import safety.policy_enforcement as policy
        assert policy.redact_output_text(first["content"]) == \
            first["content"], (
            "the page still had something for the redactor to find, so the "
            "producer-side fix did not actually run")

    def test_the_corpus_still_redacts_a_PAGE_it_is_handed_directly(self):
        """The continuation path's own `redact_output_text`, which RB1 makes
        a no-op for the only producer that reaches it -- and which stays,
        because this module's docstring says why: nothing is stored by it
        unredacted, rather than depending on a guarantee made two layers
        away that a later refactor could quietly drop.

        That sentence is only true if something asserts it, so this is the
        two-layers-away case made concrete: a result with an offset, built
        by hand, from a producer that did not redact. A fixture, a replayed
        artifact, or the next paging tool.
        """
        corpus = SourceCorpus()
        assert corpus.add("fetch_url", {
            "url": URL, "content": "x" * 500, "offset": 0}) == 1
        corpus.add("fetch_url", {
            "url": URL,
            "content": f"page two, unredacted, carrying {PAT} in it",
            "offset": 500})

        held = corpus.text_for(URL)
        assert "page two" in held, "the second page was not absorbed at all"
        assert PAT not in held

    def test_the_corpus_still_redacts_what_does_not_come_from_fetch_url(self):
        """`arxiv_search` and `web_search` do not page and do not redact at
        their own producer, so the entry-side pass they rely on has to still
        be there. A fix that deleted it would pass every test above."""
        corpus = SourceCorpus()
        corpus.add("web_search", {"results": [
            {"url": "https://example.com/hit",
             "title": "A result",
             "snippet": f"leaked in a snippet: {PAT} and more text here"},
        ]})

        held = corpus.text_for("https://example.com/hit")
        assert held, "nothing was stored, so this asserted nothing"
        assert PAT not in held
        assert REDACTION_MARKER in held


# ===========================================================================
# ---- A23: a session's output, at the two cuts that are pure functions -----
# ===========================================================================

SESSION_FILLER = "[info] starting worker and then waiting for the build\n"


def _session_output(secret, cut_at, budget):
    """Program output whose last `budget` characters begin `cut_at`
    characters into `secret`.

    Same sense of `cut_at` as `_straddling` above: that many characters of
    the secret are on the side the reader does not get. The filler is whole
    LINES and the secret starts one, for `_straddling`'s stated reason --
    two of the three credential shapes anchor on a word boundary, and a
    secret welded onto the end of a filler word does not match even unsplit.
    """
    inside = len(secret) - cut_at
    trail = (SESSION_FILLER * 40)[:max(0, budget - inside - 1)]
    text = SESSION_FILLER * 12 + secret + "\n" + trail
    assert text[-budget:].startswith(secret[cut_at:]), (cut_at, budget)
    return text


def _wake_event(tail, session_id="s1"):
    return WakeEvent(session_id=session_id, call_id="call_" + session_id,
                     kind="background", command="run.sh", shape="exited",
                     return_code=0, elapsed_s=1.0, ran_on="host",
                     output_tail=tail)


def _session_row(chars):
    return SessionRow(
        id="s1", kind="background", command="run.sh", rationale="because",
        pattern=None, owner_thread="t1", owner_agent=None, owner_depth=0,
        owner_span_id=None, call_id="call_1", state="exited",
        started_wall=0.0, timeout_s=600, match_count=0, output_chars=chars,
        ran_on="host", tier="SANDBOXED", return_code=0)


@pytest.fixture
def small_read(monkeypatch):
    """A 400-character read, so a boundary is cheap to straddle.

    The real number is 50,000 and the arithmetic is identical; the existing
    wake test (`test_a_coalesced_batch_shares_one_output_budget`) shrinks it
    the same way and for the same reason.
    """
    monkeypatch.setattr(config, "MAX_READ_CHARS", 400)
    return 400


@pytest.mark.parametrize(
    "label,secret,needle", SECRETS, ids=[row[0] for row in SECRETS])
class TestTheWakesPerSessionBudgetCutsRedactedText:
    """`_body` divides one read between the sessions a wake coalesces (SS7),
    so two sessions reporting together cut at half of it. The slice used to
    be taken first and handed to the policy after -- and what the policy got
    was two fragments of one pattern."""

    def test_no_interior_cut_leaves_the_secret_in_the_wake(
            self, small_read, label, secret, needle):
        needle = needle or secret
        budget = small_read // 2
        survived = [cut_at for cut_at in range(1, len(secret))
                    if needle in session_wake.build_wake(
                        [_wake_event(_session_output(secret, cut_at, budget)),
                         _wake_event("unrelated output", "s2")])[0]]

        assert not survived, (
            f"{label} survived the per-session budget at offsets "
            f"{survived} of 1..{len(secret) - 1}")

    def test_the_sweep_is_not_vacuous(self, small_read, label, secret,
                                      needle):
        """The control the file's docstring demands: planted in the same
        filler, with nothing cut, each secret must actually be one this
        deployment redacts."""
        needle = needle or secret
        tail = SESSION_FILLER * 4 + secret + "\n" + SESSION_FILLER * 4

        text, _ = session_wake.build_wake([_wake_event(tail)])

        assert needle not in text, (
            f"{label} is not recognised even uncut, so the sweep above "
            f"proves nothing")
        assert REDACTION_MARKER in text


@pytest.mark.parametrize(
    "label,secret,needle", SECRETS, ids=[row[0] for row in SECRETS])
class TestTheSessionViewsDisplayBudgetCutsRedactedText:
    """`live_entries` draws `tail_chars()` of a live session's output. The
    TUI used to hand it `buffer.last(tail_chars())` -- the cut first, the
    redaction after -- which is the same defect one surface over, at the
    same offsets, reaching the screen and `/copy all` instead of the
    model."""

    def test_no_interior_cut_leaves_the_secret_on_the_screen(
            self, small_read, label, secret, needle):
        needle = needle or secret
        survived = []
        for cut_at in range(1, len(secret)):
            whole = _session_output(secret, cut_at, small_read)
            entries = session_view.live_entries(_session_row(len(whole)),
                                                whole, False)
            if needle in "\n".join(text for _, text in entries):
                survived.append(cut_at)

        assert not survived, (
            f"{label} survived the display budget at offsets {survived}")

    def test_the_budget_still_bounds_what_is_drawn(self, small_read, label,
                                                   secret, needle):
        """THE CONTROL for the test above: a fix that stopped cutting at all
        would pass every assertion in this class, and it would paint a
        200,000-character buffer into the pane one line at a time."""
        whole = _session_output(secret, 1, small_read) + SESSION_FILLER * 40

        entries = session_view.live_entries(_session_row(len(whole)), whole,
                                            False)

        drawn = sum(len(text) for role, text in entries if role == "output")
        assert drawn <= small_read, drawn
        assert drawn > small_read // 2, (
            "nothing was drawn, so the sweep proves nothing")
        assert any("earlier output omitted" in text for _, text in entries)


# ===========================================================================
# ---- A59: the two search tools cut too, and redacted after ----------------
# ===========================================================================

def _web_rows(text, where):
    """One DDGS row carrying `text` in the field named by `where`."""
    row = {"title": "An ordinary title",
           "href": "https://example.com/r0",
           "body": "an ordinary snippet"}
    row[where] = text
    return [row]


def _arxiv_feed(text, where):
    """A one-entry Atom feed carrying `text` in `where`.

    Hand-built rather than reusing `test_paging._feed`: that helper is
    about paging and takes character COUNTS, and what this needs is a
    specific string in a specific element.
    """
    fields = {"title": "An ordinary title", "summary": "An ordinary summary"}
    fields[where] = text
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"'
        '      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        '<opensearch:totalResults>1</opensearch:totalResults>'
        '<entry>'
        '<id>http://arxiv.org/abs/2401.00001v1</id>'
        '<title>%(title)s</title>'
        '<summary>%(summary)s</summary>'
        '<published>2024-01-01T00:00:00Z</published>'
        '<author><name>Ada Lovelace</name></author>'
        '<category term="cs.AI"/>'
        '<link title="pdf" href="http://arxiv.org/pdf/2401.00001v1"/>'
        '</entry></feed>' % fields)


#: (label, the cap, the field the cut text lands in, a builder, the tool).
#: One row per interior cut in the two search tools -- the three batch 156
#: inherited and the three it added. A cut missing from this table is a cut
#: nothing here sweeps, which is how §51 shipped one in the first place.
SEARCH_CUTS = [
    ("web_search snippet", web_search.MAX_SNIPPET_CHARS, "snippet",
     lambda text: _web_rows(text, "body"), "web_search"),
    ("web_search title", web_search.MAX_TITLE_CHARS, "title",
     lambda text: _web_rows(text, "title"), "web_search"),
    ("arxiv summary", arxiv.MAX_SUMMARY_CHARS, "summary",
     lambda text: _arxiv_feed(text, "summary"), "arxiv_search"),
    ("arxiv title", arxiv.MAX_TITLE_CHARS, "title",
     lambda text: _arxiv_feed(text, "title"), "arxiv_search"),
]


def _straddling_at(secret, cut, kept):
    """A value in which `secret` crosses `cut`, `kept` characters of it
    before the boundary.

    `_straddling`'s filler rule, for `_straddling`'s reason: prose and a
    forced space, because two of the three credential SHAPES anchor on a
    word boundary and a secret welded to the end of a filler word does not
    match even unsplit. That trap bit batch 148's first probe.
    """
    head = ("some configuration prose here and there. "
            * 400)[:cut - kept - 1] + " "
    assert len(head) == cut - kept
    return head + secret + " and then more prose follows after it."


def _searched(monkeypatch, tool, payload, field):
    """`field` of the first result, through the real `registry.dispatch`.

    THE CACHE IS CLEARED PER CALL, which is not hygiene here but the
    difference between a sweep and a single measurement repeated: both
    tools key their cache on the query, and every offset in a sweep below
    asks the same query. Without this the second iteration onward reads
    the first iteration's answer and the sweep reports clean for a secret
    it never sent.
    """
    if tool == "web_search":
        web_search._cache.clear()
        monkeypatch.setattr(web_search, "_call_ddgs",
                            lambda *a, **kw: payload)
        result = registry.dispatch("web_search", {"query": "x"})
    else:
        arxiv._cache.clear()
        monkeypatch.setattr(arxiv, "_call_arxiv_api",
                            lambda *a, **kw: payload)
        result = registry.dispatch("arxiv_search", {"keywords": "x"})
    assert "error" not in result, result
    return result["results"][0][field]


@pytest.mark.parametrize(
    "label,cap,field,build,tool", SEARCH_CUTS,
    ids=[row[0].replace(" ", "_") for row in SEARCH_CUTS])
class TestEveryCutInTheSearchToolsRedactsFirst:
    """One row per cut, the `ghp_` token at every offset that straddles it.

    `ghp_` and not the whole table here: the question this class asks is
    whether a given CUT has the redaction above it, and the fixed-length
    vendor token is defeated by every interior cut, so it answers that
    question at every offset. Which patterns survive which offsets is a
    different question and `TestEveryPatternAtTheSnippetCut` below is
    where the whole table is swept.
    """

    def test_no_interior_cut_leaves_the_secret(
            self, monkeypatch, label, cap, field, build, tool):
        survived = []
        for kept in range(1, len(PAT)):
            got = _searched(monkeypatch, tool,
                            build(_straddling_at(PAT, cap, kept)), field)
            if PAT[:kept] in got and kept >= 4:
                survived.append(kept)

        assert not survived, (
            f"{label} kept {len(survived)} secret prefixes at offsets "
            f"{survived[:6]}... of 1..{len(PAT) - 1}")

    def test_the_sweep_is_not_vacuous(
            self, monkeypatch, label, cap, field, build, tool):
        """The cut must actually BE where this row says it is.

        Without this, a row naming the wrong cap plants its secret
        somewhere comfortable -- wholly inside the kept text or wholly
        outside it -- and the sweep above passes by never splitting
        anything. The assertion is that the value came back AT the cap,
        which only happens if the text really was longer than it.
        """
        got = _searched(monkeypatch, tool,
                        build("x " + "filler words here " * 400), field)

        assert len(got) == cap, (
            f"{label} came back {len(got)} characters long, so {cap} is "
            f"not the cut this row is about and the sweep above split "
            f"nothing")


@pytest.mark.parametrize(
    "label,secret,needle", SECRETS, ids=[row[0] for row in SECRETS])
class TestEveryPatternAtTheSnippetCut:
    """The whole pattern table at one cut, for the reason
    `TestEveryPatternAtEveryCut` gives: the ten patterns do NOT behave
    alike, and the open-quantifier ones (`sk-`, `sk-ant-`, `xox`) are
    defeated only by a cut inside their first 14 to 26 characters because a
    long enough prefix still matches on its own. A class that swept one
    pattern would test three of the ten by accident and seven not at all.
    """

    def test_no_interior_cut_leaves_the_secret_recoverable(
            self, monkeypatch, label, secret, needle):
        needle = needle or secret
        survived = []
        for kept in range(1, len(secret)):
            snippet = _searched(
                monkeypatch, "web_search",
                _web_rows(_straddling_at(secret, web_search.MAX_SNIPPET_CHARS,
                                         kept), "body"),
                "snippet")
            if needle in snippet:
                survived.append(kept)

        assert not survived, (
            f"{label} survived the snippet cut at offsets {survived} of "
            f"1..{len(secret) - 1}")

    def test_the_sweep_is_not_vacuous(self, monkeypatch, label, secret,
                                      needle):
        """Each planted secret must be one this deployment's redactor
        recognises, or the sweep above proves nothing about it."""
        needle = needle or secret
        snippet = _searched(
            monkeypatch, "web_search",
            _web_rows(_straddling_at(secret, web_search.MAX_SNIPPET_CHARS,
                                     len(secret) + 40), "body"),
            "snippet")

        assert needle not in snippet, (
            f"{label} is not recognised by the redactor even unsplit, so "
            f"the sweep above proves nothing")


class TestTheSearchToolsStillReturnWhatTheyAlwaysDid:
    """The control the three classes above need: an ordinary result is
    byte-identical to what it was before any of this, because a redactor
    that altered clean text would pass every assertion above by ruining
    everything."""

    def test_an_ordinary_snippet_is_untouched(self, monkeypatch):
        body = "an ordinary snippet about entropy and nothing else"

        snippet = _searched(monkeypatch, "web_search",
                            _web_rows(body, "body"), "snippet")

        assert snippet == body

    def test_an_ordinary_abstract_is_untouched(self, monkeypatch):
        summary = "We show that the thing is the case, under assumptions."

        got = _searched(monkeypatch, "arxiv_search",
                        _arxiv_feed(summary, "summary"), "summary")

        assert got == summary

    def test_a_secret_wholly_inside_the_kept_text_is_still_redacted(
            self, monkeypatch):
        """The case that was never broken, asserted so the fix cannot be
        'stop redacting'."""
        snippet = _searched(monkeypatch, "web_search",
                            _web_rows("here it is: " + PAT, "body"),
                            "snippet")

        assert PAT not in snippet
        assert REDACTION_MARKER in snippet
