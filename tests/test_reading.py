"""The oracle that reads the rendered page, and the codes that no reader of words can see."""

from __future__ import annotations

import io
import random
import shutil

import pikepdf
import pytest
from test_anonymize import _bytes, _privacy, _source
from test_coverage import _Boxes

from docmutant import outlines, reading
from docmutant.anonymize import anonymize, resolve_profile
from docmutant.codes import machine_codes, replace_machine_codes
from docmutant.manifest import PRIVACY_ORACLES
from docmutant.outlines import _Font
from docmutant.reading import CannotRead, Reader, ReadWord, readable, tokens
from docmutant.substitute import FontSource

real_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs tesseract")


def _at(text: str, left: int = 0, top: int = 0, width: int = 60) -> ReadWord:
    return ReadWord(text, (left, top, left + width, top + 20))


def test_a_source_word_anywhere_in_the_document_is_a_match():
    assert readable([[_at("Harvard", 0, 0)], [_at("other")]], [[], [_at("HARVARD", 500, 500)]]) == {
        1: ("harvard",)
    }


def test_what_is_left_of_a_word_is_a_match_at_the_same_place():
    # Part of a word, as when some of its letters were left as outlines.
    assert readable([[_at("Harvard")]], [[_at("RVARD")]]) == {0: ("rvard",)}


def test_a_fragment_elsewhere_is_chance():
    assert readable([[_at("Harvard")]], [[_at("rvard", 400, 400)]]) == {}


def test_a_new_word_that_shares_a_little_with_the_source_word_is_no_match():
    assert readable([[_at("international")]], [[_at("ternbolo")]]) == {}


def test_a_word_that_the_file_gave_counts_though_the_reader_missed_it():
    assert readable([[]], [[_at("Lamport")]], ["lamport"]) == {0: ("lamport",)}


def test_short_words_are_not_read_as_matches():
    assert readable([[_at("the")]], [[_at("the")]], ["the"]) == {}


def test_digits_count_as_a_word():
    assert readable([[_at("20261101")]], [[_at("20261101", 300, 300)]]) == {0: ("20261101",)}


def test_a_word_is_read_without_its_punctuation():
    assert [token for token, _ in tokens([_at("(Lamport),")])] == ["lamport"]


def test_tesseract_output_parses_and_empty_cells_are_dropped():
    data = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t10\t10\t-1\t\n"
        '5\t1\t1\t1\t1\t1\t7\t8\t20\t30\t96\t"Hello\n'
    )
    assert reading._parse_tsv(data) == [ReadWord('"Hello', (7, 8, 27, 38))]


def _reader_seeing(*pages: list[ReadWord]) -> Reader:
    calls = iter(pages)
    return Reader("scripted", lambda _png: next(calls))


def test_a_match_stops_the_run_and_its_words_stay_out_of_the_check():
    # The file is fine, as far as the operators can tell: only the picture shows the word.
    reader = _reader_seeing([_at("Lamport")], [_at("Lamport")])
    result = anonymize(_bytes(_source()), _privacy(), fonts=_Boxes(), reader=reader)
    assert result.data is None
    assert any("can still be read: lamport" in problem for problem in result.problems)
    check = next(c for c in result.checks if c.name == "readable-text")
    assert not check.passed
    assert check.details == {"source_words": 1, "output_words": 1, "matches": 1}
    assert check.target == "scripted"
    assert "lamport" not in str(result.checks).casefold()


def test_a_clean_page_passes_and_names_its_reader():
    reader = _reader_seeing([_at("Lamport")], [_at("Xiwoqu")])
    result = anonymize(_bytes(_source()), _privacy(), fonts=_Boxes(), reader=reader)
    assert result.data is not None, result.problems
    check = next(c for c in result.checks if c.name == "readable-text")
    assert check.passed
    assert check.target == "scripted"


