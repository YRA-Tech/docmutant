"""Fitted replacement: the words change, and the geometry oracle and pdfium agree on the rest."""

from __future__ import annotations

import ctypes
import io

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest

from docmutant.fit import WordFitter
from docmutant.geometry import CONTAIN, BoxSettings, check_boxes, within
from docmutant.tracker import Line, group_lines, trace_page

# 10 pt: a b c d are 5 wide, e is 2.5, w is 8. Words of equal letters are equal in width.
WIDTHS = {32: 250, 44: 250, 97: 500, 98: 500, 99: 500, 100: 500, 101: 250, 119: 800}
UPPER = {code - 32: width for code, width in WIDTHS.items() if 97 <= code <= 122}
TO_UNICODE = b"1 beginbfchar <20> <0020> endbfchar 1 beginbfrange <2C> <7A> <002C> endbfrange"
ALL_GLYPHS = b"BT /F1 10 Tf 72 100 Td (a b c d e w A B C D E W ,) Tj ET "
"""A line of single letters: it shows every glyph, and none of it is a word to replace."""


def _font(pdf: pikepdf.Pdf, widths: dict[int, float] | None = None) -> pikepdf.Object:
    widths = widths or {**WIDTHS, **UPPER}
    first, last = min(widths), max(widths)
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            FirstChar=first,
            LastChar=last,
            Widths=[widths.get(code, 0) for code in range(first, last + 1)],
            ToUnicode=pdf.make_stream(TO_UNICODE),
        )
    )


def _pdf(content: bytes, *, glyphs: bytes = ALL_GLYPHS) -> pikepdf.Pdf:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=_font(pdf)))
    page.obj.Contents = pdf.make_stream(glyphs + content)
    return pdf


def _lines(pdf: pikepdf.Pdf) -> list[Line]:
    return group_lines(trace_page(pdf.pages[0]))


def _texts(pdf: pikepdf.Pdf, skip: int = 1) -> list[str]:
    return [line.text for line in _lines(pdf)[skip:]]


def _fit(pdf: pikepdf.Pdf, vocabulary, settings=None, seed: int = 0):
    settings = settings or BoxSettings("line")
    before = _lines(pdf)
    report = WordFitter(pdf, vocabulary).fit_page(0, settings, seed)
    return before, report


def _pdfium_words(pdf: pikepdf.Pdf) -> list[tuple[str, float]]:
    """Each word that pdfium reads from page 1, with the x of its first character."""
    buffer = io.BytesIO()
    pdf.save(buffer)
    textpage = pdfium.PdfDocument(buffer.getvalue())[0].get_textpage()
    words: list[tuple[str, float]] = []
    current = ""
    start = 0.0
    for index in range(textpage.count_chars()):
        char = chr(pdfium_c.FPDFText_GetUnicode(textpage.raw, index))
        if char.isspace():
            if current:
                words.append((current, start))
            current = ""
            continue
        if not current:
            x, y = ctypes.c_double(), ctypes.c_double()
            pdfium_c.FPDFText_GetCharOrigin(textpage.raw, index, ctypes.byref(x), ctypes.byref(y))
            start = x.value
        current += char
    if current:
        words.append((current, start))
    return words


def test_equal_width_words_change_and_nothing_moves():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cba) Tj ET")
    before, report = _fit(pdf, ["bad", "cab"])
    assert report.words_replaced == 2
    assert report.geometry.passed
    assert report.geometry.largest_word_move == pytest.approx(0, abs=1e-6)
    words = _texts(pdf)[0].split()
    assert set(words) <= {"bad", "cab"}
    # An equal-width word needs no number: the operator stays a Tj.
    operators = [str(i.operator) for i in pikepdf.parse_content_stream(pdf.pages[0])]
    assert "TJ" not in operators


def test_a_narrower_inner_word_keeps_the_next_word_in_place():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cab) Tj ET")
    # ace is 2.5 narrower than abc. The last word has no fit, because its end is held.
    before, report = _fit(pdf, ["ace"])
    after = _lines(pdf)
    assert report.geometry.passed, report.geometry.violations
    assert after[1].words[0].text == "ace"
    assert after[1].words[0].box.end == pytest.approx(before[1].words[0].box.end - 2.5)
    assert after[1].words[1].box.start == pytest.approx(before[1].words[1].box.start)
    assert after[1].box.end == pytest.approx(before[1].box.end)


