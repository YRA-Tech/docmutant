"""Coverage oracle: did every word change, and what does the output still show of the source?

A word is in scope when it holds a letter or a digit, or a glyph whose text the font does not
give. Punctuation alone is out of scope. A word counts as changed only when at least half of
it differs (by edit distance), so that ``Smith`` to ``Smyth`` does not pass.

The first check pairs the tracker's words in drawing order. The tracker also decides what a
word is, so ``residual_text`` asks pdfium, which shares no code with it, which words of the
source it can still read. ``other_layers`` names the text that neither of them looks at.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import pikepdf
import pypdfium2 as pdfium

from docmutant.mutate import content_streams, read_stream
from docmutant.tracker import Line, Word

ROUNDING_EMS = 0.01
"""Ems within which the distance to the next word counts as the source's distance."""

RESIDUAL_LENGTH = 3
"""Shortest run of letters or digits that residual text looks for: a shorter one matches by
chance."""

Token = str | int
"""A letter or digit, casefolded; or the code of a glyph whose text is not known."""


def tokens(word: Word) -> tuple[Token, ...]:
    """The parts of a word that must change."""
    found: list[Token] = []
    for glyph in word.glyphs:
        if glyph.text is None:
            found.append(glyph.code)
        else:
            found.extend(char for char in glyph.text.casefold() if char.isalnum())
    return tuple(found)


def _distance(first: Sequence[Token], second: Sequence[Token]) -> int:
    previous = list(range(len(second) + 1))
    for row, a in enumerate(first, 1):
        current = [row]
        for column, b in enumerate(second, 1):
            current.append(
                min(previous[column] + 1, current[-1] + 1, previous[column - 1] + (a != b))
            )
        previous = current
    return previous[-1]


def differs(source: Sequence[Token], output: Sequence[Token]) -> bool:
    """Whether at least half of ``source`` differs in ``output``, and at least one token."""
    return _distance(source, output) >= max(1, math.ceil(len(source) / 2))


@dataclass(frozen=True)
class UnchangedWord:
    line: int
    word: int
    text: str
    distance: int
    """Edit distance from the output word; 0 when no output word could be paired."""


@dataclass(frozen=True)
class CoverageResult:
    words_in_scope: int
    words_changed: int
    words_out_of_scope: int
    unchanged: tuple[UnchangedWord, ...]
    same_lines: bool
    """False when the output has another number of lines, so that no word can be paired."""

    next_word_pairs: int
    next_word_kept: int
    """Of the words with a next word, those at the source's distance from it. That distance
    shows the width of the source word, whatever word now stands there."""

    @property
    def passed(self) -> bool:
        return self.same_lines and not self.unchanged


def check_coverage(source: Sequence[Line], output: Sequence[Line]) -> CoverageResult:
    """Compare two pages' words. Words are paired by place, as in the geometry oracle."""
    in_scope = changed = out_of_scope = pairs = kept = 0
    unchanged: list[UnchangedWord] = []
    same_lines = len(source) == len(output)
    for number, line in enumerate(source):
        other = output[number] if same_lines else None
        same_count = other is not None and len(other.words) == len(line.words)
        for position, word in enumerate(line.words):
            wanted = tokens(word)
            if not wanted:
                out_of_scope += 1
                continue
            in_scope += 1
            if same_count:
                distance = _distance(wanted, tokens(other.words[position]))
                if differs(wanted, tokens(other.words[position])):
                    changed += 1
                else:
                    unchanged.append(UnchangedWord(number, position, word.text, distance))
            elif other is not None and all(differs(wanted, tokens(w)) for w in other.words):
                # Under a free word count no place pairs the words: none may be close to it.
                changed += 1
            else:
                unchanged.append(UnchangedWord(number, position, word.text, 0))
        if same_count:
            for position in range(len(line.words) - 1):
                em = line.words[position].glyphs[0].em
                before = line.words[position + 1].box.start - line.words[position].box.start
                after = other.words[position + 1].box.start - other.words[position].box.start
                pairs += 1
                kept += abs(before - after) <= ROUNDING_EMS * em
    return CoverageResult(
        in_scope, changed, out_of_scope, tuple(unchanged), same_lines, pairs, kept
    )


