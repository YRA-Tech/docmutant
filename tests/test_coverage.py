"""Coverage: every word changes, by a definition that a near copy does not pass."""

from __future__ import annotations

import io
import re

import pikepdf
import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from test_fit import _font, _lines, _pdf, _pdfium_words, _texts

from docmutant.coverage import check_coverage, differs, other_layers, residual_text, tokens
from docmutant.fit import CoverageStop, FitPolicy, WordFitter
from docmutant.geometry import BoxSettings
from docmutant.mutate import encoding_text
from docmutant.substitute import CannotSubstitute, FontSource, Substitute, embed_substitute
from docmutant.tracker import group_lines, trace_page

ALL_GLYPHS = b"BT /F1 10 Tf 72 100 Td (a   b   c   d   e   w   A   B   C   D   E   W   ,) Tj ET "
"""Every glyph, with room around each letter for the widest to stand in for the narrowest."""

ALL = FitPolicy("all")
MOVED = FitPolicy("all", "moved")


def _save(pdf: pikepdf.Pdf) -> bytes:
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def _pdf_all(content: bytes, glyphs: bytes = ALL_GLYPHS) -> pikepdf.Pdf:
    return _pdf(content, glyphs=glyphs)


def _fit_all(pdf, vocabulary, policy=ALL, settings=None, fonts=None, seed=0):
    settings = settings or BoxSettings("line", tracking_limit=0.2)
    return WordFitter(pdf, vocabulary, fonts).fit_page(0, settings, seed, policy)


@pytest.mark.parametrize(
    ("source", "output", "expected"),
    [
        ("smith", "smyth", False),
        ("smith", "smote", False),
        ("smith", "stoke", True),
        ("smith", "bread", True),
        ("2024", "2025", False),
        ("2024", "1935", True),
        ("a", "b", True),
        ("a", "a", False),
        ("ab", "ac", True),
    ],
)
def test_a_word_changes_only_when_half_of_it_differs(source, output, expected):
    assert differs(tuple(source), tuple(output)) is expected


def test_the_oracle_ignores_punctuation_and_case():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (Abc , cab) Tj ET")
    lines = group_lines(trace_page(pdf.pages[0]))
    result = check_coverage(lines, lines)
    assert not result.passed
    assert result.words_out_of_scope == 1 + 1  # the commas: one in the source, one in the glyphs
    assert [word.text for word in result.unchanged][-2:] == ["Abc", "cab"]
    assert tokens(lines[1].words[0]) == ("a", "b", "c")


def test_a_page_compared_with_itself_never_passes_and_counts_every_distance_as_kept():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cab) Tj ET", glyphs=b"")
    lines = group_lines(trace_page(pdf.pages[0]))
    result = check_coverage(lines, lines)
    assert result.words_changed == 0
    assert (result.next_word_pairs, result.next_word_kept) == (1, 1)


def test_another_number_of_lines_fails():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cab) Tj ET", glyphs=b"")
    lines = group_lines(trace_page(pdf.pages[0]))
    result = check_coverage(lines, [])
    assert not result.same_lines
    assert not result.passed


def test_coverage_all_changes_every_word_with_letters_or_digits():
    pdf = _pdf_all(
        b"BT /F1 10 Tf 72 700 Td (abc a b,c a,b 12 a) Tj ET", ALL_GLYPHS + _digit_glyphs()
    )
    pdf.pages[0].Resources.Font.F1 = _font(pdf, {**_WIDE, **{c: 500 for c in range(48, 58)}})
    before = _lines(pdf)
    report = _fit_all(pdf, ["bad", "dab"], settings=BoxSettings("line", tracking_limit=0.5))
    assert report.coverage.passed, report.coverage.unchanged
    assert report.geometry.passed, report.geometry.violations
    assert report.coverage.words_changed == report.coverage.words_in_scope
    assert report.words_replaced == report.coverage.words_changed
    assert len(_lines(pdf)) == len(before)
    # The comma of a word stays: it is not what has to change.
    assert "," in _lines(pdf)[-1].words[2].text


