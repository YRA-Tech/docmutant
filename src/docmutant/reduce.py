"""Reduce and sanitize a PDF while an oracle keeps firing.

Each step changes a copy of the current file and keeps the change only if the
oracle still fires. The current state is kept as bytes, so a rejected step
costs nothing to undo.
"""

from __future__ import annotations

import io
import random
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pikepdf

from docmutant.content import Unit, flatten, parse_units, reduce_units
from docmutant.mutate import (
    PROPERTY_KEEP,
    StreamRef,
    form_xobjects,
    image_xobjects,
    keep_pages,
    read_stream,
    remove_font_programs,
    replace_image,
    strip_catalog,
    strip_form_extras,
    strip_marked_content_text,
    strip_metadata_streams,
    strip_page_extras,
    swap_text_codes,
    write_stream,
)
from docmutant.oracles import CommandOracle, Outcome


class OracleDidNotFire(RuntimeError):
    """The oracle is silent on the input, so there is nothing to keep."""


@dataclass
class ReduceResult:
    data: bytes
    mutations: list[dict[str, Any]] = field(default_factory=list)
    oracle_calls: int = 0


def _save(pdf: pikepdf.Pdf) -> bytes:
    buffer = io.BytesIO()
    pdf.remove_unreferenced_resources()
    pdf.save(
        buffer,
        compress_streams=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate,
        deterministic_id=True,
    )
    return buffer.getvalue()


class Reducer:
    def __init__(
        self,
        oracle: CommandOracle,
        *,
        seed: int = 0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.oracle = oracle
        self.seed = seed
        self.log = log or (lambda message: print(message, file=sys.stderr))
        self._tmp = tempfile.TemporaryDirectory(prefix="docmutant-")
        self._counter = 0

    def fires(self, data: bytes, page: int = 1) -> bool:
        self._counter += 1
        path = Path(self._tmp.name) / f"candidate-{self._counter}.pdf"
        path.write_bytes(data)
        try:
            return self.oracle.check(path, page).outcome is Outcome.FIRES
        finally:
            path.unlink(missing_ok=True)

    def run(self, source: bytes, page: int) -> ReduceResult:
        if not self.fires(source, page):
            raise OracleDidNotFire(f"{self.oracle.name} oracle is silent on the source page {page}")
        result = ReduceResult(data=source)

        def step(name: str, params: dict[str, Any], mutate: Callable[[pikepdf.Pdf], Any]) -> bool:
            with pikepdf.open(io.BytesIO(result.data)) as pdf:
                detail = mutate(pdf)
                candidate = _save(pdf)
            if candidate == result.data:
                return False
            if self.fires(candidate):
                result.data = candidate
                entry: dict[str, Any] = {"operator": name, "params": dict(params)}
                if detail:
                    entry["params"]["detail"] = detail
                result.mutations.append(entry)
                self.log(f"  kept   {name} {params or ''} -> {len(candidate)} bytes")
                return True
            self.log(f"  undone {name} {params or ''} (oracle silent)")
            return False

        # 1. One page only. This step must succeed: everything after works on page 1.
        with pikepdf.open(io.BytesIO(source)) as pdf:
            single = _save(keep_pages(pdf, [page]))
        if not self.fires(single):
            raise OracleDidNotFire("the oracle needs pages other than the source page")
        result.data = single
        result.mutations.append({"operator": "keep-pages", "params": {"pages": [page]}})
        # Every save from here on also drops resources that no content refers to.
        result.mutations.append({"operator": "remove-unreferenced-resources", "params": {}})
        self.log(f"  kept   keep-pages [{page}] -> {len(single)} bytes")

        # 2. Document- and page-level extras.
        step("strip-catalog", {}, strip_catalog)
        step("strip-page-extras", {}, strip_page_extras)
        step("strip-metadata", {}, strip_metadata_streams)

        # 3. Content of the page, then of each form XObject.
        self._reduce_stream(result, StreamRef("page", 0))
        with pikepdf.open(io.BytesIO(result.data)) as pdf:
            forms = form_xobjects(pdf)
        for ref in forms:
            self._reduce_stream(result, ref)

        # 4. Replace what remains: text-bearing properties, text codes, image
        #    samples, font programs.
        def all_streams(pdf: pikepdf.Pdf) -> list[StreamRef]:
            return [StreamRef("page", 0), *form_xobjects(pdf)]

        step("strip-form-extras", {}, strip_form_extras)
        step(
            "strip-marked-content-text",
            {"keep": sorted(PROPERTY_KEEP), "ocg_names": "Layer N"},
            strip_marked_content_text,
        )

        step(
            "swap-text-codes",
            {"seed": self.seed, "rule": "equal advance width, same font"},
            lambda pdf: swap_text_codes(pdf, all_streams(pdf), self.seed),
        )
        self._replace_images(result, step)
        if not step("remove-font-programs", {"fonts": "all"}, remove_font_programs):
            with pikepdf.open(io.BytesIO(result.data)) as pdf:
                names = sorted({str(d.get("/FontName", "")) for d in _descriptors(pdf)})
            for name in names:
                step(
                    "remove-font-programs",
                    {"fonts": [name]},
                    lambda pdf, n=name: remove_font_programs(pdf, {n}),
                )

        result.oracle_calls = self.oracle.calls
        return result

    def _reduce_stream(self, result: ReduceResult, ref: StreamRef) -> None:
        with pikepdf.open(io.BytesIO(result.data)) as pdf:
            units = parse_units(read_stream(pdf, ref))
        before = len(flatten(units))
        if before == 0:
            return

        def build(candidate: list[Unit]) -> bytes:
            with pikepdf.open(io.BytesIO(result.data)) as pdf:
                write_stream(pdf, ref, flatten(candidate))
                return _save(pdf)

        kept = reduce_units(units, lambda candidate: self.fires(build(candidate)))
        after = len(flatten(kept))
        if after < before:
            result.data = build(kept)
            result.mutations.append(
                {
                    "operator": "delete-content",
                    "params": {
                        "stream": ref.label(),
                        "instructions_before": before,
                        "after": after,
                    },
                }
            )
        self.log(f"  {ref.label()}: {before} -> {after} instructions")

    def _replace_images(self, result: ReduceResult, step: Callable[..., bool]) -> None:
        with pikepdf.open(io.BytesIO(result.data)) as pdf:
            count = len(image_xobjects(pdf))
        if count == 0:
            return
        for style in ("flat", "noise"):

            def replace_all(pdf: pikepdf.Pdf, style: str = style) -> list[str]:
                rng = random.Random(self.seed)
                return [replace_image(image, style=style, rng=rng) for image in image_xobjects(pdf)]

            if step(
                "replace-image-samples",
                {"style": style, "images": "all", "seed": self.seed},
                replace_all,
            ):
                return
        for index in range(count):

            def replace_one(pdf: pikepdf.Pdf, index: int = index) -> str:
                return replace_image(
                    image_xobjects(pdf)[index], style="noise", rng=random.Random(self.seed + index)
                )

            step(
                "replace-image-samples",
                {"style": "noise", "image": index, "seed": self.seed},
                replace_one,
            )


def _descriptors(pdf: pikepdf.Pdf) -> list[pikepdf.Dictionary]:
    return [
        obj
        for obj in pdf.objects
        if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == "/FontDescriptor"
    ]
