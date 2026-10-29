"""Text-position tracker: which bytes of which string drew which glyph, and where.

The tracker replays the text state of a content stream (``Tm``, ``Td``, ``TD``,
``T*``, ``Tc``, ``Tw``, ``Tz``, ``TL``, ``Ts``, ``Tf``, ``Tr``, the ``TJ``
adjustments and the CTM) and yields one ``Glyph`` per character code shown.
Each glyph carries its address in the stream, so that a mutator can rewrite
exactly those bytes, and its box on the page, so that an oracle can compare
geometry before and after.

Simple fonts (Type1, MMType1, TrueType) with a ``/Widths`` array are supported,
and composite (Type0) fonts with horizontal writing whose CMap is
``/Identity-H`` or embedded. Type3 fonts, standard fonts that rely on built-in
metrics instead of ``/Widths``, vertical writing and predefined CMaps other than
Identity raise ``UnsupportedText``: without their metrics, every later glyph in
the text object would be misplaced. Form XObjects are not followed yet.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

import pikepdf

from docmutant.content import Instruction, operator_name
from docmutant.mutate import SIMPLE_FONT_SUBTYPES, cid_widths

Point = tuple[float, float]
WidthSource = Literal["widths", "missing-width", "default-width", "none"]
HeightSource = Literal["ascent", "font-bbox", "default"]

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
    """Number of bytes in the code: 1 in a simple font, 1 to 4 in a composite font."""

    code: int
    cid: int | None
    """CID that the CMap maps the code to, in a composite font; None in a simple font."""

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

    width_from: WidthSource
    """``widths`` for ``/Widths`` or ``/W``; ``default-width`` for ``/DW``."""

    height_from: HeightSource


@dataclass(frozen=True)
class _CMap:
    """Splits a string into codes and maps each code to a CID."""

    codespaces: tuple[tuple[bytes, bytes], ...]
    """Low and high bounds of each codespace range; the code length is their length."""

    ranges: tuple[tuple[int, int, int, int], ...]
    """Code length, first code, last code and first CID; the earliest match wins."""

    notdefs: tuple[tuple[int, int, int, int], ...] = ()
    """Code length, first code, last code and the one CID they all map to; used last."""

    def split(self, data: bytes) -> Iterator[tuple[int, int, int]]:
        """Yield offset, length and code for each code in ``data``."""
        shortest = min(len(low) for low, _ in self.codespaces)
        offset = 0
        while offset < len(data):
            for length in range(1, 5):
                chunk = data[offset : offset + length]
                if len(chunk) == length and any(
                    len(low) == length
                    and all(lo <= b <= hi for lo, b, hi in zip(low, chunk, high, strict=True))
                    for low, high in self.codespaces
                ):
                    break
            else:
                # No codespace matches: take the shortest code length, which maps to CID 0.
                length = min(shortest, len(data) - offset)
            yield offset, length, int.from_bytes(data[offset : offset + length], "big")
            offset += length

    def cid(self, code: int, length: int) -> int:
        for range_length, first, last, first_cid in self.ranges:
            if range_length == length and first <= code <= last:
                return first_cid + code - first
        for range_length, first, last, cid in self.notdefs:
            if range_length == length and first <= code <= last:
                return cid
        return 0


IDENTITY_H = _CMap(codespaces=((b"\x00\x00", b"\xff\xff"),), ranges=((2, 0, 0xFFFF, 0),))

_CMAP_TOKEN = re.compile(
    rb"%[^\r\n]*|\([^)]*\)|<[0-9A-Fa-f\s]*>|/[^\s/<>\[\]()%]+|[^\s/<>\[\]()%]+"
)
_CMAP_SECTIONS = frozenset(
    {b"begincodespacerange", b"begincidrange", b"begincidchar", b"beginnotdefrange"}
)


def _embedded_cmap(name: str, stream: pikepdf.Stream) -> _CMap:
    """Parse the codespace and CID mappings of an embedded CMap stream."""
    tokens = [t for t in _CMAP_TOKEN.findall(stream.read_bytes()) if not t.startswith(b"%")]
    use = stream.get("/UseCMap")
    if stream.get("/WMode") == 1:
        raise UnsupportedText(f"font {name} uses vertical writing")

    codespaces: list[tuple[bytes, bytes]] = []
    ranges: list[tuple[int, int, int, int]] = []
    notdefs: list[tuple[int, int, int, int]] = []
    section: bytes | None = None
    operands: list[bytes] = []
    for token in tokens:
        if token in _CMAP_SECTIONS:
            section, operands = token, []
        elif token.startswith(b"end") and section is not None:
            section = None
        elif section is not None:
            operands.append(token)
            hexes = [bytes.fromhex(t[1:-1].decode()) for t in operands if t.startswith(b"<")]
            if section == b"begincodespacerange" and len(operands) == 2:
                if len(hexes[0]) == len(hexes[1]):
                    codespaces.append((hexes[0], hexes[1]))
                operands = []
            elif section == b"begincidchar" and len(operands) == 2:
                code = int.from_bytes(hexes[0], "big")
                ranges.append((len(hexes[0]), code, code, int(operands[1])))
                operands = []
            elif len(operands) == 3:
                first, last = int.from_bytes(hexes[0], "big"), int.from_bytes(hexes[1], "big")
                entries = notdefs if section == b"beginnotdefrange" else ranges
                entries.append((len(hexes[0]), first, last, int(operands[2])))
                operands = []
        elif token == b"usecmap" and operands:
            use = pikepdf.Name(operands[-1].decode())
        elif token == b"def" and operands[-2:] == [b"/WMode", b"1"]:
            raise UnsupportedText(f"font {name} uses vertical writing")
        else:
            operands = [*operands[-1:], token]

    if use is not None:
        if use != "/Identity-H":
            raise UnsupportedText(f"font {name} uses CMap {use}, which is not built in")
        codespaces.extend(IDENTITY_H.codespaces)
        ranges.extend(IDENTITY_H.ranges)
    if not codespaces:
        raise UnsupportedText(f"font {name} has a CMap without codespace ranges")
    return _CMap(tuple(codespaces), tuple(ranges), tuple(notdefs))


@dataclass(frozen=True)
class _Font:
    widths: Mapping[int, float]
    """Keyed by code in a simple font and by CID in a composite font."""

    default_width: float | None
    default_from: WidthSource
    ascent: float
    descent: float
    height_from: HeightSource
    cmap: _CMap | None = None
    """None for a simple font: one byte per code."""

    def codes(self, data: bytes) -> Iterator[tuple[int, int, int, int | None]]:
        """Yield offset, length, code and CID for each code in ``data``."""
        if self.cmap is None:
            for offset, code in enumerate(data):
                yield offset, 1, code, None
            return
        for offset, length, code in self.cmap.split(data):
            yield offset, length, code, self.cmap.cid(code, length)

    def width(self, key: int) -> tuple[float, WidthSource]:
        if key in self.widths:
            return self.widths[key], "widths"
        if self.default_width is not None:
            return self.default_width, self.default_from
        return 0.0, "none"


def _vertical_extent(descriptor: pikepdf.Object | None) -> tuple[float, float, HeightSource]:
    ascent = descent = 0.0
    if descriptor is not None:
        ascent = float(descriptor.get("/Ascent", 0))
        descent = float(descriptor.get("/Descent", 0))
    if ascent > descent:
        return ascent, descent, "ascent"
    bbox = descriptor.get("/FontBBox") if descriptor is not None else None
    if bbox is not None and len(bbox) == 4 and float(bbox[3]) > float(bbox[1]):
        return float(bbox[3]), float(bbox[1]), "font-bbox"
    return DEFAULT_ASCENT, DEFAULT_DESCENT, "default"


def _font(name: str, font: pikepdf.Object) -> _Font:
    subtype = font.get("/Subtype")
    if subtype == "/Type0":
        return _composite_font(name, font)
    if subtype not in SIMPLE_FONT_SUBTYPES:
        raise UnsupportedText(f"font {name} has subtype {subtype}")
    if "/Widths" not in font:
        raise UnsupportedText(f"font {name} has no /Widths (built-in metrics)")
    descriptor = font.get("/FontDescriptor")
    missing_width = None
    if descriptor is not None and "/MissingWidth" in descriptor:
        missing_width = float(descriptor.MissingWidth)
    first = int(font.get("/FirstChar", 0))
    return _Font(
        {first + i: float(w) for i, w in enumerate(font.Widths)},
        missing_width,
        "missing-width",
        *_vertical_extent(descriptor),
    )


def _composite_font(name: str, font: pikepdf.Object) -> _Font:
    encoding = font.get("/Encoding")
    if isinstance(encoding, pikepdf.Stream):
        cmap = _embedded_cmap(name, encoding)
    elif encoding == "/Identity-H":
        cmap = IDENTITY_H
    elif encoding == "/Identity-V":
        raise UnsupportedText(f"font {name} uses vertical writing")
    else:
        raise UnsupportedText(f"font {name} uses CMap {encoding}, which is not built in")
    descendants = font.get("/DescendantFonts")
    if not descendants:
        raise UnsupportedText(f"font {name} has no descendant font")
    descendant = descendants[0]
    return _Font(
        cid_widths(descendant.get("/W")),
        float(descendant.get("/DW", 1000)),
        "default-width",
        *_vertical_extent(descendant.get("/FontDescriptor")),
        cmap=cmap,
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
    fonts: dict[str, _Font] = {}
    font_resources = resources.get("/Font") if resources is not None else None
    properties = resources.get("/Properties") if resources is not None else None

    def font_metrics(name: str) -> _Font:
        if name not in fonts:
            font = font_resources.get(name) if font_resources is not None else None
            if font is None:
                raise UnsupportedText(f"font {name} is not in the resources")
            fonts[name] = _font(name, font)
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
        for offset, length, code, cid in metrics.codes(bytes(string)):
            width, width_from = metrics.width(code if cid is None else cid)
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
                length=length,
                code=code,
                cid=cid,
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
            if code == 32 and length == 1:
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
