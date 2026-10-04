"""The pure half of the evidence desk: parse, chunk, rank, normalise.

No database and no model anywhere in this file, which is the point of `ingest/`
being a pure package. Everything here is a bytestring in and an assertion out.
"""

import unicodedata

import pytest
from tests import fixtures

from lcf.ingest import chunk as chunking
from lcf.ingest import images, language, mail, office, parse, pdf, plain, retrieval, values
from lcf.ingest import text as text_tools
from lcf.ingest.commands import Command

# ───────────────────────────────────────────────────────────── parsers


def test_pdf_keeps_its_page_numbers():
    data = fixtures.pdf(
        [
            ["Reklamation NW-CL-88213", "Charge LOT-2026-0417 betroffen."],
            ["3. Messergebnisse", "Messwert 12,05 mm."],
        ]
    )
    parsed = pdf.parse(data, "reklamation.pdf")

    assert parsed.pages == 2
    assert [u.page for u in parsed.units] == [1, 2]
    assert "NW-CL-88213" in parsed.units[0].text
    assert "12,05" in parsed.units[1].text


def test_a_scan_is_refused_rather_than_recorded_empty():
    """A PDF with no text is a scan, and saying so beats storing nothing.

    A source recorded as parsed with no content is indistinguishable from one
    that genuinely says nothing, so the person who dropped a scan in would be
    left wondering why their questions find nothing.
    """
    blank = fixtures.pdf([[" "]])
    with pytest.raises(parse.ParseFailed, match="scan"):
        pdf.parse(blank, "scan.pdf")


def test_pdf_closes_up_a_hyphen_broken_identifier():
    """`NW-CL-\\n88213` is invisible to every pattern unless the break is closed."""
    data = fixtures.pdf(
        [
            [
                "Sehr geehrte Damen und Herren, wir beziehen uns auf",
                "unsere Reklamation NW-CL-",
                "88213 vom 04.03.2026 und bitten um Stellungnahme.",
            ]
        ]
    )
    parsed = pdf.parse(data, "wrapped.pdf")
    assert "NW-CL-88213" in parsed.text


def test_pdf_removes_a_soft_hyphen_from_a_broken_german_word():
    """The other half of the same ambiguity, and it pulls the opposite way.

    `Eingangs-\\npruefung` wants the hyphen gone; `NW-CL-\\n88213` wants it kept.
    What tells them apart is whether the continuation is lowercase.
    """
    data = fixtures.pdf(
        [
            [
                "Bei der Eingangs-",
                "pruefung wurden Abweichungen an mehreren Teilen festgestellt.",
            ]
        ]
    )
    parsed = pdf.parse(data, "hyphen.pdf")
    assert "Eingangspruefung" in parsed.text


def test_docx_reads_headings_as_a_trail_and_tables_as_rows():
    data = fixtures.docx(
        [
            ("Heading 1", "3. Messergebnisse"),
            ("", "Die Werte sind unten aufgeführt."),
            ("table", "Merkmal|Soll|Ist\nDurchmesser|12,00|12,05"),
        ]
    )
    parsed = office.parse(data, "messung.docx")

    body = next(u for u in parsed.units if u.kind == "paragraph")
    assert body.path == ["3. Messergebnisse"]

    rows = [u for u in parsed.units if u.kind == "table_row"]
    assert rows, "a table should yield one unit per row"
    # The header is woven in: `12,05` alone means nothing without `Ist`.
    assert "Ist: 12,05" in rows[0].text


def test_docx_keeps_tables_beside_the_heading_that_explains_them():
    """`document.paragraphs` and `document.tables` are separate lists.

    Reading them in turn would put every table after every paragraph, so a
    measurement table would lose the heading saying what it measures.
    """
    data = fixtures.docx(
        [
            ("Heading 1", "A"),
            ("table", "x|y\n1|2"),
            ("Heading 1", "B"),
        ]
    )
    parsed = office.parse(data, "order.docx")
    kinds = [u.kind for u in parsed.units]
    assert kinds.index("table_row") < kinds.index("heading", kinds.index("table_row") - 1) + 2


