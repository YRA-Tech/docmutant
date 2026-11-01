"""Text from outlines: what counts as an item, what replaces it and what stays exact."""

from __future__ import annotations

import io

import pikepdf
import pypdfium2 as pdfium
import pytest
from test_anonymize import _bytes, _privacy, _source
from test_coverage import _Boxes

from docmutant.anonymize import anonymize, resolve_profile
from docmutant.content import operator_name
from docmutant.coverage import DRAWN_TEXT, curved_fills
from docmutant.outlines import FONT_KEY, FONT_PREFIX, outline_items, text_from_outlines
from docmutant.substitute import FontSource
from docmutant.tracker import group_lines, trace_page

CURVED = b"0 0 m 0 4 3 8 6 8 c 9 8 9 0 6 0 c h"
STRAIGHT = b"0 0 m 0 8 l 2 8 l 2 5 l 6 5 l 6 8 l 8 8 l 8 0 l h"
BOX = b"0 0 2 8 re"


def _glyph(x: float, y: float = 700, shape: bytes = CURVED, colour: bytes = b"0 0 0 rg") -> bytes:
    return b"q 1 0 0 1 %.1f %.1f cm %s %s f Q\n" % (x, y, colour, shape)


def _word(x: float = 100, y: float = 700, letters: int = 5) -> bytes:
    return b"".join(_glyph(x + 10 * index, y) for index in range(letters))


def _page(content: bytes) -> pikepdf.Pdf:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.obj.Resources = pikepdf.Dictionary()
    page.obj.Contents = pdf.make_stream(content)
    return pdf


def _convert(pdf: pikepdf.Pdf, *, seed: int = 0, vocabulary=(), avoid=()) -> int:
    return text_from_outlines(pdf, _Boxes(), seed, vocabulary, avoid).items


def _operators(pdf: pikepdf.Pdf) -> list[str]:
    return [operator_name(i) for i in pikepdf.parse_content_stream(pdf.pages[0])]


def _new_lines(pdf: pikepdf.Pdf) -> list:
    return group_lines(trace_page(pdf.pages[0]))


def _pixel(pdf: pikepdf.Pdf, x: float, y: float) -> tuple[int, ...]:
    image = pdfium.PdfDocument(_bytes(pdf))[0].render(scale=1).to_pil().convert("L")
    return image.getpixel((round(x), round(792 - y)))


def test_a_word_of_glyphs_becomes_one_paragraph_of_new_text_in_its_box():
    pdf = _page(_word())
    assert outline_items(pdf) == {"page 1": 1}
    assert _convert(pdf) == 1
    assert curved_fills(pdf.pages[0]) == 0
    assert outline_items(pdf) == {}
    lines = _new_lines(pdf)
    assert len(lines) == 1
    assert lines[0].text.replace(" ", "").isalpha()
    assert lines[0].box.start == pytest.approx(100, abs=0.01)
    # The last glyph starts at 140. A box counts its control points, the furthest of them at 9.
    assert lines[0].box.end == pytest.approx(149, abs=0.01)
    font = pdf.pages[0].obj.Resources.Font[FONT_KEY]
    assert str(font.BaseFont).startswith("/" + FONT_PREFIX)


def test_letters_of_straight_lines_go_with_their_word():
    # Curves alone would leave the H, I, L and T of a word readable.
    pdf = _page(
        _glyph(100, shape=STRAIGHT)
        + _glyph(110, shape=BOX)
        + _glyph(115)
        + _glyph(125, shape=STRAIGHT)
    )
    assert _convert(pdf) == 1
    assert not {"m", "l", "re", "f"} & set(_operators(pdf))


def test_a_word_of_straight_letters_alone_is_text_too():
    pdf = _page(_glyph(100, shape=STRAIGHT) + _glyph(110, shape=STRAIGHT))
    assert _convert(pdf) == 1


def test_a_drawing_order_other_than_reading_order_makes_one_paragraph():
    # Each glyph is far from the one drawn before it, so a run in stream order sees only loners.
    order = (0, 40, 20, 10, 30)
    pdf = _page(b"".join(_glyph(100 + x) for x in order))
    assert _convert(pdf) == 1
    assert curved_fills(pdf.pages[0]) == 0


