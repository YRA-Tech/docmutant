"""Text from outlines: replace what a page draws as glyph shapes with paragraphs of new text.

Some producers draw all text as filled paths. The tracker reads text operators, so such a page
has no words to change, and its text stays readable. Under ``privacy``, each outline item becomes
a paragraph of its own: glyph-sized shapes of one colour, side by side on a line, in whatever
order the stream draws them. Nothing links a paragraph to its neighbours, and its words are new,
so they say nothing about the source's beyond the box that the item filled.

The shapes go, and so do the groups that only placed them, because a transform for each letter
records the letter's advance. A paragraph is drawn where the item's last shape was, so that what
covered the shapes still covers it. It cannot take the state that those shapes had, because some
producers clip each short run of letters, which would cut a paragraph down to one run. So the groups
open there close around the paragraph and open again.

A lone glyph stays, because it spells little and a bullet or a ring looks like one. Large shapes
stay too: on the sample they are backgrounds, and the limit of ``coverage.DRAWN_TEXT`` still applies
to the curved shapes that remain.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

import pikepdf

from docmutant.content import Instruction, operator_name
from docmutant.layers import CONSONANTS, VOWELS
from docmutant.mutate import StreamRef, content_streams, read_stream, write_stream
from docmutant.substitute import CannotSubstitute, FontSource, ShownCode, embed_substitute
from docmutant.tracker import Point

Matrix = tuple[float, float, float, float, float, float]

FONT_PREFIX = "DMTEXT+"
"""Starts the name of the font that paragraphs use, so that the word oracles can leave them out."""

FONT_KEY = "/DMText"

LARGEST = 72.0
"""Points. On the sample, shapes of glyphs are almost all under 40 and backgrounds above 150."""

WIDEST = 2.5
"""Width over height. Above this a shape of one subpath is a rule, a bar or a box, not a glyph."""

SMALLEST = 1.0
"""Points. Below this a shape cannot show a glyph that anyone reads."""

WORD_SUBPATHS = 3
"""A lone fill of this many subpaths can be a word. One or two can be a bullet or a ring."""

SIDE_BY_SIDE = 0.25
"""In item heights: how far apart two shapes' centres must be to count as two glyphs, not one."""

RECTANGLE_POINTS = 5
"""The most points that a subpath of a box can have, with a point to close it."""

COMPLEX_POINTS = 6
"""A polygon with this many points can be a letter, such as H or T. A rectangle is not evidence."""

SAME_LINE = 0.5
"""How much of the shorter of two shapes must overlap the other for them to share a line."""

GAP = 1.0
"""The widest gap inside an item, in item heights. Wider means another paragraph."""

TEXT_HEIGHT = 0.93
"""Of the font size, what a line of mixed text covers from its descenders to its ascenders."""

DESCENT = 0.2
"""Of the font size, how far a line's baseline sits above the bottom of what it draws."""

ADVANCE = 0.5
"""Of the font size, the width to allow for each new character."""

SPACE = 278.0
"""Thousandths of the font size: the advance of a space in the paragraphs' font."""

TRIES = 20
"""Attempts at a new word that is not a word of the source."""

_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_PATH = frozenset({"m", "l", "c", "v", "y", "h", "re"})
_CURVES = frozenset({"c", "v", "y"})
_FILLS = frozenset({"f", "F", "f*", "B", "B*", "b", "b*"})
_STROKES = frozenset({"S", "s"})
_COLOURS = frozenset({"g", "rg", "k", "cs", "sc", "scn"})
_PLACE = "DMPARAGRAPH"
"""Stands for a paragraph while groups are cleaned up. Paints, as far as they can tell."""

_TEXT_STATE = frozenset({"Tc", "Tw", "Tz", "TL", "Tf", "Tr", "Ts"})
_STATE = frozenset(
    {"cm", "w", "J", "j", "M", "d", "ri", "i", "gs", "g", "rg", "k", "cs", "sc", "scn"}
    | {"G", "RG", "K", "CS", "SC", "SCN", "W", "W*", "n"}
    | _PATH
)
"""What a group holds when it paints nothing."""


def multiply(first: Matrix, second: Matrix) -> Matrix:
    a, b, c, d, e, f = first
    g, h, i, j, k, m = second
    return (
        a * g + b * i,
        a * h + b * j,
        c * g + d * i,
        c * h + d * j,
        e * g + f * i + k,
        e * h + f * j + m,
    )