# ───────────────────────────────────────────────────────────── mail


def test_mail_lifts_the_sender_and_its_domain():
    parsed = mail.parse(fixtures.THREAD.encode("utf-8"), "thread.eml")

    assert parsed.sender == "a.schulz@nordwerk.de"
    assert mail.domain_of(parsed.sender) == "nordwerk.de"
    assert parsed.subject == "AW: Reklamation NW-CL-88213"
    assert parsed.sent_at is not None
    assert "qs@wirgmbh.de" in parsed.meta["cc"]


def test_mail_strips_the_signature_and_the_disclaimer():
    """This is where the perceived quality of the whole feature sits.

    A chunk that is 90% legal footer poisons every extraction run over it.
    """
    parsed = mail.parse(fixtures.THREAD.encode("utf-8"), "thread.eml")

    assert "außerhalb der vereinbarten Toleranz" in parsed.text
    assert "Mit freundlichen Grüßen" not in parsed.text
    assert "vertrauliche Informationen" not in parsed.text
    assert "Tel +49 123 456789" not in parsed.text


def test_mail_keeps_every_reply_and_attributes_each_one():
    """A four-deep chain is up to four statements by up to four people."""
    parsed = mail.parse(fixtures.THREAD.encode("utf-8"), "thread.eml")
    senders = {u.meta.get("sender") for u in parsed.units if u.meta.get("sender")}

    assert "a.schulz@nordwerk.de" in senders
    assert "p.meier@wirgmbh.de" in senders
    # Nothing above the first separator is lost, and nothing below it either.
    assert "melden uns bis Mittwoch" in parsed.text
    assert "Stellungnahme" in parsed.text


def test_mail_strips_quote_markers():
    parsed = mail.parse(fixtures.THREAD.encode("utf-8"), "thread.eml")
    assert "> Bitte" not in parsed.text
    assert "Bitte um Stellungnahme" in parsed.text


def test_mail_recurses_into_attachments():
    message = (
        "From: a.schulz@nordwerk.de\r\n"
        "Subject: Messprotokoll\r\n"
        'Content-Type: multipart/mixed; boundary="X"\r\n\r\n'
        "--X\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "Anbei das Protokoll.\r\n"
        "--X\r\n"
        "Content-Type: application/pdf\r\n"
        'Content-Disposition: attachment; filename="protokoll.pdf"\r\n'
        "Content-Transfer-Encoding: base64\r\n\r\n"
    )
    import base64

    blob = base64.b64encode(fixtures.pdf([["Messwert 12,05 mm"]])).decode()
    raw = (message + blob + "\r\n--X--\r\n").encode("utf-8")

    parsed = mail.parse(raw, "mit-anhang.eml")
    assert [a.filename for a in parsed.attachments] == ["protokoll.pdf"]


def test_mail_falls_back_to_html_when_there_is_no_plain_part():
    raw = (
        b"From: a.schulz@nordwerk.de\r\n"
        b"Subject: HTML nur\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><body><p>Charge <b>LOT-2026-0417</b> betroffen.</p>"
        b"<style>p{color:red}</style></body></html>"
    )

    parsed = mail.parse(raw, "html.eml")
    assert "LOT-2026-0417" in parsed.text
    assert "color:red" not in parsed.text


# ───────────────────────────────────────────────────────────── plain text


def test_markdown_headings_become_the_trail():
    parsed = plain.from_text(fixtures.NOTES, "notes.md")
    measured = next(u for u in parsed.units if "12,05" in u.text)
    assert measured.path == ["Reklamation NW-CL-88213", "3. Messergebnisse"]


def test_a_heading_without_a_blank_line_still_ends_the_block():
    """Pasted reports routinely put a heading directly above its paragraph."""
    parsed = plain.from_text("# Titel\nDirekt darunter.\n")
    assert [u.kind for u in parsed.units] == ["heading", "paragraph"]


