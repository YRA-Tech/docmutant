"""Displayed formulas: under ``privacy``, a patterned block in place of each one's glyphs.

A displayed formula says which paper it comes from as surely as its title, and swapping its
letters keeps its shape: a fraction over a sum, its subscripts and brackets. Nor do its letters
swap well: a math font holds the few letters that the formula uses, and the parts of a fraction
are lines of their own, which join when anything in them moves. So its glyphs go, and a light
block with a pattern of its own covers where it was. The pattern says "formula" to whoever knows
it, without any text: light plus signs on a staggered grid, as ``formula_pattern`` draws them.

A formula line is one whose glyphs are math for ``MATH_SHARE`` or more, at least ``MATH_GLYPHS``
of them: math letters, Greek letters, operators and arrows, or glyphs of a math or symbol font. A
line of text with a symbol or two in it is not. Lines whose bands overlap a formula line's, or
come within ``JOIN`` of it, go with it: the parts of a fraction, a large bracket, an equation's
number. In a line of text, a word whose glyphs are half math or more is a formula of its own,
with a small block: a font of math may hold too few letters to swap. Only formulas whose glyphs
are all in the page's own stream go, since a form can be drawn elsewhere too.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import pikepdf

from docmutant.fit import remove_words
from docmutant.tracker import Glyph, Line, Word, group_lines

Box = tuple[float, float, float, float]

MATH_SHARE = 0.4
"""Of a line's glyphs. On the sample, formula lines are half math or more, and text lines with
inline symbols a tenth or less."""

MATH_GLYPHS = 2

INLINE_SHARE = 0.5
"""Of a word's glyphs in a line of text, for it to be a formula of its own, with a letter of
math among them: a Greek letter with its subscript is, a word with a symbol after it is not,
and nor is an arrow used as a bullet."""

BAND = (0.2, 0.7)
"""Ems below and above a baseline that a line's letters fill: where lines that overlap belong to
one formula, and where lines next to a formula are not to be covered."""

JOIN = 1.0
"""Ems across which lines that share a band are one formula: a fraction's parts sit beside the
rest of its equation."""

REACH = (0.3, 0.9)
"""Ems below and above a baseline that a block covers, for descenders, accents and limits."""

SAME_PLACE = 0.05
"""Points: a glyph this close to where a removed one started, with its text, is that glyph."""

PATTERN_KEY = "/DMFormula"
TILE = 12.0
"""Points: the pattern repeats every tile, a plus sign at a quarter and at three quarters."""

LIGHT, MARK = 0.9, 0.78
"""Greys of the block and of its plus signs: lighter than text, and darker than white by less than
the render difference's limit for what an image may darken."""

_MATH_FONT = re.compile(r"math|symbol|cmmi|cmsy|cmex|stix|euler|msam|msbm|mtextra", re.IGNORECASE)


def _math_text(text: str) -> bool:
    return any(
        0x1D400 <= ord(char) <= 0x1D7FF
        or 0x0391 <= ord(char) <= 0x03C9
        or 0x2190 <= ord(char) <= 0x21FF
        or 0x2200 <= ord(char) <= 0x22FF
        or 0x27C0 <= ord(char) <= 0x2AFF
        or char == "�"
        or unicodedata.category(char) == "Mn"
        for char in text
    )


def _is_math(glyph: Glyph, font_names: Mapping[tuple[int, int], str]) -> bool:
    return _math_text(glyph.text or "") or bool(
        _MATH_FONT.search(font_names.get(glyph.font_key, ""))
    )


def _is_math_letter(glyph: Glyph, font_names: Mapping[tuple[int, int], str]) -> bool:
    """A letter of a formula: of the math alphabets, Greek, or a letter of a math font. An arrow or
    an operator alone is not: text uses them as bullets and signs."""
    text = glyph.text or ""
    return any(
        0x1D400 <= ord(char) <= 0x1D7FF or 0x0391 <= ord(char) <= 0x03C9 for char in text
    ) or (
        any(char.isalpha() for char in text)
        and bool(_MATH_FONT.search(font_names.get(glyph.font_key, "")))
    )


