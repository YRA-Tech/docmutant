"""Text-position tracker: glyph addresses and boxes, checked by hand and against pdfium."""

from __future__ import annotations

import ctypes
import io

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest

from docmutant.tracker import DEFAULT_ASCENT, DEFAULT_DESCENT, UnsupportedText, trace_page

HELVETICA_WIDTHS = {ord("H"): 722, ord("e"): 556, ord("l"): 222, ord("o"): 556, 32: 278}


def _font(pdf: pikepdf.Pdf, widths: dict[int, float], **descriptor) -> pikepdf.Object:
    first, last = min(widths), max(widths)
    font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica,
        FirstChar=first,
        LastChar=last,
        Widths=[widths.get(code, 0) for code in range(first, last + 1)],
    )
    if descriptor:
        font.FontDescriptor = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name.Helvetica, **descriptor
            )
        )
    return pdf.make_indirect(font)


def _pdf(content: bytes, font: dict | None = None, **resources) -> pikepdf.Pdf:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.obj.Resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(F1=_font(pdf, font or {65: 500, 66: 500, 32: 250})),
        **resources,
    )
    page.obj.Contents = pdf.make_stream(content)
    return pdf


def _close(actual, expected, tolerance=1e-6) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=True))


def test_tj_origins_and_box():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (AB) Tj ET")
    first, second = trace_page(pdf.pages[0])
    assert (first.instruction, first.operand, first.offset, first.code) == (3, 0, 0, 65)
    assert (second.offset, second.code) == (1, 66)
    assert _close(first.origin, (72, 700))
    assert _close(second.origin, (77, 700))
    bottom, top = 700 + DEFAULT_DESCENT / 100, 700 + DEFAULT_ASCENT / 100
    expected = ((72, bottom), (77, bottom), (77, top), (72, top))
    assert all(_close(c, e) for c, e in zip(first.box, expected, strict=True))
    assert (first.width_from, first.height_from) == ("widths", "default")


def test_spacing_scaling_and_tj_adjustment():
    # A advances (5 + Tc 1) * Tz 0.5 = 3; the space adds Tw 2: (2.5 + 1 + 2) * 0.5 = 2.75;
    # -1000 in TJ moves 1000 / 1000 * 10 * 0.5 = 5. The escaped paren is one byte.
    content = rb"BT /F1 10 Tf 1 Tc 2 Tw 50 Tz [(A ) -1000 (\)B)] TJ ET"
    pdf = _pdf(content, {65: 500, 66: 500, 41: 500, 32: 250})
    glyphs = trace_page(pdf.pages[0])
    assert [(g.operand, g.offset, g.code) for g in glyphs] == [
        (0, 0, 65),
        (0, 1, 32),
        (2, 0, 41),
        (2, 1, 66),
    ]
    assert [round(g.origin[0], 6) for g in glyphs] == [0, 3, 10.75, 13.75]


def test_line_operators():
    content = (
        b"BT /F1 10 Tf 0 100 Td 14 TL (A) Tj T* (A) Tj (A) ' 5 1 (A) \" "
        b"0 -20 TD (A) Tj T* (A) Tj ET"
    )
    pdf = _pdf(content)
    glyphs = trace_page(pdf.pages[0])
    assert [round(g.origin[1], 6) for g in glyphs] == [100, 86, 72, 58, 38, 18]
    assert glyphs[3].operand == 2


def test_state_is_saved_and_text_objects_reset():
    content = (
        b"q 1 0 0 1 100 0 cm BT /F1 10 Tf 3 Tr 2 Ts (A) Tj ET Q "
        b"BT /F1 20 Tf (A) Tj ET "
        b"BT 1 0 0 1 50 60 Tm (A) Tj ET"
    )
    pdf = _pdf(content)
    hidden, plain, moved = trace_page(pdf.pages[0])
    assert _close(hidden.origin, (100, 2))
    assert hidden.render_mode == 3
    # Q restored the CTM and the rise but not Tf; each BT starts at the origin.
    assert _close(plain.origin, (0, 0))
    assert (plain.render_mode, plain.size) == (0, 20)
    assert _close(moved.origin, (50, 60))