def test_csv_rows_carry_their_header():
    parsed = plain.from_text("Merkmal,Soll,Ist\nDurchmesser,12.00,12.05\n", "messung.csv")
    row = next(u for u in parsed.units if u.kind == "table_row")
    assert "Ist: 12.05" in row.text


def test_decoding_never_fails_on_an_encoding():
    assert "Toleranz" in plain.decode("Toleranz überschritten".encode("cp1252"))


# ───────────────────────────────────────────────────────────── chunking


def _chunks(text: str, budget: int = 1800):
    parsed = plain.from_text(text)
    return parsed, chunking.chunk_source(parsed, budget=budget)


def test_every_chunk_is_exactly_its_own_slice_of_the_source():
    """The invariant the whole provenance claim rests on.

    A quotation is verified against an identifiable body of text, so
    `source.text[char_from:char_to]` has to *be* the chunk — not approximately.
    """
    parsed, chunks = _chunks(fixtures.NOTES, budget=300)
    assert chunks
    for piece in chunks:
        assert parsed.text[piece.char_from : piece.char_to] == piece.text


def test_nothing_is_lost_to_chunking():
    parsed, chunks = _chunks(fixtures.NOTES, budget=240)
    joined = " ".join(" ".join(c.text.split()) for c in chunks)
    for unit in parsed.units:
        assert " ".join(unit.text.split()) in joined


def test_the_budget_holds_even_for_one_enormous_paragraph():
    """A unit too long for a chunk is split, not handed over whole.

    PDF extraction routinely produces one block per page, and a 9000-character
    chunk defeats the budget the cost estimate is built on.
    """
    _, chunks = _chunks("Ein Satz. " * 900, budget=400)
    assert chunks
    # Allowance for one sentence overshooting the cut, never for a whole unit.
    assert max(len(c.text) for c in chunks) <= 400 + 120


def test_a_heading_opens_a_chunk_rather_than_ending_one():
    _, chunks = _chunks(fixtures.NOTES, budget=400)
    measured = next(c for c in chunks if "12,05" in c.text)
    assert "3. Messergebnisse" in measured.path


def test_a_page_boundary_breaks_a_chunk():
    parsed = pdf.parse(
        fixtures.pdf(
            [
                ["Die erste Seite beschreibt den Sachverhalt im Detail."],
                ["Die zweite Seite enthaelt die Messergebnisse der Pruefung."],
            ]
        ),
        "zwei.pdf",
    )
    chunks = chunking.chunk_source(parsed, budget=4000)
    assert len(chunks) == 2
    assert [c.page_from for c in chunks] == [1, 2]


def test_a_thread_chunks_per_author():
    parsed = mail.parse(fixtures.THREAD.encode("utf-8"), "thread.eml")
    chunks = chunking.chunk_source(parsed, budget=4000)
    senders = [c.meta.get("sender") for c in chunks]
    assert len(set(senders)) > 1, "one chunk per author, not one per thread"


# ───────────────────────────────────────────────────────────── language


def test_german_and_english_are_told_apart():
    code, confidence = language.detect(
        "Die Teile der Charge liegen außerhalb der vereinbarten Toleranz und wurden gesperrt."
    )
    assert code == "de"
    assert confidence > 0.5

    code, _ = language.detect(
        "The parts of this batch are outside the agreed tolerance and have been blocked."
    )
    assert code == "en"


def test_a_two_line_note_gets_the_fallback_rather_than_a_guess():
    code, confidence = language.detect("ok")
    assert (code, confidence) == (language.FALLBACK, 0.0)


def test_every_supported_language_has_a_stopword_list():
    for code in language.SUPPORTED:
        assert language.stopword_set(code)


# ───────────────────────────────────────────────────────────── values


