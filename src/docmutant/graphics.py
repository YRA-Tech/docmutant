"""Graphics drawn as paths: a logo, an icon on a badge, an emblem.

A logo says whose document it is, whatever its words say. An icon is harmless, but the symbol on
its badge looks like a short word to ``outlines``, which made a paragraph of two letters of it.
Under ``privacy``, each compact graphic becomes a flat, light block of its size, as an image does.

A graphic is a group of marks that touch: fills, strokes and shadings, with the images among them.
Fills, strokes and shadings in two colours or more must draw it, a shading counting as one, and it
must fit in ``LARGEST``. These stay as they are:

- What crosses a line of text: a block must not hide the source's words, which the run changes
  where they are, and a button keeps its label.
- A mark larger than ``LARGEST``, a card or a background, joins nothing. Neither does a rule, so
  that the line linking a column of icons does not join them into one tall group.
- A group larger than ``LARGEST``, such as a table, a banner or a chart, or one smaller than
  ``SMALLEST``, such as a check box or a bullet.
- A group of one colour, which is how a word is drawn, or a logo of one colour.
- What a soft mask or a pattern draws: the one is a shape, not a picture, the other is repeated.

Outline items as wide as words do not join a graphic, but a symbol does. Nor do they keep one as it
is: each becomes a paragraph of new words, and one drawn on a graphic stays on its block. Images in
a graphic stay, because each one is flat already, and they still show on the block.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

import pikepdf

from docmutant.content import Instruction, operator_name
from docmutant.layers import soft_mask_forms
from docmutant.mutate import StreamRef, read_stream, write_stream
from docmutant.outlines import (
    Matrix,
    apply_matrix,
    drawing_streams,
    inverse_matrix,
    item_places,
    multiply,
    place,
    placed_outside_groups,
    without_empty_groups,
)
from docmutant.tracker import Line, Point

Box = tuple[float, float, float, float]
Quad = tuple[Point, Point, Point, Point]

LARGEST = 144.0
"""Points, two inches. Logos and icons on the sample are under 55; cards and tables are wider."""

SMALLEST = 12.0
"""Points. Below this, check boxes and bullets of two colours; icons on the sample are 26."""

RULE = 1.5
"""Points. A straight mark no thicker than this is a rule or a border."""

TOUCH = 2.0
"""Points. Marks closer than this are one graphic."""

SYMBOL = 1.25
"""Width over height. An outline item no wider than this is a symbol: words on the sample are 1.4
times as wide as they are tall or more, and icons and emblems 1.1 or less."""

SHADE = 0.9
"""The grey of a block, as light as a flat image."""

_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_UNIT_SQUARE: Quad = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
_PATH = frozenset({"m", "l", "c", "v", "y", "h", "re"})
_CURVES = frozenset({"c", "v", "y"})
_FILLS = frozenset({"f", "F", "f*"})
_STROKES = frozenset({"S", "s"})
_BOTH = frozenset({"B", "B*", "b", "b*"})
_FILL_COLOURS = frozenset({"g", "rg", "k", "cs", "sc", "scn"})
_STROKE_COLOURS = frozenset({"G", "RG", "K", "CS", "SC", "SCN"})


def placements(
    page: pikepdf.Page,
) -> tuple[dict[tuple[int, int] | None, set[Matrix]], dict[tuple[int, int], set[Matrix]]]:
    """Where a page draws each image and each form, including those in soft masks.

    Images are by their object, and inline images under None.
    """
    images: dict[tuple[int, int] | None, set[Matrix]] = defaultdict(set)
    forms: dict[tuple[int, int], set[Matrix]] = {}

    def form(
        stream: pikepdf.Object,
        resources: pikepdf.Object | None,
        ctm: Matrix,
        chain: frozenset[tuple[int, int]],
    ) -> None:
        placed = multiply(tuple(float(value) for value in stream.get("/Matrix", _IDENTITY)), ctm)
        here = forms.setdefault(stream.objgen, set())
        if stream.objgen not in chain and placed not in here:
            here.add(placed)
            visit(stream, stream.get("/Resources", resources), placed, chain | {stream.objgen})

    def visit(
        stream: pikepdf.Object,
        resources: pikepdf.Object | None,
        outer: Matrix,
        chain: frozenset[tuple[int, int]],
    ) -> None:
        ctm = outer
        stack: list[Matrix] = []
        xobjects = resources.get("/XObject") if resources is not None else None
        states = resources.get("/ExtGState") if resources is not None else None
        for instruction in pikepdf.parse_content_stream(stream):
            operator = operator_name(instruction)
            if operator == "q":
                stack.append(ctm)
            elif operator == "Q":
                if stack:
                    ctm = stack.pop()
            elif operator == "cm":
                ctm = multiply(tuple(float(value) for value in instruction.operands), ctm)
            elif operator == "INLINE IMAGE":
                images[None].add(ctm)
            elif operator == "gs" and states is not None:
                state = states.get(instruction.operands[0])
                mask = state.get("/SMask") if state is not None else None
                if isinstance(mask, pikepdf.Dictionary) and "/G" in mask:
                    form(mask["/G"], resources, ctm, chain)
            elif operator == "Do" and xobjects is not None:
                inner = xobjects.get(instruction.operands[0])
                if not isinstance(inner, pikepdf.Stream):
                    continue
                if inner.get("/Subtype") == "/Image":
                    images[inner.objgen].add(ctm)
                elif inner.get("/Subtype") == "/Form":
                    form(inner, resources, ctm, chain)

    visit(page, page.obj.get("/Resources"), _IDENTITY, frozenset())
    return dict(images), forms


def placed(matrix: Matrix, quad: Quad) -> Quad:
    return tuple(apply_matrix(matrix, x, y) for x, y in quad)


def _box(quad: Sequence[Point]) -> Box:
    xs, ys = zip(*quad, strict=True)
    return (min(xs), min(ys), max(xs), max(ys))


def _crosses(first: Box, second: Box) -> bool:
    return (
        first[0] < second[2]
        and second[0] < first[2]
        and first[1] < second[3]
        and second[1] < first[3]
    )


@dataclass(frozen=True)
class TextPlaces:
    """Where each page of a source shows lines of text, in default user space. Found once, so that
    a run, its regions and its check agree."""

    pages: Mapping[int, tuple[Box, ...]]
    forms: Mapping[tuple[int, int], tuple[tuple[int, Matrix], ...]]
    """Each page that draws a form, and where."""

    def in_stream(self, ref: StreamRef) -> list[Box]:
        """The places in the frame of a stream, as boxes there."""
        if ref.kind == "page":
            return list(self.pages.get(ref.page_index, ()))
        found = []
        for index, matrix in self.forms.get(ref.objgen, ()):
            inverse = inverse_matrix(matrix)
            if inverse is None:
                continue
            for box in self.pages.get(index, ()):
                corners = ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))
                found.append(_box(placed(inverse, corners)))
        return found


def text_places(pdf: pikepdf.Pdf, lines: Mapping[int, Sequence[Line]]) -> TextPlaces:
    """The text of ``pdf``, whose ``lines`` are these, page by page."""
    pages: dict[int, list[Box]] = {
        index: [_box(line.box.corners) for line in lines.get(index, ())]
        for index in range(len(pdf.pages))
    }
    forms: dict[tuple[int, int], list[tuple[int, Matrix]]] = defaultdict(list)
    for index, page in enumerate(pdf.pages):
        for objgen, matrices in placements(page)[1].items():
            forms[objgen].extend((index, matrix) for matrix in sorted(matrices))
    return TextPlaces(
        {index: tuple(boxes) for index, boxes in pages.items()},
        {objgen: tuple(where) for objgen, where in forms.items()},
    )


@dataclass(frozen=True)
class _Mark:
    box: Box
    """In the frame of its stream."""

    colour: tuple[object, ...]
    drops: tuple[int, ...]
    """The operators that draw it. Nothing for an image, which stays."""

    paint: int


def _marks(
    instructions: Sequence[Instruction],
    unit: float,
    words: frozenset[int],
    images: frozenset[str],
) -> list[_Mark]:
    """What a stream paints that can be part of a graphic. ``unit`` is the points that one unit of
    the stream takes, ``words`` paint outline items that are words, and ``images`` are the names
    of the stream's images."""
    marks: list[_Mark] = []
    ctm = _IDENTITY
    fill: tuple[str, ...] = ()
    stroke: tuple[str, ...] = ()
    width = 1.0
    clip: Box | None = None
    stack: list[tuple[Matrix, tuple[str, ...], tuple[str, ...], float, Box | None]] = []
    path: list[int] = []
    points: list[tuple[float, float]] = []
    curves = 0
    clipping = False

    def add(box: Box, colour: tuple[object, ...], drops: tuple[int, ...], paint: int) -> None:
        # Only what the clip lets show: a block, drawn outside it, must not show more.
        if clip is not None:
            box = _overlap(box, clip)
        if (
            box[2] > box[0]
            and box[3] > box[1]
            and max(box[2] - box[0], box[3] - box[1]) * unit <= LARGEST
        ):
            marks.append(_Mark(box, colour, drops, paint))

    for index, instruction in enumerate(instructions):
        operator = operator_name(instruction)
        if operator == "q":
            stack.append((ctm, fill, stroke, width, clip))
        elif operator == "Q":
            if stack:
                ctm, fill, stroke, width, clip = stack.pop()
        elif operator == "cm":
            ctm = multiply(tuple(float(value) for value in instruction.operands), ctm)
        elif operator in _FILL_COLOURS or operator in _STROKE_COLOURS:
            entry = (operator, *(str(value) for value in instruction.operands))
            if operator in _FILL_COLOURS:
                fill = (*fill[:1], entry) if operator in ("sc", "scn") else (entry,)
            else:
                stroke = (*stroke[:1], entry) if operator in ("SC", "SCN") else (entry,)
        elif operator == "w":
            width = float(instruction.operands[0])
        elif operator in _PATH:
            path.append(index)
            values = [float(value) for value in instruction.operands]
            if operator == "re":
                x, y, w, h = values
                points.extend(((x, y), (x + w, y + h)))
            elif operator != "h":
                points.extend(zip(values[::2], values[1::2], strict=True))
            curves += operator in _CURVES
        elif operator in ("W", "W*"):
            clipping = True
        elif operator in _FILLS or operator in _STROKES or operator in _BOTH or operator == "n":
            if points:
                box = _box([apply_matrix(ctm, x, y) for x, y in points])
                if clipping:
                    clip = box if clip is None else _overlap(clip, box)
                thin = min(box[2] - box[0], box[3] - box[1]) * unit <= RULE
                # A fill that also clips cannot go without changing what comes after it.
                if operator != "n" and not clipping and not (thin and not curves):
                    scale = math.sqrt(abs(ctm[0] * ctm[3] - ctm[1] * ctm[2]))
                    reach = width * scale / 2 if operator not in _FILLS else 0.0
                    if index not in words:
                        add(
                            (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach),
                            (
                                *(("fill", *fill) if operator not in _STROKES else ()),
                                *(("stroke", *stroke) if operator not in _FILLS else ()),
                            ),
                            (*path, index),
                            index,
                        )
            path, points, curves, clipping = [], [], 0, False
        elif operator == "sh" and clip is not None:
            add(clip, ("shading", str(instruction.operands[0])), (index,), index)
        elif operator == "INLINE IMAGE":
            add(_box(placed(ctm, _UNIT_SQUARE)), ("image",), (), index)
        elif operator == "Do" and str(instruction.operands[0]) in images:
            # A form is a stream of its own, with graphics of its own.
            add(_box(placed(ctm, _UNIT_SQUARE)), ("image", str(instruction.operands[0])), (), index)
    return marks


