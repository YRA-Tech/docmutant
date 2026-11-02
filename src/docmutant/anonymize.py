"""The anonymize mode: every step of a run under a profile, and every check on what it saved.

A run changes a copy of the source, saves it, opens the saved bytes again and checks them. No
file comes out unless each check that the profile requires has passed on those bytes, so a
step that goes wrong in memory cannot pass by being checked in memory.

The whole document is changed. A run on some pages would leave the others reachable through
tags and links, and the text of the source with them.
"""

from __future__ import annotations

import hashlib
import io
import math
import zlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import pikepdf
import pypdfium2

from docmutant import difference, reading
from docmutant.codes import machine_codes, replace_machine_codes
from docmutant.content import Instruction, operator_name
from docmutant.coverage import (
    DRAWN_TEXT,
    check_coverage,
    curved_fills,
    other_layers,
    residual_text,
)
from docmutant.fit import CoverageStop, FitPolicy, WordFitter
from docmutant.geometry import BoxSettings, BoxTolerance, check_boxes, within
from docmutant.graphics import (
    graphics,
    replace_graphics,
    solid_images,
    text_places,
)
from docmutant.layers import (
    clean_layers,
    drawn,
    hidden_words,
    leftover_layers,
    residual_strings,
    soft_mask_images,
    tag_shape,
)
from docmutant.mutate import (
    FONT_PROGRAM_KEYS,
    IMAGE_KEEP,
    content_streams,
    font_descriptors,
    image_hashes,
    image_xobjects,
    read_stream,
)
from docmutant.outlines import FONT_PREFIX, outline_items, text_from_outlines
from docmutant.substitute import CannotSubstitute, FontSource
from docmutant.tracker import Glyph, Line, UnsupportedText, group_lines, trace_page

SOME_TEXT = 20
"""Words or paragraphs: with this many, a reader that saw nothing must have gone wrong."""


@dataclass(frozen=True)
class Profile:
    """What a run keeps and what it requires. The table in 4.2 of the design says why."""

    name: str
    settings: BoxSettings
    policy: FitPolicy
    substitute_fonts: bool
    replace_images: bool
    claims: tuple[str, ...]
    requires_coverage: bool
    """Whether a word left, or a word that pdfium still reads, stops the run."""

    replace_drawings: bool = False
    """Whether glyph shapes become new text, and graphics and machine-readable codes flat blocks."""

    reads_render: bool = False
    """Whether source words that a person could read in the rendered output stop the run."""


PRIVACY_TOLERANCE = 1.0
"""Points that each edge of a line may move under ``privacy`` unless the user says otherwise. Less
than a letter: at exact, a lone digit whose width no other glyph has cannot change."""

PROFILES = {
    "privacy": Profile(
        "privacy",
        BoxSettings("line", tolerance=within(PRIVACY_TOLERANCE), tracking_limit=0.02),
        FitPolicy("all", "moved"),
        substitute_fonts=True,
        replace_images=True,
        claims=("privacy",),
        requires_coverage=True,
        replace_drawings=True,
        reads_render=True,
    ),
    "training": Profile(
        "training",
        BoxSettings("line", tracking_limit=0.02),
        FitPolicy("best-effort", "pinned"),
        substitute_fonts=False,
        replace_images=False,
        claims=(),
        requires_coverage=False,
    ),
}


class ProfileError(ValueError):
    """A setting that the profile does not allow."""


def resolve_profile(
    name: str,
    *,
    tolerance: BoxTolerance | None = None,
    coverage: str | None = None,
    word_starts: str | None = None,
    keep_images: bool = False,
    keep_drawings: bool = False,
) -> Profile:
    """A profile with the settings that the user changed, if the profile allows them.

    ``privacy`` holds its coverage and word starts: they are what its claim rests on. It can
    keep images or drawings, which no oracle reads, but the file then has no claim.
    """
    profile = PROFILES[name]
    if name == "privacy" and (coverage not in (None, "all") or word_starts not in (None, "moved")):
        raise ProfileError("the privacy profile needs coverage all and word starts moved")
    policy = FitPolicy(
        coverage or profile.policy.coverage, word_starts or profile.policy.word_starts
    )
    settings = (
        profile.settings if tolerance is None else replace(profile.settings, tolerance=tolerance)
    )
    return replace(
        profile,
        settings=settings,
        policy=policy,
        replace_images=profile.replace_images and not keep_images,
        replace_drawings=profile.replace_drawings and not keep_drawings,
        reads_render=profile.reads_render and not (keep_images or keep_drawings),
        claims=() if keep_images or keep_drawings else profile.claims,
        requires_coverage=profile.requires_coverage or policy.coverage == "all",
    )


