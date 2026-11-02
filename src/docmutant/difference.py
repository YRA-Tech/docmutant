"""Compare the pictures of source and output: what a run does not change must look the same.

The other oracles read operators and objects, so they pass a page that a flat image turned black.
This one renders both files with pdfium and measures three things on each page:

- Outside what the run changes, the pixels must be the same. Only the source's places are left out
  of the comparison: new text that strays beyond where the old text was counts as a difference.
- Where an image was, the page may get lighter but not much darker. These places are left out of
  the first measure, so a black image would otherwise pass.
- A line of text away from any image must still show some ink.

What stays inside a line's own place is not seen: a word that crosses a border within its line,
say. And both pictures come from one engine, so this finds damage, not a difference between
engines. No difference is evidence, not proof, and ``compared`` says how much of a page it covers.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

import pikepdf
import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageDraw

from docmutant.codes import code_quads
from docmutant.graphics import TextPlaces, graphic_quads, placed, placements, text_places
from docmutant.mutate import StreamRef
from docmutant.outlines import Matrix, outline_quads
from docmutant.tracker import Line, Point

Quad = tuple[Point, Point, Point, Point]

SCALE = 2
"""Render at 144 dots per inch, at which a thin rule or a short word covers enough pixels."""

MARGIN = 1.5
"""Points around a place, for the edge of a shape that rendering blurs."""

REACH_DOWN, REACH_UP = 0.35, 1.05
"""Ems around a baseline. A font's own numbers cannot be trusted: on the sample, one says that its
descent is above the baseline, which would leave the lower part of each letter uncovered."""

CHANGED = 32
"""Grey levels. A smaller difference is rendering noise, not something a person would see."""

STRAY = 100
"""Pixels that may change on a page. Clean sample pages reach 19, and a short word covers more."""

DARKER = 64
"""Grey levels. More than the 25 that a flat image at 0xE6 takes from a white background."""

DARKENED = 0.01
"""The share of a page that may get darker where an image was. Clean sample pages stay under 0.1%,
which is what an empty image leaves when it uncovers a small dark shape under it."""

INK = 64
"""Grey levels from the commonest level of a line's place, for a pixel to count as ink."""

SOME_INK = 20
"""Pixels. A line with less shows too little for its loss to mean anything."""

FADED = 0.2
"""The share of a line's ink that must stay, for it not to count as lost."""

_UNIT_SQUARE = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


@dataclass(frozen=True)
class PageRegions:
    """The places of one source page that the run changes, in default user space."""

    lines: tuple[Quad, ...] = ()
    images: tuple[Quad, ...] = ()
    """Images and graphics, which become light."""

    other: tuple[Quad, ...] = ()
    """Outline items, machine-readable codes, and annotations that show an appearance."""

    margin: float = MARGIN


@dataclass(frozen=True)
class PageDifference:
    same_size: bool = True
    compared: float = 0.0
    """The share of the page that the pixel comparison covers."""

    changed: int = 0
    changed_pixels_ratio: float = 0.0
    mean_abs_error: float = 0.0
    darkened: float = 0.0
    """The share of the whole page that got much darker where an image was."""

    lines_lost: int = 0


def line_quad(line: Line) -> Quad:
    """The place of a line: all that its glyphs can draw, whatever their font says about them."""
    ux, uy = line.box.direction
    glyphs = [glyph for word in line.words for glyph in word.glyphs]
    low = min(
        glyph.origin[1] * ux - glyph.origin[0] * uy - REACH_DOWN * glyph.em for glyph in glyphs
    )
    high = max(
        glyph.origin[1] * ux - glyph.origin[0] * uy + REACH_UP * glyph.em for glyph in glyphs
    )
    return replace(line.box, bottom=min(line.box.bottom, low), top=max(line.box.top, high)).corners


