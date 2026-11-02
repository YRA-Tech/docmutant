"""Hidden layers: the text of a document outside what its pages draw. See 5.6 of the design.

Producers add private keys that no list can know, so this denies by default. It walks everything
reachable from the trailer: a string that is not known to be something other than text is
rewritten, and a stream that is not known to be part of drawing a page is removed. The catalog
and the pages keep only the keys that the document's structure needs. Tags, form fields and
annotations stay, with their values rewritten, so that the structure can still be used.

A rewritten string keeps its shape: letters stay letters and digits stay digits, and the same
string always becomes the same string, so that a form value still matches its options and a
table cell its header.

``residual_strings`` and ``leftover_layers`` check the result on the saved file. They share the
walk, but not the rules for what to change.
"""

from __future__ import annotations

import random
import re
import string
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import pikepdf

from docmutant.content import operator_name
from docmutant.fit import draws_text
from docmutant.mutate import (
    _PLACEHOLDER_NAME,
    TEXT_SHOW_OPERATORS,
    content_streams,
    read_stream,
    strip_marked_content_text,
)

RUN = re.compile(r"[^\W\d_]+|\d+")
"""What a reader of the text takes for a word: punctuation splits it."""

SHORTEST = 3
"""Shortest run that counts as text of the source: a shorter one matches by chance."""

TRIES = 20
"""Attempts at a rewritten string with no word of the source in it."""

CATALOG_KEEP = frozenset(
    {
        "/Type",
        "/Pages",
        "/Outlines",
        "/StructTreeRoot",
        "/MarkInfo",
        "/Lang",
        "/AcroForm",
        "/OCProperties",
        "/ViewerPreferences",
        "/PageLabels",
        "/PageLayout",
        "/PageMode",
        "/Version",
    }
)
PAGE_KEEP = frozenset(
    {
        "/Type",
        "/Parent",
        "/Resources",
        "/MediaBox",
        "/CropBox",
        "/BleedBox",
        "/TrimBox",
        "/ArtBox",
        "/Rotate",
        "/Contents",
        "/UserUnit",
        "/Group",
        "/Annots",
        "/StructParents",
        "/Tabs",
    }
)
ANNOTATION_KEEP = frozenset(
    {
        "/Link",
        "/Widget",
        "/Text",
        "/FreeText",
        "/Highlight",
        "/Underline",
        "/Squiggly",
        "/StrikeOut",
        "/Square",
        "/Circle",
        "/Line",
        "/Polygon",
        "/PolyLine",
        "/Popup",
        "/Caret",
    }
)
"""The others carry media or a person's own marks: a file, a sound, a stamp, handwriting."""

ANNOTATION_HUSK = frozenset({"/Type", "/Subtype", "/Rect"})

REMOVED_KEYS = frozenset(
    {
        "/Metadata",
        "/PieceInfo",
        "/LastModified",
        "/CreationDate",
        "/ModDate",
        "/ActualText",
        "/RC",
        "/RV",
        "/DS",
        "/Thumb",
        "/AA",
        "/EF",
        "/AF",
        "/CharSet",
        "/XFA",
    }
)
"""Removed wherever they stand. /ActualText goes because a rewritten one would no longer match
the glyphs, and the glyphs then speak for themselves. /CharSet lists the letters of a subset."""

NOT_TEXT = frozenset({"/DA", "/Registry", "/Ordering", "/Lang", "/Panose"})
"""Strings that a viewer reads as data. /Lang stays because the language is not personal, and
because a screen reader needs it."""

STREAM_ATTRIBUTES = frozenset({"/PatternType", "/ShadingType", "/FunctionType", "/HalftoneType"})
STREAM_SUBTYPES = frozenset({"/Image", "/Form", "/Type1C", "/CIDFontType0C", "/OpenType"})
STREAM_KEYS = frozenset(
    {
        "/Contents",
        "/ToUnicode",
        "/Encoding",
        "/UseCMap",
        "/CIDToGIDMap",
        "/CIDSet",
        "/JBIG2Globals",
        "/FontFile",
        "/FontFile2",
        "/FontFile3",
        "/N",
        "/R",
        "/D",
    }
)
"""/N, /R and /D are the states of an appearance, which need no /Subtype."""