def inverse_matrix(matrix: Matrix) -> Matrix | None:
    a, b, c, d, e, f = matrix
    det = a * d - b * c
    if det == 0:
        return None
    return (
        d / det,
        -b / det,
        -c / det,
        a / det,
        (c * f - d * e) / det,
        (b * e - a * f) / det,
    )


def _rotation(turn: int) -> Matrix:
    cos, sin = math.cos(math.radians(turn)), math.sin(math.radians(turn))
    return (cos, sin, -sin, cos, 0.0, 0.0)


def apply_matrix(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


@dataclass
class _Shape:
    """One filled path, measured along the direction its transform writes in."""

    drops: tuple[int, ...]
    """Its path operators and its paint, which need not be next to each other."""

    paint: int
    box: tuple[float, float, float, float]
    turn: int
    colour: tuple[Instruction, ...]
    curves: int
    subpaths: int
    points: int

    @property
    def rectangles(self) -> bool:
        """Only straight boxes: a single fill can mask several places at once."""
        return not self.curves and self.points <= RECTANGLE_POINTS * self.subpaths

    @property
    def colour_key(self) -> tuple[tuple[str, ...], ...]:
        return tuple((operator_name(i), *map(str, i.operands)) for i in self.colour)


@dataclass
class _Item:
    shapes: list[_Shape]
    box: tuple[float, float, float, float] = field(init=False)

    def __post_init__(self) -> None:
        self._measure()

    def _measure(self) -> None:
        boxes = [shape.box for shape in self.shapes]
        self.box = (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )

    def absorb(self, shapes: Iterable[_Shape]) -> None:
        self.shapes.extend(shapes)
        self.shapes.sort(key=lambda entry: entry.paint)
        self._measure()

    def takes(self, shape: _Shape) -> bool:
        first = self.shapes[0]
        if first.turn != shape.turn or first.colour_key != shape.colour_key:
            return False
        x0, y0, x1, y1 = self.box
        height = y1 - y0
        overlap = min(y1, shape.box[3]) - max(y0, shape.box[1])
        shortest = min(height, shape.box[3] - shape.box[1])
        return (
            overlap >= SAME_LINE * shortest
            and shape.box[0] <= x1 + GAP * height
            and shape.box[2] >= x0 - GAP * height
        )

    def looks_like_text(self) -> bool:
        """A lone glyph spells little, and a bullet, a ring or a check box looks like one."""
        if any(s.subpaths >= WORD_SUBPATHS and not s.rectangles for s in self.shapes):
            return True
        x0, y0, x1, y1 = self.box
        centres = [(shape.box[0] + shape.box[2]) / 2 for shape in self.shapes]
        if max(centres) - min(centres) <= SIDE_BY_SIDE * (y1 - y0):
            return False
        return any(s.curves or s.points >= COMPLEX_POINTS for s in self.shapes)


def _points(operator: str, operands: Sequence[float]) -> list[tuple[float, float]]:
    if operator == "re":
        x, y, width, height = operands
        return [(x, y), (x + width, y + height)]
    return list(zip(operands[::2], operands[1::2], strict=True))


def _items(instructions: Sequence[Instruction], unit: float) -> list[_Item]:
    """The outline items of a content stream. ``unit`` is the points that one of its units takes."""
    items: list[_Item] = []

    def add(shape: _Shape) -> None:
        # Producers need not draw in reading order, so a shape can join items already apart.
        near = [item for item in items if item.takes(shape)]
        if not near:
            items.append(_Item([shape]))
            return
        near[0].absorb([shape])
        for other in near[1:]:
            near[0].absorb(other.shapes)
            items.remove(other)

    ctm = _IDENTITY
    colour: tuple[Instruction, ...] = ()
    stack: list[tuple[Matrix, tuple[Instruction, ...]]] = []
    points: list[tuple[float, float]] = []
    path: list[int] = []
    curves = subpaths = 0
    clipping = False

    def reset() -> None:
        nonlocal points, path, curves, subpaths, clipping
        points, path, curves, subpaths, clipping = [], [], 0, 0, False

    for index, instruction in enumerate(instructions):
        operator = operator_name(instruction)
        if operator == "q":
            stack.append((ctm, colour))
        elif operator == "Q":
            if stack:
                ctm, colour = stack.pop()
        elif operator == "cm":
            matrix = tuple(float(value) for value in instruction.operands)
            ctm = multiply(matrix, ctm)
        elif operator in _COLOURS:
            # A colour in a named space needs the operator that chose the space.
            space = colour[:1] if operator in ("sc", "scn") else ()
            colour = (*(i for i in space if operator_name(i) == "cs"), instruction)
        elif operator in _PATH:
            path.append(index)
            if operator != "h":
                points.extend(_points(operator, [float(v) for v in instruction.operands]))
            curves += operator in _CURVES
            subpaths += operator in ("m", "re")
        elif operator in ("W", "W*"):
            clipping = True
        elif operator in ("n", *_STROKES):
            reset()
        elif operator in _FILLS:
            if points and not clipping:
                shape = _measure((*path, index), index, points, ctm, colour, curves, subpaths)
                if shape is not None and _glyph_sized(shape, unit):
                    add(shape)
            reset()
    return [item for item in items if item.looks_like_text()]


def _measure(
    drops: tuple[int, ...],
    paint: int,
    points: Sequence[tuple[float, float]],
    ctm: Matrix,
    colour: tuple[Instruction, ...],
    curves: int,
    subpaths: int,
) -> _Shape | None:
    if inverse_matrix(ctm) is None:
        return None
    turn = round(math.degrees(math.atan2(ctm[1], ctm[0]))) % 360
    back = _rotation(-turn)
    placed = [apply_matrix(back, *apply_matrix(ctm, x, y)) for x, y in points]
    xs, ys = zip(*placed, strict=True)
    box = (min(xs), min(ys), max(xs), max(ys))
    return _Shape(drops, paint, box, turn, colour, curves, subpaths, len(points))


def _glyph_sized(shape: _Shape, unit: float) -> bool:
    width = (shape.box[2] - shape.box[0]) * unit
    height = (shape.box[3] - shape.box[1]) * unit
    # A word can be one fill of many subpaths, and so wider than any glyph.
    if height >= LARGEST:
        return False
    if (shape.subpaths <= 1 or shape.rectangles) and (width >= LARGEST or width > WIDEST * height):
        return False
    return max(width, height) >= SMALLEST


def _scales(pdf: pikepdf.Pdf) -> dict[tuple[int, int], float]:
    """The points that one unit of each form takes where a page first draws it."""
    scales: dict[tuple[int, int], float] = {}

    def visit(stream: pikepdf.Object, resources: pikepdf.Object | None, outer: Matrix) -> None:
        ctm = outer
        stack: list[Matrix] = []
        xobjects = resources.get("/XObject") if resources is not None else None
        for instruction in pikepdf.parse_content_stream(stream):
            operator = operator_name(instruction)
            if operator == "q":
                stack.append(ctm)
            elif operator == "Q":
                if stack:
                    ctm = stack.pop()
            elif operator == "cm":
                ctm = multiply(tuple(float(value) for value in instruction.operands), ctm)
            elif operator == "Do" and xobjects is not None:
                form = xobjects.get(instruction.operands[0])
                if (
                    isinstance(form, pikepdf.Stream)
                    and form.get("/Subtype") == "/Form"
                    and form.objgen not in scales
                ):
                    matrix = tuple(float(value) for value in form.get("/Matrix", _IDENTITY))
                    placed = multiply(matrix, ctm)
                    scales[form.objgen] = math.sqrt(
                        abs(placed[0] * placed[3] - placed[1] * placed[2])
                    )
                    visit(form, form.get("/Resources", resources), placed)

    for page in pdf.pages:
        visit(page, page.obj.get("/Resources"), _IDENTITY)
    return scales


def drawing_streams(pdf: pikepdf.Pdf) -> Iterator[tuple[StreamRef, float]]:
    """Each stream that can hold an outline item. A glyph's own procedure belongs to its font."""
    scales = _scales(pdf)
    for ref in content_streams(pdf):
        if ref.kind != "charproc":
            yield ref, scales.get(ref.objgen, 1.0) or 1.0


def outline_items(pdf: pikepdf.Pdf) -> dict[str, int]:
    """How many outline items each content stream holds. Streams with none are not listed."""
    found: dict[str, int] = {}
    for ref, unit in drawing_streams(pdf):
        count = len(_items(read_stream(pdf, ref), unit))
        if count:
            found[ref.label()] = count
    return found


def outline_quads(
    pdf: pikepdf.Pdf,
) -> list[tuple[StreamRef, tuple[Point, Point, Point, Point]]]:
    """The place of each outline item, in the frame of the stream that draws it."""
    quads = []
    for ref, unit in drawing_streams(pdf):
        for item in _items(read_stream(pdf, ref), unit):
            x0, y0, x1, y1 = item.box
            turn = _rotation(item.shapes[0].turn)
            quads.append(
                (
                    ref,
                    (
                        apply_matrix(turn, x0, y0),
                        apply_matrix(turn, x1, y0),
                        apply_matrix(turn, x1, y1),
                        apply_matrix(turn, x0, y1),
                    ),
                )
            )
    return quads


class _Words:
    """New words: from the vocabulary when it has one of the length, or else invented."""

    def __init__(self, seed: int, vocabulary: Iterable[str], avoid: Iterable[str]) -> None:
        self.rng = random.Random(f"{seed}:outlines")
        self.avoid = {word.casefold() for word in avoid}
        self.by_length: dict[int, list[str]] = {}
        for word in sorted(set(vocabulary)):
            if word.isascii() and word.isalpha() and word.casefold() not in self.avoid:
                self.by_length.setdefault(len(word), []).append(word)

    def _invented(self, length: int) -> str:
        new = ""
        for _ in range(TRIES):
            pools = (CONSONANTS, VOWELS) if self.rng.random() < 0.7 else (VOWELS, CONSONANTS)
            new = "".join(self.rng.choice(pools[i % 2]) for i in range(length))
            if new not in self.avoid:
                break
        return new

    def line(self, characters: int) -> str:
        """About this many characters of words. The source's word breaks are not known here."""
        words: list[str] = []
        left = max(1, characters)
        while left > 0:
            length = min(self.rng.randint(2, 9), left)
            known = self.by_length.get(length)
            words.append(self.rng.choice(known) if known else self._invented(length))
            left -= length + 1
        return " ".join(words)


@dataclass(frozen=True)
class OutlineReport:
    items: int
    paragraphs_by_stream: dict[str, int]


class _Font:
    """The font of the paragraphs, embedded once."""

    def __init__(self, pdf: pikepdf.Pdf, source: FontSource) -> None:
        font = pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.TrueType,
            BaseFont=pikepdf.Name.Helvetica,
            FirstChar=32,
            LastChar=32,
            Widths=[SPACE],
        )
        self.font = pdf.make_indirect(font)
        substitute = source.pick(self.font)
        if substitute is None:
            raise CannotSubstitute("no open font to draw the paragraphs in")
        embedded = embed_substitute(pdf, self.font, substitute, {32: ShownCode(1, " ", SPACE)})
        name = pikepdf.Name("/" + FONT_PREFIX + substitute.name.replace("-", ""))
        self.font.BaseFont = name
        self.font.FontDescriptor.FontName = name
        self.codes = {char: (code, width) for char, (code, width) in embedded.added.items()}
        if not set(VOWELS + CONSONANTS) <= set(self.codes):
            raise CannotSubstitute(f"{substitute.name} cannot draw every letter of a paragraph")
        self.codes[" "] = (b" ", SPACE)

    def encode(self, text: str) -> tuple[bytes, float]:
        """The codes of this text, and its width at a size of 1."""
        data = b"".join(self.codes[char][0] for char in text)
        return data, sum(self.codes[char][1] for char in text) / 1000


