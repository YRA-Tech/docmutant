"""PDF mutations used by the reducer. Each works on an open ``pikepdf.Pdf``."""

from __future__ import annotations

import hashlib
import io
import math
import random
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass

import pikepdf
from PIL import Image

from docmutant.content import Instruction

CATALOG_KEEP = frozenset({"/Type", "/Pages"})
# Page and image dictionaries keep only what rendering needs. An allow-list, not a
# blocklist: producers add private keys (for example Acrobat Capture's /CAPT_Info,
# which holds the original file path) that no blocklist can know.
PAGE_KEEP = frozenset(
    {
        "/Type",
        "/Parent",
        "/Resources",
        "/MediaBox",
        "/CropBox",
        "/Rotate",
        "/Contents",
        "/UserUnit",
        "/Group",
    }
)
IMAGE_KEEP = frozenset(
    {
        "/Type",
        "/Subtype",
        "/Width",
        "/Height",
        "/ColorSpace",
        "/BitsPerComponent",
        "/Filter",
        "/DecodeParms",
        "/Decode",
        "/ImageMask",
        "/Mask",
        "/SMask",
        "/SMaskInData",
        "/Intent",
        "/Interpolate",
        "/Length",
    }
)
FONT_PROGRAM_KEYS = ("/FontFile", "/FontFile2", "/FontFile3")
SIMPLE_FONT_SUBTYPES = frozenset({"/Type1", "/TrueType", "/MMType1"})
TEXT_SHOW_OPERATORS = frozenset({"Tj", "TJ", "'", '"'})


def keep_pages(pdf: pikepdf.Pdf, pages: list[int]) -> pikepdf.Pdf:
    """Return a new PDF with only the given 1-based pages, in order."""
    out = pikepdf.Pdf.new()
    for number in pages:
        out.pages.append(pdf.pages[number - 1])
    return out


def strip_catalog(pdf: pikepdf.Pdf) -> list[str]:
    """Drop catalog entries other than /Type and /Pages, and the document info."""
    removed = [str(key) for key in pdf.Root if key not in CATALOG_KEEP]
    for key in removed:
        del pdf.Root[key]
    if "/Info" in pdf.trailer:
        del pdf.trailer["/Info"]
        removed.append("/Info")
    return removed


def strip_page_extras(pdf: pikepdf.Pdf) -> list[str]:
    """Drop every page key that rendering does not need (annotations, private keys, ...)."""
    removed: list[str] = []
    for page in pdf.pages:
        for key in [k for k in page.obj if k not in PAGE_KEEP]:
            del page.obj[key]
            removed.append(str(key))
    return removed


def strip_metadata_streams(pdf: pikepdf.Pdf) -> int:
    """Remove /Metadata and /PieceInfo from every dictionary and stream."""
    count = 0
    for obj in pdf.objects:
        if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
            for key in ("/Metadata", "/PieceInfo"):
                if key in obj:
                    del obj[key]
                    count += 1
    return count


# --- content streams ---------------------------------------------------------


@dataclass(frozen=True)
class StreamRef:
    """A content stream to work on: a page's contents or a form XObject."""

    kind: str  # "page" or "form"
    page_index: int
    objgen: tuple[int, int] | None = None

    def label(self) -> str:
        if self.kind == "page":
            return f"page {self.page_index + 1}"
        return f"form {self.objgen[0]} {self.objgen[1]}" if self.objgen else "form"


def read_stream(pdf: pikepdf.Pdf, ref: StreamRef) -> list[Instruction]:
    if ref.kind == "page":
        return list(pikepdf.parse_content_stream(pdf.pages[ref.page_index]))
    assert ref.objgen is not None
    return list(pikepdf.parse_content_stream(pdf.get_object(ref.objgen)))


def write_stream(pdf: pikepdf.Pdf, ref: StreamRef, instructions: list[Instruction]) -> None:
    data = pikepdf.unparse_content_stream(instructions)
    if ref.kind == "page":
        pdf.pages[ref.page_index].obj.Contents = pdf.make_stream(data)
    else:
        assert ref.objgen is not None
        pdf.get_object(ref.objgen).write(data)


def form_xobjects(pdf: pikepdf.Pdf) -> list[StreamRef]:
    """Form XObjects reachable from the pages' resources, nested forms included."""
    seen: set[tuple[int, int]] = set()
    found: list[StreamRef] = []

    def visit(resources: pikepdf.Object, page_index: int) -> None:
        xobjects = resources.get("/XObject") if resources is not None else None
        if xobjects is None:
            return
        for _, xobject in xobjects.items():
            if not isinstance(xobject, pikepdf.Stream) or xobject.get("/Subtype") != "/Form":
                continue
            objgen = xobject.objgen
            if objgen == (0, 0) or objgen in seen:
                continue
            seen.add(objgen)
            found.append(StreamRef("form", page_index, objgen))
            visit(xobject.get("/Resources"), page_index)

    for index, page in enumerate(pdf.pages):
        visit(page.obj.get("/Resources"), index)
    return found