@dataclass(frozen=True)
class Check:
    """One oracle on the saved file. ``details`` holds counts, never text of the source."""

    name: str
    target: str
    passed: bool
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    data: bytes | None
    """The output, or None when the run stopped: see ``problems``."""

    problems: tuple[str, ...]
    """Why the run stopped. These can quote the source, so they stay out of the manifest."""

    checks: tuple[Check, ...] = ()
    mutations: tuple[Mapping[str, Any], ...] = ()
    kept: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    fonts: tuple[Mapping[str, Any], ...] = ()
    fingerprint: Mapping[str, Any] = field(default_factory=dict)
    pages: int = 0


def _token(value: object) -> bytes:
    return pikepdf.Array([value]).unparse()[1:-1].strip()


def _palette(space: pikepdf.Object) -> tuple[bytes, int, bool]:
    """The table of an indexed colour space, its colours' size, and whether they are inks."""
    table = space[3]
    data = table.read_bytes() if isinstance(table, pikepdf.Stream) else bytes(table)
    base = space[1]
    family = base[0] if isinstance(base, pikepdf.Array) else base
    if family == "/ICCBased":
        size = int(base[1].get("/N", 3))
    else:
        size = {"/DeviceGray": 1, "/G": 1, "/DeviceCMYK": 4, "/CMYK": 4}.get(str(family), 3)
    return data, size, size == 4 or family in ("/Separation", "/DeviceN")


def _lightest(space: pikepdf.Object) -> int:
    """The entry of an indexed colour space that is closest to no colour at all."""
    data, size, inks = _palette(space)
    colours = [data[i : i + size] for i in range(0, len(data) - size + 1, size)]
    if not colours:
        return 0
    amounts = [sum(colour) for colour in colours]
    return amounts.index(min(amounts) if inks else max(amounts))


def _fill(space: object, depth: int) -> int:
    """A byte for samples of a flat, light image, by what its numbers mean in its colour space.

    A sample counts ink in a subtractive space, where the same number that is mid-grey in RGB
    is close to black. A dark image would hide the text drawn over it.
    """
    family = space[0] if isinstance(space, pikepdf.Array) else space
    if family in ("/Indexed", "/I"):
        # Only a depth of 8 has a byte for each sample; a lower one has a low index in its bits.
        return _lightest(space) if depth == 8 else 0
    inks = family in ("/DeviceCMYK", "/CMYK", "/Separation", "/DeviceN") or (
        family == "/ICCBased" and int(space[1].get("/N", 3)) == 4
    )
    if inks:
        return 0x00
    return 0xE6 if depth == 8 else 0xFF


def shows_light(image: pikepdf.Object) -> bool:
    """Whether a flat image of this colour space is light. One with a palette of dark colours only
    is not: without a mask that gives it a shape, it would be a dark block."""
    space = image.get("/ColorSpace")
    family = space[0] if isinstance(space, pikepdf.Array) else space
    if family not in ("/Indexed", "/I"):
        return True
    data, size, inks = _palette(space)
    index = _lightest(space) if int(image.get("/BitsPerComponent", 8)) == 8 else 0
    colour = data[index * size : (index + 1) * size]
    if len(colour) < size:
        return False
    amount = sum(colour) / (255 * size)
    return amount <= 0.2 if inks else amount >= 0.8


def _components(space: object) -> int:
    family = space[0] if isinstance(space, pikepdf.Array) else space
    if family in ("/Indexed", "/I", "/Separation", "/CalGray", "/DeviceGray", "/G"):
        return 1
    if family == "/DeviceN":
        return len(space[1])
    if family == "/ICCBased":
        return int(space[1].get("/N", 3))
    return 4 if family in ("/DeviceCMYK", "/CMYK") else 3


