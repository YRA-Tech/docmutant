"""PDF mutations used by the reducer. Each works on an open ``pikepdf.Pdf``."""

from __future__ import annotations

import contextlib
import hashlib
import io
import math
import random
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass

import pikepdf
from fontTools.agl import toUnicode as glyph_name_text
from fontTools.encodings.StandardEncoding import StandardEncoding
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


# Marked-content property lists and optional-content dictionaries keep only
# structural keys. Everything else, for example /ActualText, /Alt, /E, /Lang,
# /T or private producer keys, can carry source text.
PROPERTY_KEEP = frozenset({"/Type", "/MCID", "/OCGs", "/P", "/VE", "/Intent"})
OCG_KEEP = frozenset({"/Type", "/Name", "/Intent"})
OCMD_KEEP = frozenset({"/Type", "/OCGs", "/P", "/VE"})
FORM_KEEP = frozenset(
    {
        "/Type",
        "/Subtype",
        "/FormType",
        "/BBox",
        "/Matrix",
        "/Resources",
        "/Group",
        "/Length",
        "/Filter",
        "/DecodeParms",
    }
)


def content_streams(pdf: pikepdf.Pdf) -> list[StreamRef]:
    """Every content stream: pages, form XObjects, tiling patterns, Type3 glyph procedures."""
    refs = [StreamRef("page", index) for index in range(len(pdf.pages))]
    seen: set[tuple[int, int]] = set()
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream) and (
            obj.get("/Subtype") == "/Form" or obj.get("/PatternType") == 1
        ):
            kind = "form" if obj.get("/Subtype") == "/Form" else "pattern"
            if obj.objgen not in seen:
                seen.add(obj.objgen)
                refs.append(StreamRef(kind, 0, obj.objgen))
        elif isinstance(obj, pikepdf.Dictionary) and obj.get("/Subtype") == "/Type3":
            for _, proc in obj.get("/CharProcs", pikepdf.Dictionary()).items():
                if isinstance(proc, pikepdf.Stream) and proc.objgen not in seen:
                    seen.add(proc.objgen)
                    refs.append(StreamRef("charproc", 0, proc.objgen))
    return refs


_PLACEHOLDER_NAME = re.compile(r"Layer \d+")


def _is_name(value: object) -> bool:
    return isinstance(value, pikepdf.Name)


def _visibility_expression(value: object) -> bool:
    if _is_name(value) or isinstance(value, pikepdf.Dictionary):
        return True
    return isinstance(value, pikepdf.Array) and all(_visibility_expression(v) for v in value)


def _structural(key: str, value: object) -> bool:
    """True when ``value`` has the type that the structural ``key`` requires.

    An allowed key with a value of another type (for example ``/MCID (text)``)
    could carry source text, so it is dropped like an unknown key.
    """
    if key == "/MCID":
        return isinstance(value, int) and not isinstance(value, bool)
    if key in ("/Type", "/P"):
        return _is_name(value)
    if key == "/Intent":
        return _is_name(value) or (
            isinstance(value, pikepdf.Array) and all(_is_name(v) for v in value)
        )
    if key == "/OCGs":
        return isinstance(value, pikepdf.Dictionary) or (
            isinstance(value, pikepdf.Array)
            and all(isinstance(v, pikepdf.Dictionary) for v in value)
        )
    if key == "/VE":
        return _visibility_expression(value)
    return False


def _direct_dictionaries(obj: pikepdf.Object) -> Iterator[pikepdf.Object]:
    """``obj`` and every dictionary nested in it directly (indirect references not followed)."""
    stack = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, (pikepdf.Dictionary, pikepdf.Stream)):
            yield current
            children = [current[key] for key in current]
        elif isinstance(current, pikepdf.Array):
            children = list(current)
        else:
            continue
        stack.extend(
            child
            for child in children
            if isinstance(child, (pikepdf.Dictionary, pikepdf.Array)) and child.objgen == (0, 0)
        )


def _members(value: object) -> Iterator[pikepdf.Object]:
    """Dictionaries listed in an /OCGs or /VE value, direct or indirect, at any depth."""
    if isinstance(value, pikepdf.Dictionary):
        yield value
    elif isinstance(value, pikepdf.Array):
        for item in value:
            yield from _members(item)