def _band(glyphs: Iterable[Glyph], below: float, above: float, beside: float = 0.0) -> Box:
    """The box, in default user space, of what these glyphs fill from ``below`` to ``above`` ems,
    and ``beside`` ems before and after."""
    xs: list[float] = []
    ys: list[float] = []
    for glyph in glyphs:
        ux, uy = glyph.direction
        nx, ny = -uy, ux
        x, y = glyph.origin
        for along in (-beside * glyph.em, glyph.advance + beside * glyph.em):
            for across in (-below * glyph.em, above * glyph.em):
                xs.append(x + along * ux + across * nx)
                ys.append(y + along * uy + across * ny)
    return (min(xs), min(ys), max(xs), max(ys))


def _overlap(first: Box, second: Box) -> bool:
    return (
        first[0] < second[2]
        and second[0] < first[2]
        and first[1] < second[3]
        and second[1] < first[3]
    )


@dataclass(frozen=True)
class Formula:
    lines: tuple[int, ...]
    """The lines of its page that it fills, by their place in drawing order: none for a formula
    inside a line of text."""

    words: tuple[Word, ...]
    """What goes from the page."""

    box: Box
    """Where its block goes, in default user space."""


def _glyphs(line: Line) -> list[Glyph]:
    return [glyph for word in line.words for glyph in word.glyphs if not glyph.is_space]


def font_names(pdf: pikepdf.Pdf) -> dict[tuple[int, int], str]:
    return {
        obj.objgen: str(obj.get("/BaseFont", ""))
        for obj in pdf.objects
        if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == "/Font"
    }


def _share(glyphs: Sequence[Glyph], names: Mapping[tuple[int, int], str]) -> tuple[int, float]:
    math = sum(_is_math(glyph, names) for glyph in glyphs)
    return math, math / len(glyphs) if glyphs else 0.0


def find_formulas(lines: Sequence[Line], names: Mapping[tuple[int, int], str]) -> list[Formula]:
    """The formulas among a page's lines: displayed ones, and words of math in a line of text."""
    bands = [_band(_glyphs(line), *BAND) if _glyphs(line) else None for line in lines]
    joins = [_band(_glyphs(line), *BAND, JOIN) if _glyphs(line) else None for line in lines]
    seeds = set()
    for number, line in enumerate(lines):
        math, share = _share(_glyphs(line), names)
        if math >= MATH_GLYPHS and share >= MATH_SHARE:
            seeds.add(number)

    # Lines whose bands overlap a formula line's join it, and so do the lines that overlap those.
    groups: list[set[int]] = []
    for seed in sorted(seeds):
        if any(seed in group for group in groups):
            continue
        group, frontier = {seed}, [seed]
        while frontier:
            current = frontier.pop()
            for other, band in enumerate(joins):
                if other not in group and band is not None and _overlap(joins[current], band):
                    group.add(other)
                    frontier.append(other)
        groups.append(group)

    formulas = []
    for group in groups:
        glyphs = [glyph for number in group for glyph in _glyphs(lines[number])]
        if any(glyph.stream.kind != "page" for glyph in glyphs):
            continue
        box = _trimmed(
            _band(glyphs, *REACH),
            [band for n, band in enumerate(bands) if n not in group and band is not None],
        )
        words = tuple(word for number in sorted(group) for word in lines[number].words)
        formulas.append(Formula(tuple(sorted(group)), words, box))

    # A word of math in a line of text, such as a variable with its subscript.
    taken = {number for group in groups for number in group}
    for number, line in enumerate(lines):
        if number in taken:
            continue
        for word in line.words:
            glyphs = [glyph for glyph in word.glyphs if not glyph.is_space]
            math, share = _share(glyphs, names)
            if (
                share < INLINE_SHARE
                or not any(_is_math_letter(glyph, names) for glyph in glyphs)
                or any(glyph.stream.kind != "page" for glyph in glyphs)
            ):
                continue
            others = [band for n, band in enumerate(bands) if n != number and band is not None]
            formulas.append(Formula((), (word,), _trimmed(_band(glyphs, *REACH), others)))
    return formulas


def _trimmed(box: Box, others: Iterable[Box]) -> Box:
    """A block's box, cut back so that it covers no other line's letters."""
    left, bottom, right, top = box
    middle = (bottom + top) / 2
    for other in others:
        if other[0] >= right or other[2] <= left or other[1] >= top or other[3] <= bottom:
            continue
        if (other[1] + other[3]) / 2 > middle:
            top = min(top, other[1])
        else:
            bottom = max(bottom, other[3])
    return (left, bottom, right, max(top, bottom))


