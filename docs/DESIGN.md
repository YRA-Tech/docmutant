# docmutant design

Status: draft, October 2026. This document describes the intended design. The
sections marked **built** exist in the code; the others are planned.

## 1. Purpose

docmutant changes PDF documents at the level of their structure and checks each
change with an *oracle*. It serves three uses that need the same machinery:

| Use | What must hold | What the manifest claims |
|---|---|---|
| **Bug reproduction** for parsers and renderers | The bug still shows; exact geometry of the trigger is kept | The file keeps no protectable expression from its source |
| **Training and evaluation data** for document AI | Content is plausible; labels (word boxes, item types) stay exact | The source license is respected; labels are preserved |
| **Privacy cleaning** of real documents | No personal data is left, in any layer | Personal data was removed under a named policy and reviewed |

### Goals

- Change a PDF without moving anything that the use does not need to change:
  glyph advances, word boxes, line breaks, tags and reading order.
- Prove each claim with automatic evidence and, where judgment is needed, a
  signed human review.
- Work with any target that can be run as a command: docling-parse,
  opendataloader-pdf, pypdfium2, MuPDF, a full conversion pipeline.

### Non-goals

- docmutant does not decide legal questions. It records evidence and the
  reviewer's judgment.
- It is not a general PDF editor or a redaction product with a user interface.
  Products can build those on top of it.
- It does not train models. It produces files and labels for those who do.

## 2. Concepts

```
            ┌────────────┐   regions + labels   ┌────────────┐
 source ───►│ selectors  │─────────────────────►│  mutators  │───► candidate
  PDF       └────────────┘                      └────────────┘        │
                ▲                                                     ▼
                │ Docling, marked content, bbox, "all"          ┌──────────┐
                │                                               │ oracles  │
           ┌────┴─────┐  keep / undo / record                   └────┬─────┘
           │  driver  │◄─────────────────────────────────────────────┘
           └────┬─────┘
                ▼
        output PDF + manifest (evidence, mutations, oracle results, claims)
```

- **Unit** (built): a removable piece of a content stream (a `q`…`Q` group, a
  `BT`…`ET` object, a path with its paint operator, a `Do`, a `sh`, an inline
  image, a marked-content sequence, a state operator).
- **Mutator**: a function that changes an open PDF and returns a description
  of what it did. Structural mutators work on units, resources and objects.
  Semantic mutators work on selected regions.
- **Selector**: produces the regions a semantic mutator acts on, each with a
  label: everything, a bounding box, a marked-content ID, or Docling items.
- **Oracle** (built: crash, hang, error): runs a target on a candidate and
  says *fires* or *silent*.
- **Driver**: one of the modes. It applies mutators and consults oracles.
- **Evidence** (built, partial): measurements of what the output still shares
  with its source.
- **Manifest** (built): the provenance record next to each output file.

## 3. Modes

### 3.1 Reduce and sanitize (built, rendering tier)

Start from a document that shows a bug. Each step changes a copy and is kept
only if the oracle still fires. Steps, in order:

1. Keep the target page; drop unreferenced resources on every save.
2. Strip the catalog (all but `/Type` and `/Pages`), document info, per-page
   extras (annotations, thumbnails, piece info, structure parents) and all
   `/Metadata` streams.
3. Delta-debug the page's units (Zeller's ddmin), then the units inside each
   remaining container, then unwrap containers that are not needed. Repeat for
   every reachable form XObject.
4. Swap shown text codes for codes of equal advance width in the same font,
   keeping word spaces.
5. Replace image samples (flat grey first, then seeded noise), keeping width,
   height, depth, colour space and DCT where present.
6. Remove embedded font programs (all, else one by one).

The source page must make the oracle fire before anything starts (a positive
control). A second oracle can run against a build with the fix; it must stay
silent on the output, which shows that the file isolates exactly that bug.

### 3.2 Fuzz (planned)

Start from seed documents and apply random mutators (path control points,
CTMs, clip paths, image geometry, glyph codes, font metrics, object
structure) until an oracle starts to fire. Deduplicate findings by the
target's failure signature (signal and top stack frames, or the pixel
difference pattern), then hand each finding to the reduce mode.