def _instruction(operator: str, *operands: object) -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction(list(operands), pikepdf.Operator(operator))


def _paragraph(item: _Item, font: _Font, words: _Words, inverse: Matrix) -> list[Instruction]:
    """A paragraph that fills the item's box. ``inverse`` undoes the transform where it is drawn."""
    x0, y0, x1, y1 = item.box
    first = item.shapes[0]
    size = (y1 - y0) / TEXT_HEIGHT
    width = x1 - x0
    text = words.line(round(width / (ADVANCE * size)) if size > 0 else 1)
    data, natural = font.encode(text)
    stretch = 100 * width / (natural * size) if natural > 0 and size > 0 else 100
    origin = apply_matrix(_rotation(first.turn), x0, y0 + DESCENT * size)
    placed = (*_rotation(first.turn)[:4], *origin)
    return [
        _instruction("q"),
        *(first.colour or (_instruction("g", 0),)),
        _instruction("BMC", pikepdf.Name.P),
        _instruction("BT"),
        _instruction("Tf", pikepdf.Name(FONT_KEY), size),
        _instruction("Tz", stretch),
        _instruction("Tc", 0),
        _instruction("Tw", 0),
        _instruction("Ts", 0),
        _instruction("Tr", 0),
        _instruction("Tm", *multiply(placed, inverse)),
        _instruction("Tj", pikepdf.String(data)),
        _instruction("ET"),
        _instruction("EMC"),
        _instruction("Q"),
    ]