def test_a_wider_inner_word_must_leave_the_minimum_gap():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cab) Tj ET")
    # The space is 2.5 and the minimum gap 1.5, so a word may grow by 1: abw grows by 3.
    _, report = _fit(pdf, ["abw"])
    assert report.words_replaced == 0
    assert report.words_left["no fit"] == 2
    assert _texts(pdf) == ["abc cab"]


def test_the_end_of_the_line_is_held():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj ET")
    _, report = _fit(pdf, ["ace", "abw"])
    assert report.words_replaced == 0
    assert _texts(pdf) == ["abc"]


def test_contain_lets_the_end_of_the_line_come_in():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj ET")
    before, report = _fit(pdf, ["ace", "abw"], BoxSettings("line", CONTAIN))
    assert _texts(pdf) == ["ace"]
    assert report.geometry.passed
    assert _lines(pdf)[1].box.end == pytest.approx(before[1].box.end - 2.5)


def test_tracking_fills_a_held_end():
    # ace is 2.5 narrow over 2 gaps: 1.25 per gap is 0.125 em.
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj ET")
    before, report = _fit(pdf, ["ace"], BoxSettings("line", tracking_limit=0.13))
    after = _lines(pdf)
    assert report.geometry.passed, report.geometry.violations
    assert after[1].text == "ace"
    assert after[1].box.end == pytest.approx(before[1].box.end, abs=0.01)
    assert report.geometry.largest_tracking == pytest.approx(0.125, abs=0.001)


def test_tracking_beyond_the_limit_does_not_fit():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj ET")
    _, report = _fit(pdf, ["ace"], BoxSettings("line", tracking_limit=0.1))
    assert report.words_replaced == 0
    assert report.words_left["no fit"] == 1
    assert _texts(pdf) == ["abc"]


def test_scope_word_holds_every_word():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cab) Tj ET")
    _, report = _fit(pdf, ["ace"], BoxSettings("word"))
    assert report.words_replaced == 0
    assert _texts(pdf) == ["abc cab"]
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cab) Tj ET")
    _, report = _fit(pdf, ["bad"], BoxSettings("word"))
    assert _texts(pdf) == ["bad bad"]
    assert report.geometry.largest_word_move == pytest.approx(0, abs=1e-6)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b"(Abc Cab)", "Bad Bad"),
        (b"(ABC)", "BAD"),
        # Punctuation stays with the word.
        (b"(abc, cab,)", "bad, bad,"),
    ],
)
def test_case_and_punctuation_follow_the_source(source, expected):
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td " + source + b" Tj ET")
    _, report = _fit(pdf, ["bad"])
    assert report.geometry.passed
    assert _texts(pdf) == [expected]


@pytest.mark.parametrize(
    "show",
    [
        # Kerning inside a word goes with the word; the number after it stays.
        b"[(a) 40 (bc) -100 ( cab)] TJ",
        b"[(abc cab)] TJ",
        # One string for each word, with a number between.
        b"[(abc) -300 (cab)] TJ",
        b"0 Tc 1 Tw (abc cab) Tj",
        # Character spacing and horizontal scaling.
        b"0.5 Tc (abc cab) Tj",
        b"80 Tz [(abc) -300 (cab)] TJ",
    ],
)
def test_every_way_of_showing_a_line_keeps_the_later_word_in_place(show):
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td " + show + b" ET")
    # ace is narrower than abc, so a number must return the pen. The last word has no fit.
    before, report = _fit(pdf, ["ace"])
    assert report.geometry.passed, report.geometry.violations
    after = _lines(pdf)
    assert [w.text for w in after[1].words] == ["ace", "cab"]
    assert after[1].words[1].box.start == pytest.approx(before[1].words[1].box.start, abs=1e-3)
    assert after[1].box.end == pytest.approx(before[1].box.end, abs=1e-3)


def test_quote_operators_become_tj_with_their_line_move_and_spacing():
    content = b"BT /F1 10 Tf 72 700 Td 12 TL (abc) Tj (abc cab) ' 1 0.5 (abc cab) \" ET"
    pdf = _pdf(content)
    before, report = _fit(pdf, ["ace"], BoxSettings("line"))
    assert report.geometry.passed, report.geometry.violations
    after = _lines(pdf)
    assert [line.text for line in after[1:]] == ["abc", "ace cab", "ace cab"]
    assert [line.box.bottom for line in after] == pytest.approx(
        [line.box.bottom for line in before]
    )
    for old, new in zip(before, after, strict=True):
        assert new.box.end == pytest.approx(old.box.end, abs=1e-3)
    operators = [str(i.operator) for i in pikepdf.parse_content_stream(pdf.pages[0])]
    assert "'" not in operators
    assert '"' not in operators
    assert operators.count("T*") == 2