def test_what_the_reader_saw_in_the_source_is_kept_out_of_the_new_words(monkeypatch):
    # On an outlined page the fitter has no source words, and new ones could match by chance.
    seen: list[set[str]] = []
    original = outlines._Words.__init__

    def spy(self, seed, vocabulary, avoid):
        original(self, seed, vocabulary, avoid)
        seen.append(self.avoid)

    monkeypatch.setattr(outlines._Words, "__init__", spy)
    reader = _reader_seeing([_at("Seenonly")], [_at("Xiwoqu")])
    anonymize(_bytes(_source()), _privacy(), fonts=_Boxes(), reader=reader)
    assert "seenonly" in seen[0]


def test_a_reader_that_saw_nothing_where_there_is_text_has_not_looked():
    many = b"BT /F1 10 Tf 72 400 Td (" + b"abcd " * 25 + b") Tj ET "
    reader = _reader_seeing([], [])
    result = anonymize(_bytes(_source(extra=many)), _privacy(), fonts=_Boxes(), reader=reader)
    assert result.data is None
    assert any("has not looked" in problem for problem in result.problems)


def test_a_blank_output_page_is_not_a_clean_one():
    many = b"BT /F1 10 Tf 72 400 Td (" + b"abcd " * 25 + b") Tj ET "
    reader = _reader_seeing([_at("Lamport")], [])
    result = anonymize(_bytes(_source(extra=many)), _privacy(), fonts=_Boxes(), reader=reader)
    assert result.data is None
    assert any("has not looked" in problem for problem in result.problems)


def test_no_reader_stops_a_privacy_run_rather_than_passing_it(monkeypatch):
    def missing() -> Reader:
        raise CannotRead("tesseract is not installed")

    monkeypatch.setattr(reading, "default_reader", missing)
    result = anonymize(_bytes(_source()), _privacy(), fonts=_Boxes())
    assert result.data is None
    assert any("not installed" in problem for problem in result.problems)


@pytest.mark.parametrize(
    "profile",
    [resolve_profile("training"), resolve_profile("privacy", keep_images=True)],
    ids=["training", "no claim"],
)
def test_a_run_without_the_claim_needs_no_reader(monkeypatch, profile):
    def missing() -> Reader:
        raise CannotRead("tesseract is not installed")

    monkeypatch.setattr(reading, "default_reader", missing)
    assert not profile.reads_render
    result = anonymize(_bytes(_source()), profile, fonts=_Boxes())
    assert "readable-text" not in {check.name for check in result.checks}


def test_a_privacy_claim_rests_on_both_new_oracles():
    assert {"readable-text", "machine-codes"} <= PRIVACY_ORACLES