def test_a_clip_for_each_run_does_not_split_a_word_or_cut_its_paragraph():
    # Some producers clip each short run. A letter in its own clip would stay readable.
    pdf = _page(
        b"q 100 690 10 30 re W n "
        + _glyph(100)
        + b"Q q 110 690 40 30 re W n "
        + _glyph(110)
        + _glyph(120)
        + b"Q\n"
    )
    assert _convert(pdf) == 1
    operators = _operators(pdf)
    assert not {"W", "W*"} & set(operators)
    assert _new_lines(pdf)[0].box.end > 125
    assert _pixel(pdf, 115, 703) < 250 or _pixel(pdf, 125, 703) < 250


def test_a_clip_around_the_paragraph_opens_again_for_what_comes_after():
    after = b"0 0 m 1 1 l 2 0 l h f\n"
    pdf = _page(b"q 0 0 300 800 re W n " + _word() + after + b"Q\n")
    assert _convert(pdf) == 1
    operators = _operators(pdf)
    text = operators.index("BT")
    assert "W" in operators[text:]
    assert operators.index("f") > text
    assert operators.count("q") == operators.count("Q")


def test_each_letters_place_goes_with_its_shape():
    # The transforms of a word record its letters' advances, and so which letters they are.
    pdf = _page(_word())
    _convert(pdf)
    assert "cm" not in _operators(pdf)


def test_a_paragraph_keeps_its_place_under_what_covered_the_shapes():
    panel = b"1 1 1 rg 80 680 200 50 re f\n"
    pdf = _page(_word() + panel)
    _convert(pdf)
    operators = _operators(pdf)
    assert operators.index("BT") < operators.index("re")
    assert _pixel(pdf, 110, 703) == 255


def test_a_paragraph_keeps_the_colour_of_its_shapes():
    red = b"1 0 0 rg"
    pdf = _page(b"".join(_glyph(100 + 10 * i, colour=red) for i in range(4)) + b"0 1 0 rg\n")
    _convert(pdf)
    colours = [
        [float(value) for value in instruction.operands]
        for instruction in pikepdf.parse_content_stream(pdf.pages[0])
        if operator_name(instruction) == "rg"
    ]
    assert [1.0, 0.0, 0.0] in colours


def test_a_colour_in_a_named_space_keeps_the_space():
    colour = b"/DeviceRGB cs 0 0 1 sc"
    pdf = _page(b"".join(_glyph(100 + 10 * i, colour=colour) for i in range(4)))
    _convert(pdf)
    operators = _operators(pdf)
    assert operators.index("cs") < operators.index("sc") < operators.index("BT")


def test_two_colours_make_two_paragraphs():
    # A white label over a dark box must not take the box with it.
    white = b"1 1 1 rg"
    pdf = _page(_word(letters=3) + b"".join(_glyph(130 + 10 * i, colour=white) for i in range(3)))
    assert _convert(pdf) == 2


def test_a_wide_gap_or_another_line_makes_another_paragraph():
    pdf = _page(_word(letters=3) + _word(x=300, letters=3) + _word(y=650, letters=3))
    assert _convert(pdf) == 3


@pytest.mark.parametrize(
    "marks",
    [
        pytest.param(_glyph(100), id="a lone glyph"),
        pytest.param(_glyph(100, shape=CURVED + b" " + CURVED), id="a ring"),
        pytest.param(_glyph(100) + _glyph(100), id="two shapes in one place"),
        pytest.param(_glyph(100, shape=BOX) + _glyph(110, shape=BOX), id="two bars"),
        pytest.param(_glyph(100, shape=b"0 0 70 8 re"), id="a wide rule"),
        pytest.param(_glyph(100, shape=b"0 0 300 300 re"), id="a background"),
        pytest.param(_glyph(100, shape=b"0 0 m 0 90 90 90 90 0 c h"), id="a large curve"),
        pytest.param(
            _glyph(100, shape=b"0 0 20 8 re 40 0 20 8 re 80 0 20 8 re 200 0 20 8 re"),
            id="one fill that masks four places",
        ),
        pytest.param(b"0 0 m 1 1 2 2 3 3 c S\n" * 4, id="strokes"),
        pytest.param(b"q 0 0 m 1 4 3 8 6 8 c W n Q\n" * 4, id="clip paths"),
    ],
)
def test_what_does_not_look_like_a_word_stays_exact(marks):
    pdf = _page(marks)
    before = _operators(pdf)
    assert outline_items(pdf) == {}
    assert _convert(pdf) == 0
    assert _operators(pdf) == before


def test_one_fill_of_many_subpaths_is_a_word():
    word = b" ".join(b"%d 0 m %d 8 %d 8 %d 0 c h" % (x, x, x + 5, x + 5) for x in range(0, 40, 8))
    pdf = _page(_glyph(100, shape=word))
    assert _convert(pdf) == 1


