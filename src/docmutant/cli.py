"""Command-line interface."""

from __future__ import annotations

import argparse
import datetime
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from docmutant import __version__
from docmutant.manifest import (
    MANIFEST_SUFFIX,
    SCHEMA_VERSION,
    UNKNOWN_LICENSE,
    Manifest,
    file_sha256,
    load_manifest,
    manifest_path,
    write_manifest,
)

DEFAULT_TAKEDOWN = "https://github.com/YRA-Tech/docmutant/issues"


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


def _cmd_reduce(args: argparse.Namespace) -> int:
    from docmutant.evidence import measure
    from docmutant.oracles import OracleKind, Outcome, docling_parse_render_oracle
    from docmutant.reduce import OracleDidNotFire, Reducer

    kind = OracleKind(args.oracle)
    oracle = docling_parse_render_oracle(
        kind, args.target_python, scale=args.scale, timeout=args.timeout, label=args.target_label
    )
    silent = (
        docling_parse_render_oracle(
            kind,
            args.silent_python,
            scale=args.scale,
            timeout=args.timeout,
            label=args.silent_label,
        )
        if args.silent_python
        else None
    )

    print(f"reducing {args.source} page {args.page} with the {kind.value} oracle", file=sys.stderr)
    print(f"  target: {oracle.target}", file=sys.stderr)
    try:
        result = Reducer(oracle, seed=args.seed).run(args.source.read_bytes(), args.page)
    except OracleDidNotFire as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    args.out.write_bytes(result.data)
    oracles: list[dict[str, Any]] = []
    for check, expected in ((oracle, Outcome.FIRES), (silent, Outcome.SILENT)):
        if check is None:
            continue
        observed = check.check(args.out, 1).outcome
        on_source = check.check(args.source, args.page).outcome
        oracles.append(
            {
                "name": check.name,
                "target": check.target,
                "expected": expected.value,
                "observed": observed.value,
                "details": {"scale": args.scale, "on_source_page": on_source.value},
            }
        )

    measured = measure(args.source, args.page, args.out)
    summary = measured["summary"]
    kept = [f"{number} {kind_}" for kind_, number in sorted(summary.items())]
    replaced = [m["operator"] for m in result.mutations if m["operator"] != "keep-pages"]

    draft: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "file": args.out.name,
        "sha256": file_sha256(args.out),
        "tier": "derived",
        "license": args.license,
        "rights_holder": args.rights_holder,
        "source": {
            "sha256": file_sha256(args.source),
            "url": args.source_url,
            "license": args.source_license,
            "redistributable": args.redistributable,
            "pages": [args.page],
        },
        "fonts": [font | {"license": UNKNOWN_LICENSE} for font in measured["fonts"]],
        "mutations": result.mutations,
        "generator": {
            "tool": "docmutant",
            "version": __version__,
            "seed": args.seed,
            "command": _portable_command(sys.argv),
        },
        "oracles": oracles,
        "evidence": {"kept": kept, "replaced": replaced, "metrics": measured["metrics"]},
        "takedown_contact": args.takedown_contact,
    }
    if args.source_url is None:
        del draft["source"]["url"]
    sidecar = manifest_path(args.out)
    sidecar.write_text(json.dumps(draft, indent=2) + "\n", encoding="utf-8")

    print(
        f"wrote {args.out} ({len(result.data)} bytes) and {sidecar} "
        f"after {result.oracle_calls} oracle runs",
        file=sys.stderr,
    )
    try:
        Manifest.model_validate(draft)
        print("manifest is valid", file=sys.stderr)
    except ValidationError as error:
        print("manifest is a draft until these are resolved:", file=sys.stderr)
        for issue in error.errors():
            print(f"  {issue['msg']}", file=sys.stderr)
    return 0


def _portable_command(argv: list[str]) -> str:
    """The command line with file paths reduced to their names.

    Absolute paths would publish the local user name and folder layout.
    """
    parts = ["docmutant"]
    for arg in argv[1:]:
        path = Path(arg)
        parts.append(path.name if path.is_absolute() or path.exists() else arg)
    return shlex.join(parts)


def _cmd_attest(args: argparse.Namespace) -> int:
    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    data["attestation"] = {
        "reviewer": args.reviewer,
        "date": datetime.date.today().isoformat(),
        "checklist_version": args.checklist_version,
        "statement_version": args.statement_version,
    }
    if args.signature:
        data["attestation"]["signature"] = args.signature
    data.setdefault("takedown_contact", DEFAULT_TAKEDOWN)
    try:
        manifest = Manifest.model_validate(data)
    except ValidationError as error:
        print(f"{args.manifest}: still invalid", file=sys.stderr)
        for issue in error.errors():
            print(f"  {issue['msg']}", file=sys.stderr)
        return 1
    write_manifest(manifest, args.manifest)
    print(f"{args.manifest}: attested by {args.reviewer}")
    return 0


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

    reduce = commands.add_parser(
        "reduce", help="reduce and sanitize one page while a docling-parse oracle keeps firing"
    )
    reduce.add_argument("source", type=Path)
    reduce.add_argument("--page", type=int, required=True, help="1-based source page")
    reduce.add_argument("--out", type=Path, required=True)
    reduce.add_argument("--oracle", choices=["crash", "hang", "error"], required=True)
    reduce.add_argument(
        "--target-python",
        type=Path,
        required=True,
        help="Python of the environment whose docling-parse shows the bug",
    )
    reduce.add_argument(
        "--silent-python",
        type=Path,
        help="Python of an environment with the fix; the oracle must stay silent there",
    )
    reduce.add_argument("--target-label", help="how the manifest names the buggy target")
    reduce.add_argument("--silent-label", help="how the manifest names the fixed target")
    reduce.add_argument("--scale", type=float, default=2.0)
    reduce.add_argument("--timeout", type=float, default=60.0)
    reduce.add_argument("--seed", type=int, default=0)
    reduce.add_argument("--source-url")
    reduce.add_argument("--source-license", default="LicenseRef-all-rights-reserved")
    reduce.add_argument("--redistributable", action="store_true")
    reduce.add_argument("--license", default="CC0-1.0", help="license of the output file")
    reduce.add_argument("--rights-holder", default="YRA Tech")
    reduce.add_argument("--takedown-contact", default=DEFAULT_TAKEDOWN)
    reduce.set_defaults(func=_cmd_reduce)

    attest = commands.add_parser("attest", help="add a reviewer's attestation to a manifest")
    attest.add_argument("manifest", type=Path)
    attest.add_argument("--reviewer", required=True)
    attest.add_argument("--checklist-version", default="0.1")
    attest.add_argument("--statement-version", default="0.1-draft")
    attest.add_argument("--signature")
    attest.set_defaults(func=_cmd_attest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