def formula_pattern(pdf: pikepdf.Pdf) -> pikepdf.Object:
    """The tiling pattern of a formula's block, new in ``pdf``."""

    def plus(x: float, y: float) -> bytes:
        arm, thick = 1.6, 0.6
        return b"%g %g %g %g re %g %g %g %g re f " % (
            x - arm,
            y - thick / 2,
            2 * arm,
            thick,
            x - thick / 2,
            y - arm,
            thick,
            2 * arm,
        )

    quarter = TILE / 4
    content = (
        b"%g g 0 0 %g %g re f %g g " % (LIGHT, TILE, TILE, MARK)
        + plus(quarter, quarter)
        + plus(3 * quarter, 3 * quarter)
    )
    return pdf.make_indirect(
        pdf.make_stream(
            content,
            PatternType=1,
            PaintType=1,
            TilingType=1,
            BBox=[0, 0, TILE, TILE],
            XStep=TILE,
            YStep=TILE,
            Resources=pikepdf.Dictionary(),
        )
    )


def remove_formulas(pdf: pikepdf.Pdf, formulas: Mapping[int, Sequence[Formula]]) -> int:
    """Take out the glyphs of each formula, before anything else changes the page. Returns how
    many formulas there were."""
    count = 0
    for _, found in sorted(formulas.items()):
        words = [word for formula in found for word in formula.words]
        if words:
            remove_words(pdf, words)
        count += len(found)
    return count


def draw_formulas(pdf: pikepdf.Pdf, formulas: Mapping[int, Sequence[Formula]]) -> None:
    """Draw each formula's block over its page, after every other step: a graphic that touched a
    block would otherwise take it into its own plain one."""
    pattern = None
    for index, found in sorted(formulas.items()):
        if not found:
            continue
        page = pdf.pages[index].obj
        pattern = pattern if pattern is not None else formula_pattern(pdf)
        if page.get("/Resources") is None:
            page.Resources = pikepdf.Dictionary()
        if page.Resources.get("/Pattern") is None:
            page.Resources.Pattern = pikepdf.Dictionary()
        page.Resources.Pattern[PATTERN_KEY] = pattern
        blocks = b"".join(
            b"q /Pattern cs %s scn %g %g %g %g re f Q\n"
            % (PATTERN_KEY.encode(), left, bottom, right - left, top - bottom)
            for left, bottom, right, top in (formula.box for formula in found)
        )
        # Over everything the page draws, outside any state that it leaves behind.
        pdf.pages[index].contents_add(pdf.make_stream(b"q\n"), prepend=True)
        pdf.pages[index].contents_add(pdf.make_stream(b"\nQ\n" + blocks))


def glyph_key(glyph: Glyph) -> tuple[object, ...]:
    """Where a glyph's code is in its stream: the same in every trace of the same bytes."""
    return (glyph.stream, glyph.instruction, glyph.operand, glyph.offset)


def without_formulas(glyphs: Iterable[Glyph], formulas: Sequence[Formula]) -> list[Line]:
    """A page's lines as they are once its formulas are gone, grouped as the output's are."""
    gone = {
        glyph_key(glyph) for formula in formulas for word in formula.words for glyph in word.glyphs
    }
    return group_lines([glyph for glyph in glyphs if glyph_key(glyph) not in gone])


def glyphs_left(lines: Sequence[Line], formulas: Sequence[Formula]) -> int:
    """Glyphs of a saved page that a formula should have taken: one that starts in a block, or one
    with the text of a removed glyph where that glyph was. A block can stop short of a glyph of its
    formula, so the place of each glyph counts as well as the block."""
    removed: dict[str | None, list[tuple[float, float]]] = {}
    for formula in formulas:
        for word in formula.words:
            for glyph in word.glyphs:
                if not glyph.is_space:
                    removed.setdefault(glyph.text, []).append(glyph.origin)

    def left(glyph: Glyph) -> bool:
        x, y = glyph.origin
        return any(
            box[0] <= x <= box[2] and box[1] <= y <= box[3] for box in (f.box for f in formulas)
        ) or any(
            abs(x - ox) <= SAME_PLACE and abs(y - oy) <= SAME_PLACE
            for ox, oy in removed.get(glyph.text, ())
        )

    return sum(1 for line in lines for glyph in _glyphs(line) if left(glyph))
