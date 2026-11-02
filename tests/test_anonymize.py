"""The anonymize mode: nothing comes out unless the saved file passes, in any layer."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pikepdf
import pytest
from pydantic import ValidationError
from test_coverage import _Boxes
from test_fit import _pdf

from docmutant.anonymize import ProfileError, anonymize, replace_all_images, resolve_profile
from docmutant.cli import main
from docmutant.coverage import curved_fills
from docmutant.fit import CoverageStop, FitPolicy, WordFitter
from docmutant.geometry import BoxSettings, within
from docmutant.layers import (
    Rewriter,
    clean_layers,
    hidden_words,
    leftover_layers,
    residual_strings,
    role,
    tag_shape,
)
from docmutant.manifest import Manifest, load_manifest, manifest_path
from docmutant.mutate import to_unicode
from docmutant.substitute import CannotSubstitute, embed_substitute
from docmutant.tracker import group_lines, trace_page

PAGE = (
    b"BT /F1 10 Tf 72 700 Td (abcd abcd bcda) Tj ET BT /F1 10 Tf 72 680 Td (cdab dcba abcd) Tj ET "
)
GLYPHS = b"BT /F1 10 Tf 72 100 Td (a   b   c   d   e   w   A   B   C   D   E   W   ,) Tj ET "


def _bytes(pdf: pikepdf.Pdf) -> bytes:
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def _source(*, extra: bytes = b"") -> pikepdf.Pdf:
    return _pdf(PAGE + extra, glyphs=GLYPHS)


def _privacy(**settings) -> object:
    # The fixture's lines are short, so that they need room to take a new word.
    return resolve_profile("privacy", tolerance=within(5), **settings)


def _run(pdf: pikepdf.Pdf, profile=None, **options):
    options.setdefault("fonts", _Boxes())
    return anonymize(_bytes(pdf), profile or _privacy(), **options)


def _layered(pdf: pikepdf.Pdf) -> pikepdf.Pdf:
    """Text in each hidden layer, under words that the page does not show."""
    page = pdf.pages[0].obj
    pdf.Root.Metadata = pdf.make_stream(b"<x:xmpmeta><dc:title>Hiddenxmp</dc:title></x:xmpmeta>")
    pdf.trailer.Info = pdf.make_indirect(pikepdf.Dictionary(Author=pikepdf.String("Hiddenauthor")))
    pdf.trailer.ID = pikepdf.Array([pikepdf.String("sourceid"), pikepdf.String("sourceid")])
    pdf.Root.PrivateKey = pikepdf.String("Hiddenprivate")
    target = pikepdf.Array([page, pikepdf.Name.Fit])
    pdf.Root.Names = pikepdf.Dictionary(
        Dests=pikepdf.Dictionary(Names=[pikepdf.String("cite.hiddendest"), target]),
        JavaScript=pikepdf.Dictionary(Names=[]),
    )
    pdf.Root.Outlines = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Outlines,
            First=pdf.make_indirect(
                pikepdf.Dictionary(
                    Title=pikepdf.String("Hiddenoutline"),
                    Dest=pikepdf.String("cite.hiddendest"),
                )
            ),
        )
    )
    link = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name.Link,
            Rect=[0, 0, 10, 10],
            A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String("https://hiddenuri.test")),
        )
    )
    note = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name.Text,
            Rect=[10, 10, 20, 20],
            Contents=pikepdf.String("Hiddennote"),
            M=pikepdf.String("D:20200101"),
        )
    )
    attachment = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name.FileAttachment,
            Rect=[20, 20, 30, 30],
            FS=pikepdf.Dictionary(
                F=pikepdf.String("hiddenfile.txt"),
                EF=pikepdf.Dictionary(F=pdf.make_stream(b"secret file")),
            ),
        )
    )
    page.Annots = pikepdf.Array([link, note, attachment])
    page.PrivatePage = pdf.make_stream(b"Hiddenstream")
    page.Thumb = pdf.make_stream(b"thumb")
    return pdf


HIDDEN = (
    "hiddenauthor",
    "hiddenprivate",
    "hiddendest",
    "hiddenoutline",
    "hiddenuri",
    "hiddennote",
    "hiddenfile",
    "hiddenxmp",
)


def _tagged(pdf: pikepdf.Pdf) -> pikepdf.Pdf:
    page = pdf.pages[0].obj
    attachment = pdf.make_indirect(
        pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Stamp, Rect=[0, 0, 5, 5])
    )
    page.Annots = pikepdf.Array([attachment])
    paragraph = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.StructElem,
            S=pikepdf.Name("/ClientName"),
            Pg=page,
            K=pikepdf.Array(
                [
                    0,
                    pikepdf.Dictionary(Type=pikepdf.Name.OBJR, Obj=attachment),
                ]
            ),
            Alt=pikepdf.String("Hiddenalt"),
            ActualText=pikepdf.String("Hiddenactual"),
            A=pikepdf.Dictionary(O=pikepdf.Name.Layout, Placement=pikepdf.Name.Block),
            ID=pikepdf.String("Hiddenid"),
        )
    )
    pdf.Root.StructTreeRoot = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.StructTreeRoot,
            K=paragraph,
            RoleMap=pikepdf.Dictionary(ClientName=pikepdf.Name.P),
            IDTree=pikepdf.Dictionary(Names=[pikepdf.String("Hiddenid"), paragraph]),
        )
    )
    return pdf


def test_a_rewritten_string_keeps_its_shape_and_is_the_same_each_time():
    rewrite = Rewriter(1, [])
    new = rewrite("Jane Smith, 2018-05")
    assert re.fullmatch(r"[A-Z][a-z]{3} [A-Z][a-z]{4}, \d{4}-\d{2}", new)
    assert rewrite("Jane Smith, 2018-05") == new
    assert all(a != b for a, b in zip("Jane Smith", new[:10], strict=True) if a != " ")
    assert Rewriter(1, []).__call__("Jane Smith, 2018-05") == new


def test_a_rewritten_string_uses_no_word_of_the_source():
    # Each of these letters has one other vowel or consonant left to take, in a small alphabet.
    rewrite = Rewriter(2, ["aaa", "eee", "iii"])
    assert rewrite("ooo") not in ("aaa", "eee", "iii")


def test_a_clean_file_has_no_hidden_text_in_any_layer():
    pdf = _layered(_source())
    words = hidden_words(pdf)
    assert set(HIDDEN) <= words
    report = clean_layers(pdf, 0, words)
    assert leftover_layers(pdf) == {}
    assert residual_strings(pdf, words) == {}
    assert report.removed["document info"] == 1
    assert "/Metadata" not in pdf.Root and "/Names" not in pdf.Root
    assert "/PrivateKey" not in pdf.Root
    assert "/PrivatePage" not in pdf.pages[0].obj and "/Thumb" not in pdf.pages[0].obj


def test_nothing_of_a_hidden_layer_stays_in_the_saved_bytes():
    result = _run(_layered(_source()))
    assert result.data is not None, result.problems
    # Nothing can hide in a compressed stream: the file is written out in full to be searched.
    buffer = io.BytesIO()
    pikepdf.open(io.BytesIO(result.data)).save(
        buffer,
        qdf=True,
        object_stream_mode=pikepdf.ObjectStreamMode.disable,
        stream_decode_level=pikepdf.StreamDecodeLevel.all,
    )
    text = buffer.getvalue().lower()
    for word in HIDDEN:
        assert word.encode() not in text
    assert b"secret file" not in text
    assert b"sourceid" not in text


def test_a_named_destination_points_at_its_page_and_an_outline_keeps_it():
    pdf = _layered(_source())
    clean_layers(pdf, 0, hidden_words(pdf))
    item = pdf.Root.Outlines.First
    assert isinstance(item.Dest, pikepdf.Array)
    assert item.Dest[0].objgen == pdf.pages[0].obj.objgen
    assert "hidden" not in str(item.Title).lower()


def test_an_annotation_that_holds_a_file_goes_and_the_others_stay_without_values():
    pdf = _layered(_source())
    clean_layers(pdf, 0, hidden_words(pdf))
    annotations = pdf.pages[0].obj.Annots
    assert [str(a.Subtype) for a in annotations] == ["/Link", "/Text"]
    link, note = annotations
    assert "/A" not in link
    assert "/M" not in note
    assert "hidden" not in str(note.Contents).lower()


def test_an_appearance_with_text_goes_and_a_widget_asks_for_a_new_one():
    pdf = _source()
    appearance = pdf.make_stream(b"BT (Hiddenvalue) Tj ET")
    appearance.Subtype = pikepdf.Name.Form
    plain = pdf.make_stream(b"0 0 1 rg 0 0 5 5 re f")
    plain.Subtype = pikepdf.Name.Form
    field = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name.Widget,
            FT=pikepdf.Name.Tx,
            T=pikepdf.String("Hiddenname"),
            V=pikepdf.String("Hiddenvalue"),
            DA=pikepdf.String("0 0 Td /Helv 10 Tf"),
            Rect=[0, 0, 5, 5],
            AP=pikepdf.Dictionary(N=appearance),
        )
    )
    square = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name.Square,
            Rect=[0, 0, 5, 5],
            AP=pikepdf.Dictionary(N=plain),
        )
    )
    pdf.pages[0].obj.Annots = pikepdf.Array([field, square])
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=[field])
    words = hidden_words(pdf)
    assert {"hiddenname", "hiddenvalue"} <= words
    clean_layers(pdf, 0, words)
    assert "/AP" not in field
    assert "/AP" in square
    assert pdf.Root.AcroForm.NeedAppearances is True or bool(pdf.Root.AcroForm.NeedAppearances)
    assert "hidden" not in str(field.V).lower()
    assert len(str(field.V)) == len("Hiddenvalue")
    assert str(field.DA) == "0 0 Td /Helv 10 Tf"
    assert residual_strings(pdf, words) == {}


def test_what_a_person_chose_or_signed_is_cleared():
    pdf = _source()
    box = pdf.make_indirect(
        pikepdf.Dictionary(
            FT=pikepdf.Name.Btn,
            T=pikepdf.String("Choice"),
            V=pikepdf.Name.Yes,
            AS=pikepdf.Name.Yes,
        )
    )
    signed = pdf.make_indirect(
        pikepdf.Dictionary(
            FT=pikepdf.Name.Sig,
            T=pikepdf.String("Signature"),
            V=pikepdf.Dictionary(Name=pikepdf.String("Hiddensigner")),
        )
    )
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=[box, signed], SigFlags=3)
    clean_layers(pdf, 0, hidden_words(pdf))
    assert "/V" not in box and box.AS == "/Off"
    assert "/V" not in signed
    assert "/SigFlags" not in pdf.Root.AcroForm


def test_tags_keep_their_shape_and_lose_their_values():
    pdf = _tagged(_source())
    before = tag_shape(pdf)
    assert before == [("root", (), 1), ("/P", (0,), 0)]
    words = hidden_words(pdf)
    assert {"hiddenalt", "hiddenactual", "hiddenid"} <= words
    clean_layers(pdf, 0, words)
    assert tag_shape(pdf) == before
    tree = pdf.Root.StructTreeRoot
    assert "/RoleMap" not in tree
    element = tree.K
    assert element.S == "/P"
    assert "/ActualText" not in element
    assert "hidden" not in str(element.Alt).lower()
    # The /A of a tag holds its attributes, and is no action.
    assert element.A.O == "/Layout"
    # A tag lets go of an annotation that went, or the object would stay in the file.
    assert list(element.K) == [0]
    # A name tree is sorted, and its key follows the element's new ID.
    assert str(tree.IDTree.Names[0]) == str(element.ID)
    assert residual_strings(pdf, words) == {}


def test_a_role_with_no_map_becomes_one_that_says_nothing():
    pdf = _source()
    element = pikepdf.Dictionary(S=pikepdf.Name("/ClientName"))
    assert role(element, None) == "/NonStruct"
    assert role(pikepdf.Dictionary(S=pikepdf.Name.H1), None) == "/H1"
    mapped = pikepdf.Dictionary(ClientName=pikepdf.Name.Other, Other=pikepdf.Name.Span)
    assert role(element, pdf.make_indirect(mapped)) == "/Span"


def test_an_indexed_colour_table_and_the_strings_that_are_data_stay():
    pdf = _source()
    table = bytes(range(6))
    space = pikepdf.Array([pikepdf.Name.Indexed, pikepdf.Name.DeviceRGB, 1, pikepdf.String(table)])
    stream_table = pdf.make_stream(table)
    other = pikepdf.Array([pikepdf.Name.Indexed, pikepdf.Name.DeviceRGB, 1, stream_table])
    pdf.pages[0].obj.Resources.ColorSpace = pikepdf.Dictionary(C0=space, C1=other)
    pdf.Root.Lang = pikepdf.String("en-US")
    clean_layers(pdf, 0, [])
    kept = pdf.pages[0].obj.Resources.ColorSpace
    assert bytes(kept.C0[3]) == table
    assert kept.C1[3].read_bytes() == table
    assert str(pdf.Root.Lang) == "en-US"
    assert leftover_layers(pdf) == {}


def test_the_oracles_see_what_a_cleaner_left():
    pdf = _source()
    pdf.Root.Info = pikepdf.Dictionary()
    pdf.Root.Metadata = pdf.make_stream(b"x")
    pdf.pages[0].obj.Private = pdf.make_stream(b"x")
    pdf.pages[0].obj.Other = pikepdf.String("Hiddentext")
    left = leftover_layers(pdf)
    assert left["/Metadata"] == 1
    assert left["other streams"] == 1
    assert left["catalog entries"] == 2
    assert residual_strings(pdf, ["hiddentext"]) == {"/Other": ("hiddentext",)}


def test_a_form_with_text_that_no_page_draws_is_found():
    pdf = _source()
    form = pdf.make_stream(b"BT (x) Tj ET", Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form)
    pdf.make_indirect(form)
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Unused=form)
    assert leftover_layers(pdf)["forms with text that no page draws"] == 1


def test_text_in_marked_content_is_found_and_removed():
    extra = (
        b"/Span <</ActualText (Hiddenspan) /MCID 3>> BDC BT /F1 10 Tf 72 660 Td (abcd) Tj ET EMC "
    )
    pdf = _source(extra=extra)
    words = hidden_words(pdf)
    assert "hiddenspan" in words
    assert "marked content" in residual_strings(pdf, words)
    clean_layers(pdf, 0, words)
    assert residual_strings(pdf, words) == {}
    data = pdf.pages[0].obj.Contents.read_bytes()
    assert b"Hiddenspan" not in data
    assert b"/MCID 3" in data


def test_a_run_writes_nothing_when_a_word_cannot_change():
    # One lone letter at a held end, in a font that can draw nothing else and takes no substitute.
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (a) Tj ET", glyphs=b"")
    result = anonymize(_bytes(pdf), resolve_profile("privacy"), fonts=None)
    assert result.data is None
    assert any("did not change" in problem for problem in result.problems)


def test_a_page_with_a_font_that_cannot_be_traced_stops_the_run():
    pdf = _source()
    font = pdf.pages[0].obj.Resources.Font.F1
    del font["/Widths"]
    font.BaseFont = pikepdf.Name.Symbol
    result = _run(pdf)
    assert result.data is None
    assert any("cannot be traced" in problem for problem in result.problems)


def _curved(count: int) -> bytes:
    shape = b"0 0 m 1 1 2 2 3 3 c h f "
    return shape * count


def test_curved_shapes_are_counted_in_forms_too_and_not_when_stroked():
    pdf = _source(extra=_curved(3) + b"0 0 m 1 1 2 2 3 3 c S ")
    assert curved_fills(pdf.pages[0]) == 3
    form = pdf.make_stream(_curved(2), Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form)
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Fm=pdf.make_indirect(form))
    pdf.pages[0].obj.Contents = pdf.make_stream(b"/Fm Do " + _curved(1))
    assert curved_fills(pdf.pages[0]) == 3


def test_keeping_images_gives_up_the_claim():
    assert _privacy(keep_images=True).claims == ()
    assert not _privacy(keep_images=True).replace_images
    assert _privacy().claims == ("privacy",)


def _image(pdf, **keys):
    image = pdf.make_stream(
        b"\x00" * keys.pop("size", 4),
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Image,
        Width=2,
        Height=2,
        **keys,
    )
    return pdf.make_indirect(image)


def test_a_flat_image_is_light_in_every_colour_space_and_a_mask_is_empty():
    pdf = _source()
    gray = _image(pdf, ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8, size=4)
    cmyk = _image(pdf, ColorSpace=pikepdf.Name.DeviceCMYK, BitsPerComponent=8, size=16)
    inverted = _image(
        pdf,
        ColorSpace=pikepdf.Name.DeviceCMYK,
        BitsPerComponent=8,
        size=16,
        Decode=[1, 0, 1, 0, 1, 0, 1, 0],
    )
    table = pikepdf.String(bytes([0, 0, 0, 250, 250, 250]))
    indexed = _image(
        pdf,
        ColorSpace=pikepdf.Array([pikepdf.Name.Indexed, pikepdf.Name.DeviceRGB, 1, table]),
        BitsPerComponent=8,
        size=4,
    )
    mask = _image(pdf, ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8, size=4)
    masked = _image(pdf, ColorSpace=pikepdf.Name.DeviceRGB, BitsPerComponent=8, size=12, SMask=mask)
    stencil = _image(pdf, ImageMask=True, size=2)
    assert replace_all_images(pdf) == 7
    assert gray.read_bytes() == b"\xe6" * 4
    assert cmyk.read_bytes() == b"\x00" * 16
    assert inverted.read_bytes() == b"\x00" * 16 and "/Decode" not in inverted
    assert indexed.read_bytes() == b"\x01" * 4
    assert mask.read_bytes() == b"\x00" * 4
    assert masked.read_bytes() == b"\xe6" * 12
    assert stencil.read_bytes() == b"\xff" * 2


def test_an_inline_image_is_replaced_at_its_size():
    inline = b"q 9 0 0 9 0 0 cm BI /W 2 /H 2 /CS /CMYK /BPC 8 /F /AHx ID " + b"12" * 16 + b"> EI Q "
    pdf = _source(extra=inline)
    assert replace_all_images(pdf) == 1
    instruction = next(
        i for i in pikepdf.parse_content_stream(pdf.pages[0]) if str(i.operator) == "INLINE IMAGE"
    )
    image = instruction.iimage
    assert (int(image.obj.Width), int(image.obj.Height)) == (2, 2)
    written = pikepdf.unparse_content_stream([instruction])
    assert b"ID " + b"\x00" * 16 + b" EI" in written.replace(b"\n", b" ")


def test_a_full_run_passes_every_check_on_the_saved_bytes_and_says_so():
    pdf = _tagged(_layered(_source()))
    # An image, to be replaced; and a font program, to be replaced as well.
    image = _image(pdf, ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8)
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Im0=image)
    pdf.pages[0].obj.Contents = pdf.make_stream(
        pdf.pages[0].obj.Contents.read_bytes() + b" q 9 0 0 9 0 0 cm /Im0 Do Q"
    )
    result = _run(pdf)
    assert result.problems == ()
    assert {check.name for check in result.checks} >= {
        "geometry",
        "coverage",
        "residual-text",
        "hidden-text",
        "other-layers",
        "file-identifier",
        "structure",
        "image-geometry",
        "drawn-text",
        "machine-codes",
        "readable-text",
    }
    assert all(check.passed for check in result.checks)
    assert result.metrics["words_in_scope"] == result.metrics["words_changed"] > 0
    assert result.metrics["source_images_kept"] == 0
    output = pikepdf.open(io.BytesIO(result.data))
    assert "/Info" not in output.trailer
    assert bytes(output.trailer.ID[0]) != b"sourceid"
    plain = _source()
    source_lines = group_lines(trace_page(plain.pages[0]))
    output_lines = group_lines(trace_page(output.pages[0]))
    assert len(source_lines) == len(output_lines)
    assert source_lines[1].text != output_lines[1].text


def test_nothing_in_a_report_of_a_clean_file_quotes_the_source():
    result = _run(_layered(_source()))
    text = json.dumps(
        [
            [dict(check.details) for check in result.checks],
            [dict(mutation) for mutation in result.mutations],
            list(result.kept),
            list(result.replaced),
            dict(result.fingerprint),
        ]
    ).lower()
    for word in (*HIDDEN, "abcd", "bcda", "cdab", "dcba"):
        assert word not in text


def test_the_same_seed_gives_the_same_bytes():
    source = _bytes(_source())
    first = anonymize(source, _privacy(), fonts=_Boxes(), seed=3)
    second = anonymize(source, _privacy(), fonts=_Boxes(), seed=3)
    other = anonymize(source, _privacy(), fonts=_Boxes(), seed=4)
    assert first.data == second.data
    assert first.data != other.data


def test_the_training_profile_keeps_images_and_fonts_and_lets_a_word_stay():
    pdf = _source()
    image = _image(pdf, ColorSpace=pikepdf.Name.DeviceGray, BitsPerComponent=8)
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Im0=image)
    pdf.pages[0].obj.Contents = pdf.make_stream(
        pdf.pages[0].obj.Contents.read_bytes() + b" q 9 0 0 9 0 0 cm /Im0 Do Q"
    )
    profile = resolve_profile("training")
    assert profile.policy == FitPolicy("best-effort", "pinned")
    assert profile.claims == ()
    result = anonymize(_bytes(pdf), profile, vocabulary=["abcd"])
    assert result.data is not None, result.problems
    assert {check.name for check in result.checks}.isdisjoint({"coverage", "residual-text"})
    assert result.metrics["source_images_kept"] == 1


def test_the_privacy_profile_holds_what_its_claim_rests_on():
    with pytest.raises(ProfileError, match="coverage all"):
        resolve_profile("privacy", coverage="best-effort")
    with pytest.raises(ProfileError, match="word starts"):
        resolve_profile("privacy", word_starts="pinned")
    assert resolve_profile("training", coverage="all").requires_coverage
    assert resolve_profile("training", word_starts="moved").policy.word_starts == "moved"


def test_the_command_writes_no_file_when_it_stops(tmp_path: Path, capsys):
    source = tmp_path / "Jane Smith medical.pdf"
    source.write_bytes(_bytes(_pdf(b"BT /F1 10 Tf 72 700 Td (a) Tj ET", glyphs=b"")))
    out = tmp_path / "out.pdf"
    fonts = tmp_path / "no-fonts"
    fonts.mkdir()
    arguments = ["anonymize", str(source), "--out", str(out), "--profile", "privacy"]
    assert main([*arguments, "--font-dir", str(fonts)]) == 1
    assert not out.exists() and not manifest_path(out).exists()
    assert "did not change" in capsys.readouterr().err


def test_the_command_refuses_to_overwrite_its_source(tmp_path: Path):
    source = tmp_path / "a.pdf"
    source.write_bytes(_bytes(_source()))
    assert main(["anonymize", str(source), "--out", str(source), "--profile", "privacy"]) == 2


def _ran_command(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    # The name of the file can name a person, and the manifest travels with the output.
    source = tmp_path / "Jane Smith medical.pdf"
    source.write_bytes(_bytes(_layered(_source())))
    out = tmp_path / "out.pdf"
    monkeypatch.setattr("docmutant.substitute.FontSource.__init__", lambda self, *_: None)
    monkeypatch.setattr("docmutant.substitute.FontSource.pick", _Boxes.pick)
    arguments = [
        "docmutant",
        "anonymize",
        str(source),
        "--out",
        str(out),
        "--profile",
        "privacy",
        "--tolerance",
        "5",
    ]
    monkeypatch.setattr("sys.argv", arguments)
    assert main(arguments[1:]) == 0
    return out, manifest_path(out)


def test_the_command_writes_a_manifest_that_quotes_no_part_of_the_source(tmp_path, monkeypatch):
    out, sidecar = _ran_command(tmp_path, monkeypatch)
    assert out.exists()
    text = sidecar.read_text(encoding="utf-8")
    assert "Jane" not in text and "Smith" not in text
    for word in HIDDEN:
        assert word not in text.lower()
    assert json.loads(text)["generator"]["command"].startswith("docmutant anonymize '<source>'")


def test_a_privacy_manifest_is_a_draft_until_a_reviewer_attests(tmp_path, monkeypatch):
    _, sidecar = _ran_command(tmp_path, monkeypatch)
    with pytest.raises(ValidationError, match="attestation"):
        load_manifest(sidecar)
    assert main(["attest", str(sidecar), "--reviewer", "A. Reviewer"]) == 0
    manifest = load_manifest(sidecar)
    assert manifest.claims == ["privacy"]
    assert manifest.attestation.statement_version.startswith("privacy-")
    assert manifest.profile.settings["coverage"] == "all"
    assert main(["validate", "--check-file", str(sidecar)]) == 0


def _privacy_manifest(sidecar: Path) -> dict:
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["attestation"] = {
        "reviewer": "A. Reviewer",
        "date": "2026-10-31",
        "checklist_version": "privacy-0.1",
        "statement_version": "privacy-0.1-draft",
    }
    return data


def test_a_privacy_claim_needs_each_oracle_a_fingerprint_and_its_own_statement(
    tmp_path, monkeypatch
):
    _, sidecar = _ran_command(tmp_path, monkeypatch)
    data = _privacy_manifest(sidecar)
    Manifest.model_validate(data)

    no_oracle = data | {"oracles": [o for o in data["oracles"] if o["name"] != "drawn-text"]}
    with pytest.raises(ValidationError, match="drawn-text"):
        Manifest.model_validate(no_oracle)
    with pytest.raises(ValidationError, match="fingerprint"):
        Manifest.model_validate({k: v for k, v in data.items() if k != "fingerprint"})
    wrong = data["attestation"] | {"statement_version": "0.1-draft"}
    with pytest.raises(ValidationError, match="statement"):
        Manifest.model_validate(data | {"attestation": wrong})
    with pytest.raises(ValidationError, match="privacy profile"):
        Manifest.model_validate(
            data | {"profile": data["profile"] | {"name": "training"}, "fingerprint": None}
        )
    with pytest.raises(ValidationError, match="schema version"):
        Manifest.model_validate(data | {"schema_version": "0.1"})


def test_a_training_claim_is_refused_until_there_is_a_labels_oracle(tmp_path, monkeypatch):
    _, sidecar = _ran_command(tmp_path, monkeypatch)
    data = _privacy_manifest(sidecar)
    with pytest.raises(ValidationError, match="labels"):
        Manifest.model_validate(data | {"claims": ["privacy", "training"]})


def test_a_word_of_a_hidden_layer_is_no_replacement():
    pdf = _source()
    fitter = WordFitter(pdf, ["dddd"], _Boxes(), avoid=["dddd"])
    settings = BoxSettings("line", tolerance=within(5), tracking_limit=0.2)
    fitter.fit_page(0, settings, 0, FitPolicy("all", "moved"))
    assert "dddd" not in " ".join(line.text for line in group_lines(trace_page(pdf.pages[0])))


def test_a_page_retries_with_other_words_before_it_stops():
    # A word of the same width each way: whatever the first attempt drew, a later one can do.
    pdf = _source()
    fitter = WordFitter(pdf, [], _Boxes())
    settings = BoxSettings("line", tolerance=within(5), tracking_limit=0.02)
    report = fitter.fit_page(0, settings, 0, FitPolicy("all", "moved"))
    assert report.coverage.passed


def test_a_stopped_page_says_which_words(monkeypatch):
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (a) Tj ET", glyphs=b"")
    with pytest.raises(CoverageStop, match="did not change"):
        WordFitter(pdf, []).fit_page(0, BoxSettings("line"), 0, FitPolicy("all"))


def test_a_math_letter_written_twice_for_one_glyph_counts_once():
    pdf = pikepdf.Pdf.new()
    cmap = pdf.make_stream(b"2 beginbfchar <0001> <D835DC4CD835DC4C> <0002> <00660066> endbfchar")
    assert to_unicode(cmap) == {1: "\U0001d44c", 2: "ff"}


def test_a_math_letter_draws_as_its_plain_letter_and_a_missing_symbol_draws_nothing():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (ab,) Tj ET", glyphs=b"")
    font = pdf.pages[0].Resources.Font.F1
    fitter = WordFitter(pdf, [])
    shown = fitter.shown[font.objgen]
    a, b, comma = shown[97], shown[98], shown[44]
    codes = {
        97: type(a)(a.length, "\U0001d44e", a.width),
        98: type(b)(b.length, None, b.width),
        44: type(comma)(comma.length, "∀", comma.width),
    }
    embed_substitute(pdf, font, _Boxes().pick(font), codes)
    differences = list(font.Encoding.Differences)
    assert pikepdf.Name.a in differences
    # Both draw an empty glyph, and keep their advances.
    assert differences[differences.index(98) + 1] == "/space"
    assert differences[differences.index(44) + 1] == "/space"
    plain = _pdf(b"BT /F1 10 Tf 72 700 Td (ab,) Tj ET", glyphs=b"")
    assert group_lines(trace_page(pdf.pages[0]))[0].box.end == pytest.approx(
        group_lines(trace_page(plain.pages[0]))[0].box.end
    )


def test_a_letter_that_the_substitute_lacks_is_still_refused():
    pdf = _pdf(b"BT /F1 10 Tf 72 700 Td (a) Tj ET", glyphs=b"")
    font = pdf.pages[0].Resources.Font.F1
    shown = WordFitter(pdf, []).shown[font.objgen]
    entry = shown[97]
    with pytest.raises(CannotSubstitute, match="lacks"):
        embed_substitute(
            pdf, font, _Boxes().pick(font), {97: type(entry)(entry.length, "中", entry.width)}
        )