def without_empty_groups(instructions: Sequence[Instruction]) -> list[Instruction]:
    """Drop each group that paints nothing, and with it the place that it recorded.

    A ``BDC`` stays, because structure can point at its marked content, and so does what holds
    it. A ``q`` and a marked-content sequence may overlap: once they do, nothing is dropped
    until the groups open then have closed.
    """
    output: list[Instruction] = []
    opened: list[tuple[str, int]] = []
    closes = {"Q": "q", "EMC": "BMC"}
    for instruction in instructions:
        operator = operator_name(instruction)
        if operator in ("q", "BMC", "BDC"):
            opened.append((operator, len(output)))
        elif operator in closes and opened:
            kind, start = opened.pop()
            matched = kind == closes[operator]
            if matched and all(operator_name(entry) in _STATE for entry in output[start + 1 :]):
                del output[start:]
                continue
            if not matched and not (kind == "BDC" and operator == "EMC"):
                opened.clear()
        output.append(instruction)
    return output


def _resources(pdf: pikepdf.Pdf, ref: StreamRef) -> pikepdf.Object | None:
    if ref.kind == "page":
        holder = pdf.pages[ref.page_index].obj
        if holder.get("/Resources") is None:
            holder.Resources = pikepdf.Dictionary()
        return holder.Resources
    # A form without its own resources takes a page's, which it may not share with another.
    return pdf.get_object(ref.objgen).get("/Resources")


