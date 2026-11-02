"""What stopped sample files: narrow capitals, lines that join, formulas and slanted letters."""

from __future__ import annotations

import pikepdf
import pytest
from test_coverage import ALL, _fit_all
from test_fit import TO_UNICODE, _lines

from docmutant import fit
from docmutant.anonymize import resolve_profile
from docmutant.fit import CoverageStop, FitPolicy, WordFitter
from docmutant.geometry import EXACT, ROUNDING, BoxSettings, Violation, within
from docmutant.tracker import extent


def _page(content: bytes, widths: dict[int, float]) -> pikepdf.Pdf:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    first, last = min(widths), max(widths)
    font = pdf.make_indirect(
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
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.obj.Contents = pdf.make_stream(content)
    return pdf


# Capitals are wide and lowercase l is as narrow as I.
NARROW = {32: 250, 65: 600, 66: 600, 73: 250, 97: 500, 108: 250}
NARROW_GLYPHS = b"BT /F1 10 Tf 72 100 Td (A   B   I   a   l) Tj ET "


def test_narrow_capitals_take_any_letter_when_no_capital_is_as_narrow():
    pdf = _page(NARROW_GLYPHS + b"BT /F1 10 Tf 72 700 Td (III) Tj ET", NARROW)
    settings = BoxSettings("line", EXACT, tracking_limit=0.02)
    report = _fit_all(pdf, [], settings=settings)
    assert report.coverage.passed
    word = _lines(pdf)[1].words[0]
    assert word.text != "III"
    assert word.box.end == pytest.approx(72 + 7.5, abs=ROUNDING)


def test_lines_that_join_name_their_culprit_and_the_others_stay_changed(monkeypatch):
    content = b"".join(b"BT /F1 10 Tf 72 %d Td (aaba abab) Tj ET " % y for y in (700, 680, 660))
    pdf = _page(NARROW_GLYPHS + content, NARROW)
    original = fit.check_boxes

    def joined(lines, output, settings):
        # The second line changing makes the page count one line less.
        if output[2].text != lines[2].text:
            return fit.GeometryResult(
                settings, (Violation("line-count", 3.0, 4.0),), None, None, None, None, None, 0
            )
        return original(lines, output, settings)

    monkeypatch.setattr(fit, "check_boxes", joined)
    report = WordFitter(pdf, ["lala", "alla"]).fit_page(0, BoxSettings("line"), 0)
    assert report.lines_undone == 1
    texts = [line.text for line in _lines(pdf)[1:]]
    assert texts[1] == "aaba abab"
    assert texts[0] != "aaba abab" and texts[2] != "aaba abab"


def test_a_line_refused_once_does_not_grow_in_the_next_attempt(monkeypatch):
    # The only real word is wider than the source's, which the tolerance allows.
    pdf = _page(NARROW_GLYPHS + b"BT /F1 10 Tf 72 700 Td (ll) Tj ET", NARROW)
    original = fit.check_boxes
    calls = []

    def refuse_once(lines, output, settings):
        calls.append(output[1].box.end)
        if len(calls) == 1:
            return fit.GeometryResult(
                settings,
                (Violation("line-gap", -1.0, 0.0, 1),),
                None,
                None,
                None,
                None,
                None,
                0,
            )
        return original(lines, output, settings)

    monkeypatch.setattr(fit, "check_boxes", refuse_once)
    settings = BoxSettings("line", within(3), tracking_limit=0.02)
    report = WordFitter(pdf, ["al"]).fit_page(0, settings, 0, ALL)
    assert report.coverage.passed
    assert calls[0] == pytest.approx(79.5)
    assert _lines(pdf)[1].box.end <= 77 + ROUNDING


# Letters 5 wide drawn 3 apart, as a formula draws them: no letters fit one after another.
TIGHT = {32: 250, 97: 500, 98: 500, 99: 500, 100: 500}
TIGHT_GLYPHS = b"BT /F1 10 Tf 72 100 Td (a   b   c   d) Tj ET "


def test_letters_drawn_closer_than_their_widths_keep_their_starts():
    # One space: drawn one after another, no letters end before the next word.
    shown = b"BT /F1 10 Tf 72 700 Td [(a) 200 (b) 200 (c dd)] TJ ET"
    pdf = _page(TIGHT_GLYPHS + shown, TIGHT)
    before, following = _lines(pdf)[1].words
    report = _fit_all(pdf, [], settings=BoxSettings("line", EXACT, tracking_limit=0.02))
    assert report.coverage.passed
    after, moved = _lines(pdf)[1].words
    # The word after it, in the same operator, starts where it did.
    assert moved.box.start == pytest.approx(following.box.start, abs=ROUNDING)
    assert after.text != "abc"
    assert [g.origin[0] for g in after.glyphs] == pytest.approx(
        [g.origin[0] for g in before.glyphs]
    )
    assert after.box.end == pytest.approx(before.box.end, abs=ROUNDING)


SLANT = b"BT /F1 10 Tf 1 0 0.6 1 72 700 Tm (abab dc) Tj ET "

# A capital I with no narrower letter: every other one is wider.
WIDE = {32: 250, 65: 600, 66: 600, 73: 250, 97: 500, 98: 500}
WIDE_GLYPHS = b"BT /F1 10 Tf 72 100 Td (A   B   I   a   b) Tj ET "


@pytest.mark.parametrize("policy", [ALL, FitPolicy("all", "moved")])
def test_slanted_letters_fit_by_their_widths_and_keep_their_gaps(policy):
    pdf = _page(TIGHT_GLYPHS + SLANT, TIGHT)
    lines = _lines(pdf)
    gap = lines[1].words[1].box.start - lines[1].words[0].box.end
    glyph = lines[1].words[0].glyphs[0]
    # The box leans past the width, so the extent is not the width.
    start, end, _, _ = extent(glyph, glyph.direction)
    assert end - start > 5 + 1
    settings = BoxSettings("line", within(1), tracking_limit=0.02)
    report = _fit_all(pdf, [], policy=policy, settings=settings)
    assert report.geometry.passed and report.coverage.passed
    first, second = _lines(pdf)[1].words
    # The boxes of slanted words overlap, and must not overlap more.
    assert gap < 0
    assert second.box.start - first.box.end >= gap - ROUNDING


def test_a_slanted_letter_that_reaches_past_the_last_one_leaves_its_word_not_its_line():
    # A slanted I and a small upright one after it, then a word close to the slanted one's box.
    content = (
        b"BT /F1 10 Tf 1 0 0.6 1 72 700 Tm (I) Tj ET "
        b"BT /F1 5 Tf 74.5 698 Td (I) Tj ET "
        b"BT /F1 10 Tf 81 700 Td (aaa aaa) Tj ET "
    )
    pdf = _page(WIDE_GLYPHS + content, WIDE)
    assert [word.text for word in _lines(pdf)[1].words] == ["II", "aaa", "aaa"]
    with pytest.raises(CoverageStop) as stop:
        _fit_all(pdf, [], settings=BoxSettings("line", EXACT, tracking_limit=0.02))
    # Only the word that cannot fit stays: the rest of its line changes.
    assert dict(stop.value.report.words_left) == {"no fit": 1}


def test_privacy_holds_each_edge_of_a_line_within_a_point_unless_told_otherwise():
    assert resolve_profile("privacy").settings.tolerance == within(1)
    assert resolve_profile("privacy", tolerance=EXACT).settings.tolerance == EXACT
    assert resolve_profile("training").settings.tolerance == EXACT


# Digits only, and a hyphen: each digit has one other to stand for it.
DIGITS = {32: 250, 45: 333, 48: 500, 50: 500}
DIGIT_GLYPHS = b"BT /F1 10 Tf 72 100 Td (0   2   -) Tj ET "


@pytest.mark.parametrize("avoid", [(), ("022",)])
def test_each_run_of_a_swapped_word_is_checked_against_the_source(avoid):
    pdf = _page(DIGIT_GLYPHS + b"BT /F1 10 Tf 72 700 Td (200-020) Tj ET", DIGITS)
    settings = BoxSettings("line", EXACT, tracking_limit=0.02)
    fitter = WordFitter(pdf, [], None, avoid)
    if not avoid:
        assert fitter.fit_page(0, settings, 0, ALL).coverage.passed
        assert _lines(pdf)[1].text == "022-202"
        return
    # 022-202 is the only swap, and 022 is a source word on its own.
    with pytest.raises(CoverageStop):
        fitter.fit_page(0, settings, 0, ALL)


def test_a_new_word_is_not_mostly_part_of_a_source_word_on_its_line():
    # The only real word is most of the source word after it.
    pdf = _page(TIGHT_GLYPHS + b"BT /F1 10 Tf 72 700 Td (cdab abcda) Tj ET", TIGHT)
    report = WordFitter(pdf, ["bcda"]).fit_page(
        0, BoxSettings("line", within(5), tracking_limit=0.02), 0, ALL
    )
    assert report.coverage.passed
    assert "bcda" not in [word.text for word in _lines(pdf)[1].words]