def _words_page(text: str) -> bytes | None:
    """A page that shows this text large, in a real font, or None if no such font is here."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    try:
        font = _Font(pdf, FontSource())
    except Exception:
        return None
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font.font))
    data, _ = font.encode(text)
    page.obj.Contents = pdf.make_stream(
        b"BT /F1 36 Tf 72 600 Td " + pikepdf.String(data).unparse() + b" Tj ET"
    )
    return _bytes(pdf)


@real_tesseract
@pytest.mark.real_reader
def test_tesseract_reads_a_word_that_stayed():
    source = _words_page("Confidential record")
    if source is None:
        pytest.skip("needs an open font")
    reader = reading.tesseract()
    assert "tesseract" in reader.name
    words = reading.read_pages(source, reader)
    assert {token for token, _ in tokens(words[0])} >= {"confidential", "record"}
    assert readable(words, reading.read_pages(source, reader)) == {0: ("confidential", "record")}


@real_tesseract
@pytest.mark.real_reader
def test_tesseract_finds_what_is_left_of_a_word():
    # Two letters went, and the rest stayed where it was: what stopped no other oracle.
    source = _words_page("Hanover")
    left = _words_page("nover")
    if source is None or left is None:
        pytest.skip("needs an open font")
    reader = reading.tesseract()
    assert readable(reading.read_pages(source, reader), reading.read_pages(left, reader)) == {
        0: ("nover",)
    }


@real_tesseract
@pytest.mark.real_reader
def test_tesseract_finds_no_match_in_new_words():
    source = _words_page("Confidential record")
    other = _words_page("Bimorelu quvet")
    if source is None or other is None:
        pytest.skip("needs an open font")
    reader = reading.tesseract()
    assert readable(reading.read_pages(source, reader), reading.read_pages(other, reader)) == {}


def _qr(
    size: int = 25, module: float = 1.0, x: float = 100, y: float = 100, seed: int = 0
) -> bytes:
    """A pattern with the look of a QR code: half of its modules dark, in columns."""
    rng = random.Random(seed)
    chunks = [b"0 0 0 rg\n"]
    for column in range(size):
        row = 0
        while row < size:
            if rng.random() < 0.5:
                run = rng.randint(1, 3)
                height = min(run, size - row)
                chunks.append(
                    b"%.2f %.2f %.2f %.2f re f\n"
                    % (x + column * module, y + row * module, module, height * module)
                )
                row += height + 1
            else:
                row += 1
    return b"".join(chunks)


def _barcode(x: float = 100, y: float = 100, sideways: bool = False, seed: int = 0) -> bytes:
    rng = random.Random(seed)
    chunks = [b"0 0 0 rg\n"]
    position = x
    for _ in range(30):
        width = rng.randint(1, 4)
        if sideways:
            chunks.append(b"%.2f %.2f 40 %.2f re f\n" % (y, position, width))
        else:
            chunks.append(b"%.2f %.2f %.2f 40 re f\n" % (position, y, width))
        position += width + rng.randint(1, 4)
    return b"".join(chunks)


def _page(content: bytes) -> pikepdf.Pdf:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.obj.Resources = pikepdf.Dictionary()
    page.obj.Contents = pdf.make_stream(content)
    return pdf


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(_qr(), id="a matrix"),
        pytest.param(_qr(module=0.5), id="a small matrix"),
        pytest.param(_qr(module=4), id="a large matrix"),
        pytest.param(_barcode(), id="a barcode"),
        pytest.param(_barcode(sideways=True), id="a barcode written upward"),
        pytest.param(b"q 2 0 0 2 50 50 cm " + _qr() + b"Q\n", id="under a transform"),
        pytest.param(b"q 0 1 -1 0 300 50 cm " + _qr() + b"Q\n", id="turned"),
    ],
)
def test_a_code_is_found_and_becomes_one_flat_block(content):
    pdf = _page(content)
    assert machine_codes(pdf) == {"page 1": 1}
    assert replace_machine_codes(pdf) == 1
    assert machine_codes(pdf) == {}
    stream = pikepdf.parse_content_stream(pdf.pages[0])
    assert [str(i.operator) for i in stream].count("re") == 1


def test_a_code_in_one_fill_of_many_boxes_is_found():
    boxes = _qr().replace(b" re f\n", b" re\n")
    pdf = _page(boxes + b"f\n")
    assert machine_codes(pdf) == {"page 1": 1}
    assert replace_machine_codes(pdf) == 1


def test_a_code_of_drawn_squares_is_found():
    rng = random.Random(0)
    squares = b"".join(
        b"%d %d m %d %d l %d %d l %d %d l h f\n"
        % (100 + c, 100 + r, 101 + c, 100 + r, 101 + c, 101 + r, 100 + c, 101 + r)
        for c in range(25)
        for r in range(25)
        if rng.random() < 0.5
    )
    assert machine_codes(_page(squares)) == {"page 1": 1}


def test_the_block_covers_the_codes_whole_extent_where_it_was():
    pdf = _page(b"q 2 0 0 2 50 50 cm " + _qr(size=25) + b"Q\n")
    replace_machine_codes(pdf)
    data = pdf.pages[0].obj.Contents.read_bytes().decode()
    assert " re" in data
    assert pikepdf.parse_content_stream(pdf.pages[0])
    assert machine_codes(pdf) == {}


def test_a_code_in_a_form_is_found_with_the_forms_scale():
    pdf = _page(b"q 0.1 0 0 0.1 100 100 cm /Fm Do Q\n")
    form = pdf.make_stream(
        _qr(module=10, x=0, y=0),
        Type=pikepdf.Name.XObject,
        Subtype=pikepdf.Name.Form,
        BBox=[0, 0, 300, 300],
    )
    pdf.pages[0].obj.Resources.XObject = pikepdf.Dictionary(Fm=pdf.make_indirect(form))
    assert machine_codes(pdf) == {f"form {form.objgen[0]} {form.objgen[1]}": 1}
    assert replace_machine_codes(pdf) == 1
    assert machine_codes(pdf) == {}


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(
            b"0.9 g\n" + b"".join(b"100 %d 400 10 re f\n" % (100 + 20 * row) for row in range(20)),
            id="shaded table rows",
        ),
        pytest.param(
            b"".join(b"%d 100 8 %d re f\n" % (100 + 12 * i, 20 + 7 * i) for i in range(20)),
            id="the bars of a chart",
        ),
        pytest.param(
            b"".join(b"%d 100 1 300 re f\n" % (100 + 40 * i) for i in range(12))
            + b"".join(b"100 %d 440 1 re f\n" % (100 + 20 * i) for i in range(16)),
            id="the rules of a table",
        ),
        pytest.param(
            b"".join(
                b"%d %d 1 1 re f\n" % (100 + 10 * c, 100 + 10 * r)
                for c in range(10)
                for r in range(10)
            ),
            id="a sparse grid of dots",
        ),
        pytest.param(b"100 100 40 40 re f\n" * 30, id="one box many times"),
        pytest.param(
            b"".join(
                b"%d %d 2 2 re f\n" % (100 + 2 * c, 100 + 2 * r)
                for c in range(20)
                for r in range(20)
            ),
            id="a solid block of cells",
        ),
        pytest.param(
            b"".join(b"%d 100 2 2 re f\n" % (100 + 4 * i) for i in range(20)),
            id="a dotted line",
        ),
        pytest.param(
            b"".join(b"%d 100 %d 40 re f\n" % (100 + 40 * i, 10 + 3 * i) for i in range(20)),
            id="wide columns of other widths",
        ),
        pytest.param(
            b"q 1 0.2 0 1 0 0 cm " + _qr() + b"Q\n",
            id="a skewed pattern, which is not read as a code",
        ),
    ],
)
def test_what_only_resembles_a_code_stays(content):
    pdf = _page(content)
    assert machine_codes(pdf) == {}
    assert replace_machine_codes(pdf) == 0


def test_a_heat_map_of_many_colours_is_not_a_code():
    lines = _qr(size=25).replace(b"0 0 0 rg\n", b"").splitlines()
    colours = (b"1 0 0", b"0 1 0", b"0 0 1", b"1 1 0", b"0 1 1", b"1 0 1", b"0.5 0 0", b"0 0.5 0")
    mixed = b"".join(
        colours[index % len(colours)] + b" rg\n" + line + b"\n" for index, line in enumerate(lines)
    )
    assert machine_codes(_page(mixed)) == {}


def test_a_run_replaces_a_code_and_says_so():
    pdf = _source(extra=_qr(x=300, y=300))
    result = anonymize(_bytes(pdf), _privacy(), fonts=_Boxes())
    assert result.data is not None, result.problems
    assert machine_codes(pikepdf.open(io.BytesIO(result.data))) == {}
    check = next(c for c in result.checks if c.name == "machine-codes")
    assert check.passed
    assert check.details == {"codes_replaced": 1, "codes_left": 0}
    assert {"operator": "replace-machine-codes", "params": {"codes": 1}} in result.mutations
    assert any("machine-readable codes" in item for item in result.replaced)


def test_keeping_drawings_keeps_a_code_and_training_does_too():
    for profile in (_privacy(keep_drawings=True), resolve_profile("training")):
        result = anonymize(_bytes(_source(extra=_qr(x=300, y=300))), profile, fonts=_Boxes())
        assert result.data is not None, result.problems
        assert machine_codes(pikepdf.open(io.BytesIO(result.data))) == {"page 1": 1}
