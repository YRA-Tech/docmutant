"""Write ``src/docmutant/standard_fonts.py`` from the Liberation fonts.

Liberation Sans, Serif and Mono have the advance widths of Helvetica, Times and Courier by design.
Run with the directory that holds them, for example ``/usr/share/fonts/truetype/liberation``.
"""

from __future__ import annotations

import contextlib
import sys
import unicodedata
from pathlib import Path

from fontTools.agl import toUnicode
from fontTools.encodings.StandardEncoding import StandardEncoding
from fontTools.ttLib import TTFont

FONTS = {
    "Helvetica": "LiberationSans-Regular",
    "Helvetica-Bold": "LiberationSans-Bold",
    "Times-Roman": "LiberationSerif-Regular",
    "Times-Bold": "LiberationSerif-Bold",
    "Times-Italic": "LiberationSerif-Italic",
    "Times-BoldItalic": "LiberationSerif-BoldItalic",
    "Courier": "LiberationMono-Regular",
}

ADOBE = {
    "Helvetica": {"¯": 333, "±": 584, "µ": 556, "·": 278, "÷": 584},
    "Helvetica-Bold": {"¯": 333, "±": 584, "µ": 611, "·": 278, "÷": 584},
    "Times-Roman": {"¯": 333, "±": 564, "µ": 500, "·": 250, "÷": 564},
    "Times-Bold": {"¯": 333, "±": 570, "µ": 556, "·": 250, "÷": 570},
    "Times-Italic": {"¯": 333, "±": 675, "µ": 500, "÷": 675},
    "Times-BoldItalic": {"¯": 333, "±": 570, "÷": 570},
}
"""Where Arial and Times New Roman, and so Liberation, differ from Adobe's fonts: Adobe's widths,
which pdfium's built-in fonts share."""

HEADER = '''"""Widths of the standard fonts, which a PDF may name without giving their widths.

Every reader knows the widths of Helvetica, Times and Courier, so a simple font with one of their
names may leave out ``/Widths``. These are their widths in thousandths of the font size, by
character. They come from the Liberation fonts, whose advance widths match these families by
design, and ``scripts/standard_widths.py`` writes this file again. Symbol and ZapfDingbats are not
here: a font of theirs with no widths stays unsupported.
"""

from __future__ import annotations

import re
'''

FOOTER = '''

def standard_widths(base_font: str) -> dict[str, float] | None:
    """The widths of the standard font that ``base_font`` names, or None if it names none."""
    name = re.sub(r"^[A-Z]{6}\\+", "", base_font).lower()
    if "courier" in name:
        return dict.fromkeys(CHARACTERS, 600.0)
    if "times" in name:
        bold, italic = "bold" in name, "italic" in name or "oblique" in name
        key = "Times-" + {(0, 0): "Roman", (1, 0): "Bold", (0, 1): "Italic", (1, 1): "BoldItalic"}[
            (bold, italic)
        ]
    elif "helvetica" in name or "arial" in name:
        # The oblique styles have the widths of the upright ones.
        key = "Helvetica-Bold" if "bold" in name else "Helvetica"
    else:
        return None
    return {char: float(width) for char, width in zip(CHARACTERS, WIDTHS[key], strict=True)}
'''


def characters() -> str:
    """Every character that WinAnsi, MacRoman or the standard encoding can show."""
    found = {toUnicode(name) for name in StandardEncoding if name != ".notdef"}
    for codec in ("cp1252", "mac_roman"):
        for code in range(32, 256):
            with contextlib.suppress(UnicodeDecodeError):
                found.add(bytes([code]).decode(codec))
    # Not a control character, nor MacRoman's Apple logo, in the private use area.
    return "".join(
        sorted(
            char
            for char in found
            if len(char) == 1 and char.isprintable() and unicodedata.category(char) != "Co"
        )
    )


def main(directory: Path, output: Path) -> None:
    chars = characters()
    widths: dict[str, list[int]] = {}
    for name, file in FONTS.items():
        font = TTFont(directory / f"{file}.ttf")
        cmap = font.getBestCmap()
        scale = 1000 / font["head"].unitsPerEm
        missing = [char for char in chars if ord(char) not in cmap]
        if missing:
            raise SystemExit(f"{file} has no glyph for {missing!r}")
        adobe = ADOBE.get(name, {})
        widths[name] = [
            adobe.get(char, round(font["hmtx"][cmap[ord(char)]][0] * scale)) for char in chars
        ]
    if set(widths.pop("Courier")) != {600}:
        raise SystemExit("Liberation Mono is not 600 wide throughout")
    lines = [HEADER, "CHARACTERS = ("]
    lines += [f"    {chars[start : start + 48]!r}" for start in range(0, len(chars), 48)]
    lines += [")", "", "_WIDTHS = {"]
    for name, values in widths.items():
        text = " ".join(map(str, values))
        pieces = []
        while text:
            cut = text.rfind(" ", 0, 80) + 1 if len(text) > 80 else len(text)
            pieces.append(text[:cut])
            text = text[cut:]
        lines.append(f"    {name!r}: (")
        lines += [f"        {piece!r}" for piece in pieces]
        lines.append("    ),")
    lines += [
        "}",
        "",
        "WIDTHS = {name: tuple(map(int, text.split())) for name, text in _WIDTHS.items()}",
    ]
    output.write_text("\n".join(lines) + FOOTER)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(__file__).parent.parent / "src/docmutant/standard_fonts.py")
