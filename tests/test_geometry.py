"""Geometry oracle: edge movement, signed gaps and the settings of design section 4.4."""

from __future__ import annotations

import math

import pikepdf
import pytest

from docmutant.geometry import (
    CONTAIN,
    EXACT,
    BoxSettings,
    MinimumGap,
    check_boxes,
    check_page,
    edge_moves,
    signed_gap,
    within,
)
from docmutant.mutate import StreamRef, swap_text_codes
from docmutant.tracker import Box, Line, group_lines, trace_page

RIGHT = (1.0, 0.0)
SKEWED = (0.8, 0.6)
DIAGONAL = (math.sqrt(0.5), math.sqrt(0.5))
TIGHT_SPACE = b"BT /F1 10 Tf (A) Tj [( ) 150 (B)] TJ ET"
"""Two words 0.1 em apart: the space glyph is 2.5 wide and the TJ moves back 1.5."""

TIGHTER_SPACE = b"BT /F1 10 Tf (A) Tj [( ) 200 (B)] TJ ET"
"""Two words 0.05 em apart."""


def _lines(content: bytes, *, widths: dict[int, float] | None = None) -> list[Line]:
    pdf = _pdf(content, widths)
    return group_lines(trace_page(pdf.pages[0]))


def _pdf(content: bytes, widths: dict[int, float] | None = None) -> pikepdf.Pdf:
    # A and B are 500 wide, the narrow I is 250, the wide W is 900; 10 pt gives 5, 2.5 and 9.
    widths = widths or {32: 250, 65: 500, 66: 500, 73: 250, 87: 900}
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    first, last = min(widths), max(widths)
    page.obj.Resources = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(
            F1=pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name.Font,
                    Subtype=pikepdf.Name.Type1,
                    BaseFont=pikepdf.Name.Helvetica,
                    FirstChar=first,
                    LastChar=last,
                    Widths=[widths.get(code, 0) for code in range(first, last + 1)],
                )
            )
        )
    )
    page.obj.Contents = pdf.make_stream(content)
    return pdf


def _kinds(result) -> list[str]:
    return [violation.kind for violation in result.violations]


@pytest.mark.parametrize(
    ("a", "b", "gap"),
    [
        # Apart along one axis.
        (Box(RIGHT, 0, 1, 0, 1), Box(RIGHT, 2, 3, 0, 1), 1.0),
        # Apart diagonally: corner to corner, 3-4-5.
        (Box(RIGHT, 0, 1, 0, 1), Box(RIGHT, 4, 5, 5, 6), 5.0),
        # Touching along an edge.
        (Box(RIGHT, 0, 1, 0, 1), Box(RIGHT, 1, 2, 0, 1), 0.0),
        # Overlapping by 0.25 across the baseline and 3 along it.
        (Box(RIGHT, 0, 4, 0, 1), Box(RIGHT, 1, 5, 0.75, 2), -0.25),
        # One inside the other: the shortest way out, to the left.
        (Box(RIGHT, 0, 10, 0, 10), Box(RIGHT, 4, 5, 4, 5), -5.0),
        # The same two boxes, rotated with the page.
        (Box(SKEWED, 0, 1, 0, 1), Box(SKEWED, 2, 3, 0, 1), 1.0),
        (Box(SKEWED, 0, 4, 0, 1), Box(SKEWED, 1, 5, 0.75, 2), -0.25),
        # A square at 45 degrees with its left corner at (3, 0.5): 2 from the right edge at x = 1.
        (
            Box(RIGHT, 0, 1, 0, 1),
            Box(
                DIAGONAL,
                1.75 * math.sqrt(2),
                1.75 * math.sqrt(2) + 1,
                -1.25 * math.sqrt(2) - 1,
                -1.25 * math.sqrt(2),
            ),
            2.0,
        ),
        # The same square with its corner at (0.75, 0.5), 0.25 inside that edge.
        (
            Box(RIGHT, 0, 1, 0, 1),
            Box(
                DIAGONAL,
                0.625 * math.sqrt(2),
                0.625 * math.sqrt(2) + 1,
                -0.125 * math.sqrt(2) - 1,
                -0.125 * math.sqrt(2),
            ),
            -0.25,
        ),
    ],
)
def test_signed_gap(a, b, gap):
    assert signed_gap(a, b) == pytest.approx(gap)
    assert signed_gap(b, a) == pytest.approx(gap)


