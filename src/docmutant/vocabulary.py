"""The vocabulary that a run takes its real words from, unless the caller gives another.

Common English words from SCOWL, without offensive ones: ``data/vocabulary-copyright.txt`` says
where they come from, and ``scripts/vocabulary.py`` makes the list.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from importlib.resources import files


def default_vocabulary() -> list[str]:
    return (files("docmutant") / "data" / "vocabulary.txt").read_text(encoding="ascii").split()


def fingerprint(vocabulary: Iterable[str]) -> dict[str, object]:
    """How many words, and a hash that names the list without showing it."""
    words = sorted(set(vocabulary))
    digest = hashlib.sha256("\n".join(words).encode("utf-8")).hexdigest()
    return {"words": len(words), "sha256": digest}
