"""Text-position tracker: which bytes of which string drew which glyph, and where.

The tracker replays the text state of a content stream (``Tm``, ``Td``, ``TD``,
``T*``, ``Tc``, ``Tw``, ``Tz``, ``TL``, ``Ts``, ``Tf``, ``Tr``, the ``TJ``
adjustments and the CTM) and yields one ``Glyph`` per character code shown.
Each glyph carries its address in the stream, so that a mutator can rewrite
exactly those bytes, and its box on the page, so that an oracle can compare
geometry before and after.

Simple fonts (Type1, MMType1, TrueType) with a ``/Widths`` array are supported.
Composite (Type0) and Type3 fonts, and standard fonts that rely on built-in
metrics instead of ``/Widths``, raise ``UnsupportedText``: without their
metrics, every later glyph in the text object would be misplaced. Form
XObjects are not followed yet.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Literal

import pikepdf

from docmutant.content import Instruction, operator_name
from docmutant.mutate import SIMPLE_FONT_SUBTYPES

Point = tuple[float, float]

DEFAULT_ASCENT = 750.0
DEFAULT_DESCENT = -250.0
"""Glyph-space extent used when the font gives neither ``/Ascent`` nor ``/FontBBox``."""


class UnsupportedText(Exception):
    """The tracker cannot place the glyphs of this text."""


@dataclass(frozen=True)
class Glyph:
    """One character code shown by a text operator."""

    instruction: int
    """Index of the text-showing instruction in the parsed content stream."""

    operand: int
    """For ``TJ``, the index in the array; otherwise the index in the operands."""

    offset: int
    """Byte offset of the code in the string."""

    length: int
    """Number of bytes in the code."""

    code: int
    font: str
    """Font resource name, for example ``/F1``."""

    size: float
    width: float
    """Advance width in glyph space (thousandths of a text space unit)."""

    origin: Point
    """Start of the glyph on the baseline, in default user space."""

    box: tuple[Point, Point, Point, Point]
    """Corners in default user space: bottom left, bottom right, top right, top left.

    The box spans the advance width along the baseline and the font's descent to
    ascent across it, so it is a parallelogram when the text is skewed.
    """

    render_mode: int
    """``Tr``: 3 is invisible text, as in the OCR layer of a scan."""

    mcid: int | None
    """Marked-content ID of the innermost marked-content sequence that has one."""

    width_from: Literal["widths", "missing-width", "none"]
    height_from: Literal["ascent", "font-bbox", "default"]


@dataclass(frozen=True)
class _SimpleFont:
    first_char: int
    widths: tuple[float, ...]
    missing_width: float | None
    ascent: float
    descent: float
    height_from: Literal["ascent", "font-bbox", "default"]

    def width(self, code: int) -> tuple[float, Literal["widths", "missing-width", "none"]]:
        index = code - self.first_char
        if 0 <= index < len(self.widths):
            return self.widths[index], "widths"
        if self.missing_width is not None:
            return self.missing_width, "missing-width"
        return 0.0, "none"


def _simple_font(name: str, font: pikepdf.Object) -> _SimpleFont:
    subtype = font.get("/Subtype")
    if subtype not in SIMPLE_FONT_SUBTYPES:
        raise UnsupportedText(f"font {name} has subtype {subtype}")
    if "/Widths" not in font:
        raise UnsupportedText(f"font {name} has no /Widths (built-in metrics)")
    descriptor = font.get("/FontDescriptor")
    missing_width = None
    if descriptor is not None and "/MissingWidth" in descriptor:
        missing_width = float(descriptor.MissingWidth)

    ascent = descent = 0.0
    if descriptor is not None:
        ascent = float(descriptor.get("/Ascent", 0))
        descent = float(descriptor.get("/Descent", 0))
    height_from: Literal["ascent", "font-bbox", "default"] = "ascent"
    if ascent <= descent:
        bbox = descriptor.get("/FontBBox") if descriptor is not None else None
        if bbox is not None and len(bbox) == 4 and float(bbox[3]) > float(bbox[1]):
            ascent, descent, height_from = float(bbox[3]), float(bbox[1]), "font-bbox"
        else:
            ascent, descent, height_from = DEFAULT_ASCENT, DEFAULT_DESCENT, "default"

    return _SimpleFont(
        first_char=int(font.get("/FirstChar", 0)),
        widths=tuple(float(w) for w in font.get("/Widths", [])),
        missing_width=missing_width,
        ascent=ascent,
        descent=descent,
        height_from=height_from,
    )


@dataclass(frozen=True)
class _State:
    """The parts of the graphics state that ``q`` and ``Q`` save."""

    ctm: pikepdf.Matrix = pikepdf.Matrix()
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    scale: float = 1.0
    leading: float = 0.0
    rise: float = 0.0
    render_mode: int = 0
    font: str | None = None
    size: float = 0.0


def trace(instructions: Sequence[Instruction], resources: pikepdf.Object | None) -> Iterator[Glyph]:
    """Yield the glyphs shown by ``instructions``, in content-stream order."""
    fonts: dict[str, _SimpleFont] = {}
    font_resources = resources.get("/Font") if resources is not None else None
    properties = resources.get("/Properties") if resources is not None else None

    def font_metrics(name: str) -> _SimpleFont:
        if name not in fonts:
            font = font_resources.get(name) if font_resources is not None else None
            if font is None:
                raise UnsupportedText(f"font {name} is not in the resources")
            fonts[name] = _simple_font(name, font)
        return fonts[name]

    state = _State()
    saved: list[_State] = []
    marked: list[int | None] = []
    text_matrix = line_matrix = pikepdf.Matrix()

    def next_line(tx: float, ty: float) -> None:
        nonlocal text_matrix, line_matrix
        line_matrix = pikepdf.Matrix(1, 0, 0, 1, tx, ty) @ line_matrix
        text_matrix = line_matrix

    def show(string: pikepdf.String, index: int, operand: int) -> Iterator[Glyph]:
        nonlocal text_matrix
        if state.font is None:
            raise UnsupportedText(f"instruction {index} shows text before Tf")
        metrics = font_metrics(state.font)
        mcid = next((m for m in reversed(marked) if m is not None), None)
        for offset, code in enumerate(bytes(string)):
            width, width_from = metrics.width(code)
            glyph_space = pikepdf.Matrix(
                state.size * state.scale / 1000, 0, 0, state.size / 1000, 0, state.rise
            )
            to_page = glyph_space @ text_matrix @ state.ctm
            corners = (
                (0.0, metrics.descent),
                (width, metrics.descent),
                (width, metrics.ascent),
                (0.0, metrics.ascent),
            )
            yield Glyph(
                instruction=index,
                operand=operand,
                offset=offset,
                length=1,
                code=code,
                font=state.font,
                size=state.size,
                width=width,
                origin=to_page.transform((0.0, 0.0)),
                box=tuple(to_page.transform(corner) for corner in corners),
                render_mode=state.render_mode,
                mcid=mcid,
                width_from=width_from,
                height_from=metrics.height_from,
            )
            advance = width / 1000 * state.size + state.char_spacing
            if code == 32:
                advance += state.word_spacing
            text_matrix = pikepdf.Matrix(1, 0, 0, 1, advance * state.scale, 0) @ text_matrix

    for index, instruction in enumerate(instructions):
        op = operator_name(instruction)
        operands = [] if op == "INLINE IMAGE" else list(instruction.operands)

        if op == "q":
            saved.append(state)
        elif op == "Q":
            if saved:
                state = saved.pop()
        elif op == "cm":
            state = replace(state, ctm=pikepdf.Matrix(*map(float, operands)) @ state.ctm)
        elif op == "BT":
            text_matrix = line_matrix = pikepdf.Matrix()
        elif op == "Tc":
            state = replace(state, char_spacing=float(operands[0]))
        elif op == "Tw":
            state = replace(state, word_spacing=float(operands[0]))
        elif op == "Tz":
            state = replace(state, scale=float(operands[0]) / 100)
        elif op == "TL":
            state = replace(state, leading=float(operands[0]))
        elif op == "Ts":
            state = replace(state, rise=float(operands[0]))
        elif op == "Tr":
            state = replace(state, render_mode=int(operands[0]))
        elif op == "Tf":
            state = replace(state, font=str(operands[0]), size=float(operands[1]))
        elif op == "Td":
            next_line(float(operands[0]), float(operands[1]))
        elif op == "TD":
            state = replace(state, leading=-float(operands[1]))
            next_line(float(operands[0]), float(operands[1]))
        elif op == "Tm":
            text_matrix = line_matrix = pikepdf.Matrix(*map(float, operands))
        elif op == "T*":
            next_line(0.0, -state.leading)
        elif op == "Tj":
            yield from show(operands[0], index, 0)
        elif op == "'":
            next_line(0.0, -state.leading)
            yield from show(operands[0], index, 0)
        elif op == '"':
            state = replace(state, word_spacing=float(operands[0]), char_spacing=float(operands[1]))
            next_line(0.0, -state.leading)
            yield from show(operands[2], index, 2)
        elif op == "TJ":
            for operand, item in enumerate(operands[0]):
                if isinstance(item, pikepdf.String):
                    yield from show(item, index, operand)
                else:
                    shift = -float(item) / 1000 * state.size * state.scale
                    text_matrix = pikepdf.Matrix(1, 0, 0, 1, shift, 0) @ text_matrix
        elif op == "BMC":
            marked.append(None)
        elif op == "BDC":
            props = operands[1]
            if isinstance(props, pikepdf.Name) and properties is not None:
                props = properties.get(props)
            mcid = props.get("/MCID") if isinstance(props, pikepdf.Dictionary) else None
            marked.append(int(mcid) if mcid is not None else None)
        elif op == "EMC" and marked:
            marked.pop()


def trace_page(page: pikepdf.Page) -> list[Glyph]:
    """All glyphs drawn by the page's own content streams."""
    instructions = list(pikepdf.parse_content_stream(page))
    return list(trace(instructions, page.obj.get("/Resources")))