def strip_marked_content_text(pdf: pikepdf.Pdf) -> int:
    """Reduce marked-content and optional-content properties to structural keys and values.

    Covers inline BDC/DP property lists in every content stream, and every
    /Properties entry and OCG/OCMD dictionary anywhere in the file, direct
    dictionaries included. OCG names, which the specification requires, become
    neutral placeholders.
    """
    layers = [0]
    cleaned: set[tuple[int, int]] = set()

    def clean(entry: object) -> int:
        """Clean one property, OCG or OCMD dictionary and the dictionaries it holds.

        Indirect objects are cleaned once. Direct ones are cleaned wherever they
        are met; cleaning is idempotent, and Python object ids are not stable
        enough to deduplicate them safely.
        """
        if not isinstance(entry, (pikepdf.Dictionary, pikepdf.Stream)):
            return 0
        if entry.objgen != (0, 0):
            if entry.objgen in cleaned:
                return 0
            cleaned.add(entry.objgen)
        kind = entry.get("/Type")
        allowed = {"/OCG": OCG_KEEP, "/OCMD": OCMD_KEEP}.get(str(kind), PROPERTY_KEEP)
        removed = [
            key
            for key in entry
            if key not in allowed or (key != "/Name" and not _structural(key, entry[key]))
        ]
        for key in removed:
            del entry[key]
        count = len(removed)
        if kind == "/OCG" and not _PLACEHOLDER_NAME.fullmatch(str(entry.get("/Name", ""))):
            layers[0] += 1
            entry.Name = pikepdf.String(f"Layer {layers[0]}")
            count += 1  # a rename is a change: callers must write the result back
        for key in ("/OCGs", "/VE"):
            if key in entry:
                count += sum(clean(member) for member in _members(entry[key]))
        return count

    count = 0
    for ref in content_streams(pdf):
        rewritten: list[Instruction] = []
        changed = False
        for instruction in read_stream(pdf, ref):
            if (
                isinstance(instruction, pikepdf.ContentStreamInstruction)
                and str(instruction.operator) in ("BDC", "DP")
                and len(instruction.operands) == 2
                and isinstance(instruction.operands[1], pikepdf.Dictionary)
            ):
                props = pikepdf.Dictionary({k: v for k, v in instruction.operands[1].items()})
                removed = clean(props)
                if removed:
                    count += removed
                    changed = True
                    instruction = pikepdf.ContentStreamInstruction(
                        [instruction.operands[0], props], instruction.operator
                    )
            rewritten.append(instruction)
        if changed:
            write_stream(pdf, ref, rewritten)

    for obj in list(pdf.objects):
        if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
            continue
        for dictionary in _direct_dictionaries(obj):
            if dictionary.get("/Type") in ("/OCG", "/OCMD"):
                count += clean(dictionary)
            properties = dictionary.get("/Properties")
            if isinstance(properties, pikepdf.Dictionary):
                for _, entry in properties.items():
                    count += clean(entry)
    return count


def strip_form_extras(pdf: pikepdf.Pdf) -> list[str]:
    """Drop every form XObject key that rendering does not need."""
    removed: list[str] = []
    for ref in form_xobjects(pdf):
        assert ref.objgen is not None
        form = pdf.get_object(ref.objgen)
        for key in [k for k in form if k not in FORM_KEEP]:
            del form[key]
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

    kind: str  # "page", "form", "pattern" or "charproc"
    page_index: int
    objgen: tuple[int, int] | None = None

    def label(self) -> str:
        if self.kind == "page":
            return f"page {self.page_index + 1}"
        return f"{self.kind} {self.objgen[0]} {self.objgen[1]}" if self.objgen else self.kind


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
        text = {**simple_spaces(font.get("/Encoding")), **to_unicode(font.get("/ToUnicode"))}
        return FontCodes(1, widths, _spaces(text))
    if subtype == "/Type0" and font.get("/Encoding") == "/Identity-H":
        descendant = font.DescendantFonts[0]
        default = float(descendant.get("/DW", 1000))
        widths = cid_widths(descendant.get("/W"))
        if not widths:
            return None
        keep = _spaces(to_unicode(font.get("/ToUnicode")))
        widths = {cid: (w if w is not None else default) for cid, w in widths.items()}
        return FontCodes(2, widths, keep)
    return None


def _spaces(text: dict[int, str]) -> frozenset[int]:
    return frozenset(code for code, item in text.items() if item.isspace())


def cid_widths(w_array: pikepdf.Object | None) -> dict[int, float]:
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


