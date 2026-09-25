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
"""

import logging

import pytest

from core.reasoning.source_corpus import SourceCorpus
from safety.policy_enforcement import REDACTION_MARKER
from tools.builtin import fetch_url
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