def _annotation_quads(page: pikepdf.Page) -> Iterable[Quad]:
    annotations = page.obj.get("/Annots")
    if not isinstance(annotations, pikepdf.Array):
        return
    for annotation in annotations:
        if (
            isinstance(annotation, pikepdf.Dictionary)
            and "/AP" in annotation
            and "/Rect" in annotation
        ):
            x0, y0, x1, y1 = (float(value) for value in annotation.Rect)
            yield ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def _on_page(
    found: Sequence[tuple[StreamRef, Quad]],
    index: int,
    forms: Mapping[tuple[int, int], Iterable[Matrix]],
) -> Iterable[Quad]:
    """Where page ``index`` shows these places, which are in the frames of their streams."""
    for ref, quad in found:
        if ref.kind == "page" and ref.page_index == index:
            yield quad
        elif ref.kind != "page":
            yield from (placed(matrix, quad) for matrix in forms.get(ref.objgen, ()))


def changed_regions(
    pdf: pikepdf.Pdf,
    lines: Mapping[int, Sequence[Line]],
    *,
    images: bool,
    drawings: bool,
    tolerance: float = 0.0,
    text: TextPlaces | None = None,
    blocks: Mapping[int, Sequence[tuple[float, float, float, float]]] | None = None,
) -> list[PageRegions]:
    """What a run may change on each page of ``pdf``, its source, under these settings.

    ``tolerance`` is how far an edge of a line may move, in points, ``text`` is where the
    source shows text, if already found, and ``blocks`` are boxes in default user space that the
    run covers, as it covers formulas, which may get lighter like an image. Whatever a run keeps
    stays in the comparison, so a run that keeps its images must not change a pixel of them.
    """
    streams: list[tuple[StreamRef, Quad]] = []
    graphics_found: list[tuple[StreamRef, Quad]] = []
    if drawings:
        streams = [*outline_quads(pdf), *code_quads(pdf)]
        graphics_found = graphic_quads(pdf, text or text_places(pdf, lines))
    regions: list[PageRegions] = []
    for index, page in enumerate(pdf.pages):
        image_places, forms = placements(page)

        pictures = [
            placed(matrix, _UNIT_SQUARE)
            for matrices in (image_places.values() if images else ())
            for matrix in matrices
        ]
        regions.append(
            PageRegions(
                tuple(line_quad(line) for line in lines[index]),
                (
                    *pictures,
                    *_on_page(graphics_found, index, forms),
                    *(
                        ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
                        for x0, y0, x1, y1 in (blocks or {}).get(index, ())
                    ),
                ),
                (*_annotation_quads(page), *_on_page(streams, index, forms)),
                MARGIN + tolerance,
            )
        )
    return regions


class Frame:
    """From default user space to the pixels of a page as rendered, with its box and rotation."""

    def __init__(self, page: pdfium.PdfPage, size: tuple[int, int]) -> None:
        self.left, self.bottom, self.right, self.top = page.get_bbox()
        self.rotation = page.get_rotation()
        self.width, self.height = size

    def point(self, x: float, y: float) -> Point:
        across = (x - self.left) / (self.right - self.left)
        up = (y - self.bottom) / (self.top - self.bottom)
        if self.rotation == 90:
            return up * self.width, across * self.height
        if self.rotation == 180:
            return (1 - across) * self.width, up * self.height
        if self.rotation == 270:
            return (1 - up) * self.width, (1 - across) * self.height
        return across * self.width, (1 - up) * self.height

    def quad(self, quad: Quad) -> Quad:
        return tuple(self.point(x, y) for x, y in quad)


def _grown(quad: Quad, margin: float) -> Quad:
    """A quad with each side moved out by ``margin``."""

    def along(start: Point, end: Point) -> Point:
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        return (dx / length * margin, dy / length * margin) if length else (0.0, 0.0)

    ux, uy = along(quad[0], quad[1])
    vx, vy = along(quad[0], quad[3])
    return (
        (quad[0][0] - ux - vx, quad[0][1] - uy - vy),
        (quad[1][0] + ux - vx, quad[1][1] + uy - vy),
        (quad[2][0] + ux + vx, quad[2][1] + uy + vy),
        (quad[3][0] - ux + vx, quad[3][1] - uy + vy),
    )


