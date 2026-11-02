"""Graphics: a logo or an icon becomes a light block, and what is text or too large stays."""

from __future__ import annotations

import io

import pikepdf
import pytest
from test_anonymize import _privacy, _run, _source
from test_fit import _pdf
from test_outlines import _page, _pixel, _word

from docmutant import graphics as graphics_module
from docmutant.anonymize import replace_all_images, shows_light
from docmutant.coverage import curved_fills
from docmutant.difference import changed_regions
from docmutant.graphics import (
    LARGEST,
    graphic_quads,
    graphics,
    replace_graphics,
    solid_images,
    text_places,
)
from docmutant.outlines import outline_items
from docmutant.tracker import group_lines, trace_page

BADGE = b"0 13 m 0 20 6 26 13 26 c 20 26 26 20 26 13 c 26 6 20 0 13 0 c 6 0 0 6 0 13 c h"
SYMBOL = (
    b"8 8 m 8 10 9 12 10 12 c 11 12 12 10 12 8 c h "
    b"14 8 m 14 10 15 12 16 12 c 17 12 18 10 18 8 c h "
    b"11 14 m 11 16 12 18 13 18 c 14 18 15 16 15 14 c h"
)
"""Three curved subpaths, which ``outlines`` takes for a word: the icon would become letters."""


def _icon(x: float = 100, y: float = 400, *, badge: bytes = b"0 0 1 rg", symbol=b"1 1 1 rg"):
    return b"q 1 0 0 1 %g %g cm %s %s f %s %s f Q\n" % (x, y, badge, BADGE, symbol, SYMBOL)


def _text(pdf: pikepdf.Pdf):
    return text_places(pdf, {i: group_lines(trace_page(p)) for i, p in enumerate(pdf.pages)})


def _found(pdf: pikepdf.Pdf) -> dict[str, int]:
    return graphics(pdf, _text(pdf))


def _replace(pdf: pikepdf.Pdf) -> int:
    return replace_graphics(pdf, _text(pdf))


def test_an_icon_becomes_one_light_block_of_its_size_and_not_letters():
    pdf = _page(_icon())
    assert outline_items(pdf) == {"page 1": 1}
    assert _found(pdf) == {"page 1": 1}
    assert _replace(pdf) == 1
    assert _found(pdf) == {}
    assert outline_items(pdf) == {}
    assert curved_fills(pdf.pages[0]) == 0
    # A corner of the badge's box was white, and its middle blue: both are the block now.
    assert _pixel(pdf, 101, 401) == pytest.approx(230, abs=1)
    assert _pixel(pdf, 113, 413) == pytest.approx(230, abs=1)
    assert _pixel(pdf, 128, 413) == 255


def test_what_is_drawn_in_one_colour_stays():
    # An outlined word, or a logo of one colour.
    pdf = _page(_icon(badge=b"0 0 0 rg", symbol=b"0 0 0 rg") + _word(y=600))
    assert _found(pdf) == {}


def test_a_graphic_that_text_crosses_stays():
    text = b"BT /F1 12 Tf 110 410 Td (abcd) Tj ET "
    assert _found(_pdf(_icon(), glyphs=b"")) == {"page 1": 1}
    assert _found(_pdf(_icon() + text, glyphs=b"")) == {}


def test_a_rule_does_not_join_a_column_of_icons():
    link = b"0 0 1 RG 1 w 113 426 m 113 460 l S "
    pdf = _page(_icon(y=400) + _icon(y=460) + _icon(y=520) + link)
    assert _found(pdf) == {"page 1": 3}
    _replace(pdf)
    assert b"113 460 l\nS" in pdf.pages[0].Contents.read_bytes()


@pytest.mark.parametrize("scale, found", [(0.4, 0), (1, 1), (5, 1), (6, 0)])
def test_a_graphic_must_be_larger_than_a_check_box_and_smaller_than_a_banner(scale, found):
    # The icon is 26 points: 10.4 is a check box, 156 a banner.
    pdf = _page(b"q %g 0 0 %g 100 100 cm %s Q" % (scale, scale, _icon(0, 0)))
    assert sum(_found(pdf).values()) == found


def test_a_group_of_small_marks_wider_than_a_banner_stays():
    squares = b"0 0 1 rg 100 400 80 80 re f 1 0 0 rg 180 400 80 80 re f "
    assert _found(_page(squares)) == {}
    smaller = b"0 0 1 rg 100 400 60 60 re f 1 0 0 rg 160 400 60 60 re f "
    assert _found(_page(smaller)) == {"page 1": 1}


def test_a_card_under_an_icon_joins_nothing_and_stays():
    card = b"0.95 g 50 300 400 200 re f "
    pdf = _page(card + _icon())
    assert _found(pdf) == {"page 1": 1}
    _replace(pdf)
    assert b"400 200 re" in pdf.pages[0].Contents.read_bytes()


