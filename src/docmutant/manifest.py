"""Provenance manifest for one docmutant output file.

Every PDF that docmutant writes gets a sidecar manifest named
``<file>.docmutant.json``. The manifest records where the file came from, how it
was made, which checks (oracles) it passes, and under which terms it may be
shared. The validation rules below encode the corpus policy in
``CORPUS-LICENSES.md``: a manifest that breaks the policy does not load.

The suffix is deliberately not ``.pdf.json``. In the docling-parse regression
dataset, that suffix already holds parser output.
"""

from __future__ import annotations

import datetime
import hashlib
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "0.1"
MANIFEST_SUFFIX = ".docmutant.json"

SHA256_PATTERN = r"^[0-9a-f]{64}$"

# License identifiers for sources that may not be redistributed or whose terms
# are not known. Everything else is expected to be an SPDX identifier.
ALL_RIGHTS_RESERVED = "LicenseRef-all-rights-reserved"
UNKNOWN_LICENSE = "LicenseRef-unknown"
PUBLIC_DOMAIN = "LicenseRef-public-domain"

# Font licenses that allow a font program to stay in a file derived from a
# restricted source. Any other kept font program would carry protected material.
FREE_FONT_LICENSES = frozenset(
    {"OFL-1.1", "CC0-1.0", PUBLIC_DOMAIN, "Apache-2.0", "MIT", "GUST-Font-License"}
)


class Tier(str, Enum):
    """How a file relates to real-world documents."""

    REAL = "real"
    """An unchanged real document."""

    DERIVED = "derived"
    """A real document changed by docmutant."""

    SYNTHETIC = "synthetic"
    """A document that docmutant built without a source document."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(_Model):
    """The real document that a ``real`` or ``derived`` file comes from."""

    sha256: str = Field(pattern=SHA256_PATTERN)
    url: str | None = None
    license: str = Field(
        description=(
            "SPDX identifier of the source's license, or "
            f"{ALL_RIGHTS_RESERVED}, {UNKNOWN_LICENSE} or {PUBLIC_DOMAIN}."
        )
    )
    redistributable: bool = Field(
        description="True when the source's terms allow the source itself to be shared."
    )
    attribution: str | None = Field(
        default=None, description="Credit line that a CC-BY license requires."
    )
    pages: list[int] = Field(default_factory=list, description="1-based source pages used.")


class FontRecord(_Model):
    """One font program that the source embeds, and what happened to it."""

    name: str
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    license: str = UNKNOWN_LICENSE
    action: Literal["kept", "replaced", "removed"]


class Mutation(_Model):
    """One applied transformation, in order."""

    operator: str
    params: dict[str, Any] = Field(default_factory=dict)


class Generator(_Model):
    """The tool run that produced the file, so that it can be regenerated."""

    tool: str = "docmutant"
    version: str
    seed: int | None = None
    command: str | None = None


class OracleResult(_Model):
    """One check, the result it should give, and the result it gave."""

    name: str = Field(description="For example crash, hang, pixel-diff-pypdfium2, text-cells.")
    target: str = Field(description="What was checked, for example 'docling-parse 7.24.0'.")
    expected: Literal["fires", "silent"]
    observed: Literal["fires", "silent"]
    details: dict[str, Any] = Field(default_factory=dict)


class Family(_Model):
    """A group of files that must keep an invariant, such as rotation variants."""

    id: str
    invariant: str


class WebSearch(_Model):
    """Reverse image search of the file, with the original as a positive control."""

    service: str
    date: datetime.date
    control_original_found: bool | None = Field(
        description="None when the original could not be searched, for example a private file."
    )
    mutant_found: bool
    regions_searched: int = 0
    top_matches: list[str] = Field(default_factory=list)


class Evidence(_Model):
    """Automatic record of what a derived file still shares with its source."""

    kept: list[str] = Field(description="What remains from the source, for example '1 path'.")
    replaced: list[str] = Field(description="What was replaced or removed.")
    metrics: dict[str, float] = Field(
        default_factory=dict, description="For example text_ngram_overlap or render_phash_distance."
    )
    web_search: WebSearch | None = None


class Attestation(_Model):
    """A named reviewer's good-faith statement, see docs/ATTESTATION.md."""

    reviewer: str
    date: datetime.date
    checklist_version: str
    statement_version: str
    signature: str | None = Field(
        default=None, description="For example the signed commit that added the file."
    )