STANDARD_ROLES = frozenset(
    {
        "/Document",
        "/DocumentFragment",
        "/Part",
        "/Art",
        "/Sect",
        "/Div",
        "/Aside",
        "/BlockQuote",
        "/Caption",
        "/TOC",
        "/TOCI",
        "/Index",
        "/NonStruct",
        "/Private",
        "/P",
        "/H",
        "/H1",
        "/H2",
        "/H3",
        "/H4",
        "/H5",
        "/H6",
        "/Title",
        "/FENote",
        "/L",
        "/LI",
        "/Lbl",
        "/LBody",
        "/Table",
        "/TR",
        "/TH",
        "/TD",
        "/THead",
        "/TBody",
        "/TFoot",
        "/Span",
        "/Quote",
        "/Note",
        "/Reference",
        "/BibEntry",
        "/Code",
        "/Link",
        "/Annot",
        "/Ruby",
        "/RB",
        "/RT",
        "/RP",
        "/Warichu",
        "/WT",
        "/WP",
        "/Em",
        "/Strong",
        "/Sub",
        "/Figure",
        "/Formula",
        "/Form",
    }
)

VOWELS = "aeiouy"
CONSONANTS = "".join(sorted(set(string.ascii_lowercase) - set(VOWELS)))


@dataclass(frozen=True)
class LayerReport:
    """Counts by layer: nothing in it comes from the text of the source."""

    removed: dict[str, int]
    rewritten: dict[str, int]


def runs(text: str) -> Iterator[str]:
    """The casefolded runs of letters or digits long enough to count as text of the source."""
    for run in RUN.findall(text):
        if len(run) >= SHORTEST:
            yield run.casefold()


class Rewriter:
    """Rewrites strings in their shape, the same way each time, avoiding the given words."""

    def __init__(self, seed: int, avoid: Iterable[str]) -> None:
        self.rng = random.Random(f"{seed}:layers")
        self.avoid = frozenset(avoid)
        self.done: dict[str, str] = {}

    def _char(self, char: str) -> str:
        if char.isdigit():
            pool = string.digits
        elif not char.isalpha():
            return char
        else:
            pool = VOWELS if char.casefold() in VOWELS else CONSONANTS
        new = self.rng.choice([other for other in pool if other != char.casefold()])
        return new.upper() if char.isupper() else new

    def __call__(self, text: str) -> str:
        if text in self.done:
            return self.done[text]
        new = text
        for _ in range(TRIES):
            new = "".join(self._char(char) for char in text)
            if self.avoid.isdisjoint(runs(new)):
                break
        self.done[text] = self.done[new] = new
        return new


def _indexed(array: pikepdf.Object) -> bool:
    return len(array) > 0 and array[0] in ("/Indexed", "/I")


def _walk(pdf: pikepdf.Pdf) -> Iterator[tuple[pikepdf.Object, str | int, pikepdf.Object, str]]:
    """Each entry reachable from the trailer: its holder, its key, its value and the key that
    leads to the holder. An array's members count as under the array's own key.

    Entries may be removed while walking, and a value may be replaced before going on.
    """
    seen: set[tuple[int, int]] = set()
    stack: list[tuple[pikepdf.Object, str]] = [(pdf.trailer, "")]
    while stack:
        holder, via = stack.pop()
        if isinstance(holder, pikepdf.Array):
            entries = [(index, via) for index in range(len(holder))]
        else:
            entries = [(str(key), str(key)) for key in holder]
        for key, under in entries:
            if isinstance(key, str) and key not in holder:
                continue
            yield holder, key, holder[key], via
            if isinstance(key, str) and key not in holder:
                continue
            value = holder[key]
            if not isinstance(value, (pikepdf.Dictionary, pikepdf.Array, pikepdf.Stream)):
                continue
            if value.objgen != (0, 0):
                if value.objgen in seen:
                    continue
                seen.add(value.objgen)
            stack.append((value, under))


def _is_text(holder: pikepdf.Object, key: str | int, value: pikepdf.Object) -> bool:
    if not isinstance(value, pikepdf.String) or key in NOT_TEXT:
        return False
    if isinstance(holder, pikepdf.Array) and _indexed(holder):
        return False
    return not (key == "/Name" and _PLACEHOLDER_NAME.fullmatch(str(value)))