def _flat_inline(image: pikepdf.Object) -> bytes:
    """An inline image of the same size and depth, flat and light, or unpainted if a stencil."""
    width, height = int(image.Width), int(image.Height)
    head = [b"BI /W %d /H %d" % (width, height)]
    space = image.get("/ColorSpace")
    if image.get("/ImageMask", False):
        head.append(b"/IM true")
        depth, components, fill = 1, 1, 0xFF
    else:
        depth = int(image.get("/BitsPerComponent", 8))
        head.append(b"/BPC %d" % depth)
        components, fill = 1, 0xE6 if depth == 8 else 0xFF
        if space is not None:
            head.append(b"/CS " + _token(space))
            components, fill = _components(space), _fill(space, depth)
    size = math.ceil(width * components * depth / 8) * height
    # Neither 0xE6, 0xFF nor 0x00 can begin the "EI" that ends the image.
    return b" ".join(head) + b" ID " + bytes([fill]) * size + b" EI\n"


def replace_inline_images(pdf: pikepdf.Pdf) -> int:
    """Replace each inline image of a page, form or pattern; a glyph's belongs to its font."""
    count = 0
    for ref in content_streams(pdf):
        if ref.kind == "charproc":
            continue
        instructions = read_stream(pdf, ref)
        if not any(operator_name(i) == "INLINE IMAGE" for i in instructions):
            continue
        chunks: list[bytes] = []
        run: list[Instruction] = []
        for instruction in instructions:
            if operator_name(instruction) == "INLINE IMAGE":
                chunks.extend((pikepdf.unparse_content_stream(run), b"\n"))
                chunks.append(_flat_inline(instruction.iimage.obj))
                run = []
                count += 1
            else:
                run.append(instruction)
        chunks.append(pikepdf.unparse_content_stream(run))
        data = b"".join(chunks)
        if ref.kind == "page":
            pdf.pages[ref.page_index].obj.Contents = pdf.make_stream(data)
        else:
            pdf.get_object(ref.objgen).write(data)
    return count


def _flat_image(image: pikepdf.Stream, mask: bool) -> None:
    """Replace the samples of an image, at the same size, depth and colour space.

    A mask becomes empty: its shape can show a logo or a signature, and an image that was see-
    through in places must not become a block that hides the text under it. The filter becomes
    Flate, because the numbers of a CMYK JPEG mean different things to different readers.
    """
    width, height = int(image.Width), int(image.Height)
    stencil = bool(image.get("/ImageMask", False))
    depth = 1 if stencil else int(image.get("/BitsPerComponent", 8))
    space = image.get("/ColorSpace")
    if image.get("/Filter") == "/JPXDecode" and space is None and not stencil:
        # A JPEG 2000 image can hold its colour space inside it.
        space = image.ColorSpace = pikepdf.Name.DeviceGray
    if stencil:
        # A stencil paints where its samples are 0.
        components, fill = 1, 0xFF
    else:
        components = 1 if space is None else _components(space)
        fill = 0x00 if mask else (0xE6 if space is None else _fill(space, depth))
    size = math.ceil(width * components * depth / 8) * height
    for key in [k for k in image if k not in IMAGE_KEEP and k != "/OC"]:
        del image[key]
    for key in ("/Decode", "/DecodeParms", "/SMaskInData", "/Matte"):
        if key in image:
            del image[key]
    image.write(zlib.compress(bytes([fill]) * size, 9), filter=pikepdf.Name.FlateDecode)
    if not stencil:
        image.BitsPerComponent = depth


def replace_all_images(pdf: pikepdf.Pdf, solid: Iterable[tuple[int, int]] = ()) -> int:
    """Replace every image with a flat one that hides nothing the source's image did not.

    The images in ``solid`` lose their soft masks first, and so become blocks: see
    ``graphics.solid_images``.
    """
    images = image_xobjects(pdf)
    for objgen in solid:
        del pdf.get_object(objgen)["/SMask"]
    masks = {
        image[key].objgen
        for image in images
        for key in ("/SMask", "/Mask")
        if isinstance(image.get(key), pikepdf.Stream)
    } | soft_mask_images(pdf)
    for image in images:
        _flat_image(image, image.objgen in masks)
    return len(images) + replace_inline_images(pdf)


def _font_programs(pdf: pikepdf.Pdf) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for name, descriptor in font_descriptors(pdf):
        for key in FONT_PROGRAM_KEYS:
            program = descriptor.get(key)
            if isinstance(program, pikepdf.Stream):
                found.add((name, hashlib.sha256(program.read_raw_bytes()).hexdigest()))
    return found