def test_a_written_upward_paragraph_is_written_upward():
    rotated = b"".join(
        b"q 0 1 -1 0 300 %d cm 0 0 0 rg %s f Q\n" % (100 + 10 * i, CURVED) for i in range(4)
    )
    pdf = _page(rotated)
    assert _convert(pdf) == 1
    glyphs = trace_page(pdf.pages[0])
    assert glyphs[0].direction[1] == pytest.approx(1, abs=0.01)
    assert {round(glyph.origin[0]) for glyph in glyphs} == {round(glyphs[0].origin[0])}


def test_a_flipped_transform_still_gives_upright_text_in_the_box():
    flipped = b"".join(
        b"q 1 0 0 -1 %d 700 cm 0 0 0 rg %s f Q\n" % (100 + 10 * i, CURVED) for i in range(4)
    )
    pdf = _page(flipped)
    assert _convert(pdf) == 1
    glyphs = trace_page(pdf.pages[0])
    assert glyphs[0].direction[0] == pytest.approx(1, abs=0.01)
    assert 690 < glyphs[0].origin[1] < 700


def test_a_transform_before_the_paragraph_does_not_move_it():
    pdf = _page(b"1 0 0 1 50 20 cm " + _word())
    _convert(pdf)
    assert _new_lines(pdf)[0].box.start == pytest.approx(150, abs=0.01)


def test_a_form_takes_its_paragraph_in_its_own_units():
    pdf = _page(b"q 0.1 0 0 0.1 100 700 cm /Fm Do Q\n")
    # At 0.1, this draws glyphs of 8 points in units of 80.
    glyph = b"q 80 0 0 80 %d 0 cm 0 0 0 rg 0 0 m 0 .4 .3 .8 .6 .8 c .9 .8 .9 0 .6 0 c h f Q\n"
    form = pdf.make_stream(
        b"".join(glyph % (100 * i) for i in range(4)),
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 1000, 1000],
        Resources=pikepdf.Dictionary(),
    )
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Fm=pdf.make_indirect(form))
    assert outline_items(pdf) == {f"form {form.objgen[0]} {form.objgen[1]}": 1}
    assert _convert(pdf) == 1
    assert curved_fills(pdf.pages[0]) == 0
    assert FONT_KEY in form.Resources.Font
    assert _new_lines(pdf)[0].box.start == pytest.approx(100, abs=0.5)


def test_marked_content_that_structure_can_point_at_stays():
    pdf = _page(b"/P <</MCID 0>> BDC " + _word() + b"EMC\n")
    _convert(pdf)
    operators = _operators(pdf)
    assert "BDC" in operators
    assert operators.count("BDC") + operators.count("BMC") == operators.count("EMC")


def test_marked_content_with_no_properties_goes_with_its_shape():
    pdf = _page(b"".join(b"q /Span BMC %s EMC Q\n" % _glyph(100 + 10 * i) for i in range(4)))
    _convert(pdf)
    operators = _operators(pdf)
    # The paragraph's own, and the one that it was drawn inside. The other three are gone.
    assert operators.count("BMC") == operators.count("EMC") == 2


def test_a_colour_set_between_a_path_and_its_paint_does_not_stop_the_shape_going():
    glyph = b"q 1 0 0 1 %d 700 cm %s 0 0 0 rg /Span BMC f EMC Q\n"
    pdf = _page(b"".join(glyph % (100 + 10 * i, CURVED) for i in range(4)))
    assert _convert(pdf) == 1
    operators = _operators(pdf)
    assert "f" not in operators
    assert operators.count("BMC") == operators.count("EMC")


def test_groups_that_overlap_marked_content_keep_their_balance():
    # A q and a marked-content sequence can overlap, and a wrong match would drop real state.
    pdf = _page(
        b"/P <</MCID 1>> BDC q 1 0 0 1 5 5 cm EMC Q " + _word() + b"0 0 m 1 1 l 2 0 l h f\n"
    )
    _convert(pdf)
    operators = _operators(pdf)
    assert operators.count("q") == operators.count("Q")
    assert operators.count("BDC") + operators.count("BMC") == operators.count("EMC")
    assert "f" in operators


def test_new_words_avoid_the_source_and_use_a_vocabulary():
    pdf = _page(_word(letters=20))
    _convert(pdf, vocabulary=["bo", "gem", "lamp"], avoid=["lamp"])
    words = _new_lines(pdf)[0].text.split()
    assert "lamp" not in words
    assert {"bo", "gem"} & set(words)