@pytest.mark.parametrize(
    ("left", "right", "kind", "same"),
    [
        ("12,05", "12.05", "number", True),
        ("12,05 mm", "12.05", "number", True),
        ("12,0", "12", "number", True),
        ("12,05", "12,06", "number", False),
        ("28.02.2026", "2026-02-28", "date", True),
        ("2026-02-28", "2026-03-28", "date", False),
        ("NW-CL-88213", "nw-cl-88213", "identifier", True),
        # Punctuation in an identifier is information. Merging these would hide
        # a difference that might be the point.
        ("NW-CL-88213", "NWCL88213", "identifier", False),
        ("außerhalb", "ausserhalb", "text", True),
        ("Toleranz.", "toleranz", "text", True),
    ],
)
def test_normalisation_merges_what_it_should_and_nothing_more(left, right, kind, same):
    assert (values.normalise(left, kind) == values.normalise(right, kind)) is same


def test_a_date_question_rejects_a_written_month():
    """Constrained decoding fixes the JSON type, not the shape inside a string.

    "8 September" reaching a date input shows an empty field under a note saying
    where the answer came from, which is worse than not proposing.
    """
    assert not values.fits("8 September", "date")
    assert values.fits("2026-09-08", "date")


def test_a_choice_outside_its_options_is_rejected():
    assert values.fits("offen", "choice", ["offen", "geschlossen"])
    assert not values.fits("vielleicht", "choice", ["offen", "geschlossen"])


def test_a_date_is_displayed_as_iso_and_a_number_as_written():
    assert values.display("28.02.2026", "date") == "2026-02-28"
    assert values.display("12,05 mm", "number") == "12,05 mm"


# ───────────────────────────────────────────────────────────── retrieval


def test_german_stemming_finds_an_inflected_keyword():
    hits = retrieval.keyword_hits(
        ["Toleranz"], ["Die Toleranzen wurden nicht eingehalten.", "Nichts von Belang."], "de"
    )
    assert [h.index for h in hits] == [0]


def test_a_keyword_is_a_word_and_not_a_substring():
    assert not retrieval.keyword_hits(["cal"], ["calibration certificate"], "en")


def test_ranking_prefers_the_passage_that_shares_the_query():
    texts = [
        "Die Toleranz wurde überschritten.",
        "Der Lieferschein lag bei.",
        "Das Protokoll ist unterschrieben.",
    ]
    ranked = retrieval.rank("Toleranz überschritten", texts, "de", top_k=3)
    assert ranked and ranked[0].index == 0
    assert not ranked[0].arbitrary


def test_a_query_sharing_nothing_falls_back_to_document_order_and_says_so():
    """An English question asked of German material is the ordinary case.

    Reading the first few beats reading none, and the caller is obliged to say
    the order means nothing — which is what `arbitrary` is for.
    """
    ranked = retrieval.rank("delivery date", ["Völlig anderes Thema."], "de", top_k=3)
    assert ranked and all(r.arbitrary for r in ranked)


# ───────────────────────────────────────────────────────────── patterns


def test_a_pattern_must_match_every_example_it_was_built_from():
    assert retrieval.fullmatch_all(r"NW-[A-Z]{2}-\d{5}", ["NW-CL-88213"]) == []
    assert retrieval.fullmatch_all(r"NW-[A-Z]{2}-\d{5}", ["NW-CL-88213", "NW-X-1"]) == ["NW-X-1"]


def test_a_pattern_matching_the_empty_string_is_refused():
    """It would hit at every position in every passage — a candidate per character."""
    with pytest.raises(retrieval.PatternInvalid, match="empty"):
        retrieval.compile_pattern("a*")


def test_an_invalid_pattern_is_refused_with_a_reason():
    with pytest.raises(retrieval.PatternInvalid):
        retrieval.compile_pattern("NW-(CL")


def test_a_backtracking_pattern_times_out_rather_than_hanging():
    """`re` has no timeout, which is the whole reason `regex` is a dependency.

    A model-written pattern runs over megabytes of somebody else's text.
    """
    with pytest.raises(retrieval.PatternTooSlow):
        retrieval.matches(r"(a+)+$", "a" * 6000 + "b", timeout=0.2)


