"""Metric-compatible substitution: give a font an open program, so that it can draw more glyphs.

A subset font has only the glyphs its document used. The substitute keeps every code the
document shows, with the same text and the same advance, so that nothing moves, and adds the
letters and digits the font lacks at codes it does not use. New widths follow the source font
where the two differ. A Type3 font becomes a TrueType font, its widths converted through its
``/FontMatrix``. The source's font program goes away, which the copyright and privacy claims
also need.

The names of the substitute are dropped from the subset, with its copyright and license
kept, because the OFL reserves some font names for unmodified fonts.
"""

from __future__ import annotations

import io
import re
import statistics
import string
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from fontTools import subset
from fontTools.agl import UV2AGL
from fontTools.ttLib import TTFont

from docmutant.tracker import font_height

DEFAULT_DIRECTORIES = (Path("/usr/share/fonts"), Path("/usr/local/share/fonts"))

METRIC_COMPATIBLE = (
    (re.compile(r"arial|helvetica"), "LiberationSans"),
    (re.compile(r"timesnewroman|times"), "LiberationSerif"),
    (re.compile(r"couriernew|courier"), "LiberationMono"),
    (re.compile(r"calibri"), "Carlito"),
    (re.compile(r"cambria"), "Caladea"),
)
"""Families whose open counterparts have the same advance widths."""

FLAG_FIXED = 1
FLAG_SERIF = 2
FLAG_NONSYMBOLIC = 32
FLAG_ITALIC = 64
FLAG_FORCE_BOLD = 1 << 18

ADDED = frozenset(string.ascii_letters + string.digits)


class CannotSubstitute(Exception):
    """The font stays: a substitute could not draw what the document shows in it."""


@dataclass(frozen=True)
class ShownCode:
    """A code that the document shows in a font."""

    length: int
    text: str | None
    width: float


@dataclass(frozen=True)
class Embedded:
    added: dict[str, tuple[bytes, float]]
    """New text: its code and width."""

    width_factor: float
    """What the width of a shown code was multiplied by: other than 1 only for a Type3 font."""


@dataclass(frozen=True)
class Substitute:
    source_name: str
    name: str
    """File stem of the open font, for the report."""

    program: bytes
    metric_compatible: bool


class FontSource:
    """Finds an open font for a PDF font, in directories of ``.ttf`` files."""

    def __init__(self, directories: Iterable[Path] = DEFAULT_DIRECTORIES) -> None:
        self.files: dict[str, Path] = {}
        for directory in directories:
            for path in sorted(Path(directory).rglob("*.ttf")):
                self.files.setdefault(path.stem, path)

    def pick(self, font: pikepdf.Object) -> Substitute | None:
        """The family's counterpart, or else a generic font of the same kind."""
        descendants = font.get("/DescendantFonts")
        holder = descendants[0] if descendants else font
        descriptor = holder.get("/FontDescriptor")
        flags = int(descriptor.get("/Flags", 0)) if descriptor is not None else 0
        source_name = str(font.get("/BaseFont", font.get("/Name", f"/font-{font.objgen[0]}")))[1:]
        name = re.sub(r"^[A-Z]{6}\+", "", source_name).lower().replace(" ", "")
        name = name.replace("-", "").replace(",", "").replace("_", "")
        family = next((stem for rule, stem in METRIC_COMPATIBLE if rule.search(name)), None)
        compatible = family is not None
        if family is None:
            if flags & FLAG_FIXED or re.search(r"mono|cmtt", name):
                family = "LiberationMono"
            elif flags & FLAG_SERIF or re.search(r"serif|roman|cmr|garamond|georgia", name):
                family = "LiberationSerif"
            else:
                family = "LiberationSans"
        bold = bool(flags & FLAG_FORCE_BOLD) or "bold" in name or "cmbx" in name
        italic = bool(flags & FLAG_ITALIC) or "italic" in name or "oblique" in name
        style = {
            (False, False): "Regular",
            (True, False): "Bold",
            (False, True): "Italic",
            (True, True): "BoldItalic",
        }[(bold, italic)]
        path = self.files.get(f"{family}-{style}") or self.files.get(f"{family}-Regular")
        if path is None:
            return None
        return Substitute(source_name, path.stem, path.read_bytes(), compatible)