class Manifest(_Model):
    """Provenance, method, checks and terms for one output file."""

    schema_version: Literal["0.1"] = SCHEMA_VERSION
    file: str = Field(description="File name of the PDF that this manifest describes.")
    sha256: str = Field(pattern=SHA256_PATTERN)
    tier: Tier
    license: str = Field(description="SPDX identifier of the license for this file.")
    rights_holder: str | None = Field(
        default=None, description="Who holds any copyright in a derived or synthetic file."
    )
    source: Source | None = None
    fonts: list[FontRecord] = Field(default_factory=list)
    mutations: list[Mutation] = Field(default_factory=list)
    generator: Generator | None = None
    oracles: list[OracleResult] = Field(default_factory=list)
    family: Family | None = None
    evidence: Evidence | None = None
    attestation: Attestation | None = None
    takedown_contact: str | None = None

    @model_validator(mode="after")
    def _check_policy(self) -> Manifest:
        for oracle in self.oracles:
            if oracle.observed != oracle.expected:
                raise ValueError(
                    f"oracle {oracle.name!r} on {oracle.target!r} was expected to be "
                    f"{oracle.expected} but was {oracle.observed}"
                )

        if self.tier is Tier.SYNTHETIC:
            self._check_synthetic()
        else:
            self._check_has_source()
            if self.tier is Tier.REAL:
                self._check_real()
            else:
                self._check_derived()
        return self

    def _check_has_source(self) -> None:
        if self.source is None:
            raise ValueError(f"a {self.tier.value} file must name its source")
        if _is_nc_or_nd(self.source.license):
            raise ValueError(
                f"source license {self.source.license} forbids changes or commercial use"
            )

    def _check_synthetic(self) -> None:
        if self.source is not None:
            raise ValueError("a synthetic file has no source; use tier 'derived'")
        if self.generator is None:
            raise ValueError("a synthetic file must record its generator")
        if self.rights_holder is None:
            raise ValueError("a synthetic file must name its rights holder")

    def _check_real(self) -> None:
        assert self.source is not None
        if self.mutations:
            raise ValueError("a real file has no mutations; use tier 'derived'")
        if self.sha256 != self.source.sha256:
            raise ValueError("a real file must be byte-identical to its source")
        if not self.source.redistributable:
            raise ValueError("a real file may be shared only when its source is redistributable")
        if self.license != self.source.license:
            raise ValueError("a real file keeps the license of its source")

    def _check_derived(self) -> None:
        assert self.source is not None
        if not self.mutations:
            raise ValueError("a derived file must list the mutations that produced it")
        if self.generator is None:
            raise ValueError("a derived file must record its generator")
        if self.rights_holder is None:
            raise ValueError("a derived file must name its rights holder")

        source_license = self.source.license
        if source_license.startswith("CC-BY"):
            if self.source.attribution is None:
                raise ValueError(f"a {source_license} source needs an attribution line")
            if source_license.startswith("CC-BY-SA") and not self.license.startswith("CC-BY-SA"):
                raise ValueError("a file derived from a CC-BY-SA source must stay CC-BY-SA")
            if not self.license.startswith("CC-BY"):
                raise ValueError(f"a file derived from a {source_license} source must keep CC-BY")

        if not self.source.redistributable:
            self._check_restricted_derivation()

    def _check_restricted_derivation(self) -> None:
        missing = [
            name
            for name, value in (
                ("evidence", self.evidence),
                ("attestation", self.attestation),
                ("takedown_contact", self.takedown_contact),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                "a file derived from a source that may not be redistributed needs "
                + ", ".join(missing)
            )
        kept_unfree = [
            font.name
            for font in self.fonts
            if font.action == "kept" and font.license not in FREE_FONT_LICENSES
        ]
        if kept_unfree:
            raise ValueError(
                "font programs without a free license must be replaced or removed: "
                + ", ".join(kept_unfree)
            )


def _is_nc_or_nd(license_id: str) -> bool:
    parts = license_id.upper().split("-")
    return "NC" in parts or "ND" in parts


def manifest_path(pdf_path: Path) -> Path:
    """Return the sidecar manifest path for a PDF: ``<name>.docmutant.json``."""
    return pdf_path.with_name(pdf_path.name + MANIFEST_SUFFIX)


def file_sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path) -> Manifest:
    """Load and validate a manifest file."""
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def write_manifest(manifest: Manifest, path: Path) -> None:
    """Write a manifest as indented JSON."""
    path.write_text(manifest.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