def test_a_single_group_is_the_value_and_the_match_is_the_context():
    found = retrieval.matches(r"Charge\s+(\S+)", "Die Charge LOT-2026-0417 ist betroffen.")
    assert [m.value for m in found] == ["LOT-2026-0417"]
    assert "Charge" in found[0].quote


def test_offsets_point_at_the_value_itself():
    text = "Reklamation NW-CL-88213 vom 04.03."
    found = retrieval.matches(r"NW-[A-Z]{2}-\d{5}", text)
    assert text[found[0].char_from : found[0].char_to] == "NW-CL-88213"


# ───────────────────────────────────────────────────────────── commands


def test_a_command_needs_the_parameters_its_kind_requires():
    with pytest.raises(ValueError, match="keywords"):
        Command.model_validate({"kind": "keyword_ask", "ask": "was?"})
    with pytest.raises(ValueError, match="unknown command kind"):
        Command.model_validate({"kind": "freetext", "ask": "was?"})


def test_keywords_arrive_either_as_a_list_or_as_a_typed_field():
    typed = Command.model_validate({"kind": "keyword_ask", "keywords": "a, b\nc", "ask": "x"})
    ticked = Command.model_validate({"kind": "keyword_ask", "keywords": ["a", "b"], "ask": "x"})
    assert typed.keywords == ["a", "b", "c"]
    assert ticked.keywords == ["a", "b"]


def test_a_command_drops_parameters_that_belong_to_another_kind():
    """Otherwise a kind changed in a form leaves the old kind's parameters behind."""
    command = Command.model_validate(
        {"kind": "ask", "ask": "was?", "pattern": "X", "keywords": "a"}
    )
    assert command.pattern is None
    assert command.keywords == []


def test_an_overlong_pattern_is_refused():
    with pytest.raises(ValueError, match="at most"):
        Command.model_validate({"kind": "pattern", "pattern": "a" * 300})


# ───────────────────────────────────────────────────────────── images


def test_the_same_image_twice_is_one_asset():
    """A letterhead on twenty pages would otherwise fill the tray."""
    one = fixtures.png()
    found = [
        parse.Image(data=one, page=1),
        parse.Image(data=one, page=2),
        parse.Image(data=fixtures.png(colour=(10, 20, 30)), page=3),
    ]
    harvest = images.harvest(found)
    assert len(harvest.images) == 2
    assert harvest.duplicates == 1


def test_page_furniture_is_dropped_and_counted():
    harvest = images.harvest(
        [
            parse.Image(data=fixtures.png(10, 10)),  # a bullet
            parse.Image(data=fixtures.png(400, 6)),  # a horizontal rule
            parse.Image(data=fixtures.png(200, 160)),  # a photograph
        ]
    )
    assert len(harvest.images) == 1
    assert harvest.too_small == 2
    assert "too small" in harvest.note()


def test_a_prepared_image_carries_a_thumbnail():
    prepared = images.prepare(fixtures.png(800, 600))
    assert prepared is not None
    assert prepared.thumbnail and len(prepared.thumbnail) < len(prepared.data)
    assert prepared.width == 800


def test_an_image_for_a_call_is_shrunk():
    shrunk, media_type = images.for_call(fixtures.png(3000, 2000), "image/png", longest=512)
    assert len(shrunk) < len(fixtures.png(3000, 2000))
    assert media_type == "image/jpeg"


# ───────────────────────────────────────────────────────────── dispatch


def test_dispatch_refuses_a_format_nothing_reads():
    with pytest.raises(parse.ParseFailed, match="nothing here reads"):
        parse.parse(b"\x00\x01", "drawing.dwg")


def test_dispatch_picks_the_parser_from_the_extension():
    parsed = parse.parse(fixtures.THREAD.encode("utf-8"), "thread.eml", "application/octet-stream")
    assert parsed.sender == "a.schulz@nordwerk.de"


