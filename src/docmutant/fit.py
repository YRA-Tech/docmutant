"""Fitted replacement: write other words into a line without moving what the settings hold.

Each replaced word keeps its start. The new codes go where the first glyph was, and every
instruction that held a part of the word ends with the pen where the source had it, by a
``TJ`` number if needed. So nothing later in the line moves, however it is positioned.
Only the end of the word moves: an inner word must leave the minimum gap before the next
word, and a word whose end is held fits within the box tolerance, with tracking if the
limit allows it. The word count of a line does not change.

The output is checked with the geometry oracle, and a line that fails goes back to its
source text. A replacement uses only codes that the document shows in that font, because
a subset font may have no glyph for any other code.
"""

from __future__ import annotations

import bisect
import math
import random
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import pikepdf

from docmutant.content import Instruction, operator_name
from docmutant.geometry import ROUNDING, BoxSettings, GeometryResult, check_boxes, signed_gap
from docmutant.mutate import StreamRef, read_stream, write_stream
from docmutant.tracker import (
    LINE_GAP,
    Glyph,
    Line,
    UnsupportedText,
    Word,
    extent,
    group_lines,
    trace_page,
)

LeftReason = Literal[
    "no text", "not a word", "in a form", "mixed fonts", "printed over", "no fit", "undone"
]
Style = Literal["lower", "capitalized", "upper"]

NEAR = 0.05
"""Ems from the source end within which a replacement counts as near."""

MARGIN = 0.005
"""Points kept inside each bound, so that rounding in the output does not cross it."""

PRINTED_OVER = 0.5
"""Share of the shorter line's height that two lines overlap when one is printed over the
other, as in fake bold: replacing them with different words would be unreadable."""


@dataclass(frozen=True)
class FitReport:
    words_replaced: int
    words_left: Mapping[LeftReason, int]
    """Words that keep their source text, by reason."""

    lines_undone: int
    geometry: GeometryResult


@dataclass(frozen=True)
class _Code:
    code: bytes
    width: float


@dataclass(frozen=True)
class _Candidate:
    width: float
    """Sum of the glyph widths, in glyph space."""

    text: str
    codes: tuple[bytes, ...]


@dataclass(frozen=True)
class _Edit:
    """New codes in place of the part of one word inside one instruction; or no codes."""

    instruction: int
    operand: int
    offset: int
    last_operand: int
    end: int
    codes: tuple[bytes, ...]
    tracking: float
    """``TJ`` number between two codes."""

    after: float
    """``TJ`` number after the last code."""


def _key(glyph: Glyph, page: pikepdf.Page) -> tuple[object, ...] | None:
    """Identity of the font of a glyph drawn by the page itself."""
    fonts = page.obj.get("/Resources", {}).get("/Font")
    font = fonts.get(glyph.font) if fonts is not None else None
    if font is None:
        return None
    return font.objgen if font.is_indirect else (page.index, glyph.font)


def _style(text: str) -> Style:
    if text.isupper():
        return "upper"
    return "capitalized" if text[0].isupper() else "lower"


def _recase(word: str, style: Style) -> str:
    if style == "upper":
        return word.upper()
    return word.capitalize() if style == "capitalized" else word


class _FontWords:
    """The words of a vocabulary that one font can draw, by style and length, sorted by width."""

    def __init__(self, codes: Mapping[str, _Code], vocabulary: Sequence[str]) -> None:
        self.codes = codes
        self.vocabulary = vocabulary
        self.tables: dict[Style, dict[int, tuple[list[float], list[_Candidate]]]] = {}

    def table(self, style: Style) -> Mapping[int, tuple[list[float], list[_Candidate]]]:
        if style not in self.tables:
            by_length: dict[int, list[_Candidate]] = {}
            for word in self.vocabulary:
                text = _recase(word, style)
                if all(char in self.codes for char in text):
                    entries = [self.codes[char] for char in text]
                    by_length.setdefault(len(text), []).append(
                        _Candidate(
                            sum(e.width for e in entries), text, tuple(e.code for e in entries)
                        )
                    )
            self.tables[style] = {}
            for length, candidates in by_length.items():
                candidates.sort(key=lambda candidate: (candidate.width, candidate.text))
                self.tables[style][length] = ([c.width for c in candidates], candidates)
        return self.tables[style]