def stream_resources(pdf: pikepdf.Pdf, ref: StreamRef) -> pikepdf.Object | None:
    if ref.kind == "page":
        return pdf.pages[ref.page_index].obj.get("/Resources")
    assert ref.objgen is not None
    return pdf.get_object(ref.objgen).get("/Resources")


# --- text ----------------------------------------------------------------------


@dataclass
class FontCodes:
    """Width classes of one font, for a width-preserving code swap."""

    code_bytes: int
    width_of: dict[int, float]
    keep: frozenset[int]

    def permutation(self, rng: random.Random) -> dict[int, int]:
        classes: dict[float, list[int]] = {}
        for code, width in self.width_of.items():
            if width > 0 and code not in self.keep:
                classes.setdefault(width, []).append(code)
        mapping: dict[int, int] = {}
        for codes in classes.values():
            if len(codes) < 2:
                continue
            shuffled = codes[:]
            rng.shuffle(shuffled)
            # Rotate so that no code maps to itself when the class has two or more codes.
            rotated = shuffled[1:] + shuffled[:1]
            mapping.update(zip(shuffled, rotated, strict=True))
        return mapping


def font_codes(font: pikepdf.Object) -> FontCodes | None:
    """Width table of a simple font or an Identity-H composite font; None otherwise."""
    subtype = font.get("/Subtype")
    if subtype in SIMPLE_FONT_SUBTYPES and "/Widths" in font and "/FirstChar" in font:
        first = int(font.FirstChar)
        widths = {first + i: float(w) for i, w in enumerate(font.Widths)}
        return FontCodes(1, widths, frozenset({32}))
    if subtype == "/Type0" and font.get("/Encoding") == "/Identity-H":
        descendant = font.DescendantFonts[0]
        default = float(descendant.get("/DW", 1000))
        widths = _cid_widths(descendant.get("/W"))
        if not widths:
            return None
        keep = frozenset(_tounicode_spaces(font.get("/ToUnicode")))
        widths = {cid: (w if w is not None else default) for cid, w in widths.items()}
        return FontCodes(2, widths, keep)
    return None


def _cid_widths(w_array: pikepdf.Object | None) -> dict[int, float]:
    widths: dict[int, float] = {}
    if w_array is None:
        return widths
    items = list(w_array)
    index = 0
    while index < len(items):
        first = int(items[index])
        nxt = items[index + 1] if index + 1 < len(items) else None
        if isinstance(nxt, pikepdf.Array):
            for offset, width in enumerate(nxt):
                widths[first + offset] = float(width)
            index += 2
        elif index + 2 < len(items):
            last, width = int(items[index + 1]), float(items[index + 2])
            for cid in range(first, min(last, first + 65535) + 1):
                widths[cid] = width
            index += 3
        else:
            break
    return widths


_BFCHAR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")


def _tounicode_spaces(stream: pikepdf.Object | None) -> list[int]:
    """Codes that a ToUnicode CMap maps to U+0020 (bfchar entries only)."""
    if not isinstance(stream, pikepdf.Stream):
        return []
    try:
        data = stream.read_bytes()
    except pikepdf.PdfError:
        return []
    return [int(src, 16) for src, dst in _BFCHAR.findall(data) if dst.lower() == b"0020"]


def _swap_string(
    value: pikepdf.String, codes: FontCodes, mapping: dict[int, int]
) -> pikepdf.String:
    raw = bytes(value)
    out = bytearray()
    step = codes.code_bytes
    for index in range(0, len(raw) - len(raw) % step, step):
        code = int.from_bytes(raw[index : index + step], "big")
        out += mapping.get(code, code).to_bytes(step, "big")
    out += raw[len(raw) - len(raw) % step :]
    return pikepdf.String(bytes(out))


def swap_text_codes(pdf: pikepdf.Pdf, refs: list[StreamRef], seed: int) -> dict[str, int]:
    """Replace shown text codes by codes of equal width in the same font.

    Returns the number of strings changed per font resource name. Fonts that are
    neither simple nor Identity-H are left alone and show up in the evidence.
    """
    changed: dict[str, int] = {}
    for ref in refs:
        resources = stream_resources(pdf, ref)
        fonts = resources.get("/Font") if resources is not None else None
        if fonts is None:
            continue
        tables: dict[str, tuple[FontCodes, dict[int, int]] | None] = {}
        instructions = read_stream(pdf, ref)
        current: str | None = None
        rewritten: list[Instruction] = []
        for instruction in instructions:
            if isinstance(instruction, pikepdf.ContentStreamInlineImage):
                rewritten.append(instruction)
                continue
            op = str(instruction.operator)
            operands = list(instruction.operands)
            if op == "Tf" and operands:
                current = str(operands[0])
            elif op in TEXT_SHOW_OPERATORS and current is not None:
                if current not in tables:
                    font = fonts.get(current)
                    table = font_codes(font) if font is not None else None
                    rng = random.Random(f"{seed}:{current}")
                    tables[current] = (table, table.permutation(rng)) if table else None
                entry = tables[current]
                if entry is not None:
                    table, mapping = entry
                    operands = [_swap_operand(o, table, mapping) for o in operands]
                    changed[current] = changed.get(current, 0) + 1
                    instruction = pikepdf.ContentStreamInstruction(operands, instruction.operator)
            rewritten.append(instruction)
        write_stream(pdf, ref, rewritten)
    return changed