def _characters(text: str) -> str | None:
    """The one character of the substitute that draws this text, or None."""
    if len(text) == 1:
        return text
    composed = unicodedata.normalize("NFKC", text)
    if len(composed) == 1:
        return composed
    return {"fi": "ﬁ", "fl": "ﬂ", "ff": "ﬀ", "ffi": "ﬃ", "ffl": "ﬄ"}.get(text)


def _glyph_name(char: str) -> str:
    return UV2AGL.get(ord(char), f"uni{ord(char):04X}")


def _to_unicode(codes: Mapping[int, str], length: int) -> bytes:
    lines = [
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap",
        b"/CMapName /Substitute def /CMapType 2 def",
        b"1 begincodespacerange",
        b"<00> <FF>" if length == 1 else b"<0000> <FFFF>",
        b"endcodespacerange",
    ]
    entries = sorted(codes.items())
    for start in range(0, len(entries), 100):
        chunk = entries[start : start + 100]
        lines.append(b"%d beginbfchar" % len(chunk))
        for code, text in chunk:
            lines.append(
                b"<%s> <%s>"
                % (
                    code.to_bytes(length, "big").hex().encode(),
                    text.encode("utf-16-be").hex().encode(),
                )
            )
        lines.append(b"endbfchar")
    lines.append(b"endcmap CMapName currentdict /CMap defineresource pop end end")
    return b"\n".join(lines)


