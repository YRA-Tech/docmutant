"""Automatic evidence: what a derived file still shares with its source.

The measurements use pypdfium2, an engine independent of the usual target
(docling-parse), so a target bug does not hide in the evidence.
"""

from __future__ import annotations

import re
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium

from docmutant.content import count_kinds, parse_units
from docmutant.mutate import (
    StreamRef,
    font_program_hashes,
    form_xobjects,
    image_hashes,
    read_stream,
)

WORD = re.compile(r"\w{3,}")


def page_words(path: Path, page: int) -> set[str]:
    document = pdfium.PdfDocument(path)
    try:
        text = document[page - 1].get_textpage().get_text_range()
    finally:
        document.close()
    return {word.lower() for word in WORD.findall(text)}


def page_dhash(path: Path, page: int) -> int | None:
    """64-bit difference hash of a low-resolution render, or None if rendering fails."""
    document = pdfium.PdfDocument(path)
    try:
        image = document[page - 1].render(scale=0.5).to_pil()
    except pdfium.PdfiumError:
        return None
    finally:
        document.close()
    small = image.convert("L").resize((9, 8))
    pixels = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return bits


def content_summary(pdf: pikepdf.Pdf) -> dict[str, int]:
    counts: dict[str, int] = {}
    refs = [StreamRef("page", i) for i in range(len(pdf.pages))] + form_xobjects(pdf)
    for ref in refs:
        for kind, number in count_kinds(parse_units(read_stream(pdf, ref))).items():
            counts[kind] = counts.get(kind, 0) + number
    return counts


def measure(source: Path, source_page: int, output: Path) -> dict[str, object]:
    """Compare one source page with the one-page output file."""
    with pikepdf.open(source) as src_pdf:
        src_single = pikepdf.Pdf.new()
        src_single.pages.append(src_pdf.pages[source_page - 1])
        src_images = image_hashes(src_single)
        src_fonts = font_program_hashes(src_single)
    with pikepdf.open(output) as out_pdf:
        out_images = image_hashes(out_pdf)
        out_fonts = font_program_hashes(out_pdf)
        summary = content_summary(out_pdf)

    src_words = page_words(source, source_page)
    out_words = page_words(output, 1)
    shared = src_words & out_words

    metrics: dict[str, float] = {
        "source_words": float(len(src_words)),
        "output_words": float(len(out_words)),
        "shared_words": float(len(shared)),
        "source_images_kept": float(len(src_images & out_images)),
        "source_font_programs_kept": float(len(set(src_fonts.values()) & set(out_fonts.values()))),
    }
    src_hash, out_hash = page_dhash(source, source_page), page_dhash(output, 1)
    if src_hash is not None and out_hash is not None:
        metrics["render_dhash_distance"] = float(bin(src_hash ^ out_hash).count("1"))

    fonts = []
    out_hash_set = set(out_fonts.values())
    for name, digest in sorted(src_fonts.items()):
        fonts.append(
            {
                "name": name,
                "sha256": digest,
                "action": "kept" if digest in out_hash_set else "removed",
            }
        )
    return {"metrics": metrics, "summary": summary, "fonts": fonts}