def _swap_operand(operand: object, codes: FontCodes, mapping: dict[int, int]) -> object:
    if isinstance(operand, pikepdf.String):
        return _swap_string(operand, codes, mapping)
    if isinstance(operand, pikepdf.Array):
        return pikepdf.Array([_swap_operand(item, codes, mapping) for item in operand])
    return operand


# --- images --------------------------------------------------------------------


def image_xobjects(pdf: pikepdf.Pdf) -> list[pikepdf.Stream]:
    """Image XObjects, with their soft masks and stencil masks, in object order."""
    images: list[pikepdf.Stream] = []
    seen: set[tuple[int, int]] = set()
    for obj in pdf.objects:
        if (
            isinstance(obj, pikepdf.Stream)
            and obj.get("/Subtype") == "/Image"
            and obj.objgen not in seen
        ):
            seen.add(obj.objgen)
            images.append(obj)
    return images


def _components(image: pikepdf.Stream) -> int:
    if image.get("/ImageMask", False):
        return 1
    space = image.get("/ColorSpace")
    if space is None:
        return 1
    if isinstance(space, pikepdf.Array):
        family = space[0]
        if family == "/ICCBased":
            return int(space[1].get("/N", 3))
        if family in ("/Indexed", "/Separation", "/CalGray"):
            return 1
        if family == "/DeviceN":
            return len(space[1])
        if family in ("/CalRGB", "/Lab"):
            return 3
        return 3
    return {"/DeviceGray": 1, "/DeviceRGB": 3, "/DeviceCMYK": 4}.get(str(space), 3)


def replace_image(image: pikepdf.Stream, *, style: str, rng: random.Random) -> str:
    """Replace an image's samples, keeping width, height, depth and colour space.

    ``style`` is "flat" (one mid-grey value) or "noise". DCT images stay DCT;
    every other filter becomes Flate. Returns the filter now in use.
    """
    width, height = int(image.Width), int(image.Height)
    bpc = 1 if image.get("/ImageMask", False) else int(image.get("/BitsPerComponent", 8))
    components = _components(image)
    row = math.ceil(width * components * bpc / 8)
    size = row * height
    if style == "flat":
        data = bytes([0x80]) * size if bpc == 8 else bytes([0xAA]) * size
    else:
        data = rng.randbytes(size)

    if image.get("/Filter") == "/DCTDecode" and bpc == 8 and components in (1, 3, 4):
        mode = {1: "L", 3: "RGB", 4: "CMYK"}[components]
        buffer = io.BytesIO()
        Image.frombytes(mode, (width, height), data).save(buffer, format="JPEG", quality=50)
        _drop_unknown_image_keys(image)
        image.write(buffer.getvalue(), filter=pikepdf.Name.DCTDecode)
        return "/DCTDecode"

    _drop_unknown_image_keys(image)
    image.write(zlib.compress(data, 9), filter=pikepdf.Name.FlateDecode)
    if "/DecodeParms" in image:
        del image["/DecodeParms"]
    if image.get("/BitsPerComponent") is None and not image.get("/ImageMask", False):
        image.BitsPerComponent = bpc
    return "/FlateDecode"


def _drop_unknown_image_keys(image: pikepdf.Stream) -> None:
    for key in [k for k in image if k not in IMAGE_KEEP]:
        del image[key]


# --- fonts ---------------------------------------------------------------------


def font_descriptors(pdf: pikepdf.Pdf) -> Iterator[tuple[str, pikepdf.Dictionary]]:
    """(BaseFont name, FontDescriptor) for every font with a descriptor."""
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == "/FontDescriptor":
            yield str(obj.get("/FontName", "unnamed")), obj


def font_program_hashes(pdf: pikepdf.Pdf) -> dict[str, str]:
    """sha256 of each embedded font program, by font name."""
    hashes: dict[str, str] = {}
    for name, descriptor in font_descriptors(pdf):
        for key in FONT_PROGRAM_KEYS:
            program = descriptor.get(key)
            if isinstance(program, pikepdf.Stream):
                hashes[name] = hashlib.sha256(program.read_raw_bytes()).hexdigest()
    return hashes


def remove_font_programs(pdf: pikepdf.Pdf, names: set[str] | None = None) -> list[str]:
    """Delete embedded font programs (all, or only the named fonts)."""
    removed: list[str] = []
    for name, descriptor in font_descriptors(pdf):
        if names is not None and name not in names:
            continue
        for key in FONT_PROGRAM_KEYS:
            if key in descriptor:
                del descriptor[key]
                removed.append(name)
    return removed


def image_hashes(pdf: pikepdf.Pdf) -> set[str]:
    return {hashlib.sha256(image.read_raw_bytes()).hexdigest() for image in image_xobjects(pdf)}