def embed_substitute(
    pdf: pikepdf.Pdf,
    font: pikepdf.Object,
    substitute: Substitute,
    shown: Mapping[int, ShownCode],
) -> Embedded:
    """Replace the program of ``font`` in place.

    Advances and text of the codes in ``shown`` stay. A simple font, a Type3 font or an
    Identity-H composite font can take a substitute, when the text of every shown code is
    known and the substitute has it; otherwise this raises ``CannotSubstitute`` and the font
    is unchanged.
    """
    composite = font.get("/Subtype") == "/Type0"
    if composite and font.get("/Encoding") != "/Identity-H":
        raise CannotSubstitute("composite font without Identity-H")
    factor = 1.0
    if font.get("/Subtype") == "/Type3":
        factor = float(font.get("/FontMatrix", [0.001])[0]) * 1000
    ascent, descent = font_height(font)

    drawn: dict[int, str] = {}
    known: dict[int, str] = {}
    for code, entry in shown.items():
        if entry.text is None:
            raise CannotSubstitute(f"code {code} has no text")
        known[code] = entry.text
        if entry.text and not entry.text.isspace():
            char = _characters(entry.text)
            if char is None:
                raise CannotSubstitute(f"no one glyph draws {entry.text!r}")
            drawn[code] = char

    program = TTFont(io.BytesIO(substitute.program))
    if "glyf" not in program:
        raise CannotSubstitute("substitute has no TrueType outlines")
    cmap = program.getBestCmap()
    if any(ord(char) not in cmap for char in drawn.values()):
        raise CannotSubstitute("substitute lacks a glyph the document shows")
    metrics = program["hmtx"]
    scale = 1000 / program["head"].unitsPerEm
    width_of = {value: metrics[name][0] * scale for value, name in cmap.items()}

    ratios = [
        shown[code].width * factor / width_of[ord(char)]
        for code, char in drawn.items()
        if shown[code].width > 0 and width_of[ord(char)] > 0
    ]
    ratio = statistics.median(ratios) if ratios else 1.0

    have = set(drawn.values())
    add = sorted(char for char in ADDED - have if ord(char) in cmap)

    options = subset.Options()
    options.name_IDs = [0, 13, 14]
    options.notdef_outline = True
    options.glyph_names = True
    options.layout_features = []
    options.drop_tables += ["GSUB", "GPOS", "GDEF", "kern", "FFTM"]
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes={ord(char) for char in have | set(add)} | {32})
    subsetter.subset(program)
    buffer = io.BytesIO()
    program.save(buffer)
    data = buffer.getvalue()
    subset_cmap = program.getBestCmap()
    glyph_ids = {name: index for index, name in enumerate(program.getGlyphOrder())}

    base = f"DMSUBS+{re.sub(r'[^A-Za-z0-9-]', '', substitute.name)}"
    holder = font.DescendantFonts[0] if composite else font
    old = holder.get("/FontDescriptor")
    descriptor = pikepdf.Dictionary(
        Type=pikepdf.Name.FontDescriptor,
        FontName=pikepdf.Name("/" + base),
        Flags=FLAG_NONSYMBOLIC
        | (int(old.get("/Flags", 0)) & FLAG_ITALIC if old is not None else 0),
        FontFile2=pdf.make_stream(data),
    )
    # The height the tracker sees stays, so that no box changes.
    descriptor.Ascent = ascent
    descriptor.Descent = descent
    for key, default in (
        ("/FontBBox", [-200, round(descent), 1200, round(ascent)]),
        ("/CapHeight", 700),
        ("/ItalicAngle", 0),
        ("/StemV", 80),
    ):
        descriptor[key] = old.get(key, default) if old is not None else default

    new: dict[str, tuple[bytes, float]] = {}
    if composite:
        used = set(shown)
        code = 0x100
        cids: dict[int, str] = dict(drawn)
        for cid, text in known.items():
            if text.isspace():
                cids[cid] = " "
        widths = {cid: shown[cid].width for cid in shown}
        for char in add:
            while code in used:
                code += 1
            cids[code] = char
            width = round(width_of[ord(char)] * ratio, 3)
            widths[code] = width
            known[code] = char
            new[char] = (code.to_bytes(2, "big"), width)
            used.add(code)
        size = max(cids) + 1
        table = bytearray(2 * size)
        for cid, char in cids.items():
            gid = glyph_ids[subset_cmap[ord(char)]]
            table[2 * cid : 2 * cid + 2] = gid.to_bytes(2, "big")
        holder.Subtype = pikepdf.Name.CIDFontType2
        holder.BaseFont = pikepdf.Name("/" + base)
        holder.CIDToGIDMap = pdf.make_stream(bytes(table))
        holder.FontDescriptor = pdf.make_indirect(descriptor)
        holder.W = pikepdf.Array(
            [item for cid, width in sorted(widths.items()) for item in (cid, [width])]
        )
        font.BaseFont = pikepdf.Name("/" + base)
        font.ToUnicode = pdf.make_stream(_to_unicode(known, 2))
        return Embedded(new, factor)

    used = set(shown)
    free = iter(code for code in range(33, 256) if code not in used)
    codes: dict[int, str] = dict(drawn)
    widths = {code: entry.width * factor for code, entry in shown.items()}
    for char in add:
        code = next(free, None)
        if code is None:
            break
        codes[code] = char
        widths[code] = round(width_of[ord(char)] * ratio, 3)
        known[code] = char
        new[char] = (bytes([code]), widths[code])
    for code, text in known.items():
        if text.isspace():
            codes[code] = " "
    first, last = min(widths), max(widths)
    differences: list[object] = []
    for code in sorted(codes):
        differences.extend((code, pikepdf.Name("/" + _glyph_name(codes[code]))))
    for key in ("/CharSet", "/Encoding", "/CharProcs", "/FontMatrix", "/FontBBox", "/Resources"):
        if key in font:
            del font[key]
    font.Subtype = pikepdf.Name.TrueType
    font.BaseFont = pikepdf.Name("/" + base)
    font.FirstChar = first
    font.LastChar = last
    font.Widths = pikepdf.Array([widths.get(code, 0) for code in range(first, last + 1)])
    font.Encoding = pikepdf.Dictionary(
        Type=pikepdf.Name.Encoding,
        BaseEncoding=pikepdf.Name.WinAnsiEncoding,
        Differences=pikepdf.Array(differences),
    )
    font.FontDescriptor = pdf.make_indirect(descriptor)
    font.ToUnicode = pdf.make_stream(_to_unicode(known, 1))
    return Embedded(new, factor)