def test_a_word_beside_a_logo_stays_a_word_and_is_drawn_on_its_block():
    # The word is drawn after the logo, and its box reaches into it.
    pdf = _page(_icon() + _word(x=120, y=405))
    assert _found(pdf) == {"page 1": 1}
    _replace(pdf)
    assert outline_items(pdf) == {"page 1": 1}
    operators = pdf.pages[0].Contents.read_bytes()
    assert operators.index(b"0.9 g") < operators.index(b"0 0 0 rg")


def test_a_label_drawn_between_the_marks_of_a_button_stays_on_its_block():
    # A button: its face, its label, then an arrow beside the label.
    face = b"0.8 0.8 1 rg 100 400 m 100 420 l 136 420 l 136 400 l h f "
    arrow = b"1 0 0 rg 130 405 m 134 410 l 130 415 l 128 410 l h f "
    pdf = _page(face + _word(x=102, y=405, letters=2) + arrow)
    assert _found(pdf) == {"page 1": 1}
    _replace(pdf)
    assert outline_items(pdf) == {"page 1": 1}
    operators = pdf.pages[0].Contents.read_bytes()
    assert operators.index(b"0.9 g") < operators.index(b"0 0 0 rg")


def test_a_mark_in_a_corner_of_a_graphic_goes_with_it():
    # The ring touches nothing in the corner, but its block would cover the dot there.
    ring = b"0 0 1 rg 10 0 m 26 0 l 26 16 l 10 16 l h 14 4 m 22 4 l 22 12 l 14 12 l h f* "
    dot = b"1 0 0 rg 11 20 m 11 22 12 24 13 24 c 14 24 15 22 15 20 c h f "
    stem = b"0 1 0 rg 18 16 m 18 26 l 20 26 l 20 16 l h f "
    pdf = _page(b"q 1 0 0 1 100 400 cm " + ring + stem + dot + b"Q")
    assert _found(pdf) == {"page 1": 1}
    _replace(pdf)
    assert b"1 0 0 rg" not in pdf.pages[0].Contents.read_bytes()


def test_a_block_is_not_cut_by_the_clip_around_its_marks_and_shows_no_more_than_they_did():
    clipped = b"q 100 400 13 26 re W n " + _icon() + b"Q "
    pdf = _page(clipped)
    _replace(pdf)
    assert _pixel(pdf, 105, 413) == pytest.approx(230, abs=1)
    assert _pixel(pdf, 120, 413) == 255


def test_a_shading_counts_as_a_colour():
    pdf = _page(b"")
    pdf.pages[0].obj.Resources.Shading = pikepdf.Dictionary(
        Sh0=pikepdf.Dictionary(
            ShadingType=2,
            ColorSpace=pikepdf.Name.DeviceRGB,
            Coords=[0, 0, 26, 26],
            Function=pikepdf.Dictionary(
                FunctionType=2, Domain=[0, 1], C0=[1, 0, 0], C1=[0, 0, 1], N=1
            ),
        )
    )
    shaded = b"q 1 0 0 1 100 400 cm q " + BADGE + b" W n /Sh0 sh Q 1 1 1 rg " + SYMBOL + b" f Q"
    pdf.pages[0].Contents = pdf.make_stream(shaded)
    assert _found(pdf) == {"page 1": 1}
    _replace(pdf)
    assert b"sh" not in pdf.pages[0].Contents.read_bytes()


def _in_form(page: bytes) -> pikepdf.Pdf:
    pdf = _pdf(page, glyphs=b"")
    form = pdf.make_stream(
        _icon(0, 0),
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 26, 26],
    )
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Fm0=pdf.make_indirect(form))
    return pdf


def test_a_graphic_in_a_form_is_found_there_and_text_on_the_page_keeps_it():
    pdf = _in_form(b"q 1 0 0 1 300 300 cm /Fm0 Do Q ")
    form = pdf.pages[0].obj.Resources.XObject.Fm0
    assert _found(pdf) == {f"form {form.objgen[0]} {form.objgen[1]}": 1}
    crossed = _in_form(b"q 1 0 0 1 300 300 cm /Fm0 Do Q BT /F1 12 Tf 305 310 Td (abcd) Tj ET ")
    assert _found(crossed) == {}


def test_what_a_soft_mask_or_a_pattern_draws_stays():
    pdf = _page(b"q /GS0 gs 0 0 0 rg 100 400 26 26 re f Q ")
    group = pdf.make_stream(
        _icon(100, 400),
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 612, 792],
        Group=pikepdf.Dictionary(S=pikepdf.Name.Transparency, CS=pikepdf.Name.DeviceGray),
    )
    mask = pikepdf.Dictionary(S=pikepdf.Name.Luminosity, G=pdf.make_indirect(group))
    pdf.pages[0].obj.Resources.ExtGState = pikepdf.Dictionary(GS0=pikepdf.Dictionary(SMask=mask))
    pattern = pdf.make_stream(
        _icon(0, 0),
        PatternType=1,
        PaintType=1,
        TilingType=1,
        BBox=[0, 0, 26, 26],
        XStep=30,
        YStep=30,
    )
    pdf.make_indirect(pattern)
    pdf.pages[0].obj.Resources.Pattern = pikepdf.Dictionary(P0=pattern)
    assert _found(pdf) == {}