def test_the_ladder_falls_from_real_word_to_invented_word_to_letter_swap():
    # No word of the vocabulary fits: a word of letters is swapped, vowel for vowel.
    pdf = _pdf_all(b"BT /F1 10 Tf 72 700 Td (bad) Tj ET")
    report = _fit_all(pdf, [])
    assert report.tiers.get("invented word", 0) + report.tiers.get("letter swap", 0) > 0
    assert "real word" not in report.tiers
    # A real word comes first when one fits.
    pdf = _pdf(
        b"BT /F1 10 Tf 72 700 Td (bad) Tj ET", glyphs=b"BT /F1 10 Tf 72 90 Td (c a b) Tj ET "
    )
    report = _fit_all(pdf, ["cab"])
    assert report.tiers.get("real word", 0) >= 1


def test_a_replacement_is_no_word_of_the_source_and_not_close_to_its_word():
    pdf = _pdf_all(b"BT /F1 10 Tf 72 700 Td (abc abd cab) Tj ET")
    report = _fit_all(pdf, ["abc", "abd", "cab", "abe", "bad"])
    assert report.coverage.passed
    words = _lines(pdf)[-1].text.split()
    assert not {"abc", "abd", "cab", "abe"} & set(words)


def test_a_word_in_two_fonts_changes_in_both_and_its_letters_flow():
    pdf = _pdf_all(b"BT /F1 10 Tf 72 700 Td (ab) Tj /F2 10 Tf (cd) Tj ET")
    pdf.pages[0].Resources.Font.F2 = _font(pdf)
    report = _fit_all(pdf, ["bad"])
    assert report.coverage.passed, report.words_left
    assert report.geometry.passed, report.geometry.violations
    word = _lines(pdf)[-1].words[0]
    assert {g.font for g in word.glyphs} == {"/F1", "/F2"}
    assert report.geometry.largest_tracking <= 0.2
    # The letters flow across the change of font: each starts where the last one ended.
    for first, second in zip(word.glyphs, word.glyphs[1:], strict=False):
        gap = second.origin[0] - first.origin[0] - first.advance
        assert abs(gap) <= 0.2 * first.em


def test_numbers_stay_digits():
    pdf = _pdf_all(b"BT /F1 10 Tf 72 700 Td (2024) Tj ET", ALL_GLYPHS + _digit_glyphs())
    pdf.pages[0].Resources.Font.F1 = _font(pdf, {**_WIDE, **{c: 500 for c in range(48, 58)}})
    report = _fit_all(pdf, [])
    assert report.coverage.passed
    text = _lines(pdf)[-1].text
    assert text.isdigit() and len(text) == 4


_WIDE = {32: 250, 44: 250, 97: 500, 98: 500, 99: 500, 100: 500, 101: 250, 119: 800}


def _digit_glyphs() -> bytes:
    return b"BT /F1 10 Tf 72 90 Td (0   1   2   3   4   5   6   7   8   9) Tj ET "


def test_fake_bold_keeps_one_word_in_both_lines():
    content = b"BT /F1 10 Tf 72 700 Td (abc) Tj ET BT /F1 10 Tf 72.2 700 Td (abc) Tj ET"
    pdf = _pdf_all(content)
    report = _fit_all(pdf, ["bad", "dab", "cad", "bed"])
    assert report.coverage.passed, report.coverage.unchanged
    first, second = _lines(pdf)[-2:]
    assert first.text == second.text
    assert first.text != "abc"


def _form_page(draws: bytes = b"/X1 Do", other_page: bool = False) -> pikepdf.Pdf:
    pdf = _pdf_all(draws)
    form = pdf.make_stream(
        b"BT /F1 10 Tf 72 700 Td (abc) Tj ET",
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 612, 792],
        Resources=pikepdf.Dictionary(Font=pdf.pages[0].Resources.Font),
    )
    pdf.pages[0].Resources.XObject = pikepdf.Dictionary(X1=pdf.make_indirect(form))
    if other_page:
        page = pdf.add_blank_page(page_size=(612, 792))
        page.obj.Resources = pdf.pages[0].Resources
        page.obj.Contents = pdf.make_stream(b"/X1 Do")
    return pdf


def test_text_in_a_form_changes_under_coverage_all():
    pdf = _form_page()
    report = _fit_all(pdf, ["bad"])
    assert report.coverage.passed
    assert _lines(pdf)[-1].text == "bad"