def _parts(word: Word) -> tuple[list[Glyph], list[Glyph], list[Glyph]] | None:
    """Leading punctuation, the letters and trailing punctuation, or None if not a word."""
    glyphs = list(word.glyphs)
    first = 0
    while first < len(glyphs) and not (glyphs[first].text or "").isalpha():
        first += 1
    last = len(glyphs)
    while last > first and not (glyphs[last - 1].text or "").isalpha():
        last -= 1
    core = glyphs[first:last]
    if len("".join(g.text or "" for g in core)) < 2:
        return None
    if not all((g.text or "").isalpha() for g in core):
        return None
    return glyphs[:first], core, glyphs[last:]


def _code_bytes(glyph: Glyph) -> bytes:
    return glyph.code.to_bytes(glyph.length, "big")


def _along(point: tuple[float, float], direction: tuple[float, float]) -> float:
    return point[0] * direction[0] + point[1] * direction[1]


class WordFitter:
    """Replaces words on the pages of one document, from one vocabulary.

    ``vocabulary`` holds lowercase words. Which glyphs each font can draw is read once,
    from every page, before any page changes.
    """

    def __init__(self, pdf: pikepdf.Pdf, vocabulary: Iterable[str]) -> None:
        self.pdf = pdf
        words = sorted(set(vocabulary))
        codes: dict[tuple[object, ...], dict[str, _Code]] = {}
        for page in pdf.pages:
            try:
                glyphs = trace_page(page)
            except UnsupportedText:
                continue
            for glyph in glyphs:
                if glyph.stream.kind != "page" or glyph.text is None or glyph.width <= 0:
                    continue
                if len(glyph.text) != 1 or glyph.is_space or (glyph.code, glyph.length) == (32, 1):
                    continue
                key = _key(glyph, page)
                if key is not None:
                    codes.setdefault(key, {}).setdefault(
                        glyph.text, _Code(_code_bytes(glyph), glyph.width)
                    )
        self.fonts = {key: _FontWords(table, words) for key, table in codes.items()}

    def fit_page(self, index: int, settings: BoxSettings, seed: int = 0) -> FitReport:
        """Replace what fits on one page. Text it cannot place raises ``UnsupportedText``."""
        page = self.pdf.pages[index]
        ref = StreamRef("page", index)
        source = read_stream(self.pdf, ref)
        lines = group_lines(trace_page(page))
        rng = random.Random(f"{seed}:{index}")
        left: Counter[LeftReason] = Counter()
        printed_over = _printed_over(lines)
        planned: dict[int, list[_Edit]] = {}
        replaced: dict[int, int] = {}
        for number, line in enumerate(lines):
            if number in printed_over:
                left["printed over"] += len(line.words)
                continue
            words = self._plan_line(line, page, settings, rng, left)
            if words:
                planned[number] = [edit for word in words for edit in word]
                replaced[number] = len(words)

        undone = 0
        wrote = False
        while True:
            edits = [edit for number in sorted(planned) for edit in planned[number]]
            if edits or wrote:
                write_stream(self.pdf, ref, _apply(source, edits))
                wrote = True
            result = check_boxes(lines, group_lines(trace_page(self.pdf.pages[index])), settings)
            if result.passed or not planned:
                break
            failed = _failed_lines(result, set(planned))
            undone += len(failed)
            for number in failed:
                del planned[number]
                left["undone"] += replaced[number]
        return FitReport(sum(replaced[n] for n in planned), dict(left), undone, result)

    def _plan_line(
        self,
        line: Line,
        page: pikepdf.Page,
        settings: BoxSettings,
        rng: random.Random,
        left: Counter[LeftReason],
    ) -> list[list[_Edit]]:
        words = []
        for position, word in enumerate(line.words):
            following = line.words[position + 1] if position + 1 < len(line.words) else None
            planned = self._plan_word(word, following, page, settings, rng)
            if isinstance(planned, list):
                words.append(planned)
            else:
                left[planned] += 1
        return words

    def _plan_word(
        self,
        word: Word,
        following: Word | None,
        page: pikepdf.Page,
        settings: BoxSettings,
        rng: random.Random,
    ) -> list[_Edit] | LeftReason:
        first, last = word.glyphs[0], word.glyphs[-1]
        if any(glyph.text is None for glyph in word.glyphs):
            return "no text"
        if any(glyph.stream.kind != "page" for glyph in word.glyphs):
            return "in a form"
        parts = _parts(word)
        if parts is None:
            return "not a word"
        prefix, core, suffix = parts
        if any(
            glyph.font != first.font or abs(glyph.tj_unit - first.tj_unit) > 1e-9
            for glyph in word.glyphs
        ):
            return "mixed fonts"
        key = _key(first, page)
        drawn = next((glyph for glyph in word.glyphs if glyph.width > 0), None)
        if key is None or key not in self.fonts or drawn is None or first.tj_unit == 0:
            return "no fit"

        direction = first.direction
        start = _along(first.origin, direction)
        end = max(extent(glyph, direction)[1] for glyph in word.glyphs)
        box_start, box_end, _, _ = extent(drawn, direction)
        scale = (box_end - box_start) / drawn.width
        spacing = first.advance - first.width * scale
        affixes = prefix + suffix
        affix_width = sum(glyph.width for glyph in affixes)

        limit = settings.tracking_limit * first.em
        held = settings.scope == "word" or following is None
        low, high = start, math.inf
        if held:
            tolerance = settings.tolerance
            low = max(low, end - max(tolerance.inward, ROUNDING) + MARGIN)
            high = end + max(tolerance.out, ROUNDING) - MARGIN
        if following is not None:
            next_start = extent(following.glyphs[0], direction)[0]
            em = max(last.em, following.glyphs[0].em)
            minimum = settings.word_gap.limit(em) if settings.word_gap is not None else 0.0
            floor = min(minimum, signed_gap(word.box, following.box))
            high = min(high, next_start - floor - 2 * MARGIN)
            if not held:
                low = max(low, next_start - LINE_GAP * em + 2 * MARGIN)
        if low > high:
            return "no fit"

        text = "".join(glyph.text or "" for glyph in core)
        font_words = self.fonts[key]
        table = font_words.table(_style(text))

        def natural_end(candidate: _Candidate, count: int) -> float:
            return start + (candidate.width + affix_width) * scale + (count - 1) * spacing

        def choices(band: float, lengths: Iterable[int]) -> Iterator[_Candidate]:
            for length in lengths:
                if length not in table:
                    continue
                count = length + len(affixes)
                slack = limit * (count - 1) if held else 0.0
                # The end of the word, then the width that gives it.
                lowest = max(low - slack, end - band)
                highest = min(high + slack, end + band)
                offset = start + affix_width * scale + (count - 1) * spacing
                widths, candidates = table[length]
                for candidate in candidates[
                    bisect.bisect_left(widths, (lowest - offset) / scale) : bisect.bisect_right(
                        widths, (highest - offset) / scale
                    )
                ]:
                    if candidate.text.lower() != text.lower():
                        yield candidate

        near = NEAR * first.em
        lengths = sorted(table)
        pool = list(choices(near, [n for n in lengths if abs(n - len(text)) <= 1]))
        pool = pool or list(choices(near, lengths))
        if pool:
            choice = rng.choice(pool)
        else:
            # Nothing is near: take the closest that the bounds allow.
            choice = min(
                choices(math.inf, lengths),
                key=lambda c: (abs(natural_end(c, len(c.codes) + len(affixes)) - end), c.text),
                default=None,
            )
        if choice is None:
            return "no fit"

        count = len(choice.codes) + len(affixes)
        finish = natural_end(choice, count)
        tracking = 0.0
        if held:
            target = min(high, max(low, finish))
            if abs(target - finish) > ROUNDING / 2:
                tracking = (target - finish) / (count - 1)
                finish = target
        codes = (
            *(_code_bytes(glyph) for glyph in prefix),
            *choice.codes,
            *(_code_bytes(glyph) for glyph in suffix),
        )
        return _site_edits(word.glyphs, codes, finish + spacing, -tracking / first.tj_unit)


