"""Text-position tracker: glyph addresses and boxes, checked by hand and against pdfium."""

from __future__ import annotations

import ctypes
import io

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest

from docmutant.mutate import read_stream
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


def _pdfium_origins(pdf: pikepdf.Pdf) -> list[tuple[float, float]]:
    """Origins of the characters pdfium reads from page 1, without the ones it generates."""
    buffer = io.BytesIO()
    pdf.save(buffer)
    textpage = pdfium.PdfDocument(buffer.getvalue())[0].get_textpage()
    origins = []
    for index in range(textpage.count_chars()):
        if pdfium_c.FPDFText_IsGenerated(textpage.raw, index) == 1:
            continue
        x, y = ctypes.c_double(), ctypes.c_double()
        pdfium_c.FPDFText_GetCharOrigin(textpage.raw, index, ctypes.byref(x), ctypes.byref(y))
        origins.append((x.value, y.value))
    return origins


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
    origins = _pdfium_origins(pdf)
    assert len(origins) == len(glyphs) == 5
    for glyph, origin in zip(glyphs, origins, strict=True):
        assert _close(glyph.origin, origin, tolerance=0.01)


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
        (b"BT /F2 10 Tf (A) Tj ET", "/CIDFontType2"),
        (b"BT /F3 10 Tf (A) Tj ET", "no /Widths"),
    ],
)
def test_text_that_cannot_be_placed(content, message):
    pdf = _pdf(content)
    fonts = pdf.pages[0].obj.Resources.Font
    fonts.F2 = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.CIDFontType2)
    fonts.F3 = pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
    )
    with pytest.raises(UnsupportedText, match=message):
        trace_page(pdf.pages[0])


def _type0_pdf(
    content: bytes, encoding, w: list | None = None, dw: int | None = None
) -> pikepdf.Pdf:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    descendant = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.CIDFontType2,
        BaseFont=pikepdf.Name.Arial,
        CIDSystemInfo=pikepdf.Dictionary(
            Registry=pikepdf.String("Adobe"), Ordering=pikepdf.String("Identity"), Supplement=0
        ),
        FontDescriptor=pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.FontDescriptor,
                FontName=pikepdf.Name.Arial,
                Ascent=905,
                Descent=-212,
            )
        ),
    )
    if w is not None:
        descendant.W = w
    if dw is not None:
        descendant.DW = dw
    if isinstance(encoding, bytes):
        encoding = pdf.make_stream(encoding)
    font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.Type0,
        BaseFont=pikepdf.Name.Arial,
        Encoding=encoding,
        DescendantFonts=[pdf.make_indirect(descendant)],
    )
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=pdf.make_indirect(font)))
    page.obj.Contents = pdf.make_stream(content)
    return pdf


def test_identity_h_codes_widths_and_word_spacing():
    # The 2-byte code 0x0020 is not a space for Tw: only the 1-byte code 32 is.
    content = b"BT /F1 10 Tf 5 Tw <0001 0020 0003> Tj ET"
    pdf = _type0_pdf(content, pikepdf.Name("/Identity-H"), w=[1, [600], 32, [250]], dw=500)
    glyphs = trace_page(pdf.pages[0])
    assert [(g.offset, g.length, g.code, g.cid) for g in glyphs] == [
        (0, 2, 1, 1),
        (2, 2, 32, 32),
        (4, 2, 3, 3),
    ]
    assert [(g.width, g.width_from) for g in glyphs] == [
        (600, "widths"),
        (250, "widths"),
        (500, "default-width"),
    ]
    assert [round(g.origin[0], 6) for g in glyphs] == [0, 6, 8.5]
    assert glyphs[0].height_from == "ascent"


def test_default_width_is_1000_without_dw():
    pdf = _type0_pdf(b"BT /F1 10 Tf <00050006> Tj ET", pikepdf.Name("/Identity-H"))
    glyphs = trace_page(pdf.pages[0])
    assert [(g.width, round(g.origin[0], 6)) for g in glyphs] == [(1000, 0), (1000, 10)]


