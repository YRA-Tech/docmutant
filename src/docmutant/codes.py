"""Machine-readable codes drawn as paths: a QR code, a Data Matrix or a barcode.

A code stays readable when every word around it has changed, and it can hold a URL or an ID.
OCR does not read one, and the outline items of ``outlines`` leave boxes alone. But a code is
recognizable by what it is drawn with: boxes of one colour, whose sides are whole multiples of one
module, on one grid.

Under ``privacy``, a code becomes a flat, light block of the same size. Each of its boxes goes, so
that no part of the pattern stays. A code in an image goes with the image, and one drawn in round
dots is an outline item.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import pikepdf

from docmutant.content import Instruction, operator_name
from docmutant.mutate import StreamRef, read_stream, write_stream
from docmutant.outlines import (
    Matrix,
    apply_matrix,
    drawing_streams,
    inverse_matrix,
    multiply,
    without_empty_groups,
)
from docmutant.tracker import Point

SMALLEST_CODE = 15
"""Boxes. A smaller group of boxes is too easily something else, such as a few bars in a chart."""

LARGEST_MODULE = 8.0
"""Points. Above this a grid of boxes is a table or a chart."""

MATRIX_SIDE = 18
"""Modules. The smallest QR code has 21 and the smallest Data Matrix 10, which holds very little."""

SQUARE = 1.25
"""The widest that a matrix can be for its height. A QR code is square, and most are nearly."""

LEAST_INK, MOST_INK = 0.25, 0.75
"""A matrix is about half ink. A filled block or a sparse grid of dots is something else."""

BAR_MODULES = 6
"""The widest bar or gap in modules. Barcodes use at most four or so, and scaling blurs that."""

TALL = 3.0
"""How many times as tall as its widest bar a barcode must be."""

CLOSE = 0.12
"""In modules: how far off a whole multiple a side or a position can be."""

GATHER = 5.0
"""In modules: the widest gap inside a code."""

_FILLS = frozenset({"f", "F", "f*"})
_COLOURS = frozenset({"g", "rg", "k", "cs", "sc", "scn"})
_PATH = frozenset({"m", "l", "h", "re", "c", "v", "y"})
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass(frozen=True)
class _Box:
    left: float
    bottom: float
    right: float
    top: float
    drops: tuple[int, ...]
    """The operators that draw it and the fills that it shares."""

    paint: int
    ctm: Matrix

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.top - self.bottom


@dataclass(frozen=True)
class _Code:
    boxes: tuple[_Box, ...]
    kind: str

    @property
    def extent(self) -> tuple[float, float, float, float]:
        return (
            min(box.left for box in self.boxes),
            min(box.bottom for box in self.boxes),
            max(box.right for box in self.boxes),
            max(box.top for box in self.boxes),
        )


def _subpath_boxes(subpath: list[tuple[str, list[float]]]) -> tuple[float, ...] | None:
    """The corners of a subpath, if it is a box whose sides are straight along the axes."""
    if len(subpath) == 1 and subpath[0][0] == "re":
        x, y, width, height = subpath[0][1]
        return (x, y, x + width, y + height)
    points = [operands for operator, operands in subpath if operator in ("m", "l")]
    if any(operator not in ("m", "l", "h") for operator, _ in subpath):
        return None
    if len(points) == 5 and points[0] == points[4]:
        points = points[:4]
    if len(points) != 4:
        return None
    xs, ys = {round(point[0], 3) for point in points}, {round(point[1], 3) for point in points}
    if len(xs) != 2 or len(ys) != 2:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _boxes_by_colour(
    instructions: Sequence[Instruction],
) -> dict[tuple[tuple[str, ...], ...], list[_Box]]:
    """Each box that a stream fills, grouped by colour. A fill with anything else is left out."""
    found: dict[tuple[tuple[str, ...], ...], list[_Box]] = defaultdict(list)
    ctm = _IDENTITY
    colour: tuple[tuple[str, ...], ...] = ()
    stack: list[tuple[Matrix, tuple[tuple[str, ...], ...]]] = []
    path: list[int] = []
    subpaths: list[list[tuple[str, list[float]]]] = []
    clipping = False

    for index, instruction in enumerate(instructions):
        operator = operator_name(instruction)
        if operator == "q":
            stack.append((ctm, colour))
        elif operator == "Q":
            if stack:
                ctm, colour = stack.pop()
        elif operator == "cm":
            ctm = multiply(tuple(float(value) for value in instruction.operands), ctm)
        elif operator in _COLOURS:
            entry = (operator, *(str(value) for value in instruction.operands))
            colour = (*colour[:1], entry) if operator in ("sc", "scn") else (entry,)
        elif operator in _PATH:
            path.append(index)
            operands = [float(value) for value in instruction.operands]
            if operator in ("m", "re") or not subpaths:
                subpaths.append([])
            subpaths[-1].append((operator, operands))
        elif operator in ("W", "W*"):
            clipping = True
        elif operator in _FILLS or operator in ("n", "S", "s", "B", "B*", "b", "b*"):
            if operator in _FILLS and subpaths and not clipping:
                corners = [_subpath_boxes(subpath) for subpath in subpaths]
                # Sideways or upright, a box on a grid stays one. A skewed one is not read as code.
                if all(corners) and (ctm[1] == ctm[2] == 0 or ctm[0] == ctm[3] == 0):
                    drops = (*path, index)
                    for box in corners:
                        (x0, y0) = apply_matrix(ctm, box[0], box[1])
                        (x1, y1) = apply_matrix(ctm, box[2], box[3])
                        found[colour].append(
                            _Box(
                                min(x0, x1),
                                min(y0, y1),
                                max(x0, x1),
                                max(y0, y1),
                                drops,
                                index,
                                ctm,
                            )
                        )
            path, subpaths, clipping = [], [], False
    return found


def _gathered(boxes: Sequence[_Box], unit: float) -> list[list[_Box]]:
    """Boxes small enough to be modules, in groups that lie close together."""
    small = [
        box
        for box in boxes
        if 0 < min(box.width, box.height) * unit <= LARGEST_MODULE and max(box.width, box.height)
    ]
    if not small:
        return []
    reach = GATHER * min(min(box.width, box.height) for box in small)
    parents = list(range(len(small)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    # Cells as wide as the reach, so that a box can only touch boxes in the cells around it.
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, box in enumerate(small):
        for cx in range(int(box.left // reach), int(box.right // reach) + 1):
            for cy in range(int(box.bottom // reach), int(box.top // reach) + 1):
                cells[(cx, cy)].append(index)
    for (cx, cy), members in sorted(cells.items()):
        near = [
            other
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for other in cells.get((cx + dx, cy + dy), ())
        ]
        for index in members:
            box = small[index]
            for other in near:
                them = small[other]
                if (
                    box.left <= them.right + reach
                    and them.left <= box.right + reach
                    and box.bottom <= them.top + reach
                    and them.bottom <= box.top + reach
                ):
                    parents[root(index)] = root(other)
    groups: dict[int, list[_Box]] = defaultdict(list)
    for index, box in enumerate(small):
        groups[root(index)].append(box)
    return [group for _, group in sorted(groups.items())]


def _module(values: Sequence[float]) -> float:
    """The commonest of the smallest sizes: one stray thin box must not set the grid."""
    rounded = Counter(round(value, 2) for value in values)
    smallest = min(rounded)
    near = [size for size in rounded if size <= smallest * 1.5]
    return max(near, key=lambda size: (rounded[size], -size))


def _multiple(value: float, module: float) -> bool:
    return abs(value / module - round(value / module)) <= CLOSE and round(value / module) >= 1


def _is_matrix(group: Sequence[_Box]) -> bool:
    module = _module([min(box.width, box.height) for box in group])
    left = min(box.left for box in group)
    bottom = min(box.bottom for box in group)
    width = max(box.right for box in group) - left
    height = max(box.top for box in group) - bottom
    if min(width, height) < MATRIX_SIDE * module or max(width, height) > SQUARE * min(
        width, height
    ):
        return False
    if not all(
        _multiple(box.width, module)
        and _multiple(box.height, module)
        and (abs((box.left - left) / module - round((box.left - left) / module)) <= CLOSE)
        and (abs((box.bottom - bottom) / module - round((box.bottom - bottom) / module)) <= CLOSE)
        for box in group
    ):
        return False
    ink = sum(box.width * box.height for box in group) / (width * height)
    return LEAST_INK <= ink <= MOST_INK


def _is_bars(group: Sequence[_Box], sideways: bool) -> bool:
    def across(box: _Box) -> tuple[float, float]:
        return (box.bottom, box.top) if sideways else (box.left, box.right)

    def along(box: _Box) -> tuple[float, float]:
        return (box.left, box.right) if sideways else (box.bottom, box.top)

    bars = sorted(group, key=lambda box: across(box)[0])
    widths = [across(box)[1] - across(box)[0] for box in bars]
    module = _module(widths)
    low, high = along(bars[0])
    if any(abs(along(box)[0] - low) > module or abs(along(box)[1] - high) > module for box in bars):
        return False
    if high - low < TALL * max(widths):
        return False
    gaps = [
        across(after)[0] - across(before)[1] for before, after in zip(bars, bars[1:], strict=False)
    ]
    return all(
        _multiple(width, module) and width <= BAR_MODULES * module * 1.1 for width in widths
    ) and all(_multiple(gap, module) and gap <= BAR_MODULES * module * 1.1 for gap in gaps)


def _codes(instructions: Sequence[Instruction], unit: float) -> list[_Code]:
    found: list[_Code] = []
    for _, boxes in sorted(_boxes_by_colour(instructions).items(), key=lambda entry: str(entry[0])):
        for group in _gathered(boxes, unit):
            if len(group) < SMALLEST_CODE:
                continue
            if _is_matrix(group):
                found.append(_Code(tuple(group), "matrix"))
            elif _is_bars(group, False) or _is_bars(group, True):
                found.append(_Code(tuple(group), "bars"))
    return found


def machine_codes(pdf: pikepdf.Pdf) -> dict[str, int]:
    """How many codes each content stream draws. Streams with none are not listed."""
    found: dict[str, int] = {}
    for ref, unit in drawing_streams(pdf):
        count = len(_codes(read_stream(pdf, ref), unit))
        if count:
            found[ref.label()] = count
    return found


def code_quads(
    pdf: pikepdf.Pdf,
) -> list[tuple[StreamRef, tuple[Point, Point, Point, Point]]]:
    """The place of each code, in the frame of the stream that draws it."""
    quads = []
    for ref, unit in drawing_streams(pdf):
        for code in _codes(read_stream(pdf, ref), unit):
            x0, y0, x1, y1 = code.extent
            quads.append((ref, ((x0, y0), (x1, y0), (x1, y1), (x0, y1))))
    return quads


def _block(code: _Code, inverse: Matrix) -> list[Instruction]:
    """A flat block over the code's whole extent, which undoes the transform where it is drawn."""
    left, bottom, right, top = code.extent

    def instruction(operator: str, *operands: object) -> pikepdf.ContentStreamInstruction:
        return pikepdf.ContentStreamInstruction(list(operands), pikepdf.Operator(operator))

    return [
        instruction("q"),
        instruction("cm", *inverse),
        instruction("g", 0.9),
        instruction("re", left, bottom, right - left, top - bottom),
        instruction("f"),
        instruction("Q"),
    ]


def replace_machine_codes(pdf: pikepdf.Pdf) -> int:
    """Replace each code with a flat, light block of its size. Returns how many there were."""
    count = 0
    for ref, unit in drawing_streams(pdf):
        instructions = read_stream(pdf, ref)
        codes = _codes(instructions, unit)
        if not codes:
            continue
        gone: set[int] = set()
        places: dict[int, list[Instruction]] = {}
        for code in codes:
            for box in code.boxes:
                gone.update(box.drops)
            last = max(code.boxes, key=lambda box: box.paint)
            inverse = inverse_matrix(last.ctm)
            if inverse is None:
                continue
            places.setdefault(last.paint, []).extend(_block(code, inverse))
        rebuilt: list[Instruction] = []
        for index, entry in enumerate(instructions):
            rebuilt.extend(places.get(index, ()))
            if index not in gone:
                rebuilt.append(entry)
        write_stream(pdf, ref, without_empty_groups(rebuilt))
        count += len(codes)
    return count