def _drop_unused_form_fonts(pdf: pikepdf.Pdf) -> int:
    """Remove the programs of a form's default fonts, which no page draws with."""
    forms = pdf.Root.get("/AcroForm")
    resources = forms.get("/DR") if forms is not None else None
    fonts = resources.get("/Font") if resources is not None else None
    if fonts is None:
        return 0
    shown = {glyph.font_key for page in pdf.pages for glyph in trace_page(page)}
    count = 0
    for key in fonts:
        font = fonts[key]
        if font.objgen in shown:
            continue
        descendants = font.get("/DescendantFonts")
        holder = descendants[0] if descendants else font
        descriptor = holder.get("/FontDescriptor")
        if descriptor is None:
            continue
        for program in FONT_PROGRAM_KEYS:
            if program in descriptor:
                del descriptor[program]
                count += 1
    return count


def _save(pdf: pikepdf.Pdf) -> bytes:
    buffer = io.BytesIO()
    pdf.save(
        buffer,
        compress_streams=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate,
        deterministic_id=True,
        encryption=False,
    )
    return buffer.getvalue()


def _fingerprint(pdf: pikepdf.Pdf, kept_fonts: Iterable[str]) -> dict[str, Any]:
    """What stays exact and can link the file to its source, for the reviewer. See 4.2."""
    sizes: Counter[str] = Counter()
    images: Counter[str] = Counter()
    transforms: set[tuple[str, ...]] = set()
    paths = 0
    for page in pdf.pages:
        box = page.mediabox
        sizes[f"{float(box[2]) - float(box[0]):g} x {float(box[3]) - float(box[1]):g}"] += 1
        for width, height, depth in drawn(page)[1]:
            images[f"{width} x {height} x {depth}"] += 1
    for ref in content_streams(pdf):
        for instruction in read_stream(pdf, ref):
            operator = operator_name(instruction)
            if operator == "cm":
                transforms.add(tuple(str(value) for value in instruction.operands))
            elif operator in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*"):
                paths += 1
    return {
        "page_sizes": dict(sizes),
        "image_sizes": dict(images),
        "distinct_transforms": len(transforms),
        "painted_paths": paths,
        "fonts_kept": sorted(kept_fonts),
    }


def _source_glyphs(page: pikepdf.Page, new_text: set[tuple[object, ...]]) -> list[Glyph]:
    """What the page shows apart from paragraphs made from outlines, which had no source words."""
    return [glyph for glyph in trace_page(page) if glyph.font_key not in new_text]


def _new_text_fonts(pdf: pikepdf.Pdf) -> set[tuple[object, ...]]:
    return {
        obj.objgen
        for obj in pdf.objects
        if isinstance(obj, pikepdf.Dictionary)
        and str(obj.get("/BaseFont", "")).startswith("/" + FONT_PREFIX)
    }


