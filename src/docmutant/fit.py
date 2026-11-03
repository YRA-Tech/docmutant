"""Fitted replacement: write other words into a line without moving what the settings hold.

New codes flow from the start of the word. Every instruction that held a part of the word
begins by moving the pen to where the flow needs it and ends with the pen where the source
had it, by ``TJ`` numbers if needed. So nothing else moves, however it is positioned.

With word starts ``pinned``, each word keeps its start: an inner word must leave the minimum
gap before the next word, and a word whose end is held fits within the box tolerance, with
tracking if the limit allows it. The distance to the next word then still shows the width of
the source word. With ``moved``, words are chosen without regard to the width of the source
word, and the gaps of the line take up the difference, so only the line box is held, with
each gap too wide to be a word space.

With coverage ``all``, a word that has no real word falls through to an invented word and then
to a letter swap, and a page that cannot change every word stops the run.

The output is checked with the geometry oracle, and a line that fails goes back to its source
text. A replacement uses only codes that the document shows in that font, because a subset font
may have no glyph for any other code. A ``FontSource`` extends a font that has too few.
"""

from __future__ import annotations

import bisect
import contextlib
import math
import random
import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

import pikepdf

from docmutant.content import Instruction, operator_name
from docmutant.coverage import (
    RESIDUAL_LENGTH,
    CoverageResult,
    Token,
    check_coverage,
    differs,
    tokens,
)
from docmutant.geometry import ROUNDING, BoxSettings, GeometryResult, check_boxes, signed_gap
from docmutant.mutate import (
    TEXT_SHOW_OPERATORS,
    StreamRef,
    read_stream,
    write_stream,
)
from docmutant.reading import MOSTLY, SHORTEST, longest_common
from docmutant.substitute import CannotSubstitute, FontSource, ShownCode, embed_substitute
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
    "no text",
    "not a word",
    "in a form",
    "mixed fonts",
    "printed over",
    "no fit",
    "undone",
    "nothing to change",
]
Style = Literal["lower", "capitalized", "upper"]
Tier = Literal["real word", "invented word", "letter swap"]
Coverage = Literal["best-effort", "all"]
WordStarts = Literal["pinned", "moved"]
Kind = Literal["upper", "lower", "caseless", "digit", "punctuation", "unknown"]

NEAR = 0.05
"""Ems from the source end within which a replacement counts as near."""

MARGIN = 0.005
"""Points kept inside each bound, so that rounding in the output does not cross it."""

PRINTED_OVER = 0.5
"""Share of the shorter line's height that two lines overlap when one is printed over the
other, as in fake bold: replacing them with different words would be unreadable."""

HELD_GAP = 0.75
"""Ems above which a gap is structure, such as the space between table cells, and not a word
space: words moved across it could leave their cell."""

SPREAD = 0.3
"""Share of the source width by which the width aimed at for a moved word varies, at most."""

MOVED_TRIES = 10
"""Attempts at words for a moved line whose gaps fit, with less spread each time."""

SWAP_TRIES = 5
"""Attempts at an invented word or letter swap that is not a word of the source."""

CLOSEST = 3
"""A swap takes one of this many closest widths, so that a letter has no one fixed partner:
a fixed partner is a substitution cipher, which letter frequencies break."""

PAGE_TRIES = 4
"""Attempts at a page under coverage ``all``, each with other words: whether a tight line has
room for its last word depends on the words before it. A line that the geometry oracle refused
keeps its word starts in the next attempt: words of a formula lie too close to move."""

DRAWS = 20
"""Random draws from the near words before checking them all."""

PULL = 0.05
"""Ems of letter spacing, and of difference between it and the pull back from it, for an
instruction whose numbers between letters undo its spacing: see ``_pull_backs``."""

VOWELS = frozenset("aeiouy")


@dataclass(frozen=True)
class FitPolicy:
    """What a run does beyond the box settings. See 4.4 of the design."""

    coverage: Coverage = "best-effort"
    word_starts: WordStarts = "pinned"


class CoverageStop(Exception):
    """A page under coverage ``all`` still shows source words; the file must not be used."""

    def __init__(self, page: int, report: FitReport, lines: Sequence[Line] = ()) -> None:
        self.page = page
        self.report = report
        self.lines = lines
        """The page's lines as the fitter saw them, which the report's words count in."""
        named = ", ".join(
            f"line {word.line + 1} word {word.word + 1} ({word.text!r})"
            for word in report.coverage.unchanged[:5]
        )
        super().__init__(
            f"page {page + 1}: {len(report.coverage.unchanged)} words did not change: {named}"
        )


@dataclass(frozen=True)
class FitReport:
    words_replaced: int
    words_left: Mapping[LeftReason, int]
    """Words that keep their source text, by reason."""

    lines_undone: int
    geometry: GeometryResult
    coverage: CoverageResult
    tiers: Mapping[Tier, int] = field(default_factory=dict)
    """Words replaced at each step of the ladder."""

    lines_pinned: int = 0
    """Lines that word starts ``moved`` could not move, so that their starts stayed."""

    substituted: Mapping[str, str] = field(default_factory=dict)
    """Fonts whose program was replaced in this run so far: source name, substitute name."""


@dataclass(frozen=True)
class _Code:
    code: bytes
    width: float
    text: str | None = None


@dataclass(frozen=True)
class _Candidate:
    width: float
    """Sum of the glyph widths, in glyph space."""

    text: str
    codes: tuple[_Code, ...]


@dataclass(frozen=True)
class _Edit:
    """Moves the pen, writes new codes in place of a run of source codes, and moves it again."""

    stream: StreamRef
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

    before: float = 0.0
    """``TJ`` number before the first code."""

    gaps: tuple[float, ...] = ()
    """``TJ`` numbers between the codes, one for each pair, in place of ``tracking``."""


@dataclass(frozen=True)
class _Choice:
    """The new codes of a word, by site: one list for each instruction that held a part of it."""

    tier: Tier
    parts: tuple[tuple[_Code, ...], ...]
    gaps: tuple[float, ...] = ()
    """Points between each two letters beyond their advances, which put each new letter where the
    source's was, for a word whose letters the source drew closer than their widths."""


@dataclass(frozen=True)
class _Bounds:
    start: float
    end: float
    low: float
    high: float
    held: bool
    any_width: bool = False
    """The source's width should not show: a swap takes any glyph, not one of the closest."""


@dataclass
class _LinePlan:
    edits: list[_Edit]
    tiers: Counter[Tier]
    choices: dict[int, _Choice] = field(default_factory=dict)
    pinned: bool = False