def _overlap(first: Box, second: Box) -> Box:
    return (
        max(first[0], second[0]),
        max(first[1], second[1]),
        min(first[2], second[2]),
        min(first[3], second[3]),
    )


def _groups(marks: Sequence[_Mark], unit: float) -> list[list[_Mark]]:
    """Marks in groups that touch, through other marks if need be."""
    reach = TOUCH / unit
    size = LARGEST / unit / 8
    parents = list(range(len(marks)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, mark in enumerate(marks):
        x0, y0, x1, y1 = mark.box
        for cx in range(math.floor((x0 - reach) / size), math.floor((x1 + reach) / size) + 1):
            for cy in range(math.floor((y0 - reach) / size), math.floor((y1 + reach) / size) + 1):
                cells[(cx, cy)].append(index)
    for members in cells.values():
        for position, index in enumerate(members):
            box = marks[index].box
            for other in members[:position]:
                them = marks[other].box
                if (
                    box[0] <= them[2] + reach
                    and them[0] <= box[2] + reach
                    and box[1] <= them[3] + reach
                    and them[1] <= box[3] + reach
                ):
                    parents[root(index)] = root(other)
    groups: dict[int, list[_Mark]] = defaultdict(list)
    for index, mark in enumerate(marks):
        groups[root(index)].append(mark)
    return _merged([group for _, group in sorted(groups.items())], reach)


def _merged(groups: list[list[_Mark]], reach: float) -> list[list[_Mark]]:
    """Groups whose extents touch, as one. A block fills its group's extent, so a mark there that
    touches nothing in the group would go under it, and a group in a corner of a ring would stay."""
    while True:
        groups.sort(key=lambda group: _extent(group)[0])
        extents = [_extent(group) for group in groups]
        joined = False
        for index, box in enumerate(extents):
            if not groups[index]:
                continue
            for other in range(index + 1, len(groups)):
                them = extents[other]
                if them[0] > box[2] + reach:
                    break
                if groups[other] and box[1] <= them[3] + reach and them[1] <= box[3] + reach:
                    groups[index].extend(groups[other])
                    groups[other] = []
                    joined = True
        groups = [group for group in groups if group]
        if not joined:
            return groups


def _extent(group: Sequence[_Mark]) -> Box:
    return (
        min(mark.box[0] for mark in group),
        min(mark.box[1] for mark in group),
        max(mark.box[2] for mark in group),
        max(mark.box[3] for mark in group),
    )


def _is_graphic(group: Sequence[_Mark], unit: float, text: Sequence[Box]) -> bool:
    """Images are flat already, so they add no colour: a block and the images on it are what a run
    leaves of a graphic."""
    x0, y0, x1, y1 = _extent(group)
    side = max(x1 - x0, y1 - y0) * unit
    return (
        SMALLEST <= side <= LARGEST
        and len({mark.colour for mark in group if mark.drops}) >= 2
        and not any(_crosses((x0, y0, x1, y1), box) for box in text)
    )


def _image_names(pdf: pikepdf.Pdf, ref: StreamRef) -> frozenset[str]:
    if ref.kind == "page":
        resources = pdf.pages[ref.page_index].obj.get("/Resources")
    else:
        resources = pdf.get_object(ref.objgen).get("/Resources")
    xobjects = resources.get("/XObject") if resources is not None else None
    if xobjects is None:
        return frozenset()
    return frozenset(
        str(key)
        for key in xobjects
        if isinstance(xobjects[key], pikepdf.Stream) and xobjects[key].get("/Subtype") == "/Image"
    )


def _found(
    pdf: pikepdf.Pdf, text: TextPlaces
) -> Iterator[tuple[StreamRef, list[Instruction], list[list[_Mark]]]]:
    """Each stream with graphics, its instructions and its graphics."""
    masks = soft_mask_forms(pdf)
    for ref, unit in drawing_streams(pdf):
        if ref.kind == "pattern" or ref.objgen in masks:
            continue
        instructions = read_stream(pdf, ref)
        words = frozenset(
            paint
            for item in item_places(instructions, unit)
            if item.wide > SYMBOL
            for paint in item.paints
        )
        marks = _marks(instructions, unit, words, _image_names(pdf, ref))
        if len(marks) < 2:
            continue
        here = text.in_stream(ref)
        graphics = [group for group in _groups(marks, unit) if _is_graphic(group, unit, here)]
        if graphics:
            yield ref, instructions, graphics


def graphics(pdf: pikepdf.Pdf, text: TextPlaces) -> dict[str, int]:
    """How many graphics each content stream draws. Streams with none are not listed."""
    return {ref.label(): len(found) for ref, _, found in _found(pdf, text)}


def graphic_quads(pdf: pikepdf.Pdf, text: TextPlaces) -> list[tuple[StreamRef, Quad]]:
    """The place of each graphic, in the frame of the stream that draws it."""
    quads = []
    for ref, _, found in _found(pdf, text):
        for group in found:
            x0, y0, x1, y1 = _extent(group)
            quads.append((ref, ((x0, y0), (x1, y0), (x1, y1), (x0, y1))))
    return quads


def _instruction(operator: str, *operands: object) -> pikepdf.ContentStreamInstruction:
    return pikepdf.ContentStreamInstruction(list(operands), pikepdf.Operator(operator))


def _block(extent: Box, ctm: Matrix) -> list[Instruction]:
    """In the marked content of the graphic's first mark, which the block stands for."""
    inverse = inverse_matrix(ctm)
    if inverse is None:
        return []
    x0, y0, x1, y1 = extent
    return [
        _instruction("q"),
        *(() if ctm == _IDENTITY else (_instruction("cm", *inverse),)),
        _instruction("g", SHADE),
        _instruction("re", x0, y0, x1 - x0, y1 - y0),
        _instruction("f"),
        _instruction("Q"),
    ]


def replace_graphics(pdf: pikepdf.Pdf, text: TextPlaces) -> int:
    """Replace each graphic with a flat, light block of its extent. Returns how many there were.

    The block is drawn where the first of its marks was, so that what the graphic covered it covers,
    and what was drawn on any part of the graphic, such as a label, stays on it. It is drawn outside
    the groups open there, so that no clip of one of its marks cuts it.
    """
    count = 0
    for ref, instructions, found in list(_found(pdf, text)):
        gone = {index for group in found for mark in group for index in mark.drops}
        first = {
            min(mark.paint for mark in group if mark.drops): number
            for number, group in enumerate(found)
        }
        marked: list[Instruction] = []
        for index, entry in enumerate(instructions):
            if index in first:
                marked.append(place(first[index]))
            if index not in gone:
                marked.append(entry)

        def draw(number: int, ctm: Matrix, found: list[list[_Mark]] = found) -> list[Instruction]:
            return _block(_extent(found[number]), ctm)

        placed_blocks = placed_outside_groups(without_empty_groups(marked), draw)
        write_stream(pdf, ref, without_empty_groups(placed_blocks))
        count += len(found)
    return count


def solid_images(pdf: pikepdf.Pdf, text: TextPlaces) -> set[tuple[int, int]]:
    """Images with a soft mask that no text crosses, wherever a page draws them.

    A flat image whose mask goes empty shows nothing, so an icon or a logo with a see-through
    background would vanish. Without the mask, it is a block like any other image. One that text
    crosses keeps an empty mask, so that it hides none of the text under it.
    """
    drawn: set[tuple[int, int]] = set()
    crossed: set[tuple[int, int]] = set()
    for index, page in enumerate(pdf.pages):
        boxes = text.pages.get(index, ())
        for objgen, matrices in placements(page)[0].items():
            if objgen is None:
                continue
            drawn.add(objgen)
            if any(
                _crosses(_box(placed(matrix, _UNIT_SQUARE)), box)
                for matrix in matrices
                for box in boxes
            ):
                crossed.add(objgen)
    return {
        objgen
        for objgen in drawn - crossed
        if isinstance(pdf.get_object(objgen).get("/SMask"), pikepdf.Stream)
    }