@pytest.mark.parametrize(
    "show",
    [
        # Two instructions in a row, and a string for each part of the word.
        b"(ab) Tj (c cab) Tj",
        # A glyph at a time, each placed from the start of the line: nothing follows the pen.
        b"(a) Tj 5 0 Td (b) Tj 5 0 Td (c) Tj 7.5 0 Td (cab) Tj",
        # Across text objects, with a number in the second part.
        b"(a) Tj ET BT /F1 10 Tf 77 700 Td [(b) 0 (c)] TJ ( cab) Tj",
    ],
)
def test_a_word_across_instructions_is_replaced_and_the_next_word_stays(show):
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td " + show + b" ET")
    before, report = _fit(pdf, ["ace"])
    assert report.geometry.passed, report.geometry.violations
    after = _lines(pdf)
    assert [w.text for w in after[1].words] == ["ace", "cab"]
    assert after[1].words[1].box.start == pytest.approx(before[1].words[1].box.start, abs=1e-3)
    assert after[1].box.end == pytest.approx(before[1].box.end, abs=1e-3)


def test_a_part_of_a_word_alone_in_its_instruction_leaves_only_a_number():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (ab) Tj (c) Tj ( cab) Tj ET")
    _fit(pdf, ["ace"])
    shows = [
        i.operands[0] for i in pikepdf.parse_content_stream(pdf.pages[0]) if str(i.operator) == "TJ"
    ]
    assert all(not isinstance(item, pikepdf.String) for item in shows[-1])


def test_a_word_in_two_sizes_is_left():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (ab) Tj /F1 8 Tf (c) Tj ET")
    _, report = _fit(pdf, ["bad"])
    assert report.words_left["mixed fonts"] == 1
    assert _texts(pdf) == ["abc"]


def test_later_lines_and_rotated_text_stay_in_place():
    content = b"q 0.8 0.6 -0.6 0.8 100 300 cm BT /F1 10 Tf (abc cab) Tj 0 -14 Td (abc cab) Tj ET Q"
    pdf = _pdf(content)
    before, report = _fit(pdf, ["ace"])
    assert report.geometry.passed, report.geometry.violations
    after = _lines(pdf)
    assert report.words_replaced == 2
    for old, new in zip(before, after, strict=True):
        assert new.box.start == pytest.approx(old.box.start, abs=1e-3)
        assert new.box.end == pytest.approx(old.box.end, abs=1e-3)


def test_a_composite_font_uses_two_byte_codes():
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    descendant = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.CIDFontType2,
        BaseFont=pikepdf.Name.Arial,
        CIDSystemInfo=pikepdf.Dictionary(
            Registry=pikepdf.String("Adobe"), Ordering=pikepdf.String("Identity"), Supplement=0
        ),
        W=[1, [500, 500, 500, 250, 800, 250]],
    )
    to_unicode = (
        b"1 beginbfrange <0001> <0005> [<0061> <0062> <0063> <0065> <0077>] endbfrange "
        b"1 beginbfchar <0006> <0020> endbfchar"
    )
    page.obj.Resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(
            F1=pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Font,
                    Subtype=pikepdf.Name.Type0,
                    BaseFont=pikepdf.Name.Arial,
                    Encoding=pikepdf.Name("/Identity-H"),
                    DescendantFonts=[pdf.make_indirect(descendant)],
                    ToUnicode=pdf.make_stream(to_unicode),
                )
            )
        )
    )
    # abc ace | cab, with the e and the w shown once.
    page.obj.Contents = pdf.make_stream(
        b"BT /F1 10 Tf 72 700 Td <00010002000300060003000100020006000100030004> Tj ET "
        b"BT /F1 10 Tf 72 100 Td <0005> Tj ET"
    )
    before = _lines(pdf)
    report = WordFitter(pdf, ["bca", "ace"]).fit_page(0, BoxSettings("line"), 1)
    assert report.geometry.passed, report.geometry.violations
    assert report.words_replaced > 0
    shown = [g.code for g in trace_page(pdf.pages[0])]
    assert all(code <= 6 for code in shown)
    assert _lines(pdf)[0].box.end == pytest.approx(before[0].box.end, abs=1e-3)