def test_a_rotated_box_is_compared_as_drawn():
    # A diamond around (2, 2), with corners 1.2 from its centre. Its axis-aligned box starts
    # at (0.8, 0.8) and would overlap the unit square, but its edge x + y = 2.8 is apart from
    # the square's corner (1, 1).
    half_side = 0.6 * math.sqrt(2)
    centre = 2 * math.sqrt(2)
    diamond = Box(DIAGONAL, centre - half_side, centre + half_side, -half_side, half_side)
    assert signed_gap(Box(RIGHT, 0, 1, 0, 1), diamond) == pytest.approx(0.8 * math.sqrt(0.5))


def test_edge_moves_are_positive_out_and_negative_inward():
    source = Box(RIGHT, 10, 20, 0, 10)
    assert edge_moves(source, Box(RIGHT, 9, 21, -1, 11)) == (1, 1, 1, 1)
    assert edge_moves(source, Box(RIGHT, 11, 19, 1, 9)) == (-1, -1, -1, -1)
    assert edge_moves(source, Box(RIGHT, 11, 21, 0, 10)) == (-1, 1, 0, 0)


def test_edge_moves_are_measured_in_the_frame_of_the_source_line():
    source = Box(SKEWED, 10, 20, 0, 10)
    # Sliding along the skewed baseline moves both ends and neither of the other edges.
    moves = edge_moves(source, Box(SKEWED, 12, 22, 0, 10))
    assert moves == pytest.approx((-2, 2, 0, 0))


@pytest.mark.parametrize(
    ("tolerance", "output", "passes"),
    [
        (EXACT, Box(RIGHT, 72, 82, 697.5, 707.5), True),
        # A bound of 0 allows 0.01 pt for rounding.
        (EXACT, Box(RIGHT, 72.005, 82, 697.5, 707.5), True),
        (EXACT, Box(RIGHT, 72.02, 82, 697.5, 707.5), False),
        (CONTAIN, Box(RIGHT, 73, 81, 698, 707), True),
        (CONTAIN, Box(RIGHT, 72, 83, 697.5, 707.5), False),
        (within(2), Box(RIGHT, 70, 84, 695.5, 709.5), True),
        (within(2), Box(RIGHT, 74, 80, 699.5, 705.5), True),
        (within(2), Box(RIGHT, 69.9, 82, 697.5, 707.5), False),
        (within(2), Box(RIGHT, 72, 79.9, 697.5, 707.5), False),
    ],
)
def test_box_tolerance_on_a_line(tolerance, output, passes):
    (line,) = _lines(b"BT /F1 10 Tf 72 700 Td (AB) Tj ET")
    moved = Line(line.words, output)
    settings = BoxSettings("line", tolerance, word_gap=None, line_gap=None, word_count="free")
    result = check_boxes([line], [moved], settings)
    assert result.passed is passes
    assert _kinds(result) == ([] if passes else ["line-box"])


def test_a_violation_gives_the_signed_move_and_the_bound_it_broke():
    (line,) = _lines(b"BT /F1 10 Tf 72 700 Td (AB) Tj ET")
    settings = BoxSettings("line", within(1), word_gap=None, line_gap=None)
    (violation,) = check_boxes(
        [line], [Line(line.words, Box(RIGHT, 75, 82, 697.5, 707.5))], settings
    ).violations
    assert (violation.value, violation.limit, violation.line) == (-3, 1, 0)


