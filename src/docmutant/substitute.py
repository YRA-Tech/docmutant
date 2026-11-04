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

import functools
import io
import re
import statistics
import string
import unicodedata
from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from fontTools import subset
from fontTools.agl import UV2AGL
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable

from docmutant.tracker import font_height

DEFAULT_DIRECTORIES = (
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path("/usr/share/texmf/fonts/opentype/public/lm"),
    Path("/usr/share/texlive/texmf-dist/fonts/opentype/public/lm"),
)

METRIC_COMPATIBLE = (
    (re.compile(r"arial|helvetica"), "LiberationSans"),
    (re.compile(r"timesnewroman|times"), "LiberationSerif"),
    (re.compile(r"couriernew|courier"), "LiberationMono"),
    (re.compile(r"calibri"), "Carlito"),
    (re.compile(r"cambria"), "Caladea"),
)
"""Families whose open counterparts have the same advance widths."""

TEX_NAME = re.compile(r"^(?:cm|ec|sf)([a-z]+?)(\d+)$")
"""A font of TeX's Computer Modern, or of its EC and cm-super versions: its style, then its
design size, in points or, with four digits, in hundredths of a point."""

LATIN_MODERN = {
    "r": ("lmroman", "regular"),
    "rm": ("lmroman", "regular"),
    "b": ("lmromandemi", "regular"),
    "bx": ("lmroman", "bold"),
    "ti": ("lmroman", "italic"),
    "bxti": ("lmroman", "bolditalic"),
    "bi": ("lmroman", "bolditalic"),
    "sl": ("lmromanslant", "regular"),
    "bxsl": ("lmromanslant", "bold"),
    "bl": ("lmromanslant", "bold"),
    "csc": ("lmromancaps", "regular"),
    "cc": ("lmromancaps", "regular"),
    "u": ("lmromanunsl", "regular"),
    "ui": ("lmromanunsl", "regular"),
    "dunh": ("lmromandunh", "regular"),
    "dh": ("lmromandunh", "regular"),
    "ss": ("lmsans", "regular"),
    "ssi": ("lmsans", "oblique"),
    "si": ("lmsans", "oblique"),
    "ssbx": ("lmsans", "bold"),
    "sx": ("lmsans", "bold"),
    "tt": ("lmmono", "regular"),
    "itt": ("lmmono", "italic"),
    "it": ("lmmono", "italic"),
    "sltt": ("lmmonoslant", "regular"),
    "st": ("lmmonoslant", "regular"),
}
"""Latin Modern, which is Computer Modern with its widths, for each TeX style code. The math
fonts, such as cmmi, have no counterpart."""

CURVE_ERROR = 1.0
"""Font units: how far a quadratic outline can stray from the cubic one it replaces."""

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


@functools.lru_cache(maxsize=16)
def _truetype(path: Path) -> bytes:
    """The program at ``path``, with TrueType outlines: a CFF one has its curves converted."""
    data = path.read_bytes()
    font = TTFont(io.BytesIO(data), recalcTimestamp=False)
    if "CFF " not in font:
        return data
    order = font.getGlyphOrder()
    glyphs = font.getGlyphSet()
    outlines = {}
    for name in order:
        pen = TTGlyphPen(glyphs)
        glyphs[name].draw(Cu2QuPen(pen, CURVE_ERROR, reverse_direction=True))
        outlines[name] = pen.glyph()
    del font["CFF "]
    for table in ("VORG", "CFF2"):
        if table in font:
            del font[table]
    font["loca"] = newTable("loca")
    font["glyf"] = glyf = newTable("glyf")
    glyf.glyphOrder = order
    glyf.glyphs = outlines
    font["maxp"] = maxp = newTable("maxp")
    maxp.tableVersion = 0x00010000
    for field in (
        "maxZones",
        "maxTwilightPoints",
        "maxStorage",
        "maxFunctionDefs",
        "maxInstructionDefs",
        "maxStackElements",
        "maxSizeOfInstructions",
        "maxComponentElements",
    ):
        setattr(maxp, field, 1 if field == "maxZones" else 0)
    font["post"].formatType = 2.0
    font["post"].extraNames = []
    font["post"].mapping = {}
    font["post"].glyphOrder = order
    font["head"].glyphDataFormat = 0
    font.sfntVersion = "\x00\x01\x00\x00"
    buffer = io.BytesIO()
    font.save(buffer)
    return buffer.getvalue()