def _placed(
    instructions: Sequence[Instruction], items: Sequence[_Item], font: _Font, words: _Words
) -> list[Instruction]:
    """Put each paragraph at its place, outside the groups open there, which then open again."""
    output: list[Instruction] = []
    frames: list[list[Instruction]] = []
    ctm = _IDENTITY
    path: list[Instruction] = []
    for instruction in instructions:
        operator = operator_name(instruction)
        if operator == _PLACE:
            inverse = inverse_matrix(ctm)
            if inverse is None:
                raise CannotSubstitute("a paragraph's place has a transform that cannot be undone")
            output.extend(_instruction("Q") for _ in frames)
            output.extend(_paragraph(items[int(instruction.operands[0])], font, words, inverse))
            for frame in frames:
                output.append(_instruction("q"))
                output.extend(frame)
            continue
        output.append(instruction)
        if operator == "q":
            frames.append([])
        elif operator == "Q":
            if frames:
                frames.pop()
        elif operator == "cm" and not frames:
            ctm = multiply(tuple(float(value) for value in instruction.operands), ctm)
        elif operator in _PATH or operator in ("W", "W*"):
            path.append(instruction)
        elif operator == "n":
            # Only a clip outlasts its path.
            if frames and any(operator_name(entry) in ("W", "W*") for entry in path):
                frames[-1].extend((*path, instruction))
            path = []
        elif operator in _FILLS or operator in _STROKES:
            path = []
        elif frames and (operator in _STATE or operator in _TEXT_STATE):
            frames[-1].append(instruction)
    return output


def text_from_outlines(
    pdf: pikepdf.Pdf,
    source: FontSource,
    seed: int,
    vocabulary: Iterable[str],
    avoid: Iterable[str],
) -> OutlineReport:
    """Replace each outline item with a paragraph of new words. See the module's description."""
    words = _Words(seed, vocabulary, avoid)
    font: _Font | None = None
    paragraphs: dict[str, int] = {}
    for ref, unit in drawing_streams(pdf):
        instructions = read_stream(pdf, ref)
        items = _items(instructions, unit)
        resources = _resources(pdf, ref) if items else None
        if resources is None:
            continue
        if font is None:
            font = _Font(pdf, source)
        if resources.get("/Font") is None:
            resources.Font = pikepdf.Dictionary()
        resources.Font[FONT_KEY] = font.font

        # On top of each of its shapes, as they were on top of what came before them.
        last = {item.shapes[-1].paint: number for number, item in enumerate(items)}
        gone = {index for item in items for shape in item.shapes for index in shape.drops}
        marked: list[Instruction] = []
        for index, entry in enumerate(instructions):
            if index in last:
                marked.append(_instruction(_PLACE, last[index]))
            elif index not in gone:
                marked.append(entry)
        placed = _placed(without_empty_groups(marked), items, font, words)
        write_stream(pdf, ref, without_empty_groups(placed))
        paragraphs[ref.label()] = len(items)
    return OutlineReport(sum(paragraphs.values()), paragraphs)