SETTINGS = [
    BoxSettings("word"),
    BoxSettings("word", CONTAIN),
    BoxSettings("word", within(2), tracking_limit=0.02),
    BoxSettings("line"),
    BoxSettings("line", CONTAIN, word_count="free"),
    BoxSettings("line", within(2), word_count="free", tracking_limit=0.02),
    BoxSettings("line", word_gap=MinimumGap(points=50), line_gap=MinimumGap(points=50)),
    BoxSettings(
        "line", word_gap=MinimumGap(points=50), line_gap=MinimumGap(ems=5), word_count="free"
    ),
]
MESSY_PAGE = (
    # Text over text, a thin space, a superscript, kerned and skewed lines, a one-word line.
    b"BT /F1 10 Tf 72 700 Td (AB AB) Tj 0.2 0 Td (AB AB) Tj ET "
    b"BT /F1 10 Tf 72 680 Td [(A) -160 (B) 200 (A)] TJ /F1 5 Tf 4 Ts (A) Tj ET "
    b"q 0.8 0.6 -0.6 0.8 100 300 cm BT /F1 10 Tf (AB A) Tj ET Q "
    b"BT /F1 10 Tf 72 400 Td (A) Tj ET"
)


@pytest.mark.parametrize("settings", SETTINGS)
def test_a_page_compared_with_itself_passes_under_every_setting(settings):
    lines = _lines(MESSY_PAGE)
    assert len(lines) > 3
    result = check_boxes(lines, lines, settings)
    assert result.passed, result.violations
    assert result.largest_line_move == pytest.approx(0, abs=1e-9)
    assert result.largest_tracking == 0


def test_a_swap_of_equal_width_keeps_word_boxes():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (AB BA) Tj 0 -12 Td (B A) Tj ET")
    source = group_lines(trace_page(pdf.pages[0]))
    swapped = _pdf(b"BT /F1 10 Tf 72 700 Td (BA AB) Tj 0 -12 Td (A B) Tj ET")
    result = check_boxes(source, group_lines(trace_page(swapped.pages[0])), BoxSettings("word"))
    assert result.passed
    assert result.largest_word_move == 0


def test_swap_text_codes_keeps_word_boxes():
    widths = {32: 250, **{code: 500 for code in range(65, 91)}}
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (ABC DEF) Tj 0 -14 Td [(GH) -40 (I J)] TJ ET", widths)
    source = group_lines(trace_page(pdf.pages[0]))
    swap_text_codes(pdf, [StreamRef("page", 0)], seed=1)
    output = group_lines(trace_page(pdf.pages[0]))
    assert [g.code for g in output[0].words[0].glyphs] != [
        g.code for g in source[0].words[0].glyphs
    ]
    # The kerning of 0.04 em in the source stays in the output, so it is not tracking.
    result = check_boxes(source, output, BoxSettings("word"))
    assert result.passed, result.violations
    assert result.largest_word_move == 0


def test_a_wider_word_fails_scope_word_and_passes_scope_line_when_the_space_absorbs_it():
    widths = {65: 500, 87: 520}
    source = _lines(b"BT /F1 10 Tf [(AA) -250 (AA) -250 (AA)] TJ ET", widths=widths)
    # WW is 0.4 wider than AA, and the next shift is 0.4 smaller: the last word does not move.
    output = _lines(b"BT /F1 10 Tf [(AA) -250 (WW) -210 (AA)] TJ ET", widths=widths)
    assert [len(word.glyphs) for word in output[0].words] == [2, 2, 2]

    word = check_boxes(source, output, BoxSettings("word"))
    (violation,) = word.violations
    assert (violation.kind, violation.word) == ("word-box", 1)
    assert violation.value == pytest.approx(0.4)

    line = check_boxes(source, output, BoxSettings("line"))
    assert line.passed, line.violations
    assert line.largest_word_move == pytest.approx(0.4)
    assert line.largest_line_move == pytest.approx(0)


def test_scope_word_reports_the_word_that_moved():
    source = _lines(b"BT /F1 10 Tf (AB AB) Tj ET")
    output = _lines(b"BT /F1 10 Tf [(AB ) -100 (AB)] TJ ET")
    (violation,) = check_boxes(source, output, BoxSettings("word")).violations
    assert (violation.kind, violation.line, violation.word) == ("word-box", 0, 1)