def _site_edits(
    glyphs: Sequence[Glyph], codes: tuple[bytes, ...], pen: float, tracking: float
) -> list[_Edit]:
    """An edit for each instruction that holds a part of the word.

    The first takes the new codes, whose pen ends at ``pen``. Each ends where the source had
    the pen after that part of the word.
    """
    direction = glyphs[0].direction
    sites: dict[int, list[Glyph]] = {}
    for glyph in glyphs:
        sites.setdefault(glyph.instruction, []).append(glyph)
    edits = []
    for instruction, part in sites.items():
        head, tail = part[0], part[-1]
        wanted = _along(tail.origin, direction) + tail.advance
        written = pen if not edits else _along(head.origin, direction)
        edits.append(
            _Edit(
                instruction,
                head.operand,
                head.offset,
                tail.operand,
                tail.offset + tail.length,
                codes if not edits else (),
                tracking,
                (written - wanted) / head.tj_unit,
            )
        )
    return edits


def _printed_over(lines: Sequence[Line]) -> set[int]:
    over = set()
    for i, first in enumerate(lines):
        for j in range(i + 1, len(lines)):
            second = lines[j]
            height = min(first.box.top - first.box.bottom, second.box.top - second.box.bottom)
            if signed_gap(first.box, second.box) < -PRINTED_OVER * height:
                over.update((i, j))
    return over