def _check_output(
    profile: Profile,
    source: pikepdf.Pdf,
    source_bytes: bytes,
    data: bytes,
    source_lines: Mapping[int, Sequence[Line]],
    forbidden: set[str],
) -> tuple[list[Check], list[str], pikepdf.Pdf, dict[str, float]]:
    """Run every oracle on the saved bytes. Returns the checks, what failed, the file and counts."""
    problems: list[str] = []
    output = pikepdf.open(io.BytesIO(data))
    new_text = _new_text_fonts(output)

    moved = 0.0
    violations = in_scope = changed = out_of_scope = pairs = kept = 0
    for index, page in enumerate(output.pages):
        try:
            lines = group_lines(_source_glyphs(page, new_text))
        except UnsupportedText as error:
            violations += 1
            problems.append(f"page {index + 1}: the output cannot be traced: {error}")
            continue
        geometry = check_boxes(source_lines[index], lines, profile.settings)
        violations += len(geometry.violations)
        moved = max(moved, geometry.largest_line_move or 0.0)
        if not geometry.passed:
            problems.append(f"page {index + 1}: {len(geometry.violations)} geometry violations")
        coverage = check_coverage(source_lines[index], lines)
        in_scope += coverage.words_in_scope
        changed += coverage.words_changed
        out_of_scope += coverage.words_out_of_scope
        pairs += coverage.next_word_pairs
        kept += coverage.next_word_kept
        if profile.requires_coverage and not coverage.passed:
            words = ", ".join(repr(word.text) for word in coverage.unchanged[:5])
            problems.append(
                f"page {index + 1}: {len(coverage.unchanged)} words did not change: {words}"
            )
    target = "docmutant tracker"
    checks = [
        Check(
            "geometry",
            target,
            violations == 0,
            {"violations": violations, "largest_line_move": moved},
        ),
    ]
    residual = residual_text(source_bytes, data)
    residual_count = sum(len(words) for words in residual.values())
    if profile.requires_coverage:
        checks.append(
            Check(
                "coverage",
                target,
                in_scope == changed,
                {"words_in_scope": in_scope, "words_changed": changed},
            )
        )
        checks.append(
            Check(
                "residual-text",
                f"pypdfium2 {pypdfium2.PYPDFIUM_INFO}",
                not residual,
                {"words": residual_count},
            )
        )
        for index, words in sorted(residual.items()):
            problems.append(f"page {index + 1}: pdfium still reads {', '.join(words[:5])}")

    strings = residual_strings(output, forbidden)
    checks.append(Check("hidden-text", target, not strings, {"keys": len(strings)}))
    for key, words in sorted(strings.items()):
        problems.append(f"strings under {key} still hold {', '.join(words[:5])}")

    leftover = leftover_layers(output)
    checks.append(Check("other-layers", target, not leftover, dict(leftover)))
    for label, count in sorted(leftover.items()):
        problems.append(f"{count} left in the layer {label}")

    source_id = source.trailer.get("/ID")
    output_id = output.trailer.get("/ID")
    same_id = (
        source_id is not None
        and output_id is not None
        and bytes(source_id[0]) == bytes(output_id[0])
    )
    checks.append(Check("file-identifier", target, not same_id))
    if same_id:
        problems.append("the file identifier is the source's")

    same_tags = tag_shape(source) == tag_shape(output)
    checks.append(Check("structure", target, same_tags))
    if not same_tags:
        problems.append("the tag tree changed its shape")

    wrong_images = [
        index
        for index, page in enumerate(output.pages)
        if drawn(source.pages[index])[1] != drawn(page)[1]
    ]
    checks.append(Check("image-geometry", target, not wrong_images))
    for index in wrong_images:
        problems.append(f"page {index + 1}: an image changed its size or depth")

    metrics = {
        "words_in_scope": float(in_scope),
        "words_changed": float(changed),
        "words_out_of_scope": float(out_of_scope),
        "next_word_pairs": float(pairs),
        "next_word_kept": float(kept),
        "residual_words": float(residual_count),
    }
    return checks, problems, output, metrics


def _check_render(
    source_read: Sequence[Sequence[reading.ReadWord]],
    data: bytes,
    known: Iterable[str],
    reader: reading.Reader,
    metrics: Mapping[str, float],
    paragraphs: int,
) -> tuple[Check, list[str]]:
    """Read the saved file's pages. A reader that saw nothing where there is text has not looked."""
    try:
        output_read = reading.read_pages(data, reader)
    except reading.CannotRead as error:
        return Check("readable-text", reader.name, False), [str(error)]
    found = reading.readable(source_read, output_read, known)
    source_words = sum(len(reading.tokens(page)) for page in source_read)
    output_words = sum(len(reading.tokens(page)) for page in output_read)
    problems = [
        f"page {index + 1}: can still be read: {', '.join(words[:5])}"
        for index, words in sorted(found.items())
    ]
    if metrics["words_in_scope"] + paragraphs >= SOME_TEXT and not (source_words and output_words):
        problems.append(
            f"the reader saw {source_words} words of the source and {output_words} of the output, "
            "though the pages show text, so this check has not looked"
        )
    check = Check(
        "readable-text",
        reader.name,
        not problems,
        {
            "source_words": source_words,
            "output_words": output_words,
            "matches": sum(len(words) for words in found.values()),
        },
    )
    return check, problems