def test_word_gap_default_is_the_gap_at_which_the_tracker_ends_a_word():
    source = _lines(b"BT /F1 10 Tf (A B) Tj ET")
    settings = BoxSettings("line", within(5))
    assert check_boxes(source, _lines(b"BT /F1 10 Tf [(A) -200 (B)] TJ ET"), settings).passed
    (violation,) = check_boxes(source, _lines(TIGHT_SPACE), settings).violations
    assert (violation.kind, violation.line, violation.word, violation.other) == (
        "word-gap",
        0,
        0,
        1,
    )
    assert violation.value == pytest.approx(1)
    assert violation.limit == pytest.approx(1.5)


def test_a_gap_already_under_the_minimum_may_stay_but_not_shrink():
    source = _lines(TIGHT_SPACE)
    assert len(source[0].words) == 2
    settings = BoxSettings("line", within(5))
    assert check_boxes(source, source, settings).passed
    (violation,) = check_boxes(source, _lines(TIGHTER_SPACE), settings).violations
    assert violation.kind == "word-gap"
    assert violation.limit == pytest.approx(1)


def test_word_gap_any_turns_the_check_off_and_still_reports_the_smallest():
    source = _lines(TIGHT_SPACE)
    settings = BoxSettings("line", within(5), word_gap=None)
    result = check_boxes(source, _lines(TIGHTER_SPACE), settings)
    assert result.passed
    assert result.smallest_word_gap == pytest.approx(0.5)


def test_lines_overlapping_in_the_source_may_not_overlap_more():
    source = _lines(b"BT /F1 10 Tf (AB) Tj 0 -9 Td (AB) Tj ET")
    assert source[0].box.bottom < source[1].box.top
    assert check_boxes(source, source, BoxSettings("line")).passed
    closer = _lines(b"BT /F1 10 Tf (AB) Tj 0 -8 Td (AB) Tj ET")
    result = check_boxes(source, closer, BoxSettings("line", within(5)))
    assert _kinds(result) == ["line-gap"]
    assert result.gap_pairs_broken == 1
    assert result.smallest_line_gap == pytest.approx(-2)


def test_line_gap_in_points_and_ems_and_off():
    source = _lines(b"BT /F1 10 Tf (AB) Tj 0 -30 Td (AB) Tj ET")
    output = _lines(b"BT /F1 10 Tf (AB) Tj 0 -28 Td (AB) Tj ET")
    assert source[0].box.bottom - source[1].box.top == pytest.approx(20)
    tolerance = within(5)
    assert check_boxes(source, output, BoxSettings("line", tolerance)).passed
    too_close = BoxSettings("line", tolerance, line_gap=MinimumGap(points=19))
    assert _kinds(check_boxes(source, output, too_close)) == ["line-gap"]
    too_close = BoxSettings("line", tolerance, line_gap=MinimumGap(ems=1.9))
    assert _kinds(check_boxes(source, output, too_close)) == ["line-gap"]
    off = BoxSettings("line", tolerance, line_gap=None)
    assert check_boxes(source, output, off).passed


def test_fixed_word_count_fails_a_changed_count_and_free_reports_it():
    source = _lines(b"BT /F1 10 Tf (AB BA) Tj ET")
    output = _lines(b"BT /F1 10 Tf (A B A) Tj ET", widths={32: 500, 65: 500, 66: 500})
    assert len(output[0].words) == 3

    fixed = check_boxes(source, output, BoxSettings("line", within(5)))
    assert _kinds(fixed) == ["word-count"]
    assert fixed.lines_with_changed_word_count == 1
    assert fixed.largest_word_move is None

    free = check_boxes(source, output, BoxSettings("line", within(5), word_count="free"))
    assert free.passed
    assert free.lines_with_changed_word_count == 1
    assert free.largest_word_move is None


def test_free_word_count_gives_no_word_results_even_when_the_count_is_equal():
    source = _lines(b"BT /F1 10 Tf (AB BA) Tj ET")
    result = check_boxes(source, source, BoxSettings("line", word_count="free"))
    assert result.largest_word_move is None
    assert result.lines_with_changed_word_count == 0


