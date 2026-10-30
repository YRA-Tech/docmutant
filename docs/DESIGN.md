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

Each use has a *profile* that states what must stay exact (see 4). The uses
conflict: the precise geometry that OCR training needs can also identify a
source. So no profile removes a signal that its use needs.

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
size, in every layer of the document (see 5.6). Used for training data and
for privacy cleaning, under the `training` or `privacy` profile (see 4).

## 4. Profiles: what each use keeps

A change that helps one use can harm another. Exact skew, sub-point glyph
positions and scan noise are what an OCR model must learn. The same values can
also identify the source document. So docmutant has no single "clean" setting.
Each run uses a *profile*. The profile states which signals must stay exact,
which signals may change, and which oracles prove it.

No profile normalizes by default. Normalization (canonical object numbers,
rounded coordinates, standard page sizes) exists only in the `privacy`
profile, and only at the level that the user asks for.

### 4.1 Signals

| Signal | Examples |
|---|---|
| Geometry | page size, CTMs (with skew and rotation), glyph origins and advances, word and line boxes, path coordinates, clip paths |
| Text | shown character codes, `/ToUnicode`, text inside scanned images |
| Glyph shapes | embedded font programs, Type3 glyph procedures |
| Image samples | pixels, scan noise, compression artefacts |
| Image geometry | width, height, bit depth, colour space, filter, placement, effective resolution |
| Structure | tags, reading order, marked-content IDs, form fields, annotations |
| Serialization | object numbers and order, `/ID`, incremental updates, producer quirks (for example the misspelled `/RelativeColormetric` that Acrobat Capture writes) |
| Hidden text | metadata, outlines, alternate text, field values (see 5.6) |

### 4.2 Profiles

| Signal | `bug-repro` | `metamorphic` | `training` | `privacy` |
|---|---|---|---|---|
| Geometry | exact where the oracle needs it; the rest is removed | exact, except the one variable under test | exact | exact by default; rounding is an opt-in level |
| Text | equal-width swap | changed only as the family says | changed; labels updated with it | personal data replaced |
| Glyph shapes | removed unless the oracle needs them | exact | exact, or a metric-compatible substitute | substitute |
| Image samples | flat grey or noise | exact | exact, or a content-aware replacement | replaced by image class |
| Image geometry | exact | exact | exact | exact |
| Structure | removed unless the oracle needs it | exact | exact | exact; values cleaned |
| Serialization | not changed on purpose (qpdf rewrites it) | exact | any | normalized at the chosen level; reported |
| Hidden text | removed | exact | changed with the visible text | changed or removed |

**`bug-repro`** (built: the `reduce` mode). The oracle decides what stays.
What the oracle needs stays bit-exact, even when it can identify the source.
For example, the reduced Honda page keeps its skewed `cm` exactly, because the
crash depends on it. The rest is removed or replaced. The claim is
`copyright`. Remaining identifying values go into the evidence for the
reviewer. The profile does not remove them.

**`metamorphic`**. The two files of a pair differ in exactly one variable.
All other signals stay exact, so that a changed result can only come from that
variable. An invariant oracle checks each pair (see 3.3).

**`training`**. Labels must stay true. A transform is allowed only when it
keeps every label correct, or when it updates the label at the same time
(label-consistent). Examples:

- An equal-width text swap keeps word boxes. The transcription label is
  rebuilt from the new codes through `/ToUnicode`.
- Flat or noise image replacement is refused for an image that contains text
  or that a label refers to: it removes what the label describes. Such images
  stay exact, or get the planned text-in-scan replacement. That replacement
  draws the new text into the image with the measured skew, glyph size, blur
  and noise of the original.
- Skew, sub-point positions, noise and compression artefacts are never
  normalized.

**`privacy`**. The only profile that normalizes. The user chooses a level:

