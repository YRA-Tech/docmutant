"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from docmutant import __version__
from docmutant.manifest import MANIFEST_SUFFIX, Manifest, file_sha256, load_manifest


def _cmd_schema(_args: argparse.Namespace) -> int:
    print(json.dumps(Manifest.model_json_schema(), indent=2))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    failures = 0
    for path in args.manifests:
        try:
            manifest = load_manifest(path)
        except ValidationError as error:
            print(f"{path}: invalid", file=sys.stderr)
            for issue in error.errors():
                print(f"  {issue['msg']}", file=sys.stderr)
            failures += 1
            continue

        if args.check_file:
            pdf_path = path.with_name(manifest.file)
            if not path.name.endswith(MANIFEST_SUFFIX):
                print(f"{path}: name does not end with {MANIFEST_SUFFIX}", file=sys.stderr)
                failures += 1
                continue
            if not pdf_path.is_file():
                print(f"{path}: {manifest.file} is missing", file=sys.stderr)
                failures += 1
                continue
            if file_sha256(pdf_path) != manifest.sha256:
                print(f"{path}: {manifest.file} does not match its sha256", file=sys.stderr)
                failures += 1
                continue

        print(f"{path}: ok")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="docmutant",
        description="Structure-aware PDF mutation with oracles and provenance manifests.",
    )
    parser.add_argument("--version", action="version", version=f"docmutant {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    schema = commands.add_parser("schema", help="print the manifest JSON Schema")
    schema.set_defaults(func=_cmd_schema)

    validate = commands.add_parser("validate", help="validate manifest files")
    validate.add_argument("manifests", nargs="+", type=Path)
    validate.add_argument(
        "--check-file",
        action="store_true",
        help="also check that the described PDF exists next to the manifest and matches",
    )
    validate.set_defaults(func=_cmd_validate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