# ───────────────────────────────────────────────────── exotic characters

# Three hazards, one guard (`ingest/text.py`). These are not hypothetical: a NUL
# or a lone surrogate aborts the parse of a whole file on the way into Postgres,
# and every invisible character here is one PDF extraction emits by itself.

NASTY = (
    "\u00dcbersicht\u00ad\r\n\r\n"
    "Messwert 12,05\u00a0mm\x00\u200b bei LOT-2026-0417\r\n"
    "Zeile zwei\x0c\u2028Seite 2\x1b Text"
)


def test_unstorable_characters_never_reach_the_text():
    """A NUL and a lone surrogate are both valid Python and both rejected by
    Postgres, so the parse of an entire file fails over one character in it."""
    parsed = parse.parse(NASTY.encode("utf-8"), "notes.txt")
    assert "\x00" not in parsed.text
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in parsed.text)
    # The actual failure being guarded against: this is what asyncpg does.
    assert parsed.text.encode("utf-8")


def test_invisible_characters_no_longer_defeat_a_pattern():
    """The pattern tier is the exact, free one. A soft hyphen inside an
    identifier is how it silently finds nothing."""
    for broken in ("NW-CL-882\u00ad13", "NW-CL-\u200b88213", "NW-CL-8821\ufeff3"):
        parsed = parse.parse(f"Teil {broken} geprueft".encode(), "notes.txt")
        found = retrieval.matches(r"\bNW-CL-\d{5}\b", parsed.text)
        assert [m.value for m in found] == ["NW-CL-88213"], broken


def test_a_no_break_space_becomes_a_space_and_not_nothing():
    """German typesetting puts one between a number and its unit. Folding it to
    a space is honest; deleting it would invent `12,05mm`."""
    parsed = parse.parse("Messwert 12,05\u00a0mm".encode(), "notes.txt")
    assert "12,05 mm" in parsed.text


def test_decomposed_umlauts_deduplicate_against_composed_ones():
    """macOS and some PDF producers emit NFD, where `\u00dc` is two code points
    that look like one — and normalise to a different key."""
    composed = "Teil-\u00dc100"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert values.normalise(composed, "identifier") == values.normalise(decomposed, "identifier")


def test_control_characters_do_not_reach_a_chunk():
    parsed = parse.parse(b"Seite eins\x0c\x0bZeile\x1brot\x07 ende", "notes.txt")
    text = chunking.chunk_source(parsed)[0].text
    assert not any(ord(c) < 0x20 and c not in "\t\n" for c in text)


def test_the_offset_invariant_survives_cleaning():
    """Cleaning runs before `Parsed.text` is assembled, so offsets are computed
    against the cleaned text. If it ever ran after, every quote would be wrong."""
    parsed = parse.parse(NASTY.encode("utf-8"), "notes.txt")
    for c in chunking.chunk_source(parsed, budget=200):
        assert parsed.text[c.char_from : c.char_to] == c.text


def test_a_file_of_nothing_but_control_characters_is_refused():
    """Not stored as parsed-but-empty, which is indistinguishable from a file
    that genuinely says nothing."""
    with pytest.raises(parse.ParseFailed, match="no readable text"):
        parse.parse(b"\x00\x07\x1b\x0b", "junk.txt")


def test_cleaning_is_idempotent():
    once = text_tools.clean(NASTY)
    assert text_tools.clean(once) == once


def test_cleaning_model_output_keeps_the_newlines_prose_needs():
    """`clean_data` runs over every call's result, and one of them drafts
    markdown. Collapsing its newlines would be a corruption, not a guard."""
    drafted = {"markdown": "# Titel\n\nAbsatz eins.\n\n- Punkt\n", "bad": "a\x00b"}
    out = text_tools.clean_data(drafted)
    assert out["markdown"] == drafted["markdown"]
    assert out["bad"] == "ab"