def test_a_form_drawn_twice_changes_each_drawing_on_its_own():
    pdf = _form_page(b"/X1 Do q 1 0 0 1 0 -20 cm /X1 Do Q")
    report = _fit_all(pdf, ["bad", "dab", "cad"])
    assert report.coverage.passed, report.coverage.unchanged
    assert report.geometry.passed, report.geometry.violations
    assert report.coverage.words_changed == report.coverage.words_in_scope


def test_a_form_shared_with_another_page_stays_on_that_page_until_it_is_fitted():
    pdf = _form_page(other_page=True)
    report = _fit_all(pdf, ["bad"])
    assert report.coverage.passed
    assert group_lines(trace_page(pdf.pages[1]))[0].text == "abc"
    assert _lines(pdf)[-1].text == "bad"
    second = WordFitter(pdf, ["cad"]).fit_page(
        1, BoxSettings("line", tracking_limit=0.2), 0, FitPolicy("all")
    )
    assert second.coverage.passed
    assert group_lines(trace_page(pdf.pages[1]))[0].text != "abc"


def test_a_word_that_cannot_change_stops_the_run_and_is_named():
    # The font shows a single letter, so nothing can stand for it.
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (a) Tj ET", glyphs=b"")
    with pytest.raises(CoverageStop) as stop:
        _fit_all(pdf, [])
    assert stop.value.page == 0
    assert stop.value.report.coverage.unchanged[0].text == "a"
    assert "'a'" in str(stop.value)


def test_a_line_the_oracle_rejects_stops_the_run(monkeypatch):
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj ET")
    from dataclasses import replace

    from docmutant.fit import check_boxes as real
    from docmutant.geometry import Violation

    def reject(source, output, settings):
        result = real(source, output, settings)
        return replace(result, violations=(Violation("line-box", 9, 0, 1),))

    monkeypatch.setattr("docmutant.fit.check_boxes", reject)
    with pytest.raises(CoverageStop):
        _fit_all(pdf, ["bad"])


def test_best_effort_still_leaves_words_and_does_not_stop():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (a) Tj ET", glyphs=b"")
    report = WordFitter(pdf, []).fit_page(0, BoxSettings("line"))
    assert report.words_left["not a word"] == 1
    assert not report.coverage.passed


def test_moved_starts_hold_the_line_and_leave_the_next_word_distance_behind():
    words = " ".join(["abc", "dab", "cab", "bad", "add", "bed", "cad", "ace", "dad", "aid"])
    content = b"BT /F1 10 Tf 72 700 Td (" + words.encode() + b") Tj ET"
    pdf = _pdf(content)
    vocabulary = ["ab", "bed", "abba", "dabble", "cede", "wed", "web", "dew", "awed", "cabbed"]
    before = _lines(pdf)
    report = _fit_all(pdf, vocabulary, MOVED, BoxSettings("line"))
    assert report.geometry.passed, report.geometry.violations
    assert report.coverage.passed, report.coverage.unchanged
    assert report.lines_pinned == 0
    after = _lines(pdf)
    assert after[-1].box.start == pytest.approx(before[-1].box.start, abs=0.02)
    assert after[-1].box.end == pytest.approx(before[-1].box.end, abs=0.02)
    assert report.coverage.next_word_kept < report.coverage.next_word_pairs / 2


@pytest.mark.parametrize(
    "show",
    [
        b"[(abc dab) -100 ( cab bad)] TJ",
        b"[(abc) -250 (dab) -250 (cab) -250 (bad)] TJ",
        b"(abc ) Tj (dab ) Tj (cab ) Tj (bad) Tj",
        b"(abc) Tj 17.5 0 Td (dab) Tj 17.5 0 Td (cab) Tj 17.5 0 Td (bad) Tj",
        b"1 Tw (abc dab cab bad) Tj",
        b"0.5 Tc (abc dab cab bad) Tj",
        b"80 Tz (abc dab cab bad) Tj",
    ],
)
def test_moved_starts_work_however_the_line_is_shown(show):
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td " + show + b" ET")
    before = _lines(pdf)
    report = _fit_all(pdf, ["ab", "bed", "wed", "awed", "aced", "cede"], MOVED, BoxSettings("line"))
    assert report.geometry.passed, report.geometry.violations
    assert report.coverage.passed, report.coverage.unchanged
    after = _lines(pdf)
    assert len(after) == len(before)
    assert after[-1].box.end == pytest.approx(before[-1].box.end, abs=0.02)