def _mask(frame: Frame, quads: Iterable[Quad], margin: float) -> Image.Image:
    mask = Image.new("L", (frame.width, frame.height), 0)
    draw = ImageDraw.Draw(mask)
    for quad in quads:
        draw.polygon(_grown(frame.quad(quad), margin * SCALE), fill=255)
    return mask


def _ink(image: Image.Image) -> int:
    counts = image.histogram()
    background = max(range(256), key=counts.__getitem__)
    return sum(count for level, count in enumerate(counts) if abs(level - background) > INK)


def _lost(
    before: Image.Image,
    after: Image.Image,
    frame: Frame,
    regions: PageRegions,
    images: Image.Image,
) -> int:
    lost = 0
    for line in regions.lines:
        xs, ys = zip(*frame.quad(line), strict=True)
        box = (int(min(xs)), int(min(ys)), math.ceil(max(xs)), math.ceil(max(ys)))
        # Over a flat image, the text of a dark picture can no longer stand out. That is expected.
        if box[2] <= box[0] or box[3] <= box[1] or images.crop(box).getbbox() is not None:
            continue
        ink = _ink(before.crop(box))
        if ink > SOME_INK and _ink(after.crop(box)) < FADED * ink:
            lost += 1
    return lost


def _compare_page(
    source: pdfium.PdfPage, output: pdfium.PdfPage, regions: PageRegions
) -> PageDifference:
    before = source.render(scale=SCALE).to_pil().convert("L")
    after = output.render(scale=SCALE).to_pil().convert("L")
    if before.size != after.size:
        return PageDifference(same_size=False)
    frame = Frame(source, before.size)
    images = _mask(frame, regions.images, MARGIN)
    rest = _mask(frame, (*regions.lines, *regions.other), regions.margin)
    outside = ImageChops.invert(ImageChops.lighter(images, rest))

    counts = ImageChops.difference(before, after).histogram(mask=outside)
    compared = sum(counts)
    changed = sum(counts[CHANGED:])
    error = sum(level * count for level, count in enumerate(counts))
    darker = ImageChops.subtract(before, after).histogram(mask=ImageChops.subtract(images, rest))
    area = before.width * before.height
    return PageDifference(
        compared=compared / area,
        changed=changed,
        changed_pixels_ratio=changed / compared if compared else 0.0,
        mean_abs_error=error / compared / 255 if compared else 0.0,
        darkened=sum(darker[DARKER:]) / area,
        lines_lost=_lost(before, after, frame, regions, images),
    )


def compare(source: bytes, output: bytes, regions: Sequence[PageRegions]) -> list[PageDifference]:
    """Measure each page. Raises ``pypdfium2.PdfiumError`` if one cannot be rendered."""
    first = pdfium.PdfDocument(source)
    second = pdfium.PdfDocument(output)
    try:
        return [
            _compare_page(first[index], second[index], page) for index, page in enumerate(regions)
        ]
    finally:
        first.close()
        second.close()


def problems(pages: Sequence[PageDifference]) -> list[str]:
    """What is wrong with these pages, if anything."""
    found: list[str] = []
    for index, page in enumerate(pages):
        where = f"page {index + 1}"
        if not page.same_size:
            found.append(f"{where}: the page has another size")
        if page.changed > STRAY:
            found.append(f"{where}: {page.changed} pixels changed outside what the run changes")
        if page.darkened > DARKENED:
            found.append(f"{where}: {page.darkened:.1%} of the page got darker where an image was")
        if page.lines_lost:
            found.append(f"{where}: {page.lines_lost} lines of text no longer show")
    return found