class FontSource:
    """Finds an open font for a PDF font, in directories of ``.ttf`` and ``.otf`` files."""

    def __init__(self, directories: Iterable[Path] = DEFAULT_DIRECTORIES) -> None:
        self.files: dict[str, Path] = {}
        for suffix in ("*.ttf", "*.otf"):
            for directory in directories:
                for path in sorted(Path(directory).rglob(suffix)):
                    self.files.setdefault(path.stem, path)

    def _latin_modern(self, name: str) -> str | None:
        """The Latin Modern file for a font of TeX's, at the design size nearest its own."""
        if name.startswith("lm"):
            # Latin Modern itself, as LMRoman10-Regular.
            return next((stem for stem in self.files if stem.replace("-", "") == name), None)
        match = TEX_NAME.match(name)
        if match is None or match.group(1) not in LATIN_MODERN:
            return None
        family, style = LATIN_MODERN[match.group(1)]
        digits = match.group(2)
        size = int(digits) / 100 if len(digits) == 4 else int(digits)
        sizes = {
            int(found.group(1)): stem
            for stem in self.files
            if (found := re.fullmatch(rf"{family}(\d+)-{style}", stem))
        }
        return sizes[min(sizes, key=lambda each: abs(each - size))] if sizes else None

    def pick(self, font: pikepdf.Object) -> Substitute | None:
        """The family's counterpart, or else a generic font of the same kind."""
        descendants = font.get("/DescendantFonts")
        holder = descendants[0] if descendants else font
        descriptor = holder.get("/FontDescriptor")
        flags = int(descriptor.get("/Flags", 0)) if descriptor is not None else 0
        source_name = str(font.get("/BaseFont", font.get("/Name", f"/font-{font.objgen[0]}")))[1:]
        name = re.sub(r"^[A-Z]{6}\+", "", source_name).lower().replace(" ", "")
        name = name.replace("-", "").replace(",", "").replace("_", "")
        tex = self._latin_modern(name)
        if tex is not None:
            return Substitute(source_name, tex, _truetype(self.files[tex]), True)
        family = next((stem for rule, stem in METRIC_COMPATIBLE if rule.search(name)), None)
        if flags & FLAG_FIXED or re.search(r"mono|cmtt", name):
            generic = "LiberationMono"
        elif flags & FLAG_SERIF or re.search(r"serif|roman|cmr|garamond|georgia|cambria", name):
            generic = "LiberationSerif"
        else:
            generic = "LiberationSans"
        bold = bool(flags & FLAG_FORCE_BOLD) or "bold" in name or "cmbx" in name
        italic = bool(flags & FLAG_ITALIC) or "italic" in name or "oblique" in name
        style = {
            (False, False): "Regular",
            (True, False): "Bold",
            (False, True): "Italic",
            (True, True): "BoldItalic",
        }[(bold, italic)]
        # Advances stay in any case, so a font of another family only looks further off.
        for candidate in (family, generic):
            path = self.files.get(f"{candidate}-{style}") or self.files.get(f"{candidate}-Regular")
            if path is not None:
                return Substitute(source_name, path.stem, path.read_bytes(), candidate == family)
        return None


def _character(text: str, has: Container[int]) -> str | None:
    """The one character of the substitute that draws this text, or None.

    A math letter has no glyph of its own in a text font, so it draws as the plain letter. A
    ligature draws as one glyph.
    """
    for form in (text, unicodedata.normalize("NFKC", text)):
        if len(form) == 1 and ord(form) in has:
            return form
    ligature = {"fi": "ﬁ", "fl": "ﬂ", "ff": "ﬀ", "ffi": "ﬃ", "ffl": "ﬄ"}.get(text)
    return ligature if ligature is not None and ord(ligature) in has else None


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
    Identity-H composite font can take a substitute, when the substitute has every letter and
    digit that the font shows; otherwise this raises ``CannotSubstitute`` and the font is
    unchanged. A code whose text is not known, or a symbol the substitute lacks, draws nothing.
    """
    composite = font.get("/Subtype") == "/Type0"
    if composite and font.get("/Encoding") != "/Identity-H":
        raise CannotSubstitute("composite font without Identity-H")
    factor = 1.0
    if font.get("/Subtype") == "/Type3":
        factor = float(font.get("/FontMatrix", [0.001])[0]) * 1000
    ascent, descent = font_height(font)

    program = TTFont(io.BytesIO(substitute.program), recalcTimestamp=False)
    if "glyf" not in program:
        raise CannotSubstitute("substitute has no TrueType outlines")
    cmap = program.getBestCmap()

    drawn: dict[int, str] = {}
    blank: set[int] = set()
    known: dict[int, str] = {}
    for code, entry in shown.items():
        if entry.text is None:
            # What it draws is not known. A word with it must change anyway under coverage all.
            blank.add(code)
            continue
        known[code] = entry.text
        if entry.text.isspace():
            blank.add(code)
        elif entry.text:
            char = _character(entry.text, cmap)
            if char is not None:
                drawn[code] = char
            elif any(part.isalnum() for part in entry.text):
                raise CannotSubstitute(f"substitute lacks a glyph for {entry.text!r}")
            else:
                # A symbol it lacks, or U+FFFD: its advance stays, and a page loses only a mark.
                blank.add(code)
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
        cids.update(dict.fromkeys(blank, " "))
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
    codes.update(dict.fromkeys(blank, " "))
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
