"""The oracle that compares pictures: it must see damage, and not what a run means to change."""

from __future__ import annotations

import pikepdf
import pytest
from test_anonymize import _bytes, _image, _run, _source
from test_fit import _pdf

from docmutant import anonymize as anonymize_module
from docmutant.anonymize import replace_all_images
from docmutant.difference import (
    DARKENED,
    MARGIN,
    REACH_DOWN,
    STRAY,
    PageRegions,
    changed_regions,
    compare,
    line_quad,
    problems,
)
from docmutant.manifest import PRIVACY_ORACLES
from docmutant.tracker import group_lines, trace_page

WORDS = b"BT /F1 12 Tf 72 700 Td (abcd abcd) Tj ET "
OTHER = b"BT /F1 12 Tf 72 700 Td (dcba dcba) Tj ET "
RULE = b"0 0 0 rg 72 300 200 2 re f "


def _regions(pdf: pikepdf.Pdf, *, images: bool = True, drawings: bool = True) -> list[PageRegions]:
    lines = {i: group_lines(trace_page(page)) for i, page in enumerate(pdf.pages)}
    return changed_regions(pdf, lines, images=images, drawings=drawings)


def _difference(source: pikepdf.Pdf, output: pikepdf.Pdf, **settings):
    return compare(_bytes(source), _bytes(output), _regions(source, **settings))[0]


def _with_image(content: bytes, sample: int, **keys) -> pikepdf.Pdf:
    pdf = _pdf(content, glyphs=b"")
    image = pdf.make_indirect(
        pdf.make_stream(
            bytes([sample]) * 4,
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Width=2,
            Height=2,
            ColorSpace=pikepdf.Name.DeviceGray,
            BitsPerComponent=8,
            **keys,
        )
    )
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Im0=image)
    return pdf


PICTURE = b"q 300 0 0 300 100 200 cm /Im0 Do Q "


def test_new_words_in_the_same_place_are_no_difference():
    page = _difference(_pdf(WORDS + RULE, glyphs=b""), _pdf(OTHER + RULE, glyphs=b""))
    assert page.changed == 0
    assert page.mean_abs_error == 0
    assert 0.9 < page.compared < 1
    assert problems([page]) == []


def test_a_rule_that_goes_is_seen():
    page = _difference(_pdf(WORDS + RULE, glyphs=b""), _pdf(WORDS, glyphs=b""))
    assert page.changed > STRAY
    assert page.changed_pixels_ratio > 0
    assert page.mean_abs_error > 0
    assert "pixels changed" in problems([page])[0]


def test_text_that_strays_out_of_its_place_is_seen():
    strayed = OTHER + b"BT /F1 12 Tf 300 700 Td (abcd) Tj ET "
    page = _difference(_pdf(WORDS, glyphs=b""), _pdf(strayed, glyphs=b""))
    assert page.changed > STRAY


def test_a_line_that_no_longer_shows_is_lost():
    page = _difference(_pdf(WORDS + RULE, glyphs=b""), _pdf(RULE, glyphs=b""))
    assert page.changed == 0
    assert page.lines_lost == 1
    assert "1 lines of text" in problems([page])[0]


def test_text_that_never_showed_is_not_lost():
    hidden = b"BT 3 Tr /F1 12 Tf 72 700 Td (abcd abcd) Tj ET "
    page = _difference(_pdf(hidden, glyphs=b""), _pdf(b"", glyphs=b""))
    assert page.lines_lost == 0


def test_a_line_over_an_image_can_fade_because_a_flat_image_is_light():
    white = b"1 1 1 rg BT /F1 12 Tf 150 300 Td (abcd abcd) Tj ET "
    source = _with_image(PICTURE + white, 0x00)
    output = _with_image(PICTURE + white, 0xE6)
    assert _difference(source, output).lines_lost == 0


def test_an_image_can_get_lighter_but_not_black():
    light = _difference(_with_image(PICTURE, 0x10), _with_image(PICTURE, 0xE6))
    assert (light.changed, light.darkened) == (0, 0)
    black = _difference(_with_image(PICTURE, 0xFF), _with_image(PICTURE, 0x00))
    assert black.changed == 0
    assert black.darkened > DARKENED
    assert "got darker where an image was" in problems([black])[0]


def test_a_flat_image_on_a_white_page_does_not_count_as_darker():
    page = _difference(_with_image(PICTURE, 0xFF), _with_image(PICTURE, 0xE6))
    assert page.darkened == 0


def test_a_run_that_keeps_its_images_must_not_change_them():
    source, output = _with_image(PICTURE, 0x10), _with_image(PICTURE, 0xE6)
    assert _difference(source, output, images=False).changed > STRAY


def test_an_image_in_a_form_is_found_through_the_form_and_its_matrix():
    def build(sample: int) -> pikepdf.Pdf:
        pdf = _with_image(b"q 2 0 0 2 0 0 cm /Fm0 Do Q ", sample)
        image = pdf.pages[0].obj.Resources.XObject.Im0
        form = pdf.make_stream(
            b"q 100 0 0 100 0 0 cm /Im0 Do Q",
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Form,
            BBox=[0, 0, 200, 200],
            Matrix=[1, 0, 0, 1, 50, 50],
            Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=image)),
        )
        pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Fm0=pdf.make_indirect(form))
        return pdf

    assert _difference(build(0xFF), build(0x10)).changed == 0


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_a_place_is_found_on_a_page_that_is_rotated_or_cropped(rotation):
    def build(content: bytes) -> pikepdf.Pdf:
        pdf = _pdf(content, glyphs=b"")
        pdf.pages[0].obj.Rotate = rotation
        pdf.pages[0].obj.CropBox = [40, 200, 500, 760]
        return pdf

    same = _difference(build(WORDS + RULE), build(OTHER + RULE))
    assert same.changed == 0
    assert _difference(build(WORDS + RULE), build(OTHER)).changed > STRAY