MIXED_CMAP = b"""%!PS-Adobe-3.0 Resource-CMap
/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (Test) /Supplement 0 >> def
/CMapName /Test-Mixed def
/CMapType 1 def
2 begincodespacerange
<00> <80>
<8140> <9FFC>
endcodespacerange
1 begincidrange
<20> <7e> 1
endcidrange
1 begincidchar
<8140> 633
endcidchar
1 beginnotdefrange
<8141> <81ff> 2
endnotdefrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


def test_embedded_cmap_with_one_and_two_byte_codes():
    # A (1 byte), 0x8140 (2 bytes, cidchar), space (1 byte, gets Tw),
    # 0x8141 (2 bytes, notdef range), 0xA0 (no codespace: 1 byte, CID 0).
    content = b"BT /F1 10 Tf 3 Tw <41 8140 20 8141 A0> Tj ET"
    pdf = _type0_pdf(content, MIXED_CMAP, w=[34, [500], 633, [1000], 1, [250]], dw=700)
    glyphs = trace_page(pdf.pages[0])
    assert [(g.offset, g.length, g.code, g.cid) for g in glyphs] == [
        (0, 1, 0x41, 34),
        (1, 2, 0x8140, 633),
        (3, 1, 0x20, 1),
        (4, 2, 0x8141, 2),
        (6, 1, 0xA0, 0),
    ]
    assert [round(g.origin[0], 6) for g in glyphs] == [0, 5, 15, 20.5, 27.5]


def test_embedded_cmap_based_on_identity_h():
    cmap = b"/Identity-H usecmap 1 begincidchar <0041> 7 endcidchar"
    pdf = _type0_pdf(b"BT /F1 10 Tf <00410042> Tj ET", cmap, w=[7, [300]], dw=600)
    glyphs = trace_page(pdf.pages[0])
    assert [(g.cid, g.width) for g in glyphs] == [(7, 300), (0x42, 600)]


@pytest.mark.parametrize(
    ("encoding", "message"),
    [
        (pikepdf.Name("/Identity-V"), "vertical writing"),
        (b"/WMode 1 def 1 begincodespacerange <00> <ff> endcodespacerange", "vertical"),
        (pikepdf.Name("/UniJIS-UCS2-H"), "not built in"),
        (b"/UniGB-UCS2-H usecmap", "not built in"),
        (b"1 begincidrange <00> <ff> 0 endcidrange", "without codespace"),
    ],
)
def test_composite_fonts_that_cannot_be_placed(encoding, message):
    pdf = _type0_pdf(b"BT /F1 10 Tf <0001> Tj ET", encoding)
    with pytest.raises(UnsupportedText, match=message):
        trace_page(pdf.pages[0])


def test_identity_h_matches_pdfium():
    content = (
        b"q 0.9 0.2 -0.2 0.9 50 60 cm BT /F1 14 Tf 1 Tc 120 Tz 100 400 Td "
        b"[<002B0048> -250 <004F004F0052>] TJ ET Q"
    )
    pdf = _type0_pdf(content, pikepdf.Name("/Identity-H"), w=[43, [722], 72, [556], 79, [222, 222]])
    glyphs = trace_page(pdf.pages[0])
    origins = _pdfium_origins(pdf)
    assert len(origins) == len(glyphs) == 5
    for glyph, origin in zip(glyphs, origins, strict=True):
        assert _close(glyph.origin, origin, tolerance=0.01)


def _form(pdf: pikepdf.Pdf, content: bytes, **keys) -> pikepdf.Stream:
    return pdf.make_stream(
        content, Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form, BBox=[0, 0, 500, 500], **keys
    )


def test_form_glyphs_are_placed_and_addressed_in_the_form():
    pdf = _pdf(b"q 1 0 0 1 100 200 cm /Fm1 Do Q /Fm1 Do BT /F1 10 Tf (B) Tj ET")
    resources = pdf.pages[0].obj.Resources
    resources.XObject = pikepdf.Dictionary(
        Fm1=_form(pdf, b"BT /F1 10 Tf 5 7 Td (A) Tj ET", Matrix=[2, 0, 0, 2, 10, 0])
    )
    first, second, page_glyph = trace_page(pdf.pages[0])
    # Form space (5, 7) through /Matrix, then the CTM at each Do.
    assert _close(first.origin, (120, 214))
    assert _close(second.origin, (20, 14))
    assert first.size == 10 and _close(first.box[1], (120 + 10, 214 - 5))
    assert first.stream == second.stream
    assert first.stream.kind == "form" and first.stream.objgen == resources.XObject.Fm1.objgen
    assert page_glyph.stream.kind == "page" and _close(page_glyph.origin, (0, 0))
    instruction = read_stream(pdf, first.stream)[first.instruction]
    assert str(instruction.operator) == "Tj"


def test_form_inherits_the_font_not_the_name():
    # The page selects its /F1 (width 500); the form's own /F1 has width 900 but no Tf.
    pdf = _pdf(b"/F1 10 Tf /Fm1 Do")
    form_fonts = pikepdf.Dictionary(F1=_font(pdf, {65: 900}))
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(
        Fm1=_form(pdf, b"BT (AA) Tj ET", Resources=pikepdf.Dictionary(Font=form_fonts))
    )
    glyphs = trace_page(pdf.pages[0])
    assert [(g.width, round(g.origin[0], 6)) for g in glyphs] == [(500, 0), (500, 5)]


def test_form_without_resources_uses_the_page_resources():
    pdf = _pdf(b"/Fm1 Do")
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(
        Fm1=_form(pdf, b"BT /F1 10 Tf (A) Tj ET")
    )
    (glyph,) = trace_page(pdf.pages[0])
    assert glyph.width == 500


def test_nested_forms_state_and_marked_content():
    pdf = _pdf(b"/P <</MCID 5>> BDC /Outer Do EMC BT /F1 10 Tf (A) Tj ET")
    inner = _form(pdf, b"/Span <</MCID 9>> BDC BT /F1 10 Tf (A) Tj ET")  # BDC left open
    outer = _form(
        pdf,
        b"BT /F1 10 Tf 3 Tr (A) Tj ET 1 0 0 1 0 50 cm /Inner Do",
        Resources=pikepdf.Dictionary(
            Font=pdf.pages[0].obj.Resources.Font, XObject=pikepdf.Dictionary(Inner=inner)
        ),
        Matrix=[1, 0, 0, 1, 0, 100],
    )
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Outer=outer)
    in_outer, in_inner, on_page = trace_page(pdf.pages[0])
    assert (in_outer.mcid, in_inner.mcid, on_page.mcid) == (5, 9, None)
    assert _close(in_outer.origin, (0, 100)) and _close(in_inner.origin, (0, 150))
    # The form's cm, Tr and open BDC end with the form.
    assert _close(on_page.origin, (0, 0)) and on_page.render_mode == 0
    assert in_inner.stream.objgen == inner.objgen


def test_form_that_draws_itself():
    pdf = _pdf(b"/Fm1 Do")
    form = _form(pdf, b"/Fm1 Do")
    form.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm1=form))
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Fm1=form)
    with pytest.raises(UnsupportedText, match="draws itself"):
        trace_page(pdf.pages[0])


def test_forms_match_pdfium():
    pdf = _pdf(
        b"q 0.96 0.28 -0.28 0.96 40 30 cm /Fm1 Do Q",
        HELVETICA_WIDTHS,
    )
    fonts = pdf.pages[0].obj.Resources.Font
    inner = _form(
        pdf,
        b"BT /F1 9 Tf 2 Tc 10 20 Td (Hello) Tj ET",
        Resources=pikepdf.Dictionary(Font=fonts),
        Matrix=[0.8, 0, 0.1, 0.8, 5, 5],
    )
    outer = _form(
        pdf,
        b"BT /F1 12 Tf 50 300 Td (He) Tj ET q 1 0 0 1 60 400 cm /In Do Q",
        Resources=pikepdf.Dictionary(Font=fonts, XObject=pikepdf.Dictionary(In=inner)),
        Matrix=[1.2, 0, 0, 1.2, 0, 0],
    )
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Fm1=outer)
    glyphs = trace_page(pdf.pages[0])
    origins = _pdfium_origins(pdf)
    assert len(origins) == len(glyphs) == 7
    for glyph, origin in zip(glyphs, origins, strict=True):
        assert _close(glyph.origin, origin, tolerance=0.01)


def _type3_pdf(content: bytes, font_bbox: list, descriptor: dict | None = None) -> pikepdf.Pdf:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    procs = pikepdf.Dictionary(
        a=pdf.make_stream(b"50 0 0 -20 50 80 d1 0 0 50 80 re f"),
        b=pdf.make_stream(b"70 0 0 -20 70 80 d1 0 0 70 80 re f"),
    )
    font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.Type3,
        FontBBox=font_bbox,
        FontMatrix=[0.01, 0, 0, 0.01, 0, 0],
        CharProcs=procs,
        Encoding=pikepdf.Dictionary(
            Type=pikepdf.Name.Encoding, Differences=[65, pikepdf.Name.a, pikepdf.Name.b]
        ),
        FirstChar=65,
        LastChar=66,
        Widths=[50, 70],
        Resources=pikepdf.Dictionary(),
    )
    if descriptor is not None:
        font.FontDescriptor = pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.FontDescriptor, **descriptor)
        )
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(T3=pdf.make_indirect(font)))
    page.obj.Contents = pdf.make_stream(content)
    return pdf


def test_type3_widths_and_box_use_the_font_matrix():
    # Widths 50 and 70 in a glyph space of 1/100: advances of 0.5 and 0.7 text units.
    pdf = _type3_pdf(b"BT /T3 10 Tf 100 100 Td (ABA) Tj ET", [0, -20, 70, 80])
    glyphs = trace_page(pdf.pages[0])
    assert [round(g.origin[0], 6) for g in glyphs] == [100, 105, 112]
    assert [g.width for g in glyphs] == [50, 70, 50]
    assert glyphs[0].height_from == "font-bbox"
    assert _close(glyphs[0].box[0], (100, 98)) and _close(glyphs[0].box[2], (105, 108))


@pytest.mark.parametrize(
    ("font_bbox", "descriptor", "height_from", "extent"),
    [
        ([0, 0, 0, 0], None, "default", (-2.5, 7.5)),
        ([0, 0, 0, 0], {"Ascent": 90, "Descent": -10}, "ascent", (-1, 9)),
    ],
)
def test_type3_vertical_extent(font_bbox, descriptor, height_from, extent):
    pdf = _type3_pdf(b"BT /T3 10 Tf (A) Tj ET", font_bbox, descriptor)
    (glyph,) = trace_page(pdf.pages[0])
    assert glyph.height_from == height_from
    assert _close((glyph.box[0][1], glyph.box[2][1]), extent)


def test_type3_matches_pdfium():
    content = (
        b"q 0.9 0.3 -0.3 0.9 20 30 cm BT /T3 16 Tf 1 Tc 90 Tz 80 300 Td [(AB) 300 (BA)] TJ ET Q"
    )
    pdf = _type3_pdf(content, [0, -20, 70, 80])
    glyphs = trace_page(pdf.pages[0])
    origins = _pdfium_origins(pdf)
    assert len(origins) == len(glyphs) == 4
    for glyph, origin in zip(glyphs, origins, strict=True):
        assert _close(glyph.origin, origin, tolerance=0.01)