def test_a_replacement_uses_only_codes_the_document_shows():
    # The font maps every letter, but the document shows only a, b and c.
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj ET", glyphs=b"")
    _, report = _fit(pdf, ["dab", "bca"])
    assert _lines(pdf)[0].text == "bca"
    assert report.words_replaced == 1


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"BT /F1 10 Tf 72 700 Td (a,c) Tj ET", "not a word"),
        (b"BT /F1 10 Tf 72 700 Td (a) Tj ET", "not a word"),
        # Fake bold: two different words printed over each other would be unreadable.
        (
            b"BT /F1 10 Tf 72 700 Td (abc) Tj ET BT /F1 10 Tf 72.2 700 Td (abc) Tj ET",
            "printed over",
        ),
    ],
)
def test_a_word_left_alone_gives_its_reason(content, reason):
    pdf = _pdf(content)
    before = _texts(pdf)
    _, report = _fit(pdf, ["bad"])
    assert report.words_left[reason] >= 1
    assert _texts(pdf) == before


def test_a_word_without_text_is_left():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj ET")
    del pdf.pages[0].Resources.Font.F1.ToUnicode
    _, report = _fit(pdf, ["bad"])
    assert report.words_replaced == 0
    assert report.words_left["no text"] >= 1


def test_text_in_a_form_is_left():
    pdf = _pdf(b"/X1 Do")
    form = pdf.make_stream(
        b"BT /F1 10 Tf 72 700 Td (abc) Tj ET",
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 612, 792],
        Resources=pikepdf.Dictionary(Font=pdf.pages[0].Resources.Font),
    )
    pdf.pages[0].Resources.XObject = pikepdf.Dictionary(X1=pdf.make_indirect(form))
    _, report = _fit(pdf, ["bad"])
    assert report.words_left["in a form"] == 1
    assert _texts(pdf) == ["abc"]


def test_the_oracle_undoes_a_line_it_rejects(monkeypatch):
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj 0 -14 Td (cab) Tj ET")
    calls = {"count": 0}

    def reject_once(source, output, settings):
        calls["count"] += 1
        result = check_boxes(source, output, settings)
        if calls["count"] > 1:
            return result
        from dataclasses import replace

        from docmutant.geometry import Violation

        return replace(result, violations=(Violation("line-box", 9, 0, 1),))

    monkeypatch.setattr("docmutant.fit.check_boxes", reject_once)
    _, report = _fit(pdf, ["bad"])
    assert report.lines_undone == 1
    assert report.words_left["undone"] == 1
    assert _texts(pdf) == ["abc", "bad"]
    assert report.words_replaced == 1


def test_the_same_seed_gives_the_same_words_and_another_seed_can_differ():
    vocabulary = ["bad", "cab", "dab", "abd", "bac", "cad"]

    def run(seed: int) -> list[str]:
        pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cab abc cab abc cab) Tj ET")
        _fit(pdf, vocabulary, seed=seed)
        return _texts(pdf)

    assert run(1) == run(1)
    assert len({tuple(run(seed)) for seed in range(6)}) > 1


def test_pdfium_reads_the_new_words_at_the_old_starts():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cab) Tj 0 -14 Td [(cab) -300 (abc)] TJ ET")
    before = _pdfium_words(pdf)
    _, report = _fit(pdf, ["ace"])
    assert report.geometry.passed, report.geometry.violations
    after = _pdfium_words(pdf)
    assert [word for word, _ in after][-4:] == ["ace", "cab", "ace", "abc"]
    assert [x for _, x in after] == pytest.approx([x for _, x in before], abs=0.05)


def test_free_word_count_is_accepted_and_keeps_the_count():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cab) Tj ET")
    _, report = _fit(pdf, ["ace"], BoxSettings("line", word_count="free"))
    assert report.geometry.passed, report.geometry.violations
    assert report.geometry.lines_with_changed_word_count == 0
    assert _texts(pdf) == ["ace cab"]


def test_a_loose_tolerance_passes_the_oracle():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cab) Tj ET")
    _, report = _fit(pdf, ["abw", "dab"], BoxSettings("word", within(5), word_gap=None))
    assert report.geometry.passed, report.geometry.violations
    assert report.words_replaced == 2
