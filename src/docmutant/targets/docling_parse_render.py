"""Render one page with docling-parse. Standalone: run it with the target's Python.

The script imports only docling-parse, so it runs in the environment under test
without docmutant installed there. A crash shows as a negative exit status, a
hang as a timeout in the caller.
"""

import argparse
from pathlib import Path

from docling_parse.pdf_parser import (
    DecodeConfig,
    DoclingThreadedPdfParser,
    RenderConfig,
    ThreadedPdfParserConfig,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--scale", type=float, default=2.0)
    args = parser.parse_args()

    render_config = RenderConfig()
    render_config.scale = args.scale
    pdf_parser = DoclingThreadedPdfParser(
        parser_config=ThreadedPdfParserConfig(
            loglevel="fatal",
            threads=1,
            max_concurrent_results=4,
            render_config=render_config,
        ),
        decode_config=DecodeConfig(
            do_sanitization=True, keep_glyphs=True, keep_qpdf_warnings=False
        ),
    )
    key = pdf_parser.load(str(args.pdf), page_numbers=[args.page])
    try:
        for result in pdf_parser.iterate_results():
            if result.doc_key == key and result.page_number == args.page:
                if not result.success:
                    print(result.error_message)
                    return 3
                width, height = result.get_image().size
                print(f"{width} {height}")
                return 0
    finally:
        pdf_parser.unload_all()
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