def _known_stream(holder: pikepdf.Object, stream: pikepdf.Stream, key: str | int, via: str) -> bool:
    return (
        (isinstance(holder, pikepdf.Array) and _indexed(holder))
        or key in STREAM_KEYS
        or via in STREAM_KEYS
        or via == "/CharProcs"
        or str(stream.get("/Subtype")) in STREAM_SUBTYPES
        or stream.get("/Type") == "/CMap"
        or any(attribute in stream for attribute in STREAM_ATTRIBUTES)
        or "/Length1" in stream
        or "/N" in stream
    )


def _marked_strings(pdf: pikepdf.Pdf) -> Iterator[str]:
    for ref in content_streams(pdf):
        for instruction in read_stream(pdf, ref):
            if operator_name(instruction) not in ("BDC", "DP"):
                continue
            stack = list(instruction.operands)
            while stack:
                value = stack.pop()
                if isinstance(value, pikepdf.String):
                    yield str(value)
                elif isinstance(value, pikepdf.Array):
                    stack.extend(value)
                elif isinstance(value, pikepdf.Dictionary):
                    stack.extend(value[key] for key in value)


def hidden_words(pdf: pikepdf.Pdf) -> set[str]:
    """The words of the hidden layers, which no replacement in any layer may use."""
    found: set[str] = set()
    for holder, key, value, _ in _walk(pdf):
        if _is_text(holder, key, value):
            found.update(runs(str(value)))
        elif key == "/Metadata" and isinstance(value, pikepdf.Stream):
            xml = value.read_bytes().decode("utf-8", "replace")
            for part in re.findall(r">([^<]+)<", xml):
                found.update(runs(part))
    found.update(run for text in _marked_strings(pdf) for run in runs(text))
    return found


def _name_tree(tree: pikepdf.Object) -> Iterator[tuple[str, pikepdf.Object]]:
    seen: set[tuple[int, int]] = set()
    stack = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, pikepdf.Dictionary) or node.objgen in seen - {(0, 0)}:
            continue
        seen.add(node.objgen)
        names = node.get("/Names", [])
        for index in range(0, len(names) - 1, 2):
            yield str(names[index]), names[index + 1]
        stack.extend(node.get("/Kids", []))


def _named_destinations(pdf: pikepdf.Pdf) -> dict[str, pikepdf.Object]:
    found: dict[str, pikepdf.Object] = {}
    old = pdf.Root.get("/Dests")
    if isinstance(old, pikepdf.Dictionary):
        found.update({str(key)[1:]: old[key] for key in old})
    names = pdf.Root.get("/Names")
    tree = names.get("/Dests") if names is not None else None
    if tree is not None:
        found.update(_name_tree(tree))
    return {
        name: target.get("/D", target) if isinstance(target, pikepdf.Dictionary) else target
        for name, target in found.items()
    }


def _destination_name(value: pikepdf.Object) -> str | None:
    if isinstance(value, pikepdf.Name):
        return str(value)[1:]
    return str(value) if isinstance(value, pikepdf.String) else None


def _clean_navigation(
    holder: pikepdf.Object, destinations: dict[str, pikepdf.Object], rewritten: Counter[str]
) -> None:
    """Point a link or outline item at its page, and take away any other action.

    A destination's name can hold text, such as ``cite.smith2018``, and an action can hold a
    web address or a script.
    """
    if "/Dest" in holder:
        name = _destination_name(holder.Dest)
        if name is not None:
            if name in destinations:
                holder.Dest = destinations[name]
                rewritten["named destinations"] += 1
            else:
                del holder["/Dest"]
    action = holder.get("/A")
    # The /A of a tag holds its attributes, which are no action.
    if not isinstance(action, pikepdf.Dictionary) or "/S" not in action:
        return
    if action.get("/S") == "/GoTo" and "/D" in action:
        name = _destination_name(action.D)
        if name is None:
            action_target = action.D
        elif name in destinations:
            action_target = destinations[name]
            rewritten["named destinations"] += 1
        else:
            del holder["/A"]
            rewritten["actions"] += 1
            return
        holder.A = pikepdf.Dictionary(S=pikepdf.Name.GoTo, D=action_target)
    else:
        del holder["/A"]
        rewritten["actions"] += 1


def _appearance_draws_text(annotation: pikepdf.Object) -> bool:
    appearance = annotation.get("/AP")
    if not isinstance(appearance, pikepdf.Dictionary):
        return False
    stack = [appearance[key] for key in appearance]
    while stack:
        state = stack.pop()
        if isinstance(state, pikepdf.Stream):
            try:
                if draws_text(state, None, frozenset()):
                    return True
            except pikepdf.PdfError:
                return True
        elif isinstance(state, pikepdf.Dictionary):
            stack.extend(state[key] for key in state)
    return False


