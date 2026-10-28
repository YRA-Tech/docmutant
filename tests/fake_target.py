"""A stand-in for software under test: it aborts when the PDF has a chosen feature.

Usage: python fake_target.py PDF PAGE FEATURE
FEATURE is one of:
  coordinate  - a content operand equal to 123.456 (a path trigger)
  text        - any text object
  image       - any image XObject drawn by the page
"""

import os
import sys
from decimal import Decimal

import pikepdf


def operands(pdf: pikepdf.Pdf, page: pikepdf.Page):
    streams = [page]
    xobjects = page.obj.get("/Resources", {}).get("/XObject", {})
    streams += [x for _, x in xobjects.items() if x.get("/Subtype") == "/Form"]
    for stream in streams:
        yield from pikepdf.parse_content_stream(stream)


def main() -> None:
    path, page_no, feature = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    with pikepdf.open(path) as pdf:
        page = pdf.pages[page_no - 1]
        for instruction in operands(pdf, page):
            if isinstance(instruction, pikepdf.ContentStreamInlineImage):
                continue
            op = str(instruction.operator)
            if feature == "coordinate" and Decimal("123.456") in [
                Decimal(str(o)) for o in instruction.operands if isinstance(o, (int, Decimal))
            ]:
                os.abort()
            if feature == "text" and op == "BT":
                os.abort()
            if feature == "image" and op == "Do":
                xobject = page.obj.Resources.XObject[instruction.operands[0]]
                if xobject.get("/Subtype") == "/Image":
                    os.abort()


if __name__ == "__main__":
    main()