def _check_difference(
    source: bytes, data: bytes, regions: Sequence[difference.PageRegions]
) -> tuple[Check, list[str]]:
    """Compare the pictures of source and saved file. A page that does not render has not passed."""
    target = f"pypdfium2 {pypdfium2.PYPDFIUM_INFO}"
    try:
        pages = difference.compare(source, data, regions)
    except pypdfium2.PdfiumError as error:
        return Check("render-difference", target, False), [f"a page does not render: {error}"]
    problems = difference.problems(pages)
    check = Check(
        "render-difference",
        target,
        not problems,
        {
            "pages": [
                {
                    "compared": round(page.compared, 4),
                    "changed_pixels_ratio": round(page.changed_pixels_ratio, 6),
                    "mean_abs_error": round(page.mean_abs_error, 6),
                    "darkened": round(page.darkened, 6),
                    "lines_lost": page.lines_lost,
                }
                for page in pages
            ],
        },
    )
    return check, problems


def anonymize(
    source: bytes,
    profile: Profile,
    *,
    vocabulary: Iterable[str] = (),
    fonts: FontSource | None = None,
    seed: int = 0,
    reader: reading.Reader | None = None,
) -> Result:
    """Change a copy of ``source`` under ``profile``. See the module's description."""
    original = pikepdf.open(io.BytesIO(source))
    pdf = pikepdf.open(io.BytesIO(source))
    found = other_layers(original)
    avoid = hidden_words(original)
    problems: list[str] = []

    source_lines: dict[int, list[Line]] = {}
    for index, page in enumerate(original.pages):
        try:
            source_lines[index] = group_lines(trace_page(page))
        except UnsupportedText as error:
            problems.append(f"page {index + 1}: the text cannot be traced: {error}")
    if problems:
        return Result(None, tuple(problems), pages=len(original.pages))

    source_read: list[list[reading.ReadWord]] = []
    if profile.reads_render:
        try:
            reader = reader or reading.default_reader()
            source_read = reading.read_pages(source, reader)
        except reading.CannotRead as error:
            return Result(None, (str(error),), pages=len(original.pages))
        # An outlined page gives the fitter no words, so new ones could match them by chance.
        avoid = avoid | {token for page in source_read for token, _ in reading.tokens(page)}

    fitter = WordFitter(pdf, vocabulary, fonts if profile.policy.coverage == "all" else None, avoid)
    tiers: Counter[str] = Counter()
    lines_pinned = 0
    for index in range(len(pdf.pages)):
        try:
            report = fitter.fit_page(index, profile.settings, seed, profile.policy)
        except CoverageStop as stop:
            problems.append(str(stop))
            continue
        tiers.update(report.tiers)
        lines_pinned += report.lines_pinned
    if problems:
        return Result(None, tuple(problems), pages=len(original.pages))

    pdf.remove_unreferenced_resources()
    mutations: list[dict[str, Any]] = [
        {
            "operator": "fit-words",
            "params": {
                "seed": seed,
                "coverage": profile.policy.coverage,
                "word_starts": profile.policy.word_starts,
                "tiers": dict(tiers),
                "lines_pinned": lines_pinned,
            },
        },
        {"operator": "remove-unreferenced-resources", "params": {}},
    ]
    if profile.substitute_fonts:
        substituted = fitter.substitute_remaining() if fonts is not None else {}
        mutations.append({"operator": "substitute-fonts", "params": {"fonts": len(substituted)}})
    else:
        substituted = dict(fitter.substituted)
    paragraphs = codes = blocks = 0
    text = text_places(original, source_lines)
    if profile.replace_drawings:
        codes = replace_machine_codes(pdf)
        mutations.append({"operator": "replace-machine-codes", "params": {"codes": codes}})
        blocks = replace_graphics(pdf, text)
        mutations.append({"operator": "replace-graphics", "params": {"graphics": blocks}})
        try:
            outlines = text_from_outlines(
                pdf,
                fonts or FontSource(()),
                seed,
                vocabulary,
                avoid | fitter.source_words,
            )
        except CannotSubstitute as error:
            return Result(None, (f"outlines cannot become text: {error}",), pages=len(pdf.pages))
        paragraphs = outlines.items
        mutations.append({"operator": "text-from-outlines", "params": {"items": paragraphs}})
    images = 0
    if profile.replace_images:
        solid = {
            objgen
            for objgen in solid_images(original, text)
            if shows_light(original.get_object(objgen))
        }
        images = replace_all_images(pdf, solid)
        mutations.append(
            {
                "operator": "replace-image-samples",
                "params": {"style": "flat, light", "images": images, "made_solid": len(solid)},
            }
        )
    dropped = _drop_unused_form_fonts(pdf) if profile.substitute_fonts else 0
    layers = clean_layers(pdf, seed, avoid | fitter.source_words)
    mutations.append(
        {
            "operator": "clean-layers",
            "params": {"removed": layers.removed, "rewritten": layers.rewritten},
        }
    )

    data = _save(pdf)
    checks, problems, output, metrics = _check_output(
        profile, original, source, data, source_lines, avoid | fitter.source_words
    )
    drawings = {index: curved_fills(page) for index, page in enumerate(output.pages)}
    if profile.replace_drawings:
        left = outline_items(output)
        over = {index: count for index, count in drawings.items() if count > DRAWN_TEXT}
        graphics_left = graphics(output, text)
        checks.append(
            Check(
                "drawn-text",
                "docmutant content scan",
                not left and not over and not graphics_left,
                {
                    "items_replaced": paragraphs,
                    "items_left": sum(left.values()),
                    "graphics_replaced": blocks,
                    "graphics_left": sum(graphics_left.values()),
                    "limit": DRAWN_TEXT,
                    "largest": max(drawings.values(), default=0),
                },
            )
        )
        problems.extend(
            f"{label}: {count} outline items left" for label, count in sorted(left.items())
        )
        problems.extend(
            f"{label}: {count} graphics left" for label, count in sorted(graphics_left.items())
        )
        problems.extend(
            f"page {index + 1}: {count} curved shapes remain, too large to be glyphs; "
            "no oracle reads them (--keep-drawings keeps them, and gives up the claim)"
            for index, count in sorted(over.items())
        )
        codes_left = machine_codes(output)
        checks.append(
            Check(
                "machine-codes",
                "docmutant content scan",
                not codes_left,
                {"codes_replaced": codes, "codes_left": sum(codes_left.values())},
            )
        )
        problems.extend(
            f"{label}: {count} machine-readable codes left"
            for label, count in sorted(codes_left.items())
        )
    if profile.reads_render:
        check, unread = _check_render(
            source_read, data, avoid | fitter.source_words, reader, metrics, paragraphs
        )
        checks.append(check)
        problems.extend(unread)
    regions = difference.changed_regions(
        original,
        source_lines,
        images=profile.replace_images,
        drawings=profile.replace_drawings,
        tolerance=profile.settings.tolerance.out,
        text=text,
    )
    check, damage = _check_difference(source, data, regions)
    checks.append(check)
    problems.extend(damage)
    if problems:
        return Result(None, tuple(problems), tuple(checks), pages=len(original.pages))

    before = _font_programs(original)
    after = _font_programs(output)
    still = {digest for _, digest in after}
    names = {f"/{name}" for name in substituted}
    font_records = [
        {
            "name": name,
            "sha256": digest,
            "action": "kept" if digest in still else "replaced" if name in names else "removed",
        }
        for name, digest in sorted(before)
    ]
    kept_fonts = [record["name"] for record in font_records if record["action"] == "kept"]
    fingerprint = _fingerprint(output, kept_fonts)

    shapes = sum(drawings.values())
    kept = [
        f"{fingerprint['painted_paths']} painted paths, not checked: they can show a logo, a QR "
        f"code or text, and {shapes} of their shapes are curved",
        f"{len(kept_fonts)} font programs",
    ]
    if found.images and not profile.replace_images:
        kept.append(f"{found.images} images, not checked: they can show text")
    replaced = ["the words of every page", *sorted(found.blocking)]
    if paragraphs:
        replaced.append(f"{paragraphs} outline items: paragraphs of new text")
    if codes:
        replaced.append(f"{codes} machine-readable codes: flat blocks")
    if blocks:
        replaced.append(f"{blocks} graphics: flat blocks")
    if profile.replace_images and images:
        replaced.append(f"{images} images: flat")
    if substituted:
        replaced.append(f"{len(substituted)} font programs: open substitutes")
    if dropped:
        replaced.append(f"{dropped} form font programs: removed")
    metrics["source_images_kept"] = float(len(image_hashes(original) & image_hashes(output)))
    metrics["source_font_programs_kept"] = float(len(kept_fonts))
    return Result(
        data,
        (),
        tuple(checks),
        tuple(mutations),
        tuple(kept),
        tuple(replaced),
        metrics,
        tuple(font_records),
        fingerprint,
        len(original.pages),
    )