def _clean_annotations(pdf: pikepdf.Pdf, removed: Counter[str]) -> set[tuple[int, int]]:
    """Remove what must go; returns the objects, so that the tags can let go of them too."""
    gone: set[tuple[int, int]] = set()
    for page in pdf.pages:
        annotations = page.obj.get("/Annots")
        if not isinstance(annotations, pikepdf.Array):
            continue
        kept = pikepdf.Array()
        for annotation in annotations:
            if not isinstance(annotation, pikepdf.Dictionary):
                continue
            if str(annotation.get("/Subtype")) not in ANNOTATION_KEEP:
                if annotation.objgen != (0, 0):
                    gone.add(annotation.objgen)
                # Tags and popups may still point here, so what is left must be empty.
                for key in [k for k in annotation if str(k) not in ANNOTATION_HUSK]:
                    del annotation[key]
                removed["annotations"] += 1
                continue
            kept.append(annotation)
        if kept:
            page.obj.Annots = kept
        else:
            del page.obj["/Annots"]
    return gone


def _clean_fields(pdf: pikepdf.Pdf, removed: Counter[str]) -> None:
    """Clear what a person chose or signed. Text values are rewritten with the other strings."""
    forms = pdf.Root.get("/AcroForm")
    if forms is None:
        return
    if "/SigFlags" in forms:
        del forms["/SigFlags"]
    seen: set[tuple[int, int]] = set()
    stack = [(field, None) for field in forms.get("/Fields", [])]
    while stack:
        field, inherited = stack.pop()
        if not isinstance(field, pikepdf.Dictionary) or field.objgen in seen:
            continue
        seen.add(field.objgen)
        kind = field.get("/FT", inherited)
        if kind in ("/Btn", "/Sig"):
            for key in ("/V", "/DV", "/SV", "/Lock"):
                if key in field:
                    del field[key]
                    removed["form choices and signatures"] += 1
            if "/AS" in field:
                field.AS = pikepdf.Name.Off
        if "/I" in field:
            del field["/I"]
        stack.extend((kid, kind) for kid in field.get("/Kids", []))


def role(element: pikepdf.Object, role_map: pikepdf.Object | None) -> str:
    """The structure type of an element, by its role map. A role name can hold text."""
    current = element.get("/S")
    for _ in range(10):
        if role_map is None or current not in role_map:
            break
        current = role_map[current]
    return str(current) if str(current) in STANDARD_ROLES else "/NonStruct"


def _elements(pdf: pikepdf.Pdf) -> Iterator[pikepdf.Object]:
    tree = pdf.Root.get("/StructTreeRoot")
    if tree is None:
        return
    seen: set[tuple[int, int]] = set()
    stack = [tree]
    while stack:
        element = stack.pop()
        if element.objgen != (0, 0):
            if element.objgen in seen:
                continue
            seen.add(element.objgen)
        yield element
        kids = element.get("/K")
        members = kids if isinstance(kids, pikepdf.Array) else [kids] if kids is not None else []
        stack.extend(kid for kid in members if isinstance(kid, pikepdf.Dictionary))


def _clean_tags(pdf: pikepdf.Pdf, gone: set[tuple[int, int]], rewrite: Rewriter) -> None:
    tree = pdf.Root.get("/StructTreeRoot")
    if tree is None:
        return
    role_map = tree.get("/RoleMap")
    for element in _elements(pdf):
        if "/S" in element and element.get("/Type") != "/OBJR":
            element.S = pikepdf.Name(role(element, role_map))
        kids = element.get("/K")
        if isinstance(kids, pikepdf.Array):
            if any(_points_at(kid, gone) for kid in kids):
                element.K = pikepdf.Array([kid for kid in kids if not _points_at(kid, gone)])
        elif _points_at(kids, gone):
            del element["/K"]
    if role_map is not None:
        del tree["/RoleMap"]
    # A name tree is sorted, and rewriting its keys breaks the order.
    ids = tree.get("/IDTree")
    if ids is not None:
        pairs: list[object] = []
        for name, target in sorted(
            ((rewrite(name), target) for name, target in _name_tree(ids)), key=lambda pair: pair[0]
        ):
            pairs.extend((pikepdf.String(name), target))
        tree.IDTree = pikepdf.Dictionary(Names=pikepdf.Array(pairs))