| Level | Changes | Cost to other uses |
|---|---|---|
| 0 (default) | none; the fingerprint report only | none |
| 1 | serialization: canonical object order, new random `/ID`, no incremental updates, producer quirks corrected | none for geometry, text or images |
| 2 | geometry: coordinates rounded to a grid, page size snapped to a standard size | degrades the signals that `training` keeps exact |

The *fingerprint report* lists the values that stay and can link the output
to its source: exact CTMs, the page size, image dimensions and resolution,
font subset prefixes, producer quirks and object order. Later versions can
estimate how rare each value is against a reference corpus. The reviewer signs
the privacy attestation with the report in front of them.

### 4.3 Kept signals are checked, not assumed

Each "exact" cell in the table maps to an oracle that runs on every candidate:

- geometry: the output's word boxes stay within the box tolerance of the
  source's and keep the minimum gap (see 4.4); the CTMs of kept units are
  equal;
- image geometry: width, height, bit depth, colour space and filter are equal;
- structure: the tag tree has the same shape and the same marked-content IDs;
- labels (`training`): labels computed from the output equal the updated
  labels.

A mutation that breaks a kept signal is undone, as in the reduce mode. The
manifest records the profile, each kept signal with its oracle result, and each
changed signal with the mutators that changed it.

### 4.4 Word and line boxes

Two settings control how far text that a mutator writes may move from the
source text. They are separate from the profile, and each profile has a
default.

**Box tolerance** states how far each edge of a word box may move, in points.
Each edge has two bounds: `out` (away from the centre of the box) and `in`
(toward it).

| Value | out | in | Use |
|---|---|---|---|
| `exact` (default) | 0 | 0 | equal-width swaps; metamorphic pairs |
| `contain` | 0 | any | replacement text that must stay inside the source boxes |
| `±d` | d | d | test what a target does when text moves up to d points in any direction |

A bound of 0 allows 0.01 pt for rounding. Edges are measured in the frame of
the line: along the baseline and across it. On a skewed scan, the
axis-aligned box of a word is larger than the word, so a tolerance on that box
would allow more movement than the user set.

A line box is the union of its word boxes. If each word edge moves at most d,
each line edge also moves at most d, so lines need no tolerance of their own.

The tolerance is a limit, not a target: a mutator can move text less than the
limit, or not at all. To make text move by a set amount, use a metamorphic
family with the shift as its variable (see 3.3).

**Minimum gap** states how close two boxes may come after the change. It has
one value for words in the same line and one for lines on the same page. Each
value is a distance in points (default 0) or `any`, which turns the check
off. Word spacing and line spacing are different tests, so a user can set
one and turn the other off. Words in different lines are not compared: the
line gap covers them.

- The gap between two boxes is a signed distance. When the boxes are apart,
  it is the shortest distance between them. When they overlap, it is minus
  the overlap depth: the shortest move that would separate them. The boxes
  are compared as drawn, as rotated rectangles, so skew does not change the
  result.
- A pair must keep a gap of at least the minimum, or at least its gap in
  the source if that is smaller. Source files often have boxes that are
  closer than the minimum, or that overlap (superscripts, tight kerning,
  text printed over text). Such a pair is allowed, but it may not come any
  closer.
- With the default of 0, boxes may touch but not overlap, except where they
  overlapped in the source.
- Under `contain`, the gap rule always passes. A box inside its source box
  is never closer to another box than the source box was. The minimum gap
  has an effect only when `out` is greater than 0.

The text-position tracker computes the boxes from font metrics (see 5.2). It
pairs each output word with its source word by the glyphs that replaced it,
not by matching text, so a changed space cannot pair the wrong words.

| Profile | Box tolerance | Minimum gap |
|---|---|---|
| `bug-repro` | not checked, unless the user sets it | not checked, unless the user sets it |
| `metamorphic` | `exact`, unless box position is the variable | 0 |
| `training` | `exact`; `contain` and `±d` are allowed because labels are rebuilt from the output | 0 |
| `privacy` | `exact`; at level 2, at least `±` half the grid step | 0 |