def test_pdfium_reads_moved_words_where_the_tracker_puts_them():
    pdf = _pdf(
        b"BT /F1 10 Tf 72 700 Td (abc dab cab bad) Tj ET",
        glyphs=b"BT /F1 10 Tf 72 90 Td (w e) Tj ET ",
    )
    _fit_all(pdf, ["ab", "bed", "wed", "awed", "aced"], MOVED, BoxSettings("line"))
    words = _pdfium_words(pdf)
    line = _lines(pdf)[-1]
    expected = [(word.text, word.box.start) for word in line.words]
    assert [word for word, _ in words][-4:] == [word for word, _ in expected]
    assert [x for _, x in words][-4:] == pytest.approx([x for _, x in expected], abs=0.05)


def test_moved_needs_scope_line():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj ET")
    with pytest.raises(ValueError, match="scope line"):
        WordFitter(pdf, ["bad"]).fit_page(0, BoxSettings("word"), 0, MOVED)


def test_moved_tight_gaps_give_less_than_wide_ones():
    # The first gap is already at the minimum of 1.5, and the second has room.
    pdf = _pdf_all(b"BT /F1 10 Tf 72 700 Td [(abc) 100 (,) -600 (dab)] TJ ET", ALL_GLYPHS)
    before = _lines(pdf)
    report = _fit_all(pdf, ["abw", "ace", "bed"], MOVED, BoxSettings("line"))
    assert report.geometry.passed, report.geometry.violations
    assert report.lines_pinned == 0
    assert _lines(pdf)[-1].box.end == pytest.approx(before[-1].box.end, abs=0.02)


def test_moved_words_stay_on_their_side_of_a_wide_gap():
    # 1.5 em between two table cells: words may move inside a cell but not across the gap.
    pdf = _pdf_all(b"BT /F1 10 Tf 72 700 Td [(abc dab) -1500 (cab bad)] TJ ET", ALL_GLYPHS)
    before = _lines(pdf)[-1]
    report = _fit_all(pdf, ["ab", "bed", "wed", "awed", "aced"], MOVED, BoxSettings("line"))
    assert report.geometry.passed, report.geometry.violations
    assert report.coverage.passed, report.coverage.unchanged
    after = _lines(pdf)[-1]
    assert after.words[1].box.end == pytest.approx(before.words[1].box.end, abs=0.02)
    assert after.words[2].box.start == pytest.approx(before.words[2].box.start, abs=0.02)
    assert after.words[1].box.start != pytest.approx(before.words[1].box.start, abs=0.02)


def test_the_same_seed_moves_words_the_same_way():
    def run(seed: int) -> list[float]:
        pdf = _pdf_all(b"BT /F1 10 Tf 72 700 Td (abc dab cab bad) Tj ET")
        _fit_all(pdf, ["ab", "bed", "wed", "awed", "aced"], MOVED, BoxSettings("line"), seed=seed)
        return [word.box.start for word in _lines(pdf)[-1].words]

    assert run(3) == run(3)


def test_the_parts_of_a_hyphenated_source_word_are_no_replacements():
    pdf = _pdf_all(b"BT /F1 10 Tf 72 700 Td (add-bad cab) Tj ET")
    report = _fit_all(pdf, ["add", "bad", "bed", "dew"])
    assert report.coverage.passed
    words = {word.lower() for word in re.findall(r"[a-zA-Z]+", _lines(pdf)[-1].text)}
    assert not words & {"add", "bad", "cab"}


def test_a_moved_line_with_a_word_that_stays_moves_it_with_its_kerning():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td [(abc , c) 40 (ab)] TJ ET")
    report = _fit_all(pdf, ["ab", "wed", "bed"], MOVED, BoxSettings("line"))
    assert report.geometry.passed, report.geometry.violations
    assert "," in _lines(pdf)[-1].text