def _points_at(kid: object, gone: set[tuple[int, int]]) -> bool:
    if not isinstance(kid, pikepdf.Dictionary) or kid.get("/Type") != "/OBJR":
        return False
    target = kid.get("/Obj")
    return target is not None and target.objgen in gone


def clean_layers(pdf: pikepdf.Pdf, seed: int, avoid: Iterable[str]) -> LayerReport:
    """Remove or rewrite the hidden layers of ``pdf``, in place. See the module's description."""
    removed: Counter[str] = Counter()
    rewritten: Counter[str] = Counter()
    rewrite = Rewriter(seed, avoid)
    destinations = _named_destinations(pdf)
    forms = pdf.Root.get("/AcroForm")

    gone = _clean_annotations(pdf, removed)
    _clean_fields(pdf, removed)
    _clean_tags(pdf, gone, rewrite)
    if strip_marked_content_text(pdf):
        rewritten["marked content properties"] += 1

    for key in [k for k in pdf.trailer if str(k) not in ("/Root", "/Size")]:
        if str(key) in ("/Info", "/ID"):
            removed["document info" if str(key) == "/Info" else "file identifier"] += 1
        del pdf.trailer[key]
    for key in [k for k in pdf.Root if str(k) not in CATALOG_KEEP]:
        del pdf.Root[key]
        removed["catalog entries"] += 1
    for page in pdf.pages:
        for key in [k for k in page.obj if str(k) not in PAGE_KEEP]:
            del page.obj[key]
            removed["page entries"] += 1

    for holder, key, value, via in _walk(pdf):
        if isinstance(holder, pikepdf.Dictionary) and ("/Dest" in holder or "/A" in holder):
            _clean_navigation(holder, destinations, rewritten)
            # It can have replaced or removed the very entry that this step is at.
            if key not in holder:
                continue
            value = holder[key]
        if isinstance(key, str) and (
            key in REMOVED_KEYS or (key == "/M" and isinstance(value, pikepdf.String))
        ):
            del holder[key]
            removed["metadata and attachments"] += 1
        elif key == "/AP" and _appearance_draws_text(holder):
            del holder[key]
            removed["appearances with text"] += 1
            if forms is not None and (holder.get("/Subtype") == "/Widget" or "/FT" in holder):
                forms.NeedAppearances = True
        elif _is_text(holder, key, value):
            holder[key] = pikepdf.String(rewrite(str(value)))
            rewritten["strings"] += 1
        elif isinstance(value, pikepdf.Stream) and not _known_stream(holder, value, key, via):
            if isinstance(key, str):
                del holder[key]
                removed["other streams"] += 1
    return LayerReport(dict(removed), dict(rewritten))


def soft_mask_images(pdf: pikepdf.Pdf) -> set[tuple[int, int]]:
    """The images that a soft mask draws: they say where a page shows, and show nothing."""
    found: set[tuple[int, int]] = set()
    seen: set[tuple[int, int]] = set()

    def visit(group: pikepdf.Object) -> None:
        if group.objgen in seen:
            return
        seen.add(group.objgen)
        resources = group.get("/Resources")
        xobjects = resources.get("/XObject") if resources is not None else None
        for key in xobjects if xobjects is not None else ():
            inner = xobjects[key]
            if inner.get("/Subtype") == "/Image":
                found.add(inner.objgen)
            elif inner.get("/Subtype") == "/Form":
                visit(inner)

    for holder, key, value, _ in _walk(pdf):
        if key == "/G" and "/S" in holder and isinstance(value, pikepdf.Stream):
            visit(value)
    return found


