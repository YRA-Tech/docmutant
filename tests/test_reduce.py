"""End-to-end reduction against a fake target that crashes on one chosen feature."""

from __future__ import annotations

import io
import sys
import zlib
from pathlib import Path

import pikepdf
import pytest

from docmutant.cli import _portable_command
from docmutant.content import ddmin, flatten, parse_units
from docmutant.mutate import (
    FORM_KEEP,
    IMAGE_KEEP,
    OCG_KEEP,
    PAGE_KEEP,
    PROPERTY_KEEP,
    font_codes,
    image_hashes,
    strip_marked_content_text,
)
from docmutant.oracles import CommandOracle, OracleKind
from docmutant.reduce import OracleDidNotFire, Reducer

FAKE_TARGET = Path(__file__).with_name("fake_target.py")
SECRET = "Confidential quarterly figures"


def _oracle(feature: str) -> CommandOracle:
    return CommandOracle(
        kind=OracleKind.CRASH,
        argv=[sys.executable, str(FAKE_TARGET), "{pdf}", "{page}", feature],
        target="fake target",
        timeout=30,
    )


def _source_pdf() -> bytes:
    """Three pages; page 2 holds text, an image, unrelated paths and one trigger path."""
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            FirstChar=32,
            LastChar=122,
            Widths=[278 if code == 32 else (556 if code % 2 else 500) for code in range(32, 123)],
        )
    )
    image = pdf.make_stream(
        zlib.compress(bytes(range(256)) * 16),
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Image,
        Width=64,
        Height=64,
        ColorSpace=pikepdf.Name.DeviceGray,
        BitsPerComponent=8,
        Filter=pikepdf.Name.FlateDecode,
        Name=pikepdf.Name.X,
        PrivateNote=pikepdf.String(SECRET),
    )
    form = pdf.make_stream(
        b"0 0 5 5 re f",
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 10, 10],
        PrivateData=pikepdf.String(SECRET),
    )
    content = (
        f"q 1 0 0 1 10 10 cm 0 0 50 50 re f Q\n"
        f"BT /F1 12 Tf 72 700 Td ({SECRET}) Tj ET\n"
        f"q 100 0 0 100 300 300 cm /Im1 Do Q\n"
        f"q 0.5 w 10 10 m 123.456 50 l S Q\n"
        f"q 200 200 m 250 250 l 300 200 l f Q\n"
        f"/Span <</ActualText ({SECRET})>> BDC 5 5 m 6 6 l S EMC\n"
        f"/OC /P1 BDC 7 7 m 8 8 l S EMC\n"
        f"q /Fm1 Do Q\n"
    ).encode()
    for index in range(3):
        body = content if index == 1 else b"q 0 0 10 10 re f Q"
        page = pikepdf.Page(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Page,
                MediaBox=[0, 0, 612, 792],
                Resources=pikepdf.Dictionary(
                    Font=pikepdf.Dictionary(F1=font),
                    XObject=pikepdf.Dictionary(Im1=image, Fm1=form),
                    Properties=pikepdf.Dictionary(
                        P1=pikepdf.Dictionary(
                            Type=pikepdf.Name.OCG,
                            Name=pikepdf.String(SECRET),
                            Alt=pikepdf.String(SECRET),
                            Usage=pikepdf.Dictionary(
                                CreatorInfo=pikepdf.Dictionary(Creator=pikepdf.String(SECRET))
                            ),
                        )
                    ),
                ),
                Contents=pdf.make_stream(body),
                CAPT_Info=pikepdf.Dictionary(SK=pikepdf.String(r"c:\scans\secret_0002.pdf")),
            )
        )
        pdf.pages.append(page)
    with pdf.open_metadata() as meta:
        meta["dc:title"] = SECRET
    pdf.docinfo["/Title"] = SECRET
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def _content_ops(data: bytes) -> list[str]:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return [str(i.operator) for i in pikepdf.parse_content_stream(pdf.pages[0])]


def test_reduction_keeps_only_the_trigger_path() -> None:
    source = _source_pdf()
    result = Reducer(_oracle("coordinate"), log=lambda _: None).run(source, page=2)

    assert SECRET.encode() not in result.data
    with pikepdf.open(io.BytesIO(result.data)) as pdf:
        assert len(pdf.pages) == 1
        assert "/Info" not in pdf.trailer and "/Metadata" not in pdf.Root
        assert image_hashes(pdf) == set()
        assert set(pdf.pages[0].obj.keys()) <= PAGE_KEEP
    assert _content_ops(result.data) == ["m", "l", "S"]
    operators = [m["operator"] for m in result.mutations]
    assert operators[:2] == ["keep-pages", "remove-unreferenced-resources"]
    assert "delete-content" in operators


def test_needed_text_is_kept_but_swapped_at_equal_widths() -> None:
    source = _source_pdf()
    result = Reducer(_oracle("text"), log=lambda _: None).run(source, page=2)

    with pikepdf.open(io.BytesIO(result.data)) as pdf:
        shown = [
            bytes(i.operands[0])
            for i in pikepdf.parse_content_stream(pdf.pages[0])
            if str(i.operator) == "Tj"
        ]
        widths = font_codes(pdf.pages[0].Resources.Font.F1).width_of
    assert len(shown) == 1
    original = SECRET.encode()
    assert shown[0] != original
    assert [widths[c] for c in shown[0]] == [widths[c] for c in original]
    assert [c == 32 for c in shown[0]] == [c == 32 for c in original]


