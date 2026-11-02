"""Formulas: a patterned block in place of displayed math, and of a word of math in a sentence."""

from __future__ import annotations

import io

import pikepdf
import pytest
from test_anonymize import _privacy, _run
from test_fit import TO_UNICODE, WIDTHS, _lines

from docmutant import anonymize as anonymize_module
from docmutant.anonymize import resolve_profile
from docmutant.formulas import (
    PATTERN_KEY,
    draw_formulas,
    find_formulas,
    font_names,
    glyphs_left,
    remove_formulas,
    without_formulas,
)
from docmutant.manifest import PRIVACY_ORACLES
from docmutant.tracker import group_lines, trace_page

GLYPHS = b"BT /F1 10 Tf 72 100 Td (a   b   c   d   e   w   A   B   C   D   E   W   ,) Tj ET "
TEXT = b"BT /F1 10 Tf 72 720 Td (abcd dcba abab) Tj ET BT /F1 10 Tf 72 620 Td (cabd bacd) Tj ET "
FORMULA = (
    # A fraction: its numerator and denominator are lines of their own, over and under a bar.
    b"BT /F2 10 Tf 200 676 Td (ab) Tj ET "
    b"0 0 0 rg 196 672 20 0.5 re f "
    b"BT /F2 10 Tf 200 664 Td (cd) Tj ET "
    b"BT /F2 10 Tf 220 670 Td (= abc) Tj ET "
)


def _font(pdf: pikepdf.Pdf, name: str) -> pikepdf.Object:
    widths = {**WIDTHS, 61: 500}
    first, last = min(widths), max(widths)
    return pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name(name),
            FirstChar=first,
            LastChar=last,
            Widths=[widths.get(code, 0) for code in range(first, last + 1)],
            ToUnicode=pdf.make_stream(TO_UNICODE.replace(b"<002C>", b"<002C>")),
        )
    )


def _page(content: bytes) -> pikepdf.Pdf:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.obj.Resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(
            F1=_font(pdf, "/Helvetica"),
            F2=_font(pdf, "/ABCDEF+CambriaMath"),
            F3=_font(pdf, "/Symbol"),
        )
    )
    page.obj.Contents = pdf.make_stream(GLYPHS + content)
    return pdf


def _found(pdf: pikepdf.Pdf) -> list:
    return find_formulas(_lines(pdf), font_names(pdf))


def test_a_displayed_formula_is_found_with_the_parts_of_its_fraction():
    pdf = _page(TEXT + FORMULA)
    (formula,) = _found(pdf)
    texts = sorted(_lines(pdf)[number].text for number in formula.lines)
    assert texts == ["= abc", "ab", "cd"]
    left, bottom, right, top = formula.box
    assert left <= 200 and right >= 240 and bottom <= 664 and top >= 676
    # The lines of text above and below are not covered.
    assert top <= 720 - 2 and bottom >= 620 + 7


def test_a_block_stops_short_of_a_line_of_text_just_above_it():
    # Too far above to join the formula, near enough for its block to reach.
    above = b"BT /F1 10 Tf 180 685.5 Td (abcd dcba abab) Tj ET "
    (formula,) = _found(_page(above + FORMULA))
    assert formula.box[3] == pytest.approx(683.5)


def test_a_line_of_text_with_a_symbol_in_a_word_is_no_formula():
    pdf = _page(b"BT /F1 10 Tf 72 700 Td (abcd dcba) Tj /F2 10 Tf (a) Tj /F1 10 Tf ( abab) Tj ET")
    assert _found(pdf) == []


def test_a_word_of_math_in_a_sentence_is_a_formula_of_its_own():
    sentence = b"BT /F1 10 Tf 72 700 Td (abcd dcba ) Tj /F3 10 Tf (ab) Tj /F1 10 Tf ( abab) Tj ET"
    pdf = _page(sentence)
    (formula,) = _found(pdf)
    assert formula.lines == ()
    assert [word.text for word in formula.words] == ["ab"]
    left, _, right, _ = formula.box
    # abcd and dcba are 20 points each, and the spaces 2.5.
    assert left == pytest.approx(72 + 45) and right == pytest.approx(72 + 55)


