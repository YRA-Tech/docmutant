"""Command-line interface."""

from __future__ import annotations

import argparse
import datetime
import json
import math
import re
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pikepdf
from pydantic import ValidationError

from docmutant import __version__
from docmutant.geometry import CONTAIN, EXACT, BoxTolerance, within
from docmutant.manifest import (
    MANIFEST_SUFFIX,
    PRIVACY_STATEMENT,
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


def _tolerance(value: str) -> BoxTolerance:
    if value == "exact":
        return EXACT
    if value == "contain":
        return CONTAIN
    try:
        distance = float(value.lstrip("±"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not exact, contain or ±d") from None
    if not math.isfinite(distance) or distance < 0:
        raise argparse.ArgumentTypeError(f"{value!r} is not a distance")
    return within(distance)


def _read_vocabulary(path: Path) -> list[str]:
    words = path.read_text(encoding="utf-8", errors="ignore").split()
    return sorted({word.lower() for word in words if re.fullmatch(r"[A-Za-z]{2,}", word)})


PRESERVED = {
    "geometry": "geometry",
    "structure": "structure",
    "image geometry": "image-geometry",
    "what no step changes": "render-difference",
}
"""Each signal that a run under a profile keeps exact, and the oracle that checks it."""


def _manifest_draft(args: argparse.Namespace, profile: Any, result: Any) -> dict[str, Any]:
    changed = [
        {"signal": "text", "mutators": ["fit-words"]},
        {"signal": "hidden text", "mutators": ["clean-layers"]},
    ]
    if profile.replace_drawings:
        changed.append(
            {"signal": "drawn text", "mutators": ["text-from-outlines", "replace-machine-codes"]}
        )
    if profile.substitute_fonts:
        changed.append({"signal": "glyph shapes", "mutators": ["substitute-fonts"]})
    if profile.replace_images:
        changed.append({"signal": "image samples", "mutators": ["replace-image-samples"]})
    tolerance = profile.settings.tolerance
    draft: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "file": args.out.name,
        "sha256": file_sha256(args.out),
        "tier": "derived",
        "license": args.license,
        "rights_holder": args.rights_holder,
        "source": {
            "sha256": file_sha256(args.source),
            "license": args.source_license,
            "redistributable": args.redistributable,
            "pages": list(range(1, result.pages + 1)),
        },
        "fonts": [record | {"license": UNKNOWN_LICENSE} for record in result.fonts],
        "mutations": list(result.mutations),
        "generator": {
            "tool": "docmutant",
            "version": __version__,
            "seed": args.seed,
            "command": _portable_command(sys.argv, hide=[str(args.source)]),
        },
        "oracles": [
            {
                "name": check.name,
                "target": check.target,
                "expected": "silent",
                "observed": "silent" if check.passed else "fires",
                "details": dict(check.details),
            }
            for check in result.checks
        ],
        "evidence": {
            "kept": list(result.kept),
            "replaced": list(result.replaced),
            "metrics": dict(result.metrics),
        },
        "takedown_contact": args.takedown_contact,
        "claims": list(profile.claims),
        "profile": {
            "name": profile.name,
            "settings": {
                "scope": profile.settings.scope,
                "tolerance": [tolerance.out, tolerance.inward]
                if math.isfinite(tolerance.inward)
                else "contain",
                "tracking_limit": profile.settings.tracking_limit,
                "word_count": profile.settings.word_count,
                "coverage": profile.policy.coverage,
                "word_starts": profile.policy.word_starts,
            },
            "preserved": PRESERVED,
            "changed": changed,
        },
    }
    if args.source_url is not None:
        draft["source"]["url"] = args.source_url
    if profile.name == "privacy":
        draft["fingerprint"] = {"level": 0, "report": dict(result.fingerprint)}
    return draft


def _cmd_anonymize(args: argparse.Namespace) -> int:
    from docmutant.anonymize import ProfileError, anonymize, resolve_profile
    from docmutant.substitute import FontSource

    if args.source.resolve() == args.out.resolve():
        print("error: the output would overwrite the source", file=sys.stderr)
        return 2
    try:
        profile = resolve_profile(
            args.profile,
            tolerance=args.tolerance,
            coverage=args.coverage,
            word_starts=args.word_starts,
            keep_images=args.keep_images,
            keep_drawings=args.keep_drawings,
        )
    except ProfileError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    fonts = FontSource(args.font_dir) if args.font_dir else FontSource()
    vocabulary = _read_vocabulary(args.vocabulary) if args.vocabulary else []
    try:
        result = anonymize(
            args.source.read_bytes(), profile, vocabulary=vocabulary, fonts=fonts, seed=args.seed
        )
    except (pikepdf.PdfError, pikepdf.PasswordError, OSError) as error:
        print(f"error: {args.source}: {error}", file=sys.stderr)
        return 2

    if result.data is None:
        print(f"stopped: {args.source} gave no output, because:", file=sys.stderr)
        for problem in result.problems:
            print(f"  {problem}", file=sys.stderr)
        if args.out.exists():
            print(f"  {args.out} is not from this run", file=sys.stderr)
        return 1

    args.out.write_bytes(result.data)
    draft = _manifest_draft(args, profile, result)
    sidecar = manifest_path(args.out)
    sidecar.write_text(json.dumps(draft, indent=2) + "\n", encoding="utf-8")
    metrics = result.metrics
    print(
        f"wrote {args.out} and {sidecar}: {metrics['words_changed']:.0f} of "
        f"{metrics['words_in_scope']:.0f} words changed on {result.pages} pages",
        file=sys.stderr,
    )
    for item in result.kept:
        print(f"  kept: {item}", file=sys.stderr)
    try:
        Manifest.model_validate(draft)
        print("manifest is valid", file=sys.stderr)
    except ValidationError as error:
        print("manifest is a draft until these are resolved:", file=sys.stderr)
        for issue in error.errors():
            print(f"  {issue['msg']}", file=sys.stderr)
    return 0


def _portable_command(argv: list[str], hide: Sequence[str] = ()) -> str:
    """The command line with file paths reduced to their names.

    Absolute paths would publish the local user name and folder layout. The arguments in
    ``hide`` go altogether: a source's file name can name a person.
    """
    parts = ["docmutant"]
    for arg in argv[1:]:
        if arg in hide:
            parts.append("<source>")
            continue
        option, equals, value = arg.partition("=") if arg.startswith("--") else ("", "", arg)
        path = Path(value)
        if value and (path.is_absolute() or path.exists()):
            value = path.name
        parts.append(f"{option}{equals}{value}" if equals else value if not option else arg)
    return shlex.join(parts)


def _cmd_attest(args: argparse.Namespace) -> int:
    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    prefix = PRIVACY_STATEMENT if "privacy" in data.get("claims", []) else ""
    data["attestation"] = {
        "reviewer": args.reviewer,
        "date": datetime.date.today().isoformat(),
        "checklist_version": args.checklist_version or f"{prefix}0.1",
        "statement_version": args.statement_version or f"{prefix}0.1-draft",
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

    anonymize = commands.add_parser(
        "anonymize",
        help="change every layer of a document under a profile, and check the saved file",
    )
    anonymize.add_argument("source", type=Path)
    anonymize.add_argument("--out", type=Path, required=True)
    anonymize.add_argument("--profile", choices=["privacy", "training"], required=True)
    anonymize.add_argument(
        "--vocabulary",
        type=Path,
        help="file of real words to replace with; without one, every word is invented",
    )
    anonymize.add_argument(
        "--font-dir",
        type=Path,
        action="append",
        help="where to find substitute fonts; by default, the system's font directories",
    )
    anonymize.add_argument(
        "--tolerance",
        type=_tolerance,
        help="how far each edge of a line box may move: exact, contain or ±d in points",
    )
    anonymize.add_argument("--coverage", choices=["best-effort", "all"])
    anonymize.add_argument("--word-starts", choices=["pinned", "moved"])
    anonymize.add_argument(
        "--keep-images",
        action="store_true",
        help="keep image samples, which no oracle reads: the file then has no privacy claim",
    )
    anonymize.add_argument(
        "--keep-drawings",
        action="store_true",
        help="keep outlines, codes and graphics drawn as paths: the file then has no privacy claim",
    )
    anonymize.add_argument("--seed", type=int, default=0)
    anonymize.add_argument("--source-url")
    anonymize.add_argument("--source-license", default="LicenseRef-all-rights-reserved")
    anonymize.add_argument("--redistributable", action="store_true")
    anonymize.add_argument("--license", default="CC0-1.0", help="license of the output file")
    anonymize.add_argument("--rights-holder", default="YRA Tech")
    anonymize.add_argument("--takedown-contact", default=DEFAULT_TAKEDOWN)
    anonymize.set_defaults(func=_cmd_anonymize)

    attest = commands.add_parser("attest", help="add a reviewer's attestation to a manifest")
    attest.add_argument("manifest", type=Path)
    attest.add_argument("--reviewer", required=True)
    attest.add_argument("--checklist-version", help="by default, the latest for the file's claim")
    attest.add_argument("--statement-version", help="by default, the latest for the file's claim")
    attest.add_argument("--signature")
    attest.set_defaults(func=_cmd_attest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