def test_residual_text_finds_what_pdfium_still_reads():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc cab) Tj ET", glyphs=b"")
    source = _save(pdf)
    assert residual_text(source, source) == {0: ("abc", "cab")}
    pdf = _pdf_all(b"BT /F1 10 Tf 72 700 Td (abc cab) Tj ET")
    source = _save(pdf)
    report = _fit_all(pdf, ["bad", "dab", "cad"])
    assert report.coverage.passed
    assert residual_text(source, _save(pdf)) == {}


def test_other_layers_are_named_and_images_counted():
    pdf = _pdf(b"")
    assert other_layers(pdf).blocking == {}
    pdf.docinfo["/Title"] = "A private title"
    pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Outlines))
    pdf.pages[0].obj.Annots = pikepdf.Array(
        [
            pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Annot,
                    Subtype=pikepdf.Name.Text,
                    Rect=[0, 0, 9, 9],
                    Contents=pikepdf.String("A private note"),
                )
            )
        ]
    )
    pdf.pages[0].Resources.XObject = pikepdf.Dictionary(
        I1=pdf.make_stream(
            b"\x00",
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Width=1,
            Height=1,
            BitsPerComponent=8,
            ColorSpace=pikepdf.Name.DeviceGray,
        )
    )
    found = other_layers(pdf)
    assert found.blocking == {"document info": 1, "outlines": 1, "annotation text": 1}
    assert found.images == 1


@pytest.mark.parametrize(
    ("encoding", "code", "text"),
    [
        ("/WinAnsiEncoding", 0x41, "A"),
        ("/WinAnsiEncoding", 0x93, "“"),
        ("/MacRomanEncoding", 0x8E, "é"),
    ],
)
def test_a_named_encoding_gives_text(encoding, code, text):
    assert encoding_text(pikepdf.Name(encoding))[code] == text


def test_glyph_names_give_text_and_a_name_that_says_nothing_removes_it():
    encoding = pikepdf.Dictionary(
        BaseEncoding=pikepdf.Name.WinAnsiEncoding,
        Differences=pikepdf.Array(
            [65, pikepdf.Name.B, pikepdf.Name("/g123"), pikepdf.Name("/uni0416"), pikepdf.Name.fi]
        ),
    )
    table = encoding_text(encoding)
    assert table[65] == "B"
    assert 66 not in table
    assert table[67] == "Ж"
    assert table[68] == "ﬁ" or table[68] == "fi"
    assert table[69] == "E"


def test_no_named_encoding_says_nothing_beyond_differences():
    assert encoding_text(None) == {}
    assert encoding_text(pikepdf.Dictionary(Differences=pikepdf.Array([65, pikepdf.Name.B]))) == {
        65: "B"
    }


def test_a_font_with_glyph_names_and_no_to_unicode_now_changes():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj ET")
    font = pdf.pages[0].Resources.Font.F1
    del font.ToUnicode
    font.Encoding = pikepdf.Name.WinAnsiEncoding
    report = WordFitter(pdf, ["bad"]).fit_page(0, BoxSettings("line"))
    assert report.words_replaced == 1
    assert _lines(pdf)[-1].text == "bad"


def test_a_font_with_no_text_is_swapped_by_code_under_coverage_all():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj ET")
    del pdf.pages[0].Resources.Font.F1.ToUnicode
    before = [g.code for g in trace_page(pdf.pages[0])]
    report = _fit_all(pdf, [])
    assert report.coverage.passed, report.coverage.unchanged
    assert [g.code for g in trace_page(pdf.pages[0])] != before


# --- substitution -------------------------------------------------------------------------


def _program(
    chars: str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ",
) -> bytes:
    """A small TrueType font: each glyph is a box, 500 wide, and w is 800."""
    names = [".notdef"] + [f"uni{ord(char):04X}" for char in chars]
    pen = TTGlyphPen(None)
    pen.moveTo((50, 0))
    pen.lineTo((450, 0))
    pen.lineTo((450, 700))
    pen.lineTo((50, 700))
    pen.closePath()
    box = pen.glyph()
    empty = TTGlyphPen(None).glyph()
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({ord(char): f"uni{ord(char):04X}" for char in chars})
    builder.setupGlyf({name: (empty if name == "uni0020" else box) for name in names})
    builder.setupHorizontalMetrics(
        {name: (800 if name == "uni0077" else 500, 50) for name in names}
    )
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Boxes", "styleName": "Regular", "copyright": "c"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200)
    builder.setupPost()
    buffer = io.BytesIO()
    builder.save(buffer)
    return buffer.getvalue()