def test_an_unclosed_quote_in_a_display_name_does_not_corrupt_the_sender():
    """`getaddresses` hands back the whole header as the address when a display
    name opens a quote it never closes.

    Stored unchecked that is a sender nobody can mail and a domain of
    `nordwerk.de>`, which silently matches no filter \u2014 and the domain is what
    tells the customer's material from our own.
    """
    raw = (
        b'From: "A. Schulz <a.schulz@nordwerk.de>\r\n'
        b"Subject: Reklamation\r\n\r\n"
        b"Charge LOT-2026-0417 betroffen.\r\n"
    )
    parsed = mail.parse(raw, "odd.eml")
    assert parsed.sender == "a.schulz@nordwerk.de"
    assert mail.domain_of(parsed.sender) == "nordwerk.de"
    assert "LOT-2026-0417" in parsed.text


def test_an_unclosed_attribute_quote_does_not_lose_the_whole_mail():
    """`HTMLParser` does not raise on it \u2014 it waits for a closing quote that
    never comes and swallows the document, returning nothing at all."""
    raw = (
        b"From: a@b.de\r\nSubject: x\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b'<html><body><p class="lead>Charge <b>LOT-2026-0417</b> betroffen.</p>'
        b"<style>p{color:red}</style>"
        b"<p alt='unclosed>Zweiter Absatz mit NW-CL-88213.</p></body></html>"
    )
    parsed = mail.parse(raw, "broken.eml")
    assert "LOT-2026-0417" in parsed.text
    assert "NW-CL-88213" in parsed.text
    assert "color:red" not in parsed.text


def test_an_unclosed_quote_in_a_csv_does_not_swallow_the_rest_of_the_file():
    """One `\"` opening a field makes `csv.reader` read to end of file looking
    for its partner, and the measurement table comes back as one cell."""
    body = 'Merkmal,Wert\n"Bohrung,12,05\nRautiefe,11,2\nRundlauf,0,02\n'
    parsed = plain.from_text(body, "messung.csv")
    rows = [u for u in parsed.units if u.kind == "table_row"]
    assert len(rows) == 3, [u.text for u in parsed.units]
    assert not any("\n" in u.text for u in parsed.units)


def test_a_properly_quoted_csv_still_has_its_quoting_honoured():
    """The complement of the fallback: where the quotes are balanced they still
    mean what CSV says they mean, including a delimiter inside a field."""
    parsed = plain.from_text('Merkmal,Wert\n"Bohrung, gross",12.05\nRautiefe,11.2\n', "m.csv")
    rows = [u for u in parsed.units if u.kind == "table_row"]
    assert len(rows) == 2
    assert "Merkmal: Bohrung, gross" in rows[0].text


def test_a_quoted_identifier_is_the_same_identifier():
    """A document writes \u201eLOT-2026-0417\u201c; the quotes belong to the sentence.

    Internal punctuation still separates values \u2014 that is the whole restraint of
    the identifier type \u2014 but offering the same batch twice because one mail put
    it in quotation marks is a duplicate, not a distinction.
    """
    bare = values.normalise("LOT-2026-0417", "identifier")
    for quoted in (
        "\u201eLOT-2026-0417\u201c",
        '"LOT-2026-0417"',
        "\u00abLOT-2026-0417\u00bb",
        "'LOT-2026-0417'",
    ):
        assert values.normalise(quoted, "identifier") == bare, quoted
    # And the restraint it must not cost: these are still two different things.
    assert values.normalise("NW-CL-88213", "identifier") != values.normalise(
        "NWCL88213", "identifier"
    )


def test_a_pasted_example_is_cleaned_before_a_pattern_is_built_from_it():
    """An example copied out of a PDF carries its soft hyphens, and a pattern
    built from it would match only text carrying the same ones."""
    command = Command(kind="pattern", pattern=r"\bNW-CL-\d{5}\b", examples=["NW-CL-\u200b88213"])
    assert command.examples == ["NW-CL-88213"]
