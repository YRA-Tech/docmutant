"""Most tests are about what a run changes, not what OCR sees, and OCR is slow and may be absent."""

from __future__ import annotations

import itertools

import pytest

from docmutant import reading


def _no_two_alike() -> reading.Reader:
    """A reader that sees text, and never the same word twice, so that it finds no match."""
    counter = itertools.count()

    def read(_png: bytes) -> list[reading.ReadWord]:
        # Each at its own place too: two of them differ by little, which is a match at one place.
        number = next(counter)
        return [reading.ReadWord(f"stub{number}", (1000 * number, 0, 1000 * number + 10, 10))]

    return reading.Reader("stub reader", read)


@pytest.fixture(autouse=True)
def _stub_reader(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if "real_reader" not in request.keywords:
        monkeypatch.setattr(reading, "default_reader", _no_two_alike)