def _pdfium_words(page: pdfium.PdfPage) -> Counter[str]:
    text = page.get_textpage().get_text_range()
    found = re.findall(rf"[^\W\d_]{{{RESIDUAL_LENGTH},}}|\d{{{RESIDUAL_LENGTH},}}", text)
    return Counter(word.casefold() for word in found)


def residual_text(
    source: bytes, output: bytes, pages: Iterable[int] | None = None
) -> dict[int, tuple[str, ...]]:
    """The words pdfium reads on each page of ``source`` that it also reads there in ``output``.

    Replacement words avoid every word of the source, so a match is text that was not changed,
    or a chance match of an invented word. Pages with no match are not listed.
    """
    before = pdfium.PdfDocument(source)
    after = pdfium.PdfDocument(output)
    found: dict[int, tuple[str, ...]] = {}
    for index in range(len(before)) if pages is None else pages:
        shared = _pdfium_words(before[index]) & _pdfium_words(after[index])
        if shared:
            found[index] = tuple(sorted(shared))
    return found


def _text_keys(obj: pikepdf.Object, keys: frozenset[str]) -> bool:
    return isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)) and any(
        key in obj and len(str(obj[key])) > 0 for key in keys
    )


_STRUCTURE_TEXT = frozenset({"/Alt", "/ActualText", "/E", "/T"})
_ANNOTATION_TEXT = frozenset({"/Contents", "/RC", "/T", "/Subj", "/AP"})
_FIELD_TEXT = frozenset({"/V", "/DV", "/TU", "/T", "/AP"})


@dataclass(frozen=True)
class OtherLayers:
    """Text outside the content that the tracker reads, and images that may show text."""

    blocking: dict[str, int]
    """Layers that hold text no replacement has reached, with a count of what is in each."""

    images: int
    """Images, whose pixels no oracle reads: only a reviewer can say whether they show text."""


def other_layers(pdf: pikepdf.Pdf) -> OtherLayers:
    """What a run under coverage ``all`` must still remove or clean: see 5.6 of the design."""
    found: Counter[str] = Counter()
    if len(pdf.docinfo):
        found["document info"] += 1
    for key, label in (
        ("/Outlines", "outlines"),
        ("/PageLabels", "page labels"),
        ("/Dests", "named destinations"),
        ("/OCProperties", "layer names"),
        ("/OpenAction", "actions"),
    ):
        if key in pdf.Root:
            found[label] += 1
    names = pdf.Root.get("/Names")
    if names is not None:
        for key, label in (
            ("/EmbeddedFiles", "embedded files"),
            ("/JavaScript", "JavaScript"),
            ("/Dests", "named destinations"),
        ):
            if key in names:
                found[label] += 1
    images = 0
    for obj in pdf.objects:
        if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
            continue
        if "/Metadata" in obj:
            found["XMP metadata"] += 1
        kind = obj.get("/Type")
        if kind == "/StructElem" and _text_keys(obj, _STRUCTURE_TEXT):
            found["structure text"] += 1
        elif kind == "/Annot" and _text_keys(obj, _ANNOTATION_TEXT):
            found["annotation text"] += 1
        elif "/FT" in obj and _text_keys(obj, _FIELD_TEXT):
            found["form fields"] += 1
        if "/Thumb" in obj:
            found["thumbnails"] += 1
        if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image":
            images += 1
    for page in pdf.pages:
        for annotation in page.obj.get("/Annots", []) or []:
            if annotation.get("/Type") != "/Annot" and _text_keys(annotation, _ANNOTATION_TEXT):
                found["annotation text"] += 1
    for ref in content_streams(pdf):
        if ref.kind == "pattern" and any(
            str(getattr(i, "operator", "")) in ("Tj", "TJ", "'", '"') for i in read_stream(pdf, ref)
        ):
            found["text in patterns"] += 1
    return OtherLayers(dict(found), images)