The manifest records both settings with the geometry oracle result: the
largest edge movement found, the smallest word and line gaps found, and the
number of pairs that broke the gap rule.

### 4.5 Combined uses

Training data from private documents needs both `training` and `privacy`.
The privacy rules decide *which* values are personal data. The training rules
decide *how* a replacement is made: label-consistent, and with no flat images
in place of text. Privacy level 2 degrades training signals, so it is refused
for training output unless the user names the degraded signals. The manifest
then lists them, and users of the corpus can filter on them.

## 5. Components

### 5.1 Content model (built)

`parse_units` turns a content stream into nested units and falls back to flat
units when the stream is unbalanced. `reduce_units` runs ddmin top-down and
unwraps containers.

### 5.2 Text-position tracker (built, partial)

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

Built (`tracker.py`): page content streams and the form XObjects they draw
(with each form's `/Matrix`, nested forms included), with simple fonts that
have `/Widths`, Type3 fonts (through their `/FontMatrix`), and composite fonts
with horizontal writing whose CMap is `/Identity-H` or embedded (codes of 1 to
4 bytes, `/W`, `/DW`). Each glyph names the stream that holds its instruction,
so a glyph in a form is addressed in the form, not in the page. Each glyph also records its text render mode (3 is the invisible
OCR layer of a scan) and where its width and height came from: `/Widths`,
`/MissingWidth`, the descriptor's `/Ascent` and `/Descent`, `/FontBBox`, or a
default. Text that the tracker cannot place raises an error, so that no glyph
is silently misplaced. On the real documents tested so far, glyph origins match
pdfium's character origins within 0.1 pt, apart from spaces that pdfium leaves
out and one known difference: pdfium truncates fractional widths to whole glyph
units, so its origins fall behind the tracker's along a string (0.14 pt after
about 40 glyphs at 12 pt).

Also built: `group_lines` groups the glyphs into words and lines, in drawing
order, so that text printed over text and table cells stay apart. Each glyph is
compared with the one before it. A new line starts when the glyph turns away
from the baseline, shares less than half its height with the previous glyph,
jumps back more than 1 em or forward more than 2 em, or changes between visible
and invisible. Inside a line, a space or a gap of more than 0.15 em ends a word.
Spaces come from `/ToUnicode` (`bfchar` and `bfrange`) and, in a simple font,
from the encoding. Each box is a rectangle in the frame of its line, as 4.4
requires, and a line box is the union of its word boxes.

On the sample documents, 99% of the words match pdfium's exactly. In the rest,
pdfium joins words hyphenated across lines, expands ligatures or leaves out a
space, or the font's `/ToUnicode` maps a hyphen to U+0000. The sample has no gaps between 0.05 and 0.3 em, so it
confirms the word gap of 0.15 em but does not pin it down; that value comes from
typography (kerning stays under about 0.1 em, word spaces rarely fall under
0.18 em). Known limits: a font with no `/ToUnicode` and a space at an unknown
code runs words together, letter spacing (`Tc`) above 0.15 em splits a word into
single glyphs, and glyphs not drawn in reading order, such as logically ordered
right-to-left text, make separate lines.

Planned, in order:

1. Built-in metrics for the 14 standard fonts, which older PDFs use without
   `/Widths`.
2. Structure elements, from the marked-content IDs and the parent tree.
3. A cross-check of boxes against pypdfium2 (see the open questions).
4. Vertical writing (`/Identity-V`, `/W2`, `/DW2`) and the predefined CJK
   CMaps, which need Adobe's CMap resources.

### 5.3 Fonts

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

### 5.4 Images (built, basic)

Samples are replaced at the same geometry. Planned: choose the treatment by
the image's class (logo, signature, photo, chart, scanned text) from Docling's
picture classification; for scanned text, replace the text in the image, not
only the pixels.