@dataclass(frozen=True)
class _Run:
    settings: BoxSettings
    policy: FitPolicy
    rng: random.Random
    left: Counter[LeftReason]
    blocked: list[Word] = field(default_factory=list)
    """Words that have letters or digits and no fit."""

    nearby: frozenset[str] = frozenset()
    """The runs of letters or digits of the source's words on the line being planned: a new word
    must not be mostly part of one, which a reader would see as what is left of it."""


def _style(text: str) -> Style:
    if text.isupper():
        return "upper"
    return "capitalized" if text[0].isupper() else "lower"


def _recase(word: str, style: Style) -> str:
    if style == "upper":
        return word.upper()
    return word.capitalize() if style == "capitalized" else word


def _kind(text: str | None) -> Kind:
    if text is None:
        return "unknown"
    if any(char.isdigit() for char in text):
        return "digit"
    if any(char.isalpha() for char in text):
        return "upper" if text.isupper() else "lower" if text.islower() else "caseless"
    return "punctuation"


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
                    entries = tuple(self.codes[char] for char in text)
                    by_length.setdefault(len(text), []).append(
                        _Candidate(sum(e.width for e in entries), text, entries)
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


def _source_code(glyph: Glyph) -> _Code:
    return _Code(_code_bytes(glyph), glyph.width, glyph.text)


def _along(point: tuple[float, float], direction: tuple[float, float]) -> float:
    return point[0] * direction[0] + point[1] * direction[1]


def _sites(glyphs: Iterable[Glyph]) -> list[list[Glyph]]:
    """The glyphs of a word by the instruction that shows them."""
    sites: dict[tuple[StreamRef, int], list[Glyph]] = {}
    for glyph in glyphs:
        sites.setdefault((glyph.stream, glyph.instruction), []).append(glyph)
    return list(sites.values())


def _unit(glyph: Glyph, direction: tuple[float, float]) -> float:
    """Points along the baseline per glyph-space unit of a glyph with a width.

    From the bottom edge of its box, which a skew does not lengthen: the box's extent along the
    baseline also holds the overhang of a slanted glyph, which a wrong scale would take as width.
    """
    (x0, y0), (x1, y1) = glyph.box[0], glyph.box[1]
    return ((x1 - x0) * direction[0] + (y1 - y0) * direction[1]) / glyph.width


def _overhang(site: Sequence[Glyph], direction: tuple[float, float]) -> float:
    """How far the box of a glyph of this site reaches past its width: a slanted glyph's box
    leans past it, by the same amount for any glyph of the font at that size."""
    drawn = next((glyph for glyph in site if glyph.width > 0), None)
    if drawn is None:
        return 0.0
    return (
        extent(drawn, direction)[1]
        - _along(drawn.origin, direction)
        - drawn.width * _unit(drawn, direction)
    )


def _scale(site: Sequence[Glyph], direction: tuple[float, float]) -> tuple[float, float]:
    """Points along the baseline per glyph-space unit, and per glyph apart from its width."""
    drawn = next((glyph for glyph in site if glyph.width > 0), None)
    scale = site[0].tj_unit if drawn is None else _unit(drawn, direction)
    return scale, site[0].advance - site[0].width * scale


def _ink_changes(word: Word, parts: Sequence[Sequence[_Code]]) -> list[float]:
    """For each letter, how much further its box reaches with its new code, in points."""
    direction = word.glyphs[0].direction
    changes = []
    for site, codes in zip(_sites(word.glyphs), parts, strict=True):
        scale = _scale(site, direction)[0]
        changes.extend(
            (code.width - glyph.width) * scale for glyph, code in zip(site, codes, strict=True)
        )
    return changes


def _gaps(word: Word) -> tuple[float, ...]:
    """Points between each two letters of a word beyond the first one's advance: kerning."""
    direction = word.glyphs[0].direction
    return tuple(
        _along(second.origin, direction) - _along(first.origin, direction) - first.advance
        for first, second in zip(word.glyphs, word.glyphs[1:], strict=False)
    )


Pulls = Mapping[tuple[StreamRef, int], float]
"""Points that an instruction's numbers take back between each two letters: see
``_pull_backs``."""


def _pull_backs(glyphs: Sequence[Glyph]) -> dict[tuple[StreamRef, int], float]:
    """Points between letters that each instruction takes back from its letter spacing.

    Some converters set letter spacing and undo it with a number after each letter, so the
    letters sit as if there were none. New letters need the same number between them, or each
    stands apart as a word of its own. Only an instruction whose letters mostly sit so counts;
    a word space, or a kerned pair, is one pair among many.
    """
    pairs: dict[tuple[StreamRef, int], list[tuple[float, float, float]]] = {}
    for first, second in zip(glyphs, glyphs[1:], strict=False):
        key = (first.stream, first.instruction)
        if (
            (second.stream, second.instruction) != key
            or first.is_space
            or second.is_space
            or first.width <= 0
            or first.direction != second.direction
        ):
            continue
        direction = first.direction
        spacing = first.advance - first.width * _unit(first, direction)
        gap = _along(second.origin, direction) - _along(first.origin, direction) - first.advance
        pairs.setdefault(key, []).append((spacing, gap, first.em))
    pulls = {}
    for key, found in pairs.items():
        spacing = statistics.median(item[0] for item in found)
        gap = statistics.median(item[1] for item in found)
        em = statistics.median(item[2] for item in found)
        if spacing > PULL * em and abs(spacing + gap) <= PULL * em:
            pulls[key] = gap
    return pulls


def _pull(site: Sequence[Glyph], pulls: Pulls) -> float:
    return pulls.get((site[0].stream, site[0].instruction), 0.0)


def _natural(word: Word, parts: Sequence[Sequence[_Code]], pulls: Pulls | None = None) -> float:
    """From the start of the first new glyph to the end of the box that reaches furthest: not
    always the last one's, since a slanted letter can lean past a small one after it. With
    ``pulls``, the new letters are as close as the source's: see ``_pull_backs``."""
    direction = word.glyphs[0].direction
    pen = end = 0.0
    for site, codes in zip(_sites(word.glyphs), parts, strict=True):
        scale, spacing = _scale(site, direction)
        spacing += _pull(site, pulls or {})
        reach = _overhang(site, direction)
        for code in codes:
            end = max(end, pen + code.width * scale + reach)
            pen += code.width * scale + spacing
    return end


def _edits(
    word: Word,
    parts: Sequence[Sequence[_Code]],
    start: float,
    tracking: float | Sequence[float],
    pulls: Pulls | None = None,
) -> list[_Edit]:
    """Edits that draw ``parts`` from ``start`` and leave the pen where the source left it.

    ``tracking`` is points between each two codes: one number for all, or one for each pair.
    One number for all adds what ``pulls`` takes back between the source's letters; one for
    each pair is the source's own gaps, which hold it already.
    """
    steps = None if isinstance(tracking, (int, float)) else tuple(tracking)
    tracked = 0.0 if steps is not None else float(tracking)
    direction = word.glyphs[0].direction
    pen = start
    total = sum(len(codes) for codes in parts)
    written = 0
    edits = []
    for site, codes in zip(_sites(word.glyphs), parts, strict=True):
        head, tail = site[0], site[-1]
        unit = head.tj_unit
        source_head = _along(head.origin, direction)
        wanted = _along(tail.origin, direction) + tail.advance
        scale, spacing = _scale(site, direction)
        even = tracked + (_pull(site, pulls or {}) if steps is None else 0.0)
        before, here = 0.0, source_head
        first = written
        if codes:
            before = (source_head - pen) / unit
            for code in codes:
                pen += code.width * scale + spacing
                here = pen
                written += 1
                # The next site adds this gap with its own first number, if it is the next.
                if written < total:
                    pen += steps[written - 1] if steps is not None else even
        edits.append(
            _Edit(
                head.stream,
                head.instruction,
                head.operand,
                head.offset,
                tail.operand,
                tail.offset + tail.length,
                tuple(code.code for code in codes),
                -even / unit,
                (here - wanted) / unit,
                before,
                tuple(-step / unit for step in steps[first : first + len(codes) - 1])
                if steps is not None
                else (),
            )
        )
    return edits


def remove_words(pdf: pikepdf.Pdf, words: Iterable[Word]) -> None:
    """Take these words out of their streams, and leave the pen where each left it."""
    edits = [
        edit
        for word in words
        for edit in _edits(
            word,
            ((),) * len(_sites(word.glyphs)),
            _along(word.glyphs[0].origin, word.glyphs[0].direction),
            0.0,
        )
    ]
    for ref in sorted(
        {edit.stream for edit in edits},
        key=lambda ref: (ref.kind, ref.page_index, ref.objgen or ()),
    ):
        here = [edit for edit in edits if edit.stream == ref]
        write_stream(pdf, ref, _apply(read_stream(pdf, ref), here))


def _shift_edits(word: Word, shift: float) -> list[_Edit]:
    """Move a word that keeps its codes, with what it holds between them, such as kerning."""
    edits = []
    for site in _sites(word.glyphs):
        head, tail = site[0], site[-1]
        number = shift / head.tj_unit
        end = tail.offset + tail.length
        for operand, offset, value in (
            (head.operand, head.offset, -number),
            (tail.operand, end, number),
        ):
            edits.append(
                _Edit(
                    head.stream, head.instruction, operand, offset, operand, offset, (), 0.0, value
                )
            )
    return edits


def _space_edit(space: Glyph, pen: float) -> _Edit:
    """Draw a space at ``pen``, and leave the pen where the source left it."""
    number = (_along(space.origin, space.direction) - pen) / space.tj_unit
    return _Edit(
        space.stream,
        space.instruction,
        space.operand,
        space.offset,
        space.operand,
        space.offset + space.length,
        (_code_bytes(space),),
        0.0,
        -number,
        number,
    )


class WordFitter:
    """Replaces words on the pages of one document, from one vocabulary.

    ``vocabulary`` holds lowercase words. Which glyphs each font can draw is read once,
    from every page, before any page changes. ``fonts`` is where to find a substitute for
    a font with too few glyphs; without it, such a word stays and is reported. No replacement
    uses a word of ``avoid``, which is for words of the source that no page shows.
    """

    def __init__(
        self,
        pdf: pikepdf.Pdf,
        vocabulary: Iterable[str],
        fonts: FontSource | None = None,
        avoid: Iterable[str] = (),
    ) -> None:
        self.pdf = pdf
        self.vocabulary = sorted(set(vocabulary))
        self.source = fonts
        self.substituted: dict[str, str] = {}
        self.tried: set[tuple[object, ...]] = set()
        self.replaced: set[tuple[object, ...]] = set()
        self.shown: dict[tuple[object, ...], dict[int, ShownCode]] = {}
        self.source_words: set[str] = set(avoid)
        self.pulls: Pulls = {}
        """What the page being fitted takes back between letters: see ``_pull_backs``."""
        by_text: dict[tuple[object, ...], dict[str, _Code]] = {}
        self.codes: dict[tuple[object, ...], dict[bytes, _Code]] = {}
        for page in pdf.pages:
            try:
                glyphs = trace_page(page)
            except UnsupportedText:
                continue
            for glyph in glyphs:
                key = glyph.font_key
                self.shown.setdefault(key, {})[glyph.code] = ShownCode(
                    glyph.length, glyph.text, glyph.width
                )
                if glyph.width <= 0 or glyph.is_space or (glyph.code, glyph.length) == (32, 1):
                    continue
                if glyph.text is None or len(glyph.text) == 1:
                    code = _source_code(glyph)
                    self.codes.setdefault(key, {}).setdefault(code.code, code)
                    if glyph.text is not None:
                        by_text.setdefault(key, {}).setdefault(glyph.text, code)
            for line in group_lines(glyphs):
                for word in line.words:
                    letters = tokens(word)
                    if letters and all(isinstance(token, str) for token in letters):
                        self.source_words.add("".join(letters))
                    # A reader of the text splits at punctuation, so the parts count too.
                    self.source_words.update(
                        run.casefold() for run in re.findall(r"[^\W\d_]+|\d+", word.text)
                    )
        self.fonts = {key: _FontWords(table, self.vocabulary) for key, table in by_text.items()}

    def fit_page(
        self, index: int, settings: BoxSettings, seed: int = 0, policy: FitPolicy | None = None
    ) -> FitReport:
        """Replace what fits on one page. Text it cannot place raises ``UnsupportedText``.

        Under coverage ``all``, a page with a word left raises ``CoverageStop``, and the
        document is left part-way: it must not be used.
        """
        policy = policy or FitPolicy()
        if policy.word_starts == "moved" and settings.scope != "line":
            raise ValueError("word starts can move only with scope line")
        page = self.pdf.pages[index]
        if policy.coverage == "all":
            _own_forms(self.pdf, page)
        glyphs = trace_page(page)
        lines = group_lines(glyphs)
        self.pulls = _pull_backs(glyphs)

        planned, left, blocked = self._plan_page(index, lines, glyphs, settings, policy, seed)
        if policy.coverage == "all" and self.source is not None and self._extend_fonts(blocked):
            glyphs = trace_page(page)
            lines = group_lines(glyphs)
            self.pulls = _pull_backs(glyphs)
            planned, left, blocked = self._plan_page(index, lines, glyphs, settings, policy, seed)

        source: dict[StreamRef, list[Instruction]] = {}
        refused: set[int] = set()
        for attempt in range(1, PAGE_TRIES + 1):
            replaced = {number: sum(plan.tiers.values()) for number, plan in planned.items()}
            pinned = sum(plan.pinned for plan in planned.values())
            streams = {edit.stream for plan in planned.values() for edit in plan.edits}
            for ref in sorted(
                streams, key=lambda ref: (ref.kind, ref.page_index, ref.objgen or ())
            ):
                # Every attempt starts from the source: a stream that it does not edit goes back.
                source.setdefault(ref, read_stream(self.pdf, ref))
            undone = 0

            def attempt_lines(
                numbers: Iterable[int],
                planned: dict[int, _LinePlan] = planned,
                source: dict[StreamRef, list[Instruction]] = source,
            ) -> tuple[list[Line], GeometryResult]:
                """Write the edits of these lines only, and check the page."""
                chosen = sorted(numbers)
                for ref, instructions in source.items():
                    edits = [
                        edit
                        for number in chosen
                        for edit in planned[number].edits
                        if edit.stream == ref
                    ]
                    write_stream(self.pdf, ref, _apply(instructions, edits))
                output = group_lines(trace_page(self.pdf.pages[index]))
                return output, check_boxes(lines, output, settings)

            while True:
                output, result = attempt_lines(planned)
                if result.passed or not planned:
                    break
                failed = _failed_lines(result, set(planned))
                if _unnamed(result) and failed == set(planned) and len(planned) > 1:
                    # A count of lines names no line: find one that changes it on its own.
                    culprit = _culprit(
                        sorted(planned), lambda numbers: _unnamed(attempt_lines(numbers)[1])
                    )
                    failed = {culprit} if culprit is not None else failed
                undone += len(failed)
                # A violation that names no line says nothing about any line's word starts.
                if len(failed) < len(planned):
                    refused |= failed
                for number in failed:
                    del planned[number]
                    left["undone"] += replaced[number]

            tiers: Counter[Tier] = Counter()
            for plan in planned.values():
                tiers.update(plan.tiers)
            report = FitReport(
                sum(replaced[n] for n in planned),
                dict(left),
                undone,
                result,
                check_coverage(lines, output),
                dict(tiers),
                pinned,
                dict(self.substituted),
            )
            if policy.coverage != "all" or (report.coverage.passed and result.passed):
                return report
            if attempt < PAGE_TRIES:
                planned, left, _ = self._plan_page(
                    index, lines, glyphs, settings, policy, f"{seed}/{attempt}", frozenset(refused)
                )
        raise CoverageStop(index, report, lines)

    def _plan_page(
        self,
        index: int,
        lines: Sequence[Line],
        glyphs: Sequence[Glyph],
        settings: BoxSettings,
        policy: FitPolicy,
        seed: int | str,
        pinned: frozenset[int] = frozenset(),
    ) -> tuple[dict[int, _LinePlan], Counter[LeftReason], list[Word]]:
        run = _Run(settings, policy, random.Random(f"{seed}:{index}"), Counter())
        order = {id(glyph): place for place, glyph in enumerate(glyphs)}
        over = _printed_over(lines)
        planned: dict[int, _LinePlan] = {}
        for number, line in enumerate(lines):
            here = replace(
                run,
                nearby=frozenset(
                    part
                    for word in line.words
                    for part in _runs(word.text)
                    if len(part) >= SHORTEST
                ),
            )
            leader = None
            if number in over:
                if policy.coverage != "all":
                    run.left["printed over"] += len(line.words)
                    continue
                leader = next(
                    (
                        plan
                        for other, plan in planned.items()
                        if other in over[number]
                        and lines[other].text == line.text
                        and len(lines[other].words) == len(line.words)
                    ),
                    None,
                )
            if number in pinned:
                # Refused once: its words keep their starts, and its box may not grow toward the
                # lines around it, which can lie close or overlap, as the parts of a formula do.
                tolerance = replace(here.settings.tolerance, out=0.0)
                here = replace(here, settings=replace(here.settings, tolerance=tolerance))
            plan = self._plan_line(line, glyphs, order, here, leader, number in pinned)
            if plan.tiers:
                planned[number] = plan
        return planned, run.left, run.blocked

    def _extend_fonts(self, blocked: Sequence[Word]) -> bool:
        """Give a substitute to each font of a word that could not change. True if any did."""
        changed = False
        for key in sorted({glyph.font_key for word in blocked for glyph in word.glyphs}, key=str):
            if key in self.tried or not isinstance(key[0], int):
                continue
            self.tried.add(key)
            font = self.pdf.get_object(key)
            substitute = self.source.pick(font)
            if substitute is None:
                continue
            try:
                embedded = embed_substitute(self.pdf, font, substitute, self.shown.get(key, {}))
            except CannotSubstitute:
                continue
            self.substituted[substitute.source_name] = substitute.name
            self.replaced.add(key)
            factor = embedded.width_factor
            table = {
                code: _Code(code, entry.width * factor, entry.text)
                for code, entry in self.codes.get(key, {}).items()
            }
            by_text = {text: table[entry.code] for text, entry in self._by_text(key).items()}
            for text, (code, width) in embedded.added.items():
                entry = _Code(code, width, text)
                table[code] = entry
                by_text.setdefault(text, entry)
            self.codes[key] = table
            self.fonts[key] = _FontWords(by_text, self.vocabulary)
            changed = True
        return changed

    def substitute_remaining(self) -> dict[str, str]:
        """Give a substitute to every other font that the pages show, once they have changed.

        A code with unknown text no longer blocks a font when no page shows it any more. A font
        that cannot take a substitute keeps its program, which the caller reports. Returns the
        fonts that have a substitute now: source name, substitute name.
        """
        if self.source is None:
            return dict(self.substituted)
        shown: dict[tuple[object, ...], dict[int, ShownCode]] = {}
        for page in self.pdf.pages:
            for glyph in trace_page(page):
                shown.setdefault(glyph.font_key, {})[glyph.code] = ShownCode(
                    glyph.length, glyph.text, glyph.width
                )
        for key, codes in shown.items():
            if key in self.replaced or not isinstance(key[0], int):
                continue
            font = self.pdf.get_object(key)
            substitute = self.source.pick(font)
            if substitute is None:
                continue
            with contextlib.suppress(CannotSubstitute):
                embed_substitute(self.pdf, font, substitute, codes)
                self.replaced.add(key)
                self.substituted[substitute.source_name] = substitute.name
        return dict(self.substituted)

    def _by_text(self, key: tuple[object, ...]) -> Mapping[str, _Code]:
        return self.fonts[key].codes if key in self.fonts else {}

    def _plan_line(
        self,
        line: Line,
        glyphs: Sequence[Glyph],
        order: Mapping[int, int],
        run: _Run,
        leader: _LinePlan | None,
        pinned: bool = False,
    ) -> _LinePlan:
        words = line.words
        if run.policy.word_starts != "moved":
            return self._plan_pinned(words, None, run, leader, 0)
        if pinned:
            plan = self._plan_pinned(words, None, run, leader, 0)
            plan.pinned = bool(plan.tiers)
            return plan
        plan = _LinePlan([], Counter())
        for first, last in _stretches(words):
            part = words[first:last]
            after = words[last] if last < len(words) else None
            found = None
            if len(part) > 1:
                found = self._plan_moved(part, glyphs, order, run, leader, first)
            if found is None:
                found = self._plan_pinned(part, after, run, leader, first)
                found.pinned = len(part) > 1 and bool(found.tiers)
            plan.edits.extend(found.edits)
            plan.tiers.update(found.tiers)
            plan.choices.update(found.choices)
            plan.pinned = plan.pinned or found.pinned
        return plan

    def _plan_pinned(
        self,
        words: Sequence[Word],
        after: Word | None,
        run: _Run,
        leader: _LinePlan | None,
        offset: int,
    ) -> _LinePlan:
        plan = _LinePlan([], Counter())
        for position, word in enumerate(words):
            following = words[position + 1] if position + 1 < len(words) else after
            bounds = _bounds(word, following, run.settings)
            choice = self._follow(word, leader, position + offset) if bounds is not None else None
            choice = choice or self._choose(word, bounds, run)
            if isinstance(choice, str):
                run.left[choice] += 1
                if choice == "no fit" and tokens(word):
                    run.blocked.append(word)
                continue
            count = sum(len(codes) for codes in choice.parts)
            pulls = self.pulls if not choice.gaps else None
            finish = bounds.start + _natural(word, choice.parts, pulls) + sum(choice.gaps)
            tracking = 0.0
            if bounds.held and not choice.gaps:
                target = min(bounds.high, max(bounds.low, finish))
                if abs(target - finish) > ROUNDING / 2 and count > 1:
                    tracking = (target - finish) / (count - 1)
            steps = tuple(gap + tracking for gap in choice.gaps) if choice.gaps else tracking
            plan.edits.extend(_edits(word, choice.parts, bounds.start, steps, self.pulls))
            plan.choices[position + offset] = choice
            plan.tiers[choice.tier] += 1
        return plan

    def _plan_moved(
        self,
        words: Sequence[Word],
        glyphs: Sequence[Glyph],
        order: Mapping[int, int],
        run: _Run,
        leader: _LinePlan | None,
        offset: int,
    ) -> _LinePlan | None:
        """Words chosen for a stretch as a whole; the gaps take up what the widths change."""
        if run.policy.coverage != "all" and any(
            glyph.stream.kind != "page" for word in words for glyph in word.glyphs
        ):
            # Moving text would change a form that other drawings may share.
            return None
        widths = [word.box.end - word.box.start for word in words]
        gaps = [words[i + 1].box.start - words[i].box.end for i in range(len(words) - 1)]
        floors = []
        ceilings = []
        for i in range(len(gaps)):
            em = max(words[i].glyphs[-1].em, words[i + 1].glyphs[0].em)
            minimum = (
                run.settings.word_gap.limit(em) if run.settings.word_gap is not None else -math.inf
            )
            floors.append(min(minimum, signed_gap(words[i].box, words[i + 1].box)) + 2 * MARGIN)
            ceilings.append(LINE_GAP * em - 2 * MARGIN)

        lean = 0.0
        """How much narrower to aim each word, after an attempt that had too little room."""
        for attempt in range(MOVED_TRIES):
            spread = SPREAD * (1 - attempt / (MOVED_TRIES - 1))
            left: Counter[LeftReason] = Counter()
            blocked: list[Word] = []
            choices: list[_Choice | None] = []
            new_widths: list[float] = []
            remainder = 0.0
            for position, word in enumerate(words):
                last = position == len(words) - 1
                target = widths[position] + (remainder if last else remainder / 2)
                low, high = -math.inf, math.inf
                if last:
                    # What the gaps cannot take up, this word must.
                    low = target - sum(ceilings) + sum(gaps) + 2 * MARGIN
                    high = target + sum(gaps) - sum(floors) - 2 * MARGIN
                else:
                    target += run.rng.uniform(-spread, spread) * widths[position] - lean
                wide = not last and attempt < MOVED_TRIES // 2
                choice = self._follow(word, leader, position + offset) or self._choose_moved(
                    word, target, low, high, wide, run
                )
                if isinstance(choice, str):
                    left[choice] += 1
                    if choice == "no fit":
                        blocked.append(word)
                    choices.append(None)
                    new_widths.append(widths[position])
                else:
                    choices.append(choice)
                    new_widths.append(_natural(word, choice.parts, self.pulls))
                remainder += widths[position] - new_widths[-1]
            # Each gap gives in proportion to its room, so a tight gap does not break the line.
            if remainder >= 0:
                rooms = [
                    max(ceiling - gap, 0.0) for gap, ceiling in zip(gaps, ceilings, strict=True)
                ]
            else:
                rooms = [max(gap - floor, 0.0) for gap, floor in zip(gaps, floors, strict=True)]
            room = sum(rooms)
            if room >= abs(remainder) and (not blocked or attempt == MOVED_TRIES - 1):
                share = remainder / room if room else 0.0
                new_gaps = [gap + share * own for gap, own in zip(gaps, rooms, strict=True)]
                break
            short = max(abs(remainder) - room, 0.0) if remainder < 0 else 0.0
            for word in blocked:
                short += (word.box.end - word.box.start) / 2
            lean += short / len(gaps)
        else:
            return None

        run.left.update(left)
        run.blocked.extend(blocked)
        plan = _LinePlan([], Counter())
        start = words[0].box.start
        for position, word in enumerate(words):
            choice = choices[position]
            if choice is None:
                plan.edits.extend(_shift_edits(word, start - word.box.start))
            else:
                plan.edits.extend(_edits(word, choice.parts, start, 0.0, self.pulls))
                plan.choices[position + offset] = choice
                plan.tiers[choice.tier] += 1
            end = start + new_widths[position]
            if position < len(gaps):
                first = order[id(words[position + 1].glyphs[0])]
                pen = end
                for space in glyphs[order[id(word.glyphs[-1])] + 1 : first]:
                    plan.edits.append(_space_edit(space, pen))
                    pen += space.advance
                start = end + new_gaps[position]
        return plan

    def _follow(self, word: Word, leader: _LinePlan | None, position: int) -> _Choice | None:
        """The same text as the line this one is printed over, if its font can draw it."""
        chosen = leader.choices.get(position) if leader is not None else None
        if chosen is None:
            return None
        sites = _sites(word.glyphs)
        key = word.glyphs[0].font_key
        by_text = self._by_text(key)
        codes = []
        for code in (code for part in chosen.parts for code in part):
            own = self.codes.get(key, {}).get(code.code)
            if own is None or own.text != code.text:
                own = by_text.get(code.text) if code.text is not None else None
            if own is None:
                return None
            codes.append(own)
        if chosen.tier == "real word":
            return _Choice(chosen.tier, (tuple(codes), *((),) * (len(sites) - 1)))
        if len(codes) != len(word.glyphs):
            return None
        parts = []
        place = 0
        for site in sites:
            parts.append(tuple(codes[place : place + len(site)]))
            place += len(site)
        return _Choice(chosen.tier, tuple(parts), chosen.gaps)

    def _choose(self, word: Word, bounds: _Bounds | None, run: _Run) -> _Choice | LeftReason:
        """A word for this place, or the reason there is none."""
        if run.policy.coverage == "all":
            if not tokens(word):
                return "nothing to change"
            if bounds is None:
                return "no fit"
            choice = self._real_word(word, bounds, run) if self._can_take_real_word(word) else None
            return (
                choice
                or self._ladder(word, bounds, run)
                or self._kerned(word, bounds, run)
                or "no fit"
            )
        reason = self._refusal(word)
        if reason is not None or bounds is None:
            return reason or "no fit"
        return self._real_word(word, bounds, run) or "no fit"

    def _choose_moved(
        self, word: Word, target: float, low: float, high: float, wide: bool, run: _Run
    ) -> _Choice | LeftReason:
        """A word about ``target`` wide, whatever the source word's width was."""
        start = _along(word.glyphs[0].origin, word.glyphs[0].direction)
        free = _Bounds(start, start + target, start + low, start + high, False, wide)
        if run.policy.coverage == "all":
            if not tokens(word):
                return "nothing to change"
            choice = (
                self._real_word(word, free, run, any_length=True)
                if self._can_take_real_word(word)
                else None
            )
            return choice or self._ladder(word, free, run) or "no fit"
        reason = self._refusal(word)
        return reason or self._real_word(word, free, run, any_length=True) or "no fit"

    def _ladder(self, word: Word, bounds: _Bounds, run: _Run) -> _Choice | None:
        # Narrow capitals, as in III, have no capital as narrow: any letter or digit will do.
        return (
            self._swap(word, "invented word", bounds, run)
            or self._swap(word, "letter swap", bounds, run)
            or self._swap(word, "letter swap", bounds, run, any_kind=True)
        )

    def _kerned(self, word: Word, bounds: _Bounds, run: _Run) -> _Choice | None:
        """A swap for a word whose letters the source drew closer than their widths, as a formula
        does: drawn one after the other, no letters of the font fit its box. A swap keeps the count
        of letters, and each new one starts where the source's did, as a numerator over its
        denominator. The swap takes letters of close widths, so little else moves."""
        kern = sum(_gaps(word))
        if kern >= -ROUNDING:
            return None
        room = _Bounds(
            bounds.start,
            bounds.end - kern,
            bounds.low - kern,
            bounds.high - kern,
            bounds.held,
            bounds.any_width,
        )
        choice = self._ladder(word, room, run)
        if choice is None:
            return None
        direction = word.glyphs[0].direction
        advances = []
        for site, codes in zip(_sites(word.glyphs), choice.parts, strict=True):
            scale, spacing = _scale(site, direction)
            advances.extend(code.width * scale + spacing for code in codes)
        starts = [_along(glyph.origin, direction) for glyph in word.glyphs]
        gaps = tuple(
            after - before - advance
            for before, after, advance in zip(starts, starts[1:], advances, strict=False)
        )
        # Each letter keeps its start, so the word ends where its furthest-reaching letter does.
        end = max(
            extent(glyph, direction)[1] + change
            for glyph, change in zip(word.glyphs, _ink_changes(word, choice.parts), strict=True)
        )
        if not bounds.low <= end <= bounds.high:
            return None
        return _Choice(choice.tier, choice.parts, gaps)

    def _refusal(self, word: Word) -> LeftReason | None:
        first = word.glyphs[0]
        if any(glyph.text is None for glyph in word.glyphs):
            return "no text"
        if any(glyph.stream.kind != "page" for glyph in word.glyphs):
            return "in a form"
        if _parts(word) is None:
            return "not a word"
        if any(
            glyph.font != first.font or abs(glyph.tj_unit - first.tj_unit) > 1e-9
            for glyph in word.glyphs
        ):
            return "mixed fonts"
        return None

    def _can_take_real_word(self, word: Word) -> bool:
        """Under coverage ``all`` a form is no obstacle, but a digit beside the letters is."""
        first = word.glyphs[0]
        parts = _parts(word)
        return (
            parts is not None
            and all(glyph.text is not None for glyph in word.glyphs)
            and not any(c.isalnum() for g in (*parts[0], *parts[2]) for c in g.text)
            and all(
                g.font_key == first.font_key and abs(g.tj_unit - first.tj_unit) <= 1e-9
                for g in word.glyphs
            )
        )

    def _real_word(
        self, word: Word, bounds: _Bounds, run: _Run, any_length: bool = False
    ) -> _Choice | None:
        first = word.glyphs[0]
        key = first.font_key
        parts = _parts(word)
        drawn = next((glyph for glyph in word.glyphs if glyph.width > 0), None)
        if key not in self.fonts or drawn is None or first.tj_unit == 0 or parts is None:
            return None
        prefix, core, suffix = parts

        direction = first.direction
        scale = _unit(drawn, direction)
        spacing = first.advance - first.width * scale + _pull(_sites(word.glyphs)[0], self.pulls)
        affixes = prefix + suffix
        affix_width = sum(glyph.width for glyph in affixes)
        limit = run.settings.tracking_limit * first.em
        start, end, low, high, held = (
            bounds.start,
            bounds.end,
            bounds.low,
            bounds.high,
            bounds.held,
        )

        text = "".join(glyph.text or "" for glyph in core)
        strict = run.policy.coverage == "all"
        wanted = tokens(word)
        table = self.fonts[key].table(_style(text))

        # A real word's codes are all of the first site, whose box can lean past the last width.
        reach = _overhang(_sites(word.glyphs)[0], direction)

        def natural_end(candidate: _Candidate, count: int) -> float:
            return start + reach + (candidate.width + affix_width) * scale + (count - 1) * spacing

        def choices(band: float, lengths: Iterable[int]) -> Iterator[_Candidate]:
            for length in lengths:
                if length not in table:
                    continue
                count = length + len(affixes)
                slack = limit * (count - 1) if held else 0.0
                # The end of the word, then the width that gives it.
                lowest = max(low - slack, end - band)
                highest = min(high + slack, end + band)
                offset = start + reach + affix_width * scale + (count - 1) * spacing
                widths, candidates = table[length]
                for candidate in candidates[
                    bisect.bisect_left(widths, (lowest - offset) / scale) : bisect.bisect_right(
                        widths, (highest - offset) / scale
                    )
                ]:
                    if candidate.text.lower() != text.lower():
                        yield candidate

        def allowed(candidate: _Candidate) -> bool:
            if not strict:
                return True
            lowered = candidate.text.lower()
            return (
                lowered not in self.source_words
                and differs(wanted, tuple(lowered))
                and not _left_of(lowered, run.nearby)
            )

        near = NEAR * first.em
        lengths = sorted(table)
        pool: list[_Candidate] = []
        if not any_length:
            pool = list(choices(near, [n for n in lengths if abs(n - len(text)) <= 1]))
        pool = pool or list(choices(near, lengths))
        choice = None
        # Most candidates pass, so a few draws save checking each one.
        for _ in range(DRAWS if pool else 0):
            drawn_word = run.rng.choice(pool)
            if allowed(drawn_word):
                choice = drawn_word
                break
        if choice is None and pool:
            pool = [candidate for candidate in pool if allowed(candidate)]
            choice = run.rng.choice(pool) if pool else None
        if choice is None:
            # Nothing is near: take the closest that the bounds allow.
            ranked = sorted(
                choices(math.inf, lengths),
                key=lambda c: (abs(natural_end(c, len(c.codes) + len(affixes)) - end), c.text),
            )
            choice = next(filter(allowed, ranked), None)
        if choice is None:
            return None
        codes = (
            *(_source_code(glyph) for glyph in prefix),
            *choice.codes,
            *(_source_code(glyph) for glyph in suffix),
        )
        return _Choice("real word", (codes, *((),) * (len(_sites(word.glyphs)) - 1)))

    def _alternatives(self, glyph: Glyph, tier: Tier, any_kind: bool = False) -> list[_Code]:
        """Codes of the font that could stand for this glyph, at this step of the ladder.

        With ``any_kind``, any letter or digit, whatever the glyph's kind or case.
        """
        kind = _kind(glyph.text)
        table = self.codes.get(glyph.font_key, {})
        source = _code_bytes(glyph)
        folded = glyph.text.casefold() if glyph.text is not None else None
        pool = [
            code
            for code in table.values()
            if code.code != source
            and _kind(code.text) == kind
            and (code.text is None or code.text.casefold() != folded)
        ]
        if any_kind:
            pool = []
        elif tier == "invented word":
            if kind in ("upper", "lower"):
                vowel = (glyph.text or "").casefold()[:1] in VOWELS
                pool = [c for c in pool if ((c.text or "").casefold() in VOWELS) == vowel]
            return pool
        if pool:
            return pool
        return [
            code
            for code in table.values()
            if code.code != source
            and _kind(code.text) != "punctuation"
            and (code.text is None or code.text.casefold() != folded)
        ]

    def _swap(
        self, word: Word, tier: Tier, bounds: _Bounds, run: _Run, any_kind: bool = False
    ) -> _Choice | None:
        """Another glyph in place of each letter or digit, each in its own font."""
        direction = word.glyphs[0].direction
        scales: dict[int, float] = {}
        ordered: list[Glyph] = []
        sizes: list[int] = []
        for site in _sites(word.glyphs):
            scale = _scale(site, direction)[0]
            sizes.append(len(site))
            for glyph in site:
                scales[len(ordered)] = scale
                ordered.append(glyph)
        options = [
            [] if _kind(glyph.text) == "punctuation" else self._alternatives(glyph, tier, any_kind)
            for glyph in ordered
        ]
        if tier == "invented word" and any(
            not options[i] and _kind(glyph.text) != "punctuation" for i, glyph in enumerate(ordered)
        ):
            return None
        wanted = tokens(word)
        count = len(ordered)
        slack = run.settings.tracking_limit * word.glyphs[0].em * (count - 1) if bounds.held else 0
        low, high = bounds.low - slack, bounds.high + slack
        sites = _sites(word.glyphs)
        between = [_scale(site, direction)[1] + _pull(site, self.pulls) for site in sites]
        spacing = sum(each * len(site) for each, site in zip(between, sites, strict=True))
        spacing -= between[-1]
        base = bounds.start + spacing + _overhang(_sites(word.glyphs)[-1], direction)

        for _ in range(SWAP_TRIES):
            picks: list[_Code] = []
            for glyph, pool in zip(ordered, options, strict=True):
                if pool and bounds.any_width:
                    picks.append(run.rng.choice(pool))
                elif pool:
                    closest = sorted(pool, key=lambda c: (abs(c.width - glyph.width), c.code))
                    picks.append(run.rng.choice(closest[:CLOSEST]))
                else:
                    picks.append(_source_code(glyph))
            if not _settle(picks, options, scales, base, low, high):
                continue
            output: list[Token] = []
            for code in picks:
                if code.text is None:
                    output.append(int.from_bytes(code.code, "big"))
                else:
                    output.extend(char for char in code.text.casefold() if char.isalnum())
            if not differs(wanted, output):
                continue
            if (
                len(output) >= RESIDUAL_LENGTH
                and all(isinstance(token, str) for token in output)
                and "".join(output) in self.source_words
            ):
                continue
            # Each run between punctuation, such as each year of a range, is read on its own.
            pieces = _runs("".join(code.text or "" for code in picks))
            if any(
                (len(piece) >= RESIDUAL_LENGTH and piece in self.source_words)
                or _left_of(piece, run.nearby)
                for piece in pieces
            ):
                continue
            parts = []
            place = 0
            for size in sizes:
                parts.append(tuple(picks[place : place + size]))
                place += size
            # Settling counts widths; a slanted letter can reach past the last one.
            if not low <= bounds.start + _natural(word, parts, self.pulls) <= high:
                continue
            return _Choice(tier, tuple(parts))
        return None


def _settle(
    picks: list[_Code],
    options: Sequence[Sequence[_Code]],
    scales: Mapping[int, float],
    base: float,
    low: float,
    high: float,
) -> bool:
    """Change picks, one at a time, until the end of the word is within the bounds."""

    def end() -> float:
        return base + sum(code.width * scales[i] for i, code in enumerate(picks))

    def breach(value: float) -> float:
        return max(low - value, value - high, 0.0)

    for _ in range(2 * len(picks)):
        current = end()
        if not breach(current):
            return True
        best = None
        for i, pool in enumerate(options):
            for code in pool:
                value = current + (code.width - picks[i].width) * scales[i]
                if breach(value) < breach(current) and (best is None or breach(value) < best[0]):
                    best = (breach(value), i, code)
        if best is None:
            return False
        picks[best[1]] = best[2]
    return not breach(end())


def _stretches(words: Sequence[Word]) -> list[tuple[int, int]]:
    """Runs of words between gaps too wide to be word spaces. Those gaps stay where they are."""
    stretches = []
    first = 0
    for position in range(len(words) - 1):
        em = max(words[position].glyphs[-1].em, words[position + 1].glyphs[0].em)
        if words[position + 1].box.start - words[position].box.end > HELD_GAP * em:
            stretches.append((first, position + 1))
            first = position + 1
    stretches.append((first, len(words)))
    return stretches


def _bounds(word: Word, following: Word | None, settings: BoxSettings) -> _Bounds | None:
    """Where the end of a word with a pinned start may go, or None if nowhere."""
    first, last = word.glyphs[0], word.glyphs[-1]
    direction = first.direction
    start = _along(first.origin, direction)
    end = max(extent(glyph, direction)[1] for glyph in word.glyphs)
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
        # The oracle allows a gap ROUNDING under its floor. At a tight gap, as between slanted
        # words whose boxes overlap, a word as wide as the source's must fit.
        high = min(high, next_start - floor + ROUNDING - MARGIN)
        if not held:
            low = max(low, next_start - LINE_GAP * em + 2 * MARGIN)
    return None if low > high else _Bounds(start, end, low, high, held)


def _printed_over(lines: Sequence[Line]) -> dict[int, set[int]]:
    """For each line printed over another, the lines it is printed over."""
    over: dict[int, set[int]] = {}
    for i, first in enumerate(lines):
        for j in range(i + 1, len(lines)):
            second = lines[j]
            height = min(first.box.top - first.box.bottom, second.box.top - second.box.bottom)
            if signed_gap(first.box, second.box) < -PRINTED_OVER * height:
                over.setdefault(i, set()).add(j)
                over.setdefault(j, set()).add(i)
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


def _runs(text: str) -> list[str]:
    """The casefolded runs of letters or of digits of a text, as a reader splits it."""
    return re.findall(r"[^\W\d_]+|\d+", text.casefold())


def _left_of(new: str, nearby: Iterable[str]) -> bool:
    """Whether most of a new run is part of one of these: what a reader takes for what is left of
    a source word at the same place."""
    return len(new) >= SHORTEST and any(
        longest_common(new, old) >= MOSTLY * len(new) for old in nearby
    )


def _unnamed(result: GeometryResult) -> bool:
    """Whether a violation names no line, as when lines join or split."""
    return any(violation.line is None for violation in result.violations)


def _culprit(numbers: Sequence[int], breaks: Callable[[Sequence[int]], bool]) -> int | None:
    """One of ``numbers``, which break the page together, whose edits break it alone: found by
    halving. None if it takes lines from both halves."""
    while len(numbers) > 1:
        half = len(numbers) // 2
        if breaks(numbers[:half]):
            numbers = numbers[:half]
        elif breaks(numbers[half:]):
            numbers = numbers[half:]
        else:
            return None
    return numbers[0] if numbers else None


def draws_text(form: pikepdf.Stream, resources: pikepdf.Object | None, seen: frozenset) -> bool:
    """Whether a form shows text, or draws a form that does."""
    if form.objgen in seen:
        return False
    resources = form.get("/Resources", resources)
    xobjects = resources.get("/XObject") if resources is not None else None
    for instruction in pikepdf.parse_content_stream(form):
        op = operator_name(instruction)
        if op in TEXT_SHOW_OPERATORS:
            return True
        if op == "Do" and xobjects is not None:
            inner = xobjects.get(instruction.operands[0])
            if (
                isinstance(inner, pikepdf.Stream)
                and inner.get("/Subtype") == "/Form"
                and draws_text(inner, resources, seen | {form.objgen})
            ):
                return True
    return False


def _own_forms(pdf: pikepdf.Pdf, page: pikepdf.Page) -> None:
    """Give each drawing of a form with text a copy of its own, so that an edit changes one.

    A form can be drawn several times and on other pages. The resources of the page are
    copied too, since pages can share them.
    """
    count = [0]

    def own(
        instructions: Sequence[Instruction], resources: pikepdf.Object | None
    ) -> tuple[list[Instruction], pikepdf.Object | None] | None:
        xobjects = resources.get("/XObject") if resources is not None else None
        if xobjects is None:
            return None
        rewritten: list[Instruction] = []
        own_xobjects = None
        for instruction in instructions:
            form = None
            if operator_name(instruction) == "Do":
                form = xobjects.get(instruction.operands[0])
            if (
                not isinstance(form, pikepdf.Stream)
                or form.get("/Subtype") != "/Form"
                or not draws_text(form, resources, frozenset())
            ):
                rewritten.append(instruction)
                continue
            data = list(pikepdf.parse_content_stream(form))
            inner = own(data, form.get("/Resources", resources))
            copy = pdf.make_indirect(
                pdf.make_stream(
                    pikepdf.unparse_content_stream(inner[0] if inner is not None else data)
                )
            )
            for key, value in form.items():
                if key not in ("/Length", "/Filter", "/DecodeParms"):
                    copy[key] = value
            if inner is not None and inner[1] is not None:
                copy.Resources = inner[1]
            count[0] += 1
            if own_xobjects is None:
                own_xobjects = pikepdf.Dictionary({k: v for k, v in xobjects.items()})
            name = pikepdf.Name(f"/DocmutantForm{count[0]}")
            own_xobjects[name] = copy
            rewritten.append(pikepdf.ContentStreamInstruction([name], instruction.operator))
        if own_xobjects is None:
            return None
        copied = pikepdf.Dictionary({k: v for k, v in resources.items()})
        copied.XObject = own_xobjects
        return rewritten, copied

    result = own(list(pikepdf.parse_content_stream(page)), page.obj.get("/Resources"))
    if result is not None:
        page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(result[0]))
        page.obj.Resources = result[1]


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
            write_number(edit.before)
            for number, code in enumerate(edit.codes):
                if number:
                    write_number(edit.gaps[number - 1] if edit.gaps else edit.tracking)
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