def test_an_arrow_or_a_sign_alone_in_a_sentence_is_no_formula():
    # Code 61 is = in the math font: an operator, not a letter of math.
    sentence = b"BT /F1 10 Tf 72 700 Td (abcd dcba ) Tj /F2 10 Tf (=) Tj /F1 10 Tf ( abab) Tj ET"
    assert _found(_page(sentence)) == []


def test_a_formula_in_a_form_is_left_to_the_fitter():
    pdf = _page(b"q 1 0 0 1 0 0 cm /Fm0 Do Q")
    form = pdf.make_stream(
        FORMULA,
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 612, 792],
        Resources=pikepdf.Dictionary(Font=pdf.pages[0].obj.Resources.Font),
    )
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Fm0=pdf.make_indirect(form))
    assert _found(pdf) == []


def test_a_formula_goes_and_a_block_with_the_pattern_covers_it():
    pdf = _page(TEXT + FORMULA)
    formulas = {0: _found(pdf)}
    source = trace_page(pdf.pages[0])
    assert remove_formulas(pdf, formulas) == 1
    after = group_lines(trace_page(pdf.pages[0]))
    assert [line.text for line in after] == [
        line.text for line in without_formulas(source, formulas[0])
    ]
    assert glyphs_left(after, formulas[0]) == 0
    draw_formulas(pdf, formulas)
    stream = b"".join(part.read_bytes() for part in pdf.pages[0].obj.Contents)
    assert stream.rstrip().endswith(b"re f Q")
    assert b"/Pattern cs " + PATTERN_KEY.encode() + b" scn" in stream
    tile = pdf.pages[0].obj.Resources.Pattern[PATTERN_KEY]
    assert (int(tile.PatternType), float(tile.XStep)) == (1, 12)
    # The block, light, then two plus signs of two bars each.
    assert tile.read_bytes().count(b" re ") == 1 + 4


def test_glyphs_left_in_a_block_are_counted():
    pdf = _page(TEXT + FORMULA)
    formulas = _found(pdf)
    # ab, cd, and = abc without its space.
    assert glyphs_left(_lines(pdf), formulas) == 8


def test_a_full_run_replaces_formulas_and_checks_none_is_left():
    result = _run(_page(TEXT + FORMULA))
    assert result.problems == ()
    mutation = next(m for m in result.mutations if m["operator"] == "replace-formulas")
    assert mutation["params"] == {"displayed": 1, "inline": 0, "pattern": "formula"}
    check = next(c for c in result.checks if c.name == "formulas")
    assert check.passed and check.details == {"formulas": 1, "glyphs_left": 0}
    assert "formulas" in PRIVACY_ORACLES
    output = pikepdf.open(io.BytesIO(result.data))
    texts = [line.text for line in group_lines(trace_page(output.pages[0]))]
    assert not any("=" in text for text in texts)


def test_keeping_formulas_keeps_their_text_and_gives_up_the_claim():
    profile = resolve_profile("privacy", keep_formulas=True)
    assert not profile.replace_formulas and profile.claims == () and not profile.reads_render
    assert resolve_profile("privacy").replace_formulas
    assert not resolve_profile("training").replace_formulas
    result = _run(_page(TEXT + FORMULA), _privacy(keep_formulas=True))
    assert not any(m["operator"] == "replace-formulas" for m in result.mutations)


def test_a_formula_left_on_the_page_stops_the_run(monkeypatch):
    monkeypatch.setattr(anonymize_module, "remove_formulas", lambda pdf, formulas: 0)
    result = _run(_page(TEXT + FORMULA))
    assert result.data is None
    assert any("glyphs left in a formula's block" in problem for problem in result.problems)
    check = next(c for c in result.checks if c.name == "formulas")
    assert not check.passed and check.details["glyphs_left"] == 8