def residual_strings(pdf: pikepdf.Pdf, words: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Words of the source in any string of ``pdf``, by the key that holds them."""
    forbidden = frozenset(words)
    found: dict[str, set[str]] = {}
    for holder, key, value, via in _walk(pdf):
        if _is_text(holder, key, value):
            shared = forbidden.intersection(runs(str(value)))
            if shared:
                found.setdefault(str(key) if isinstance(key, str) else via, set()).update(shared)
    for text in _marked_strings(pdf):
        shared = forbidden.intersection(runs(text))
        if shared:
            found.setdefault("marked content", set()).update(shared)
    return {key: tuple(sorted(value)) for key, value in found.items()}


def drawn(page: pikepdf.Page) -> tuple[set[tuple[int, int]], set[tuple[int, int, int]]]:
    """The forms that a page draws, at any depth, and the sizes and depths of its images.

    Sizes, not a count of drawings, because a form with text gets one copy for each drawing.
    """
    forms: set[tuple[int, int]] = set()
    images: set[tuple[int, int, int]] = set()

    def visit(stream: pikepdf.Object, resources: pikepdf.Object | None) -> None:
        xobjects = resources.get("/XObject") if resources is not None else None
        for instruction in pikepdf.parse_content_stream(stream):
            operator = operator_name(instruction)
            if operator == "INLINE IMAGE":
                inline = instruction.iimage.obj
                images.add(
                    (
                        int(inline.Width),
                        int(inline.Height),
                        int(inline.get("/BitsPerComponent", 1)),
                    )
                )
            elif operator == "Do" and xobjects is not None:
                inner = xobjects.get(instruction.operands[0])
                if not isinstance(inner, pikepdf.Stream):
                    continue
                if inner.get("/Subtype") == "/Image":
                    images.add(
                        (
                            int(inner.Width),
                            int(inner.Height),
                            int(inner.get("/BitsPerComponent", 1)),
                        )
                    )
                elif inner.get("/Subtype") == "/Form" and inner.objgen not in forms:
                    forms.add(inner.objgen)
                    visit(inner, inner.get("/Resources", resources))

    visit(page, page.obj.get("/Resources"))
    return forms, images


def leftover_layers(pdf: pikepdf.Pdf) -> dict[str, int]:
    """What the saved file must no longer hold, by layer. Empty when it is clean."""
    found: Counter[str] = Counter()
    if "/Info" in pdf.trailer:
        found["document info"] += 1
    for key in pdf.Root:
        if str(key) not in CATALOG_KEEP:
            found["catalog entries"] += 1
    for holder, key, value, via in _walk(pdf):
        if key in REMOVED_KEYS or (key == "/M" and isinstance(value, pikepdf.String)):
            found[str(key)] += 1
            continue
        if (
            key == "/A"
            and isinstance(value, pikepdf.Dictionary)
            and "/S" in value
            and value.get("/S") != "/GoTo"
        ):
            found["actions"] += 1
        if key == "/AP" and _appearance_draws_text(holder):
            found["appearances with text"] += 1
        if key == "/Dest" and isinstance(value, (pikepdf.String, pikepdf.Name)):
            found["named destinations"] += 1
        if isinstance(value, pikepdf.Stream) and not _known_stream(holder, value, key, via):
            found["other streams"] += 1
    shown: set[tuple[int, int]] = set()
    for page in pdf.pages:
        shown |= drawn(page)[0]
    for ref in content_streams(pdf):
        if ref.kind not in ("form", "pattern"):
            continue
        shows = any(operator_name(i) in TEXT_SHOW_OPERATORS for i in read_stream(pdf, ref))
        if shows and ref.kind == "pattern":
            found["text in patterns"] += 1
        elif shows and ref.objgen not in shown:
            found["forms with text that no page draws"] += 1
    return dict(found)


def tag_shape(pdf: pikepdf.Pdf) -> list[tuple[object, ...]]:
    """The tag tree without its values: each element's type and marked-content IDs.

    A link to an annotation does not count, since an annotation that held a file is removed.
    """
    tree = pdf.Root.get("/StructTreeRoot")
    if tree is None:
        return []
    role_map = tree.get("/RoleMap")
    shape: list[tuple[object, ...]] = []
    for element in _elements(pdf):
        if element.get("/Type") == "/OBJR" or element.get("/Type") == "/MCR":
            continue
        kids = element.get("/K")
        members = kids if isinstance(kids, pikepdf.Array) else [kids] if kids is not None else []
        ids = []
        children = 0
        for kid in members:
            if isinstance(kid, pikepdf.Dictionary):
                if kid.get("/Type") == "/MCR":
                    ids.append(int(kid.get("/MCID", -1)))
                elif kid.get("/Type") != "/OBJR":
                    children += 1
            elif kid is not None:
                ids.append(int(kid))
        kind = role(element, role_map) if "/S" in element else "root"
        shape.append((kind, tuple(ids), children))
    return shape
