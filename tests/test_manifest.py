"""The manifest rejects files that break the corpus policy in CORPUS-LICENSES.md."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from docmutant.cli import main
from docmutant.manifest import (
    ALL_RIGHTS_RESERVED,
    Manifest,
    file_sha256,
    manifest_path,
    write_manifest,
)

SOURCE_SHA = "a" * 64
OUTPUT_SHA = "b" * 64


def _restricted_derived(**overrides: Any) -> dict[str, Any]:
    """A file derived from an all-rights-reserved source, with everything the policy needs."""
    data: dict[str, Any] = {
        "file": "honda-p7.pdf",
        "sha256": OUTPUT_SHA,
        "tier": "derived",
        "license": "CC0-1.0",
        "rights_holder": "YRA Tech",
        "source": {
            "sha256": SOURCE_SHA,
            "url": "https://example.org/manual.pdf",
            "license": ALL_RIGHTS_RESERVED,
            "redistributable": False,
            "pages": [7],
        },
        "fonts": [
            {"name": "ABCDEF+Helvetica", "license": ALL_RIGHTS_RESERVED, "action": "replaced"}
        ],
        "mutations": [{"operator": "keep-pages", "params": {"pages": [7]}}],
        "generator": {"version": "0.0.1", "seed": 7},
        "oracles": [
            {
                "name": "crash",
                "target": "docling-parse 7.24.0",
                "expected": "fires",
                "observed": "fires",
            }
        ],
        "evidence": {"kept": ["1 image placement"], "replaced": ["text", "image samples"]},
        "attestation": {
            "reviewer": "A. Reviewer",
            "date": datetime.date(2026, 10, 28),
            "checklist_version": "0.1",
            "statement_version": "0.1-draft",
        },
        "takedown_contact": "https://github.com/YRA-Tech/docmutant/issues",
    }
    data.update(overrides)
    return data


def test_restricted_derived_file_with_full_record_is_accepted() -> None:
    Manifest.model_validate(_restricted_derived())


@pytest.mark.parametrize("missing", ["attestation", "evidence", "takedown_contact"])
def test_restricted_derived_file_needs_review_record(missing: str) -> None:
    data = _restricted_derived()
    del data[missing]
    with pytest.raises(ValidationError, match=missing):
        Manifest.model_validate(data)


def test_restricted_derived_file_may_not_keep_an_unfree_font_program() -> None:
    fonts = [{"name": "ABCDEF+Helvetica", "license": ALL_RIGHTS_RESERVED, "action": "kept"}]
    with pytest.raises(ValidationError, match="Helvetica"):
        Manifest.model_validate(_restricted_derived(fonts=fonts))


def test_restricted_derived_file_may_keep_an_ofl_font() -> None:
    fonts = [{"name": "ABCDEF+NotoSans", "license": "OFL-1.1", "action": "kept"}]
    Manifest.model_validate(_restricted_derived(fonts=fonts))


@pytest.mark.parametrize("license_id", ["CC-BY-ND-4.0", "CC-BY-NC-4.0", "CC-BY-NC-SA-4.0"])
def test_nc_and_nd_sources_are_refused(license_id: str) -> None:
    source = _restricted_derived()["source"] | {"license": license_id, "attribution": "X"}
    with pytest.raises(ValidationError, match="forbids"):
        Manifest.model_validate(_restricted_derived(source=source))


def test_cc_by_source_passes_attribution_and_license_to_the_derived_file() -> None:
    source = {
        "sha256": SOURCE_SHA,
        "license": "CC-BY-4.0",
        "redistributable": True,
        "attribution": "A. Author, arXiv:2401.00001",
    }
    Manifest.model_validate(_restricted_derived(source=source, license="CC-BY-4.0"))
    with pytest.raises(ValidationError, match="keep CC-BY"):
        Manifest.model_validate(_restricted_derived(source=source, license="CC0-1.0"))
    with pytest.raises(ValidationError, match="attribution"):
        Manifest.model_validate(
            _restricted_derived(source=source | {"attribution": None}, license="CC-BY-4.0")
        )


def test_cc_by_sa_source_keeps_share_alike() -> None:
    source = {
        "sha256": SOURCE_SHA,
        "license": "CC-BY-SA-4.0",
        "redistributable": True,
        "attribution": "A. Author",
    }
    with pytest.raises(ValidationError, match="CC-BY-SA"):
        Manifest.model_validate(_restricted_derived(source=source, license="CC-BY-4.0"))


def test_real_file_must_be_redistributable_and_unchanged() -> None:
    real: dict[str, Any] = {
        "file": "paper.pdf",
        "sha256": SOURCE_SHA,
        "tier": "real",
        "license": "CC-BY-4.0",
        "source": {
            "sha256": SOURCE_SHA,
            "license": "CC-BY-4.0",
            "redistributable": True,
            "attribution": "A. Author",
        },
    }
    Manifest.model_validate(real)
    with pytest.raises(ValidationError, match="redistributable"):
        Manifest.model_validate(real | {"source": real["source"] | {"redistributable": False}})
    with pytest.raises(ValidationError, match="byte-identical"):
        Manifest.model_validate(real | {"sha256": OUTPUT_SHA})


def test_oracle_must_give_the_claimed_result() -> None:
    oracles = [
        {
            "name": "crash",
            "target": "docling-parse 7.24.0",
            "expected": "fires",
            "observed": "silent",
        }
    ]
    with pytest.raises(ValidationError, match="expected to be fires"):
        Manifest.model_validate(_restricted_derived(oracles=oracles))


def test_synthetic_file_has_no_source() -> None:
    synthetic = {
        "file": "cubic.pdf",
        "sha256": OUTPUT_SHA,
        "tier": "synthetic",
        "license": "CC0-1.0",
        "rights_holder": "YRA Tech",
        "generator": {"version": "0.0.1", "seed": 1},
    }
    Manifest.model_validate(synthetic)
    with pytest.raises(ValidationError, match="no source"):
        Manifest.model_validate(synthetic | {"source": _restricted_derived()["source"]})


def test_sidecar_name_does_not_collide_with_parser_output() -> None:
    path = manifest_path(Path("regression/case.pdf"))
    assert path == Path("regression/case.pdf.docmutant.json")
    assert not path.name.endswith(".pdf.json")


def test_validate_command_checks_the_described_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pdf = tmp_path / "honda-p7.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    manifest = Manifest.model_validate(_restricted_derived(sha256=file_sha256(pdf)))
    sidecar = manifest_path(pdf)
    write_manifest(manifest, sidecar)

    assert main(["validate", "--check-file", str(sidecar)]) == 0

    pdf.write_bytes(b"%PDF-1.7\nchanged\n")
    assert main(["validate", "--check-file", str(sidecar)]) == 1
    assert "does not match its sha256" in capsys.readouterr().err
