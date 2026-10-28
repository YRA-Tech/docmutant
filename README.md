# docmutant

Structure-aware PDF mutation with oracles.

docmutant changes PDF files at the level of their structure (content-stream
operators, paths, glyphs, images, fonts) and checks every change against an
*oracle*: a test that says whether a bug shows. One engine serves three modes:

| Mode | Starts from | Goal | The oracle must |
|---|---|---|---|
| **reduce / sanitize** | a document that shows a bug | remove and replace content until no original expression is left | keep firing |
| **fuzz** | seed documents | mutate until a new failure appears | start firing |
| **metamorphic** | any document | apply a change that should not matter, such as a width-preserving glyph swap | stay silent |

Typical oracles: the parser crashes, rendering hangs, the render differs from a
second engine (for example pypdfium2), or the extracted text cells differ.

Every output file gets a **provenance manifest** that records its source, the
mutations applied, the oracle results, and the terms under which the file may
be shared.

## Why

Parser and renderer bugs are often found on documents that cannot be shared:
manuals, reports, customer files. Maintainers need a real document that shows
the bug, and they need a license that allows it to go into a regression
corpus. docmutant aims to turn such a document into a small file that still
shows the bug, keeps none of the original expression, and says so in a
reviewable record.

The same mutations serve testing and training data. A width-preserving glyph
swap keeps every word box where it was, so a layout model or a segmentation
step can be checked, or trained, on the same geometry with different text, and
the labels stay exact.

## Status

Pre-alpha. Built so far: the manifest model with its policy checks, a
validator, and a first reduce/sanitize mode for rendering bugs with crash,
hang and error oracles that run docling-parse. See
[docs/DESIGN.md](docs/DESIGN.md) for the design and the roadmap.

```console
$ docmutant reduce manual.pdf --page 7 --out p7.pdf --oracle crash \
    --target-python /path/to/buggy/.venv/bin/python \
    --silent-python /path/to/fixed/.venv/bin/python --scale 3.0
$ docmutant attest p7.pdf.docmutant.json --reviewer "A. Reviewer"
$ docmutant validate --check-file p7.pdf.docmutant.json
$ docmutant schema                     # print the manifest JSON Schema
```

## Manifest

The manifest is a sidecar file named `<file>.docmutant.json`, next to the PDF.
(It does not use `.pdf.json`, which some corpora use for parser output.)

Each file has a **tier**:

- `real`: an unchanged real document. It must be byte-identical to its source,
  and the source must be redistributable.
- `derived`: a real document changed by docmutant. The manifest lists the
  mutations and the tool version and seed, so that the file can be regenerated.
- `synthetic`: built by docmutant without a source document.

A file derived from a source that may not be redistributed must also carry
automatic `evidence` (what remains from the source, similarity measurements),
a signed `attestation` by a named reviewer, and a `takedown_contact`. Font
programs without a free license must be replaced or removed. The validator
refuses manifests that break these rules. See
[CORPUS-LICENSES.md](CORPUS-LICENSES.md) and
[docs/ATTESTATION.md](docs/ATTESTATION.md).

## Licensing

The docmutant code is under the [MIT License](LICENSE).

Documents produced with docmutant carry their own license, recorded per file in
the manifest. [CORPUS-LICENSES.md](CORPUS-LICENSES.md) explains the policy.
Nothing in this repository is legal advice.

## Prior art

- [pdf-mangler](https://github.com/cfcurtis/pdf-mangler) (Curtis, DocEng 2022,
  MPL-2.0) anonymizes PDFs for bug reports. It strips metadata, replaces text
  at random, jitters vector paths and blurs images. docmutant differs in that it
  checks each change against an oracle, keeps glyph widths and exact trigger
  geometry, replaces font programs, and records provenance.
- [GTR](https://github.com/sherfert/GTR) (Herfert, Patra and Pradel, ASE 2017)
  reduces tree-structured inputs, including PDF, with delta debugging.
- Structure-aware PDF fuzzers (for example IUST-DeepFuzz and FuzzInMem) target
  crashes and code coverage, not render or text differences.
- Document-image augmentation and robustness work (Augraphy, RoDLA, SynthDoG,
  Genalog) changes rendered images, not the PDF structure.

## Contributing

Issues and pull requests are welcome. Please sign off your commits
(`git commit -s`, Developer Certificate of Origin).

## Takedown

If you hold rights in a document from which a file here was derived and you
object to it, open an issue titled `Takedown: <file name>`. We remove the file
first and discuss afterwards.