def test_the_same_seed_gives_the_same_paragraph_and_another_seed_another():
    def text(seed: int) -> str:
        pdf = _page(_word(letters=20))
        _convert(pdf, seed=seed)
        return _new_lines(pdf)[0].text

    assert text(1) == text(1)
    assert text(1) != text(2)


def test_pdfium_reads_the_paragraph():
    pdf = _page(_word(letters=20))
    _convert(pdf)
    text = pdfium.PdfDocument(_bytes(pdf))[0].get_textpage().get_text_bounded()
    assert text.replace(" ", "").replace("\r\n", "").isalpha()
    assert len(text) > 10


def test_a_run_turns_outlines_into_text_and_says_so():
    pdf = _source(extra=_word(y=500, letters=8))
    result = anonymize(_bytes(pdf), _privacy(), fonts=_Boxes())
    assert result.data is not None, result.problems
    output = pikepdf.open(io.BytesIO(result.data))
    assert outline_items(output) == {}
    assert curved_fills(output.pages[0]) == 0
    check = next(check for check in result.checks if check.name == "drawn-text")
    assert check.passed
    assert check.details["items_replaced"] == 1
    assert check.details["items_left"] == 0
    assert {"operator": "text-from-outlines", "params": {"items": 1}} in result.mutations
    assert any("outline items" in item for item in result.replaced)
    # The source's words are still counted without the paragraph.
    assert result.metrics["words_in_scope"] == result.metrics["words_changed"] > 0


def test_a_page_with_nothing_but_outlines_goes_on():
    result = anonymize(_bytes(_page(_word())), _privacy(), fonts=_Boxes())
    assert result.data is not None, result.problems
    assert result.metrics["words_in_scope"] == 0
    assert next(c for c in result.checks if c.name == "drawn-text").details["items_replaced"] == 1


def test_many_large_curved_shapes_still_stop_the_run():
    large = b"0 0 m 0 90 90 90 90 0 c h f\n" * (DRAWN_TEXT + 1)
    result = anonymize(_bytes(_page(large)), _privacy(), fonts=_Boxes())
    assert result.data is None
    assert any("too large to be glyphs" in problem for problem in result.problems)


def test_an_item_that_cannot_be_replaced_stops_the_run():
    pdf = _page(b"/Fm Do\n")
    # A form without resources of its own: it could not name the paragraph's font.
    form = pdf.make_stream(
        _word(0, 0),
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 100, 100],
    )
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Fm=pdf.make_indirect(form))
    result = anonymize(_bytes(pdf), _privacy(), fonts=_Boxes())
    assert result.data is None
    assert any("outline items left" in problem for problem in result.problems)


def test_no_font_to_draw_a_paragraph_in_stops_the_run():
    result = anonymize(_bytes(_page(_word())), _privacy(), fonts=FontSource(()))
    assert result.data is None
    assert any("no open font" in problem for problem in result.problems)


def test_no_font_is_needed_when_a_page_has_no_outlines():
    pdf = _page(_glyph(100))
    result = anonymize(_bytes(pdf), _privacy(), fonts=FontSource(()))
    assert result.data is not None, result.problems


def _shapes(data: bytes) -> list[str]:
    output = pikepdf.open(io.BytesIO(data))
    return [str(i) for i in pikepdf.parse_content_stream(output.pages[0])]


def test_the_training_profile_keeps_outlines_exact():
    profile = resolve_profile("training")
    assert not profile.text_from_outlines
    pdf = _source(extra=_word(y=500))
    before = _shapes(_bytes(pdf))
    result = anonymize(_bytes(pdf), profile, fonts=_Boxes())
    assert result.data is not None, result.problems
    after = _shapes(result.data)
    paths = [line for line in before if " c" in line or " m" in line]
    assert paths and [line for line in after if line in paths] == paths
    assert "drawn-text" not in {check.name for check in result.checks}
    assert not any(m["operator"] == "text-from-outlines" for m in result.mutations)


def test_keeping_drawings_under_privacy_keeps_outlines_and_gives_up_the_claim():
    profile = _privacy(keep_drawings=True)
    assert not profile.text_from_outlines
    assert profile.claims == ()
    result = anonymize(_bytes(_source(extra=_word(y=500))), profile, fonts=_Boxes())
    assert result.data is not None, result.problems
    output = pikepdf.open(io.BytesIO(result.data))
    assert curved_fills(output.pages[0]) == 5
    assert outline_items(output) == {"page 1": 1}