def test_rotated_scaled_text_matches_pdfium():
    content = (
        b"q 0.992546 0.121869 -0.121869 0.992546 30 40 cm "
        b"BT /F1 12 Tf 1.5 0 0.2 1 100 500 Tm 0.5 Tc 80 Tz [(He) 120 (llo)] TJ ET Q"
    )
    pdf = _pdf(content, HELVETICA_WIDTHS)
    glyphs = trace_page(pdf.pages[0])

    buffer = io.BytesIO()
    pdf.save(buffer)
    document = pdfium.PdfDocument(buffer.getvalue())
    textpage = document[0].get_textpage()
    assert textpage.count_chars() == len(glyphs) == 5
    for index, glyph in enumerate(glyphs):
        x, y = ctypes.c_double(), ctypes.c_double()
        pdfium_c.FPDFText_GetCharOrigin(textpage.raw, index, ctypes.byref(x), ctypes.byref(y))
        assert _close(glyph.origin, (x.value, y.value), tolerance=0.01)


def test_marked_content_ids():
    content = (
        b"/P <</MCID 4>> BDC BT /F1 10 Tf (A) Tj /Span BMC (A) Tj EMC ET EMC "
        b"/Artifact BMC BT /F1 10 Tf (A) Tj ET EMC "
        b"/P /MC0 BDC BT /F1 10 Tf (A) Tj ET EMC"
    )
    properties = pikepdf.Dictionary(MC0=pikepdf.Dictionary(MCID=7))
    pdf = _pdf(content, Properties=properties)
    glyphs = trace_page(pdf.pages[0])
    assert [g.mcid for g in glyphs] == [4, 4, None, 7]


@pytest.mark.parametrize(
    ("descriptor", "height_from", "extent"),
    [
        ({"Ascent": 718, "Descent": -207}, "ascent", (-207, 718)),
        ({"FontBBox": [-166, -225, 1000, 931]}, "font-bbox", (-225, 931)),
        ({"Ascent": 0, "Descent": 0}, "default", (DEFAULT_DESCENT, DEFAULT_ASCENT)),
    ],
)
def test_vertical_extent_source(descriptor, height_from, extent):
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    page.obj.Resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(F1=_font(pdf, {65: 500}, **descriptor))
    )
    page.obj.Contents = pdf.make_stream(b"BT /F1 100 Tf (A) Tj ET")
    (glyph,) = trace_page(pdf.pages[0])
    assert glyph.height_from == height_from
    assert _close((glyph.box[0][1], glyph.box[2][1]), (extent[0] / 10, extent[1] / 10))


def test_width_outside_the_table():
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    page.obj.Resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(
            F1=_font(pdf, {65: 500}, MissingWidth=300), F2=_font(pdf, {65: 500})
        )
    )
    page.obj.Contents = pdf.make_stream(b"BT /F1 10 Tf (BA) Tj /F2 10 Tf (BA) Tj ET")
    glyphs = trace_page(pdf.pages[0])
    assert [(g.width, g.width_from) for g in glyphs] == [
        (300, "missing-width"),
        (500, "widths"),
        (0, "none"),
        (500, "widths"),
    ]
    assert _close(glyphs[1].origin, (3, 0))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"BT (A) Tj ET", "before Tf"),
        (b"BT /F9 10 Tf (A) Tj ET", "not in the resources"),
        (b"BT /F2 10 Tf (A) Tj ET", "/Type0"),
        (b"BT /F3 10 Tf (A) Tj ET", "no /Widths"),
    ],
)
def test_text_that_cannot_be_placed(content, message):
    pdf = _pdf(content)
    fonts = pdf.pages[0].obj.Resources.Font
    fonts.F2 = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type0)
    fonts.F3 = pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
    )
    with pytest.raises(UnsupportedText, match=message):
        trace_page(pdf.pages[0])