def test_free_word_count_still_holds_the_line_box_and_the_word_gap():
    source = _lines(b"BT /F1 10 Tf (AB BA) Tj ET")
    longer = _lines(b"BT /F1 10 Tf (AB BAA) Tj ET")
    held = BoxSettings("line", word_count="free")
    assert _kinds(check_boxes(source, longer, held)) == ["line-box"]
    tight = _lines(b"BT /F1 10 Tf (AB) Tj [( ) 150 (AB)] TJ ET")
    loose = BoxSettings("line", within(5), word_count="free")
    assert _kinds(check_boxes(source, tight, loose)) == ["word-gap"]


def test_free_word_count_takes_the_floor_from_the_closest_words_of_the_source_line():
    source = _lines(TIGHT_SPACE)
    settings = BoxSettings("line", within(5), word_count="free")
    assert check_boxes(source, source, settings).passed
    assert _kinds(check_boxes(source, _lines(TIGHTER_SPACE), settings)) == ["word-gap"]


def test_a_changed_line_count_stops_the_comparison():
    source = _lines(b"BT /F1 10 Tf (AB) Tj 0 -12 Td (AB) Tj ET")
    output = _lines(b"BT /F1 10 Tf (AB) Tj ET")
    result = check_boxes(source, output, BoxSettings("line", word_count="free"))
    assert _kinds(result) == ["line-count"]
    assert (result.violations[0].value, result.violations[0].limit) == (1, 2)
    assert result.largest_line_move is None


def test_a_gap_that_splits_the_line_fails_the_line_count():
    source = _lines(b"BT /F1 10 Tf [(AB) -250 (AB)] TJ ET")
    output = _lines(b"BT /F1 10 Tf [(AB) -2500 (AB)] TJ ET")
    assert len(output) == 2
    assert _kinds(check_boxes(source, output, BoxSettings("line", within(50)))) == ["line-count"]


def test_tracking_limit():
    source = _lines(b"BT /F1 10 Tf (AB) Tj ET")
    # 0.02 em at 10 pt is 0.2 pt: 20 in TJ.
    tracked = _lines(b"BT /F1 10 Tf [(A) -20 (B)] TJ ET")
    settings = BoxSettings("word", within(1))
    result = check_boxes(source, tracked, settings)
    assert _kinds(result) == ["tracking"]
    assert result.largest_tracking == pytest.approx(0.02)
    allowed = BoxSettings("word", within(1), tracking_limit=0.02)
    assert check_boxes(source, tracked, allowed).passed
    assert _kinds(check_boxes(source, _lines(b"BT /F1 10 Tf [(A) -40 (B)] TJ ET"), allowed)) == [
        "tracking"
    ]


def test_kerning_the_source_already_has_is_not_tracking():
    source = _lines(b"BT /F1 10 Tf [(A) 80 (B)] TJ ET")
    assert check_boxes(source, source, BoxSettings("word")).largest_tracking == 0
    swapped = _lines(b"BT /F1 10 Tf [(B) 80 (A)] TJ ET")
    assert check_boxes(source, swapped, BoxSettings("word")).passed


def test_tracking_with_free_word_count_ignores_the_spaces():
    source = _lines(b"BT /F1 10 Tf (AB AB) Tj ET")
    wider_space = _lines(b"BT /F1 10 Tf [(AB) -100 ( AB)] TJ ET")
    result = check_boxes(source, wider_space, BoxSettings("line", within(5), word_count="free"))
    assert result.largest_tracking == 0


def test_word_count_free_needs_scope_line():
    with pytest.raises(ValueError, match="scope line"):
        BoxSettings("word", word_count="free")


def test_check_page_compares_two_pages():
    source = _pdf(b"BT /F1 10 Tf (AB) Tj ET")
    moved = _pdf(b"BT /F1 10 Tf 1 0 Td (AB) Tj ET")
    assert check_page(source.pages[0], source.pages[0], BoxSettings("word")).passed
    assert _kinds(check_page(source.pages[0], moved.pages[0], BoxSettings("word"))) == ["word-box"]
