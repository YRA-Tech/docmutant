"""Write ``src/docmutant/data/vocabulary.txt`` from SCOWL, without offensive words.

Usage: vocabulary.py SCOWL_FINAL_DIRECTORY BLOCKLIST

The words are SCOWL's American and English words up to level 35, the common words of a small
dictionary (http://wordlist.aspell.net/, 2020.12.07). Only lowercase entries of 2 to 15 letters
stay, so no proper name, abbreviation, contraction or possessive does. A word of the blocklist, or
one of its simple inflections, goes. The blocklist used is the English list of LDNOOBW
(https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words, CC BY 4.0),
which this repository does not hold.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LISTS = ("american-words", "english-words")
LEVELS = (10, 20, 35)
SUFFIXES = ("", "s", "es", "ed", "er", "ers", "ing", "ings", "y", "ies", "ier", "iest", "ish")
"""What a blocked word can take and still be the same word. It also blocks a few harmless ones,
such as butter: a few dozen of some 39,000."""

EXCLUDED = frozenset({"cs", "ms", "ho"})
"""Letters in the plural, and a slur that the blocklist leaves out."""

OUTPUT = Path(__file__).parent.parent / "src/docmutant/data/vocabulary.txt"


def words(directory: Path) -> set[str]:
    found: set[str] = set()
    for name in LISTS:
        for level in LEVELS:
            text = (directory / f"{name}.{level}").read_text(encoding="latin-1")
            found.update(word for word in text.split() if re.fullmatch(r"[a-z]{2,15}", word))
    return found


def blocked(path: Path) -> set[str]:
    entries = (line.strip().lower() for line in path.read_text(encoding="utf-8").splitlines())
    roots = {entry for entry in entries if re.fullmatch(r"[a-z]+", entry)}
    return {root + suffix for root in roots for suffix in SUFFIXES} | EXCLUDED


def main(directory: Path, blocklist: Path) -> None:
    kept = sorted(words(directory) - blocked(blocklist))
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text("\n".join(kept) + "\n", encoding="ascii")
    print(f"{len(kept)} words to {OUTPUT}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
