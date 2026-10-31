"""Geometry oracle: did a mutation keep word and line boxes where the settings allow?

The settings are those of section 4.4 of the design: the scope that holds a word or a
line box, the box tolerance, the tracking limit, the word count and the minimum gaps.
Lines and words are paired by their place in drawing order. A mutator does not reorder
text, and a changed count is itself reported, so a wrong pair cannot pass unnoticed.
A page compared with itself passes under every setting.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Literal, NamedTuple

import pikepdf

from docmutant.tracker import WORD_GAP, Box, Line, Point, Word, group_lines, trace_page

ROUNDING = 0.01
"""Points allowed for rounding where a bound is 0."""

Scope = Literal["word", "line"]
WordCount = Literal["fixed", "free"]
ViolationKind = Literal[
    "line-count", "word-count", "line-box", "word-box", "tracking", "word-gap", "line-gap"
]


@dataclass(frozen=True)
class BoxTolerance:
    """How far each edge of a box may move, in points."""

    out: float
    """Away from the centre of the box."""

    inward: float
    """Toward the centre of the box."""


EXACT = BoxTolerance(0.0, 0.0)
CONTAIN = BoxTolerance(0.0, math.inf)


def within(distance: float) -> BoxTolerance:
    """``±d``: each edge may move up to ``distance`` points in either direction."""
    return BoxTolerance(distance, distance)


@dataclass(frozen=True)
class MinimumGap:
    """A distance in points, in ems of the larger glyph, or the sum of both."""

    points: float = 0.0
    ems: float = 0.0

    def limit(self, em: float) -> float:
        return self.points + self.ems * em


@dataclass(frozen=True)
class BoxSettings:
    scope: Scope
    tolerance: BoxTolerance = EXACT
    tracking_limit: float = 0.0
    """Ems per gap between letters."""

    word_count: WordCount = "fixed"
    word_gap: MinimumGap | None = MinimumGap(ems=WORD_GAP)
    """None is ``any``: the check is off."""

    line_gap: MinimumGap | None = MinimumGap()
    """None is ``any``: the check is off."""

    def __post_init__(self) -> None:
        if self.scope == "word" and self.word_count == "free":
            raise ValueError("word count applies only with scope line")


@dataclass(frozen=True)
class Violation:
    kind: ViolationKind
    value: float
    """A signed move for a box (positive out), a gap, a count or ems of tracking."""

    limit: float
    line: int | None = None
    """Index of the line, in drawing order."""

    word: int | None = None
    """Index of the word in the line."""

    other: int | None = None
    """The second of a pair: a line for a line gap, a word for a word gap."""


@dataclass(frozen=True)
class GeometryResult:
    settings: BoxSettings
    violations: tuple[Violation, ...]
    largest_line_move: float | None
    largest_word_move: float | None
    """None when no words are paired: the results are absent, not zero."""

    largest_tracking: float | None
    """Ems per gap, beyond what the source already has."""

    smallest_word_gap: float | None
    smallest_line_gap: float | None
    lines_with_changed_word_count: int

    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def gap_pairs_broken(self) -> int:
        return sum(v.kind in ("word-gap", "line-gap") for v in self.violations)


class EdgeMoves(NamedTuple):
    """Movement of each edge in the source frame: positive out, negative inward."""

    start: float
    end: float
    bottom: float
    top: float


def edge_moves(source: Box, output: Box) -> EdgeMoves:
    """How far each edge of ``output`` lies from that of ``source``, along and across its line."""
    ux, uy = source.direction
    along = [x * ux + y * uy for x, y in output.corners]
    across = [y * ux - x * uy for x, y in output.corners]
    return EdgeMoves(
        source.start - min(along),
        max(along) - source.end,
        source.bottom - min(across),
        max(across) - source.top,
    )


def _breach(moves: EdgeMoves, tolerance: BoxTolerance) -> tuple[float, float] | None:
    """The edge move furthest over its bound and that bound, or None if all edges fit."""
    out, inward = max(tolerance.out, ROUNDING), max(tolerance.inward, ROUNDING)
    worst = max(moves, key=lambda move: move - out if move > 0 else -move - inward)
    limit = out if worst > 0 else inward
    return (worst, limit) if abs(worst) > limit else None


def _segment_distance(point: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length if length else 0.0
    t = min(1.0, max(0.0, t))
    return math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy)


def signed_gap(a: Box, b: Box) -> float:
    """Shortest distance between two rotated boxes, or minus the shortest move that parts them."""
    corners_a, corners_b = a.corners, b.corners
    depth = math.inf
    for ux, uy in (a.direction, (-a.direction[1], a.direction[0])) + (
        b.direction,
        (-b.direction[1], b.direction[0]),
    ):
        on_a = [x * ux + y * uy for x, y in corners_a]
        on_b = [x * ux + y * uy for x, y in corners_b]
        depth = min(depth, max(on_a) - min(on_b), max(on_b) - min(on_a))
    if depth > 0:
        return -depth
    return min(
        _segment_distance(point, edge[i], edge[(i + 1) % 4])
        for point_set, edge in ((corners_a, corners_b), (corners_b, corners_a))
        for point in point_set
        for i in range(4)
    )


def _widest_letter_gap(words: Sequence[Word]) -> float:
    """The largest distance of a letter from where the last one left the pen, in ems.

    Measured from the pen, so ``Tc`` is part of the source's spacing and not tracking.
    """
    widest = 0.0
    for word in words:
        for first, second in zip(word.glyphs, word.glyphs[1:], strict=False):
            em = max(first.em, second.em)
            if em:
                ux, uy = first.direction
                shift = (second.origin[0] - first.origin[0]) * ux + (
                    second.origin[1] - first.origin[1]
                ) * uy
                widest = max(widest, abs(shift - first.advance) / em)
    return widest


def _word_em(first: Word, second: Word) -> float:
    return max(first.glyphs[-1].em, second.glyphs[0].em)


def _line_em(line: Line) -> float:
    return max(glyph.em for word in line.words for glyph in word.glyphs)


def check_boxes(
    source: Sequence[Line], output: Sequence[Line], settings: BoxSettings
) -> GeometryResult:
    """Compare the lines of one page before and after a mutation."""
    if len(source) != len(output):
        violation = Violation("line-count", float(len(output)), float(len(source)))
        return GeometryResult(settings, (violation,), None, None, None, None, None, 0)

    violations: list[Violation] = []
    line_moves: list[float] = []
    word_moves: list[float] = []
    tracking: list[float] = []
    word_gaps: list[float] = []
    line_gaps: list[float] = []
    changed_counts = 0

    def check_tracking(
        index: int, word: int | None, before: Sequence[Word], after: Sequence[Word]
    ) -> None:
        used = max(0.0, _widest_letter_gap(after) - _widest_letter_gap(before))
        tracking.append(used)
        em = max(glyph.em for item in after for glyph in item.glyphs)
        if (used - settings.tracking_limit) * em > ROUNDING:
            violations.append(Violation("tracking", used, settings.tracking_limit, index, word))

    for index, (before, after) in enumerate(zip(source, output, strict=True)):
        moves = edge_moves(before.box, after.box)
        line_moves.append(max(abs(move) for move in moves))
        breach = _breach(moves, settings.tolerance)
        if settings.scope == "line" and breach is not None:
            violations.append(Violation("line-box", *breach, index))

        same_count = len(before.words) == len(after.words)
        if not same_count:
            changed_counts += 1
            if settings.word_count == "fixed":
                violations.append(
                    Violation(
                        "word-count", float(len(after.words)), float(len(before.words)), index
                    )
                )
        paired = same_count and settings.word_count == "fixed"
        if paired:
            for position, (first, second) in enumerate(zip(before.words, after.words, strict=True)):
                moves = edge_moves(first.box, second.box)
                word_moves.append(max(abs(move) for move in moves))
                breach = _breach(moves, settings.tolerance)
                if settings.scope == "word" and breach is not None:
                    violations.append(Violation("word-box", *breach, index, position))
                check_tracking(index, position, (first,), (second,))
        elif settings.word_count == "free":
            check_tracking(index, None, before.words, after.words)

        # Without pairs, an unchanged line must still pass: its closest words set the floor.
        closest = math.inf
        if not paired:
            closest = min(
                (signed_gap(a.box, b.box) for a, b in combinations(before.words, 2)),
                default=math.inf,
            )
        for (i, first), (j, second) in combinations(enumerate(after.words), 2):
            gap = signed_gap(first.box, second.box)
            word_gaps.append(gap)
            if settings.word_gap is None:
                continue
            was = signed_gap(before.words[i].box, before.words[j].box) if paired else closest
            floor = min(settings.word_gap.limit(_word_em(first, second)), was)
            if gap < floor - ROUNDING:
                violations.append(Violation("word-gap", gap, floor, index, i, j))

    ems = [_line_em(line) for line in output]
    for i, j in combinations(range(len(output)), 2):
        gap = signed_gap(output[i].box, output[j].box)
        line_gaps.append(gap)
        if settings.line_gap is None:
            continue
        allowed = settings.line_gap.limit(max(ems[i], ems[j]))
        floor = min(allowed, signed_gap(source[i].box, source[j].box))
        if gap < floor - ROUNDING:
            violations.append(Violation("line-gap", gap, floor, i, other=j))

    return GeometryResult(
        settings,
        tuple(violations),
        max(line_moves, default=None),
        max(word_moves, default=None),
        max(tracking, default=None),
        min(word_gaps, default=None),
        min(line_gaps, default=None),
        changed_counts,
    )


def check_page(source: pikepdf.Page, output: pikepdf.Page, settings: BoxSettings) -> GeometryResult:
    """Compare two pages. Text that the tracker cannot place raises ``UnsupportedText``."""
    return check_boxes(group_lines(trace_page(source)), group_lines(trace_page(output)), settings)