def _failed_lines(result: GeometryResult, planned: set[int]) -> set[int]:
    """The planned lines to undo: those named by the violations, or all if none can be named."""
    named = set()
    for violation in result.violations:
        if violation.line is not None:
            named.add(violation.line)
        if violation.kind == "line-gap" and violation.other is not None:
            named.add(violation.other)
    return (named & planned) or set(planned)


def _apply(source: Sequence[Instruction], edits: Sequence[_Edit]) -> list[Instruction]:
    """The source instructions with the edits written in."""
    by_instruction: dict[int, list[_Edit]] = {}
    for edit in edits:
        by_instruction.setdefault(edit.instruction, []).append(edit)
    output: list[Instruction] = []
    for index, instruction in enumerate(source):
        if index in by_instruction:
            output.extend(_rewrite(instruction, by_instruction[index]))
        else:
            output.append(instruction)
    return output


def _rewrite(instruction: Instruction, edits: Sequence[_Edit]) -> list[Instruction]:
    """A show operator with new codes; ``Tj``, ``'`` and ``"`` become ``TJ`` to hold numbers."""
    op = operator_name(instruction)
    operands = list(instruction.operands)
    items = list(enumerate(operands[0])) if op == "TJ" else [(len(operands) - 1, operands[-1])]

    written: list[bytes | object] = []

    def write(data: bytes) -> None:
        if data and written and isinstance(written[-1], bytes):
            written[-1] += data
        elif data:
            written.append(data)

    def write_number(number: float) -> None:
        if abs(number) >= 0.0005:
            written.append(round(number, 3))

    pending = sorted(edits, key=lambda edit: (edit.operand, edit.offset))
    active: _Edit | None = None
    for operand, item in items:
        if not isinstance(item, pikepdf.String):
            if active is None:
                written.append(item)
            continue
        data = bytes(item)
        position = 0
        if active is not None:
            if active.last_operand != operand:
                continue
            position = active.end
            active = None
        while pending and pending[0].operand == operand:
            edit = pending.pop(0)
            write(data[position : edit.offset])
            for number, code in enumerate(edit.codes):
                if number:
                    write_number(edit.tracking)
                write(code)
            write_number(edit.after)
            if edit.last_operand != operand:
                active = edit
                position = len(data)
                break
            position = edit.end
        write(data[position:])

    array = pikepdf.Array(
        [pikepdf.String(item) if isinstance(item, bytes) else item for item in written]
    )
    show_array = pikepdf.ContentStreamInstruction([array], pikepdf.Operator("TJ"))
    if op == "TJ":
        return [show_array]
    if all(isinstance(item, bytes) for item in written):
        string = pikepdf.String(b"".join(written))
        return [pikepdf.ContentStreamInstruction([*operands[:-1], string], instruction.operator)]
    before: list[Instruction] = []
    if op == '"':
        before.append(pikepdf.ContentStreamInstruction([operands[0]], pikepdf.Operator("Tw")))
        before.append(pikepdf.ContentStreamInstruction([operands[1]], pikepdf.Operator("Tc")))
    if op in ("'", '"'):
        before.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*")))
    return [*before, show_array]