def test_needed_image_keeps_its_place_but_not_its_samples() -> None:
    source = _source_pdf()
    with pikepdf.open(io.BytesIO(source)) as pdf:
        source_images = image_hashes(pdf)
    result = Reducer(_oracle("image"), log=lambda _: None).run(source, page=2)

    with pikepdf.open(io.BytesIO(result.data)) as pdf:
        images = image_hashes(pdf)
        image = next(iter(pdf.pages[0].Resources.XObject.values()))
        assert (int(image.Width), int(image.Height)) == (64, 64)
        assert set(image.keys()) <= IMAGE_KEEP
    assert len(images) == 1
    assert images.isdisjoint(source_images)


def test_silent_source_is_refused() -> None:
    with pytest.raises(OracleDidNotFire):
        Reducer(_oracle("coordinate"), log=lambda _: None).run(_source_pdf(), page=1)


def test_unbalanced_stream_falls_back_to_flat_units() -> None:
    pdf = pikepdf.Pdf.new()
    instructions = list(pikepdf.parse_content_stream(pdf.make_stream(b"Q q")))
    units = parse_units(instructions)
    assert [u.kind for u in units] == ["state", "state"]
    assert flatten(units) == instructions


def test_ddmin_finds_a_one_minimal_pair() -> None:
    items = list(range(20))
    assert ddmin(items, lambda subset: 3 in subset and 17 in subset) == [3, 17]


def test_recorded_command_has_no_local_paths(tmp_path: Path) -> None:
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    command = _portable_command(
        ["/home/user/.venv/bin/docmutant", "reduce", str(pdf), "--target-python", "/opt/x/python"]
    )
    assert command == "docmutant reduce manual.pdf --target-python python"


def test_needed_marked_content_loses_its_text() -> None:
    result = Reducer(_oracle("marked"), log=lambda _: None).run(_source_pdf(), page=2)
    with pikepdf.open(io.BytesIO(result.data)) as pdf:
        page = pdf.pages[0]
        for instruction in pikepdf.parse_content_stream(page):
            if str(instruction.operator) == "BDC" and isinstance(
                instruction.operands[1], pikepdf.Dictionary
            ):
                assert set(instruction.operands[1].keys()) <= PROPERTY_KEEP
        for _, entry in page.Resources.get("/Properties", {}).items():
            assert set(entry.keys()) <= OCG_KEEP
            assert str(entry.Name) != SECRET


def test_needed_form_keeps_only_rendering_keys() -> None:
    result = Reducer(_oracle("form"), log=lambda _: None).run(_source_pdf(), page=2)
    with pikepdf.open(io.BytesIO(result.data)) as pdf:
        form = pdf.pages[0].Resources.XObject.Fm1
        assert set(form.keys()) <= FORM_KEEP


def test_recorded_command_cleans_option_values(tmp_path: Path) -> None:
    command = _portable_command(["docmutant", "reduce", "--out=/home/user/secret/p7.pdf"])
    assert command == "docmutant reduce --out=p7.pdf"


def test_marked_content_in_patterns_and_type3_glyphs_is_cleaned() -> None:
    pdf = pikepdf.Pdf.new()
    marked = f"/Span <</ActualText ({SECRET}) /MCID 0>> BDC 0 0 1 1 re f EMC".encode()
    pattern = pdf.make_stream(
        marked, PatternType=1, PaintType=1, TilingType=1, BBox=[0, 0, 4, 4], XStep=4, YStep=4
    )
    glyph = pdf.make_stream(b"0 0 d0 " + marked)
    type3 = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type3, CharProcs={"/a": glyph}
        )
    )
    pdf.add_blank_page()
    pdf.pages[0].Resources = pikepdf.Dictionary(
        Pattern=pikepdf.Dictionary(P0=pattern), Font=pikepdf.Dictionary(T3=type3)
    )

    strip_marked_content_text(pdf)

    for stream in (pattern, glyph):
        for instruction in pikepdf.parse_content_stream(stream):
            if str(instruction.operator) == "BDC":
                assert set(instruction.operands[1].keys()) == {"/MCID"}


def test_properties_in_direct_resources_and_wrong_value_types_are_cleaned() -> None:
    pdf = pikepdf.Pdf.new()
    form = pdf.make_stream(
        f"/Span <</MCID ({SECRET}) /P /Art>> BDC 0 0 1 1 re f EMC".encode(),
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 4, 4],
        # A direct /Resources dictionary: not an object of its own in the file.
        Resources=pikepdf.Dictionary(
            Properties=pikepdf.Dictionary(
                MC0=pikepdf.Dictionary(Alt=pikepdf.String(SECRET), MCID=3),
                OC0=pikepdf.Dictionary(
                    Type=pikepdf.Name.OCMD,
                    OCGs=pikepdf.Dictionary(
                        Type=pikepdf.Name.OCG,
                        Name=pikepdf.String(SECRET),
                        Usage=pikepdf.Dictionary(Creator=pikepdf.String(SECRET)),
                    ),
                ),
            )
        ),
    )
    pdf.add_blank_page()
    pdf.pages[0].Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm0=form))

    strip_marked_content_text(pdf)

    properties = form.Resources.Properties
    assert set(properties.MC0.keys()) == {"/MCID"}
    ocg = properties.OC0.OCGs
    assert set(ocg.keys()) <= OCG_KEEP and str(ocg.Name) == "Layer 1"
    for instruction in pikepdf.parse_content_stream(form):
        if str(instruction.operator) == "BDC":
            assert dict(instruction.operands[1].items()) == {"/P": pikepdf.Name.Art}
