"""Read the rendered pages: find source words that a person could still read in the output.

Every other oracle reads the file's operators and objects, so what they cannot see they pass.
A letter left as a path has no text operator, and it looks to them like any other drawing. This
oracle looks at the picture of the page instead, through OCR, and so does not share the blind
spots of the code that changed the page.

A word of the output is a match if it is a word of the source, anywhere in the document, unless it
begins or ends a new word that the run wrote there: a reader can split a long word in two. A source
word in the middle of a new one still counts. Or if most of it is part of a source word that was
read at the same place: what is left of a word in which only some letters changed. Chance
fragments are common in new words, but layout holds, so a fragment in the same place is not chance.

A match stops the run. No match is evidence, not proof: OCR misses small and stylized text, and
it cannot tell a logo from a drawing.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pypdfium2 as pdfium

SCALE = 3
"""Render at 216 dots per inch, at which body text reads well."""

SHORTEST = 4
"""Letters. On the sample a shorter match says little, and new words hit it by chance."""

MOSTLY = 0.8
"""How much of an output word must be part of a source word to count as what is left of it."""

NEAR = 12
"""Pixels at ``SCALE``: how far apart two words can be and still be at the same place."""

TOKEN = re.compile(r"[^\W_]+")


class CannotRead(Exception):
    """The reader is not there or failed. A check that did not look must not pass."""


@dataclass(frozen=True)
class ReadWord:
    text: str
    box: tuple[int, int, int, int]
    """Left, top, right and bottom, in pixels of the rendered page."""


@dataclass(frozen=True)
class Reader:
    name: str
    """The tool and its version, for the manifest."""

    read: Callable[[bytes], list[ReadWord]]
    """The words on a page, from a PNG."""


def _parse_tsv(data: str) -> list[ReadWord]:
    words: list[ReadWord] = []
    for row in csv.DictReader(io.StringIO(data), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if text:
            left, top = int(row["left"]), int(row["top"])
            words.append(
                ReadWord(text, (left, top, left + int(row["width"]), top + int(row["height"])))
            )
    return words


def tesseract() -> Reader:
    """Tesseract, on the path. Raises ``CannotRead`` if it is not."""
    path = shutil.which("tesseract")
    if path is None:
        raise CannotRead(
            "tesseract is not installed: a privacy run needs it to read the rendered pages "
            "(--keep-images or --keep-drawings give up the claim, and the check with it)"
        )
    done = subprocess.run([path, "--version"], capture_output=True, text=True, check=False)
    version = (done.stdout or done.stderr).splitlines()
    # One thread each, because pages run side by side. Sparse text: a page is not one column.
    command = [path, "stdin", "stdout", "--psm", "11", "--dpi", str(72 * SCALE), "tsv"]
    env = dict(os.environ, OMP_THREAD_LIMIT="1")

    def read(png: bytes) -> list[ReadWord]:
        result = subprocess.run(command, input=png, capture_output=True, env=env, check=False)
        if result.returncode != 0:
            raise CannotRead(f"tesseract failed: {result.stderr.decode('utf8', 'replace')[:200]}")
        return _parse_tsv(result.stdout.decode("utf8", "replace"))

    return Reader(version[0].strip() if version else "tesseract", read)


def default_reader() -> Reader:
    return tesseract()


def read_pages(data: bytes, reader: Reader) -> list[list[ReadWord]]:
    """The words that the reader sees on each page of a PDF."""
    document = pdfium.PdfDocument(data)
    images: list[bytes] = []
    for index in range(len(document)):
        buffer = io.BytesIO()
        document[index].render(scale=SCALE).to_pil().convert("L").save(buffer, "PNG")
        images.append(buffer.getvalue())
    with ThreadPoolExecutor(os.cpu_count() or 1) as pool:
        return list(pool.map(reader.read, images))


def tokens(words: Iterable[ReadWord]) -> list[tuple[str, tuple[int, int, int, int]]]:
    """The casefolded runs of letters or digits long enough to mean something, with their boxes."""
    return [
        (match.group().casefold(), word.box)
        for word in words
        for match in TOKEN.finditer(word.text)
        if len(match.group()) >= SHORTEST
    ]


def _same_place(first: Sequence[int], second: Sequence[int]) -> bool:
    return (
        first[0] < second[2] + NEAR
        and second[0] < first[2] + NEAR
        and first[1] < second[3] + NEAR
        and second[1] < first[3] + NEAR
    )


def longest_common(first: str, second: str) -> int:
    """The length of the longest run of ``first`` that is in ``second``."""
    best = 0
    for start in range(len(first)):
        for end in range(start + best + 1, len(first) + 1):
            if first[start:end] not in second:
                break
            best = end - start
    return best


def _split_from(token: str, written: str) -> bool:
    """Whether a reader could have read ``token`` by splitting one of these runs in two."""
    return any(
        run != token and (run.startswith(token) or run.endswith(token)) for run in written.split()
    )


def readable(
    source: Sequence[Sequence[ReadWord]],
    output: Sequence[Sequence[ReadWord]],
    known: Iterable[str] = (),
    written: Sequence[Sequence[tuple[str, tuple[float, float, float, float]]]] = (),
) -> dict[int, tuple[str, ...]]:
    """What can still be read of the source, on each page of the output, if anything.

    ``known`` holds source words that the file gave, which the reader need not have seen.
    ``written`` holds, for each page, the new words that the run wrote, as their casefolded runs
    of letters or digits with spaces between, and their boxes in pixels of the rendered page. None
    of them may be a word of the source.
    """
    forbidden = {word.casefold() for word in known if len(word) >= SHORTEST}
    forbidden.update(token for page in source for token, _ in tokens(page))
    found: dict[int, tuple[str, ...]] = {}
    for index, page in enumerate(output):
        here = tokens(source[index]) if index < len(source) else []
        new = written[index] if index < len(written) else ()
        hits = {
            token
            for token, box in tokens(page)
            if (
                token in forbidden
                and not any(
                    _split_from(token, text) and _same_place(box, place) for text, place in new
                )
            )
            or any(
                _same_place(box, place) and longest_common(token, word) >= MOSTLY * len(token)
                for word, place in here
            )
        }
        if hits:
            found[index] = tuple(sorted(hits))
    return found