CMAP_TOKEN = re.compile(
    rb"%[^\r\n]*|\([^)]*\)|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/<>\[\]()%]+|[^\s/<>\[\]()%]+"
)


def _hex_bytes(token: bytes) -> bytes:
    digits = b"".join(token[1:-1].split())
    return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode())


def to_unicode(stream: pikepdf.Object | None) -> dict[int, str]:
    """Code to text, from the ``bfchar`` and ``bfrange`` sections of a ``/ToUnicode`` CMap.

    A damaged CMap gives what was read before the damage: the text decides only where
    words end, so it is no reason to refuse a page whose positions are known.
    """
    if not isinstance(stream, pikepdf.Stream):
        return {}
    try:
        tokens = [t for t in CMAP_TOKEN.findall(stream.read_bytes()) if not t.startswith(b"%")]
    except pikepdf.PdfError:
        return {}

    def code(token: bytes) -> int:
        return int.from_bytes(_hex_bytes(token), "big")

    def text(target: bytes) -> str:
        return target.decode("utf-16-be", "replace")

    mapping: dict[int, str] = {}
    section: bytes | None = None
    operands: list[bytes] = []
    try:
        for token in tokens:
            if token in (b"beginbfchar", b"beginbfrange"):
                section, operands = token, []
            elif token in (b"endbfchar", b"endbfrange"):
                section = None
            elif section is not None:
                operands.append(token)
                if section == b"beginbfchar" and len(operands) == 2:
                    mapping[code(operands[0])] = text(_hex_bytes(operands[1]))
                    operands = []
                elif section == b"beginbfrange" and len(operands) >= 3:
                    first, last = code(operands[0]), code(operands[1])
                    last = min(last, first + 65535)
                    if operands[2] != b"[":
                        # One target: its value counts up with the code.
                        target = _hex_bytes(operands[2])
                        base, size = int.from_bytes(target, "big"), len(target)
                        for value in range(first, last + 1):
                            shifted = (base + value - first) % (1 << (8 * size))
                            mapping[value] = text(shifted.to_bytes(size, "big"))
                        operands = []
                    elif token == b"]":
                        targets = operands[3:-1]
                        for value, item in zip(range(first, last + 1), targets, strict=False):
                            mapping[value] = text(_hex_bytes(item))
                        operands = []
    except ValueError:
        pass
    # U+0000 is what some producers write for a glyph they could not name.
    return {value: item for value, item in mapping.items() if item != "\x00"}


def simple_spaces(encoding: pikepdf.Object | None) -> dict[int, str]:
    """Codes that a simple font's encoding names as spaces: 32 unless ``/Differences`` moves it."""
    spaces = {32: " "}
    if not isinstance(encoding, pikepdf.Dictionary):
        return spaces
    code = 0
    for item in encoding.get("/Differences", []):
        if isinstance(item, pikepdf.Name):
            if item in ("/space", "/nbspace"):
                spaces[code] = " "
            else:
                spaces.pop(code, None)
            code += 1
        else:
            code = int(item)
    return spaces


def encoding_text(encoding: pikepdf.Object | None) -> dict[int, str]:
    """Code to text from a simple font's encoding: its named base and its glyph names.

    A font with no named base has a built-in encoding, which only the font program knows, so
    only its ``/Differences`` say anything. A glyph name that says nothing, such as ``g12``,
    takes its code out of the table, because the code no longer means what the base says.
    """
    if isinstance(encoding, pikepdf.Name):
        base, differences = encoding, []
    elif isinstance(encoding, pikepdf.Dictionary):
        base, differences = encoding.get("/BaseEncoding"), encoding.get("/Differences", [])
    else:
        return {}
    text: dict[int, str] = {}
    if base == "/StandardEncoding":
        for code, name in enumerate(StandardEncoding):
            if name != ".notdef":
                text[code] = glyph_name_text(name)
    elif base in ("/WinAnsiEncoding", "/MacRomanEncoding"):
        codec = "cp1252" if base == "/WinAnsiEncoding" else "mac_roman"
        for code in range(32, 256):
            with contextlib.suppress(UnicodeDecodeError):
                text[code] = bytes([code]).decode(codec)
    code = 0
    for item in differences:
        if isinstance(item, pikepdf.Name):
            named = glyph_name_text(str(item)[1:])
            if named:
                text[code] = named
            else:
                text.pop(code, None)
            code += 1
        else:
            code = int(item)
    return {value: item for value, item in text.items() if item}


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