class _Boxes:
    def pick(self, font):
        return Substitute(str(font.get("/BaseFont"))[1:], "Boxes-Regular", _program(), False)


def test_a_font_with_too_few_glyphs_gets_a_substitute_and_the_word_changes():
    # Only a is shown, so nothing can stand for it until the font can draw more.
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (a) Tj ET", glyphs=b"")
    before = _lines(pdf)
    report = _fit_all(pdf, [], fonts=_Boxes())
    assert report.coverage.passed, report.coverage.unchanged
    assert report.substituted == {"Helvetica": "Boxes-Regular"}
    assert report.geometry.passed, report.geometry.violations
    assert _lines(pdf)[-1].box.end == pytest.approx(before[-1].box.end, abs=0.02)
    font = pdf.pages[0].Resources.Font.F1
    assert font.Subtype == "/TrueType"
    assert "/FontFile2" in font.FontDescriptor


def test_a_substitute_keeps_the_codes_widths_and_vertical_metrics_of_the_source():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (abc) Tj ET", glyphs=b"BT /F1 10 Tf 72 90 Td (e) Tj ET ")
    font = pdf.pages[0].Resources.Font.F1
    font.FontDescriptor = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.FontDescriptor,
            FontName=pikepdf.Name.Helvetica,
            Flags=32,
            Ascent=700,
            Descent=-100,
            FontBBox=[0, -100, 900, 700],
        )
    )
    before = _lines(pdf)
    fitter = WordFitter(pdf, [])
    added = embed_substitute(pdf, font, _Boxes().pick(font), fitter.shown[font.objgen])
    after = _lines(pdf)
    assert [line.text for line in after] == [line.text for line in before]
    for old, new in zip(before, after, strict=True):
        assert (new.box.start, new.box.end, new.box.bottom, new.box.top) == pytest.approx(
            (old.box.start, old.box.end, old.box.bottom, old.box.top)
        )
    assert "z" in added.added
    assert font.ToUnicode is not None


def test_a_substitute_renders_and_pdfium_reads_its_new_letters():
    pdf = _pdf(b"BT /F1 20 Tf 72 700 Td (a) Tj ET", glyphs=b"")
    _fit_all(pdf, [], fonts=_Boxes())
    text = _lines(pdf)[-1].text
    assert text != "a" and text.isalpha()
    assert [word for word, _ in _pdfium_words(pdf)] == [text]
    import pypdfium2 as pdfium

    image = pdfium.PdfDocument(_save(pdf))[0].render(scale=1).to_pil().convert("L")
    assert image.getextrema()[0] < 128


def test_a_composite_font_takes_a_substitute_at_new_codes():
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    descendant = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.CIDFontType2,
        BaseFont=pikepdf.Name.Arial,
        CIDSystemInfo=pikepdf.Dictionary(
            Registry=pikepdf.String("Adobe"), Ordering=pikepdf.String("Identity"), Supplement=0
        ),
        W=[1, [500]],
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
                    ToUnicode=pdf.make_stream(b"1 beginbfchar <0001> <0061> endbfchar"),
                )
            )
        )
    )
    page.obj.Contents = pdf.make_stream(b"BT /F1 10 Tf 72 700 Td <0001> Tj ET")
    report = _fit_all(pdf, [], fonts=_Boxes())
    assert report.coverage.passed, report.coverage.unchanged
    glyph = trace_page(pdf.pages[0])[0]
    assert glyph.length == 2 and glyph.code >= 0x100
    assert glyph.text != "a"