### 3.3 Metamorphic (planned)

Apply a change that should not matter and check an invariant, for example:

- width-preserving glyph swap: word and line boxes must not change;
- page rotation in steps of 90°: the extracted text must not change;
- renaming resources or re-ordering independent objects: render must not change.

A violated invariant is a finding, and the file pair goes into the corpus with
a `family` entry in its manifest.

### 3.4 Anonymize (planned)

Replace selected content with plausible content of the same type and the same
size, in every layer of the document (see 4.6). Used for training data and
for privacy cleaning.

## 4. Components

### 4.1 Content model (built)

`parse_units` turns a content stream into nested units and falls back to flat
units when the stream is unbalanced. `reduce_units` runs ddmin top-down and
unwraps containers.

### 4.2 Text-position tracker (planned, next)

Semantic replacement and finer text reduction both need to know which bytes of
which string drew which glyph, and where. The tracker replays the text state
(`Tm`, `Td`, `TD`, `T*`, `Tc`, `Tw`, `Tz`, `TL`, `Ts`, `TJ` adjustments, the
CTM) and yields, for every glyph:

- the stream, the instruction index, the operand index and the byte offset;
- the character code, the font resource, the advance width;
- the device-space box;
- the marked-content ID and structure element, if the page is tagged.

With it, a region from any selector (a Docling item's box, a PII span) maps to
exact string bytes, and a replacement can be written back without moving any
other glyph.

### 4.3 Fonts

- **Width classes** (built): codes with equal advance width in a simple font or
  an Identity-H composite font, with ToUnicode spaces kept.
- **Glyph availability** (planned): subset fonts lack glyphs that the document
  never used. fontTools reads the embedded program to tell which codes have
  glyphs, so a replacement uses only codes that render.
- **Metric-compatible substitution** (planned): replace the embedded program
  with an open font that has the same advance widths, so that any text can be
  set without moving a line: Liberation Sans, Serif and Mono (for Arial,
  Times New Roman, Courier New), Carlito (Calibri), Caladea (Cambria), and
  further families from a table. The substitute is subset to the codes used,
  `/Widths` or `/W` and `/ToUnicode` are rewritten, and the OFL terms (including
  Reserved Font Names) are honoured. This also removes the source's font
  program, which the copyright and privacy claims need.

### 4.4 Images (built, basic)

Samples are replaced at the same geometry. Planned: choose the treatment by
the image's class (logo, signature, photo, chart, scanned text) from Docling's
picture classification; for scanned text, replace the text in the image, not
only the pixels.

### 4.5 Vector graphics

The reduce mode keeps the paths that the oracle needs, exactly. In the
anonymize mode, recognisable illustrations that no oracle needs can be
distorted (as pdf-mangler does) or removed.

### 4.6 Hidden layers (planned)

Text lives in more places than the content stream. The anonymize mode must
change every one of them, consistently with the visible text, and keep the
document's tags valid:

| Layer | Where |
|---|---|
| Structure tree | `/Alt`, `/ActualText`, `/E`, `/T`, `/Lang` of structure elements |
| Forms | field values `/V`, defaults `/DV`, tooltips `/TU`, appearance streams |
| Annotations | `/Contents`, `/RC`, popups, appearance streams |
| Navigation | outlines, page labels, named destinations |
| Metadata | document info, XMP, per-object `/Metadata` |
| Other | embedded files, JavaScript, optional-content group names, thumbnails |

Keeping the tags valid means that a cleaned file from a tagged source stays as
conformant (for example to PDF/UA) as it was.

### 4.7 Semantic selection (planned)

1. Convert the document with Docling (optional `docling` extra).
2. Map each item (text, heading, table cell, key-value pair, form field,
   picture) to glyph runs with the tracker: by marked-content ID when the
   page is tagged, else by box overlap and text match.
3. Run a PII detector (for example Microsoft Presidio) on the item text to find
   names, dates, numbers, addresses and identifiers.
4. Apply a policy per type, for example: names → generated names; dates → dates
   in the same format; amounts → amounts with the same digit count; signatures
   → noise; logos → removed.
5. Keep replacements consistent within the document. The mapping is
   discarded at the end by default; keeping it makes the result pseudonymous,
   not anonymous.

Recall matters more than precision: the review report lists every replaced
region and every text region that stayed, with the detector's confidence.

### 4.8 Oracles

| Oracle | Status |
|---|---|
| crash (signal), hang (timeout), error (nonzero exit) | built |
| render difference against a second engine (pypdfium2 by default), with per-page `mean_abs_error` and `changed_pixels_ratio` | planned |
| text-cell difference between two runs or two engines | planned |
| invariant check for metamorphic families | planned |

Every oracle runs the target in a child process. A target is any command with
`{pdf}` and `{page}` placeholders, so Java, Rust and C++ tools work as well as
Python ones.

### 4.9 Evidence (built, partial)

Built: words shared with the source page (pypdfium2 text), source images kept
(sample hashes), source font programs kept (program hashes), render dHash
distance, and a count of the remaining units by kind. Planned: SSIM, n-gram
overlap, region crops for reverse image search with the original as a positive
control, and a side-by-side render for the reviewer.

### 4.10 Manifest and claims (built: schema 0.1)

The manifest records tier, source, fonts, mutations, generator, oracle results,
family, evidence, attestation and takedown contact, and enforces the corpus
policy (see `CORPUS-LICENSES.md`). Schema 0.2 adds `claims`: a list of
`copyright`, `training` and `privacy`, each with its own required evidence and
attestation statement.

### 4.11 Attestation

A claim that needs judgment needs a named reviewer, a checklist version and a
statement version (`docs/ATTESTATION.md`). The copyright statement exists as a
draft. A privacy statement ("personal data removed under policy X") follows the
same pattern. CI refuses corpus files whose claims lack the required review.

## 5. Interfaces (sketch)

```python
class Mutator(Protocol):
    name: str

    def __call__(self, pdf: pikepdf.Pdf, regions: Sequence[Region], rng: Random) -> dict: ...


class Selector(Protocol):
    def select(
        self, pdf_path: Path, page: int
    ) -> list[Region]: ...  # Region = box + glyph runs + label


class Oracle(Protocol):
    name: str
    target: str

    def check(self, pdf: Path, page: int) -> CheckResult: ...
```

## 6. Integration

- **docling-parse regression dataset.** Derived files with manifests can
  supply real-document evidence where the original cannot be shared. The
  manifest suffix (`.docmutant.json`) does not collide with the dataset's
  parser output (`.pdf.json`). The render oracle uses the same metrics as the
  dataset's pypdfium2 comparison.
- **opendataloader-pdf and its benchmark.** The command oracle runs the
  opendataloader CLI as it is. Metamorphic families give benchmark items with
  exact, automatically derived ground truth.
- **Products.** A remediation tool can offer "clean and attest" on top of the
  anonymize mode, and form-template creation (remove filled values, keep labels,
  add fields) on top of the same selectors and mutators.

## 7. Dependencies and licensing

Code: MIT. Required: pikepdf (MPL-2.0), pypdfium2 (Apache-2.0 or BSD-3-Clause),
pydantic (MIT). Planned optional: fontTools (MIT), docling (MIT), presidio
(MIT), Faker (MIT). No dependency's code is copied into docmutant's files.

## 8. Roadmap

| Milestone | Content | Status |
|---|---|---|
| M1 | Manifest, policy validation, CLI | done |
| M2 | Reduce mode for rendering bugs, crash/hang oracles, basic evidence | built; first real-document runs in progress |
| M3 | Text-position tracker; render-difference oracle | next |
| M4 | Font glyph availability and metric-compatible substitution | |
| M5 | Hidden layers; anonymize mode with Docling selection; schema 0.2 claims | |
| M6 | Fuzz and metamorphic modes; corpus packaging | |

## 9. Open questions

- Will the maintainers of corpora (docling-parse's regression dataset,
  opendataloader-bench) accept derived files as evidence, and with which claims?
- Counsel's review of `CORPUS-LICENSES.md` and the attestation statements.
- Text in images: which OCR to use for evidence and replacement, without making
  the core depend on a model.
- Tolerances for the render-difference oracle across renderers.