def test_a_turned_line_keeps_its_place_and_covers_little_of_the_page():
    words = b"BT /F1 12 Tf 0.7071 0.7071 -0.7071 0.7071 100 100 Tm (abcdabcdabcdabcdabcd) Tj ET "
    other = b"BT /F1 12 Tf 0.7071 0.7071 -0.7071 0.7071 100 100 Tm (dcbadcbadcbadcbadcba) Tj ET "
    page = _difference(_pdf(words, glyphs=b""), _pdf(other, glyphs=b""))
    assert page.changed == 0
    assert page.compared > 0.99


def test_a_font_that_says_its_descent_is_above_the_baseline_does_not_shrink_a_place():
    pdf = _pdf(WORDS, glyphs=b"")
    line = group_lines(trace_page(pdf.pages[0]))[0]
    low = min(y for _, y in line_quad(line))
    assert low <= 700 - REACH_DOWN * line.words[0].glyphs[0].em
    font = pdf.pages[0].obj.Resources.Font.F1
    font.FontDescriptor = pikepdf.Dictionary(Ascent=900, Descent=200, Flags=32)
    wrong = group_lines(trace_page(pdf.pages[0]))[0]
    assert wrong.box.bottom > 700
    assert min(y for _, y in line_quad(wrong)) == low


def test_an_annotation_that_shows_something_is_left_out_of_the_comparison():
    source = _pdf(RULE, glyphs=b"")
    appearance = source.make_stream(
        b"0 0 0 rg 0 0 100 50 re f",
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 100, 50],
    )
    source.pages[0].obj.Annots = source.make_indirect(
        pikepdf.Array(
            [
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Annot,
                    Subtype=pikepdf.Name.Stamp,
                    Rect=[100, 500, 200, 550],
                    AP=pikepdf.Dictionary(N=source.make_indirect(appearance)),
                )
            ]
        )
    )
    assert _difference(source, _pdf(RULE, glyphs=b"")).changed == 0


def _soft_masked(sample: int) -> pikepdf.Pdf:
    """A black fill that shows only through a soft mask, which is a dark image here."""
    pdf = _pdf(b"q /GS0 gs 0 0 0 rg 100 200 300 300 re f Q ", glyphs=b"")
    shape = pdf.make_indirect(
        pdf.make_stream(
            bytes([sample]) * 4,
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Width=2,
            Height=2,
            ColorSpace=pikepdf.Name.DeviceGray,
            BitsPerComponent=8,
        )
    )
    group = pdf.make_stream(
        b"q 300 0 0 300 100 200 cm /Im0 Do Q",
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 612, 792],
        Group=pikepdf.Dictionary(S=pikepdf.Name.Transparency, CS=pikepdf.Name.DeviceGray),
        Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=shape)),
    )
    mask = pikepdf.Dictionary(S=pikepdf.Name.Luminosity, G=pdf.make_indirect(group))
    state = pikepdf.Dictionary(SMask=mask)
    pdf.pages[0].obj.Resources.ExtGState = pikepdf.Dictionary(GS0=state)
    return pdf


def test_an_image_in_a_soft_mask_goes_empty_and_does_not_show_what_it_masked():
    source = _soft_masked(0x20)
    output = _soft_masked(0x20)
    assert replace_all_images(output) == 1
    page = _difference(source, output)
    assert page.changed == 0
    assert page.darkened == 0


def test_a_light_soft_mask_would_be_seen_as_a_dark_block():
    assert _difference(_soft_masked(0x20), _soft_masked(0xE6)).darkened > DARKENED


def test_an_ordinary_image_stays_light_next_to_one_in_a_soft_mask():
    pdf = _soft_masked(0x20)
    picture = _image(pdf, ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8)
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Im1=picture)
    assert replace_all_images(pdf) == 2
    assert picture.read_bytes() == b"\xe6" * 4


def test_a_full_run_reports_the_comparison_of_each_page():
    result = _run(_source())
    assert result.problems == ()
    check = next(c for c in result.checks if c.name == "render-difference")
    assert check.passed
    assert check.target.startswith("pypdfium2")
    assert set(check.details["pages"][0]) == {
        "compared",
        "changed_pixels_ratio",
        "mean_abs_error",
        "darkened",
        "lines_lost",
    }
    assert "render-difference" in PRIVACY_ORACLES


def test_a_run_that_turns_an_image_black_writes_nothing(monkeypatch):
    pdf = _source()
    image = _image(pdf, ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8)
    image.write(b"\xff" * 4)
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Im0=image)
    pdf.pages[0].obj.Contents = pdf.make_stream(
        pdf.pages[0].obj.Contents.read_bytes() + b" q 300 0 0 300 100 200 cm /Im0 Do Q"
    )
    assert _run(pdf).problems == ()
    monkeypatch.setattr(anonymize_module, "_fill", lambda _space, _depth: 0)
    result = _run(pdf)
    assert result.data is None
    assert any("got darker where an image was" in problem for problem in result.problems)


def test_a_margin_grows_with_the_tolerance_of_a_line():
    pdf = _pdf(WORDS, glyphs=b"")
    lines = {0: group_lines(trace_page(pdf.pages[0]))}
    regions = changed_regions(pdf, lines, images=True, drawings=True, tolerance=3.0)
    assert regions[0].margin == MARGIN + 3.0