def _type3_page() -> pikepdf.Pdf:
    """Two codes of a Type3 font, in a unit of 1/500 of the font size."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    proc = pdf.make_stream(b"250 0 d0 0 0 200 200 re f")
    font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.Type3,
        Name=pikepdf.Name.T3,
        FontBBox=[0, -100, 300, 400],
        FontMatrix=[0.002, 0, 0, 0.002, 0, 0],
        CharProcs=pikepdf.Dictionary(a=proc, b=proc),
        Encoding=pikepdf.Dictionary(Differences=[97, pikepdf.Name.a, pikepdf.Name.b]),
        FirstChar=97,
        LastChar=98,
        Widths=[250, 250],
    )
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=pdf.make_indirect(font)))
    page.obj.Contents = pdf.make_stream(b"BT /F1 10 Tf 72 700 Td (aba) Tj ET")
    return pdf


def test_a_type3_font_becomes_truetype_with_the_same_advances_and_height():
    pdf = _type3_page()
    before = _lines(pdf)
    font = pdf.pages[0].Resources.Font.F1
    fitter = WordFitter(pdf, [])
    embedded = embed_substitute(pdf, font, _Boxes().pick(font), fitter.shown[font.objgen])
    assert embedded.width_factor == pytest.approx(2)
    after = _lines(pdf)
    assert after[0].text == before[0].text
    for old, new in zip(before[0].words[0].glyphs, after[0].words[0].glyphs, strict=True):
        assert new.origin == pytest.approx(old.origin)
        for new_corner, old_corner in zip(new.box, old.box, strict=True):
            assert new_corner == pytest.approx(old_corner)
    assert font.Subtype == "/TrueType"
    assert "/CharProcs" not in font


def test_a_scarce_type3_font_changes_with_a_substitute():
    pdf = _type3_page()
    # The font shows only a, so nothing can stand for it until the font draws more.
    pdf.pages[0].obj.Contents = pdf.make_stream(b"BT /F1 10 Tf 72 700 Td (aaa) Tj ET")
    report = _fit_all(pdf, [], fonts=_Boxes())
    assert report.coverage.passed, report.coverage.unchanged
    assert report.geometry.passed, report.geometry.violations


def test_a_substitute_cannot_stand_for_a_code_with_no_text():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (a) Tj ET", glyphs=b"")
    font = pdf.pages[0].Resources.Font.F1
    del font.ToUnicode
    fitter = WordFitter(pdf, [])
    with pytest.raises(CannotSubstitute, match="no text"):
        embed_substitute(pdf, font, _Boxes().pick(font), fitter.shown[font.objgen])


def test_a_substitute_that_lacks_a_shown_glyph_is_refused():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (a) Tj ET", glyphs=b"")
    font = pdf.pages[0].Resources.Font.F1
    fitter = WordFitter(pdf, [])
    substitute = Substitute("x", "y", _program("bc "), False)
    with pytest.raises(CannotSubstitute, match="lacks"):
        embed_substitute(pdf, font, substitute, fitter.shown[font.objgen])


@pytest.mark.parametrize(
    ("name", "flags", "family", "compatible"),
    [
        ("ABCDEF+Arial-BoldMT", 0, "LiberationSans-Bold", True),
        ("TimesNewRomanPS-ItalicMT", 0, "LiberationSerif-Italic", True),
        ("Courier", 0, "LiberationMono-Regular", True),
        ("CMR10", 2, "LiberationSerif-Regular", False),
        ("Unknown", 1, "LiberationMono-Regular", False),
        ("Unknown", 0, "LiberationSans-Regular", False),
    ],
)
def test_font_source_picks_by_family_then_by_kind(tmp_path, name, flags, family, compatible):
    for stem in (
        "LiberationSans-Regular",
        "LiberationSans-Bold",
        "LiberationSerif-Regular",
        "LiberationSerif-Italic",
        "LiberationMono-Regular",
    ):
        (tmp_path / f"{stem}.ttf").write_bytes(b"x")
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            BaseFont=pikepdf.Name("/" + name),
            FontDescriptor=pikepdf.Dictionary(Flags=flags),
        )
    )
    picked = FontSource([tmp_path]).pick(font)
    assert (picked.name, picked.metric_compatible) == (family, compatible)
    assert picked.source_name == name


def test_font_source_with_no_files_picks_nothing(tmp_path):
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(BaseFont=pikepdf.Name.Arial))
    assert FontSource([tmp_path]).pick(font) is None


def test_without_a_font_source_a_scarce_font_still_stops():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (a) Tj ET", glyphs=b"")
    with pytest.raises(CoverageStop):
        _fit_all(pdf, [])
    assert _texts(pdf, skip=0) == ["a"]