def _picture(pdf: pikepdf.Pdf, *, smask: bool = False) -> pikepdf.Object:
    image = pdf.make_stream(
        b"\x10" * 4,
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Image,
        Width=2,
        Height=2,
        ColorSpace=pikepdf.Name.DeviceGray,
        BitsPerComponent=8,
    )
    if smask:
        image.SMask = pdf.make_stream(
            b"\xff\x00\x00\xff",
            Type=pikepdf.Name.XObject,
            Subtype=pikepdf.Name.Image,
            Width=2,
            Height=2,
            ColorSpace=pikepdf.Name.DeviceGray,
            BitsPerComponent=8,
        )
    image = pdf.make_indirect(image)
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Im0=image)
    return image


def test_an_image_adds_no_colour_so_a_block_and_its_images_are_not_a_graphic():
    picture = b"q 10 0 0 10 108 408 cm /Im0 Do Q "
    shape = b"q 1 0 0 1 100 400 cm 0 0 1 rg " + BADGE + b" f Q "
    pdf = _page(shape + picture)
    _picture(pdf)
    assert _found(pdf) == {}
    pdf = _page(_icon() + picture)
    _picture(pdf)
    assert _replace(pdf) == 1
    assert _found(pdf) == {}
    assert b"/Im0 Do" in pdf.pages[0].Contents.read_bytes()


@pytest.mark.parametrize("crossed", [False, True])
def test_an_image_that_shows_through_becomes_a_block_unless_text_crosses_it(crossed):
    text = b"BT /F1 12 Tf 110 410 Td (abcd) Tj ET " if crossed else b""
    pdf = _pdf(b"q 26 0 0 26 100 400 cm /Im0 Do Q " + text, glyphs=b"")
    image = _picture(pdf, smask=True)
    solid = solid_images(pdf, _text(pdf))
    assert solid == (set() if crossed else {image.objgen})
    replace_all_images(pdf, solid)
    assert ("/SMask" in image) == crossed
    # A corner that the mask hid, at the left of the top row of samples.
    assert _pixel(pdf, 103, 422) == (255 if crossed else pytest.approx(230, abs=1))


def test_an_image_whose_palette_is_only_dark_stays_see_through():
    # Its mask is its shape: without it, it would be a black block.
    pdf = _pdf(b"q 26 0 0 26 100 400 cm /Im0 Do Q ", glyphs=b"")
    image = _picture(pdf, smask=True)
    assert shows_light(image)
    image.ColorSpace = pikepdf.Array(
        [pikepdf.Name.Indexed, pikepdf.Name.DeviceRGB, 0, pikepdf.String(b"\x00\x00\x00")]
    )
    assert not shows_light(image)
    image.ColorSpace = pikepdf.Array(
        [
            pikepdf.Name.Indexed,
            pikepdf.Name.DeviceRGB,
            1,
            pikepdf.String(b"\x00\x00\x00\xff\xff\xff"),
        ]
    )
    assert shows_light(image)


def test_the_render_comparison_sees_a_graphic_as_an_image_that_may_get_lighter():
    pdf = _page(_icon())
    lines = {0: []}
    assert len(changed_regions(pdf, lines, images=False, drawings=True)[0].images) == 1
    assert changed_regions(pdf, lines, images=False, drawings=False)[0].images == ()
    assert graphic_quads(pdf, _text(pdf))[0][1][0] == (100, 400)


L_SHAPE = b"q 1 0 0 1 300 300 cm 0 0 1 rg 0 0 140 3 re f 1 0 0 rg 0 3 3 137 re f Q "
"""Two bars at a right angle: a block of their extent covers a lot of white."""


def test_a_full_run_replaces_graphics_and_says_so():
    result = _run(_source(extra=_icon(400, 400)))
    assert result.problems == ()
    mutation = next(m for m in result.mutations if m["operator"] == "replace-graphics")
    assert mutation["params"] == {"graphics": 1}
    drawn = next(c for c in result.checks if c.name == "drawn-text")
    assert drawn.details["graphics_replaced"] == 1
    assert drawn.details["graphics_left"] == 0
    assert "1 graphics: flat blocks" in result.replaced
    assert next(c for c in result.checks if c.name == "render-difference").passed


def test_a_block_that_came_out_dark_stops_the_run(monkeypatch):
    assert LARGEST >= 140
    assert _run(_source(extra=L_SHAPE)).problems == ()
    monkeypatch.setattr(graphics_module, "SHADE", 0.0)
    result = _run(_source(extra=L_SHAPE))
    assert result.data is None
    assert any("got darker where an image was" in problem for problem in result.problems)


def test_a_run_that_keeps_drawings_keeps_graphics():
    result = _run(_source(extra=_icon(400, 400)), _privacy(keep_drawings=True))
    assert result.problems == ()
    assert not any(m["operator"] == "replace-graphics" for m in result.mutations)
    output = pikepdf.open(io.BytesIO(result.data))
    assert curved_fills(output.pages[0]) > 0