### 5.5 Vector graphics

The reduce mode keeps the paths that the oracle needs, exactly. In the
anonymize mode, recognisable illustrations that no oracle needs can be
distorted (as pdf-mangler does) or removed.

### 5.6 Hidden layers (planned)

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

### 5.7 Semantic selection (planned)

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

### 5.8 Oracles

| Oracle | Status |
|---|---|
| crash (signal), hang (timeout), error (nonzero exit) | built |
| render difference against a second engine (pypdfium2 by default), with per-page `mean_abs_error` and `changed_pixels_ratio` | planned |
| text-cell difference between two runs or two engines | planned |
| invariant check for metamorphic families | planned |

Every oracle runs the target in a child process. A target is any command with
`{pdf}` and `{page}` placeholders, so Java, Rust and C++ tools work as well as
Python ones.

### 5.9 Evidence (built, partial)

Built: words shared with the source page (pypdfium2 text), source images kept
(sample hashes), source font programs kept (program hashes), render dHash
distance, and a count of the remaining units by kind. Planned: SSIM, n-gram
overlap, region crops for reverse image search with the original as a positive
control, and a side-by-side render for the reviewer.

### 5.10 Manifest and claims (built: schema 0.1)

The manifest records tier, source, fonts, mutations, generator, oracle results,
family, evidence, attestation and takedown contact, and enforces the corpus
policy (see `CORPUS-LICENSES.md`). Schema 0.2 adds:

- `claims`: a list of `copyright`, `training` and `privacy`, each with its own
  required evidence and attestation statement;
- `profile`, with `preserved` (each kept signal and its oracle result) and
  `changed` (each changed signal and its mutators);
- `fingerprint` (`privacy` only): the normalization level and the fingerprint
  report.

### 5.11 Attestation

A claim that needs judgment needs a named reviewer, a checklist version and a
statement version (`docs/ATTESTATION.md`). The copyright statement exists as a
draft. A privacy statement ("personal data removed under policy X") follows the
same pattern. CI refuses corpus files whose claims lack the required review.

## 6. Interfaces (sketch)

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

## 7. Integration

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

## 8. Dependencies and licensing

Code: MIT. Required: pikepdf (MPL-2.0), pypdfium2 (Apache-2.0 or BSD-3-Clause),
pydantic (MIT). Planned optional: fontTools (MIT), docling (MIT), presidio
(MIT), Faker (MIT). No dependency's code is copied into docmutant's files.

## 9. Roadmap

| Milestone | Content | Status |
|---|---|---|
| M1 | Manifest, policy validation, CLI | done |
| M2 | Reduce mode for rendering bugs, crash/hang oracles, basic evidence | built; first real-document runs in progress |
| M3 | Text-position tracker; render-difference oracle; geometry (with box tolerance and minimum gap settings) and image-geometry preservation oracles | next |
| M4 | Font glyph availability and metric-compatible substitution | |
| M5 | Hidden layers; anonymize mode with Docling selection; profiles; privacy normalization levels and fingerprint report; schema 0.2 | |
| M6 | Fuzz and metamorphic modes; corpus packaging | |

## 10. Open questions

- Will the maintainers of corpora (docling-parse's regression dataset,
  opendataloader-bench) accept derived files as evidence, and with which claims?
- Counsel's review of `CORPUS-LICENSES.md` and the attestation statements.
- Text in images: which OCR to use for evidence and replacement, without making
  the core depend on a model.
- Tolerances for the render-difference oracle across renderers.
- How much measurement difference to allow, on top of the box tolerance, when
  a text engine (pypdfium2 or the user's target) reports the word boxes
  instead of the tracker. A fixed allowance per glyph is not enough: an
  engine that rounds widths (pdfium truncates them) drifts further from the
  tracker with each glyph until the next positioning operator.
- How to model text-in-scan replacement so that OCR training data keeps the
  noise and skew statistics of the original scan.
