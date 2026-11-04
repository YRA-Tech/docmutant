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

### 3.4 Anonymize (built: `docmutant anonymize`)

Replace the content of a whole document, in every layer (see 5.6), under the
`training` or `privacy` profile (see 4). Steps, in order:

1. Refuse a page that cannot be traced.
2. Under `privacy`, take out displayed formulas and words of math (see 5.5).
3. Fit new words on every page (see 5.3).
4. Under `privacy`: give every font a substitute, turn machine-readable codes and
   graphics such as logos into flat blocks and text drawn as outlines into paragraphs
   of new text (see 5.5), replace every image, and cover each formula with a block in
   the formula pattern.
5. Clean the hidden layers (see 5.6).
6. Save, open the saved bytes again, and run every oracle on them (see 5.8).

If an oracle that the profile requires fails, there is no output file: a reason
for each failure goes to the user, and only there, since it can quote the source.
The same seed gives the same bytes.

A run takes the whole document. A run on some pages would leave the other pages
reachable through tags and links, and the text of the source with them.

Replacement is not yet of the same type (a name for a name): that takes semantic
selection (see 5.7). Real words come from a bundled vocabulary of common
English words, or from the file that `--vocabulary` names; an empty file leaves
invented words only. The manifest names the vocabulary by its size and hash. `--keep-images` and `--keep-drawings` keep what would stop the
claim, and the file then has no `privacy` claim and no reading of its render.
`--keep-drawings` keeps outlines, codes and graphics exact, and `--keep-formulas` keeps
formulas as text, which is what `training` always does.

A `privacy` run needs Tesseract, to read the rendered pages (see 5.5). Without it the
run stops, because a check that did not look must not pass. The output's bytes depend
on its version too, because what it reads in the source joins the words to avoid.

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
| Geometry | exact where the oracle needs it; the rest is removed | exact, except the one variable under test | exact, except words moved inside a held line (see 4.4) | as `training`, except that outlines of text become text (see 5.5); rounding is an opt-in level |
| Text | equal-width swap | changed only as the family says | changed; labels updated with it | every word replaced (see 4.4) |
| Glyph shapes | removed unless the oracle needs them | exact | exact, or a metric-compatible substitute | substitute |
| Image samples | flat grey or noise | exact | exact, or a content-aware replacement | flat and light; by image class is planned |
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
- A replacement that moves words inside a held line (see 4.4) changes word
  boxes, so the word-box labels are rebuilt from the output as well.
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

- geometry: the output's word or line boxes, by the scope, stay within the box
  tolerance of the source's and keep the minimum gap (see 4.4); the CTMs of kept
  units are equal;
- coverage, where a profile sets `all`: every word in scope changed, and pdfium
  reads no word of the source on the page (see 4.4);
- image geometry: width, height, bit depth, colour space and filter are equal;
- structure: the tag tree has the same shape and the same marked-content IDs;
- labels (`training`): labels computed from the output equal the updated
  labels.

A mutation that breaks a kept signal is undone, as in the reduce mode. The
manifest records the profile, each kept signal with its oracle result, and each
changed signal with the mutators that changed it.

### 4.4 Word and line boxes

These settings control how far text that a mutator writes may move from the
source text. They are separate from the profile, and each profile has defaults.

| Setting | Values | Default |
|---|---|---|
| Scope | `word`, `line` | by profile |
| Box tolerance | `exact`, `contain`, `±d` | `exact` |
| Tracking limit | ems per gap between letters | 0 |
| Word count | `fixed`, `free` | `fixed` |
| Minimum gap | one value for words, one for lines | words 0.15 em, lines 0 |
| Coverage | `best-effort`, `all` | by profile |
| Word starts | `pinned`, `moved` | by profile |

**Scope** states which box the tolerance applies to. With `word`, each word box
stays within the tolerance of its source box. Text must fit word by word, so
only some replacements are possible. With `line`, only the line box is held. A
difference in one word can be offset by the next word or by the spaces, so words
move inside the line.

**Box tolerance** states how far each edge of a box may move, in points. Each
edge has two bounds: `out` (away from the centre of the box) and `in` (toward
it).

| Value | out | in | Use |
|---|---|---|---|
| `exact` | 0 | 0 | equal-width swaps; metamorphic pairs; a justified line |
| `contain` | 0 | any | replacement text that must stay inside the source box |
| `±d` | d | d | test what a target does when text moves up to d points in any direction |

A bound of 0 allows 0.01 pt for rounding. Edges are measured in the frame of
the line: along the baseline and across it. On a skewed scan, the axis-aligned
box of a word is larger than the word, so a tolerance on that box would allow
more movement than the user set.

A line box is the union of its word boxes. With scope `word`, if each word edge
moves at most d, each line edge also moves at most d, so the line box follows
from the word boxes. With scope `line`, the line box is checked directly.

The tolerance is a limit, not a target: a mutator can move text less than the
limit, or not at all. To make text move by a set amount, use a metamorphic
family with the shift as its variable (see 3.3).

**Tracking limit** states how far a mutator may change the gaps between the
letters of a word. It lets a near-width word fill a box exactly. The word box
holds, but the glyphs inside it move, so the geometry signal changes from
"exact glyph origins" to "exact boxes", and the manifest says so. A profile that
needs exact glyph origins keeps the limit at 0.

**Word count** states whether a line may change its number of words. It applies
only with scope `line`.

- With `fixed`, each output word is paired with the source word at the same
  place in drawing order, not by matching text. A mutator does not reorder text,
  and a changed count fails the check, so a changed space cannot pair the wrong
  words without being reported. The oracle reports how far each word moved. A
  line with one word has no spaces to absorb a difference, so it needs a
  near-width word and tracking.
- With `free`, the output words have no source words to pair with. Replacement
  text is less constrained, and a line with one word can become two. The oracle
  checks line boxes and the gaps inside the output, and reports the lines whose
  count changed. The word-level results are absent, not zero.

With either value, each source line maps to one output line, and the output must
group into the same number of lines. A space that grows past the line gap limit
of the tracker (2 em) splits the line and fails the check, so spaces need no
separate maximum.

A mutator absorbs a difference with `TJ` adjustments. `Tw` applies only to the
single-byte code 32, so it has no effect in a two-byte font.

**Minimum gap** states how close two boxes may come after the change. It has
one value for words in the same line and one for lines on the same page. Each
value is a distance in points or ems, or `any`, which turns the check off. Word
spacing and line spacing are different tests, so a user can set one and turn
the other off. Words in different lines are not compared: the line gap covers
them.

- The gap between two boxes is a signed distance. When the boxes are apart,
  it is the shortest distance between them. When they overlap, it is minus
  the overlap depth: the shortest move that would separate them. The boxes
  are compared as drawn, as rotated rectangles, so skew does not change the
  result.
- A pair must keep a gap of at least the minimum, or at least its gap in
  the source if that is smaller. Source files often have boxes that are
  closer than the minimum, or that overlap (superscripts, tight kerning,
  text printed over text). Such a pair is allowed, but it may not come any
  closer. With `free` word count, words have no source gap to compare, so the
  closest two words of the source line take its place. An unchanged line
  therefore passes under every setting.
- The word default of 0.15 em is the gap at which the tracker ends a word (see
  5.2). Words closer than that read as one word. The line default of 0 lets
  lines touch but not overlap, except where they overlapped in the source.
- With scope `word` and tolerance `contain`, the gap rule always passes: a box
  inside its source box is never closer to another box than the source box was.
  With scope `line`, the word gap always applies, because words move inside the
  line.

**Coverage** states whether every word must change.

- With `best-effort`, a word that has no fit keeps its source text, and the
  report gives the reason.
- With `all`, a word that cannot change stops the run, which names it. The user
  loosens the settings or removes the text. The file written so far must not be
  used.

A word is in scope when it holds a letter or a digit, or a glyph whose text the
font does not give. Punctuation alone is out of scope. A word counts as changed
only when at least half of it differs by edit distance, with at least one change,
so `Smith` to `Smyth` and `2024` to `2025` do not pass. A replacement is never a
word of the source document, nor a part of one split at punctuation.

Under `all`, a word goes down a ladder until something fits:

1. a real word from the vocabulary;
2. an invented word: each letter replaced by one of its case and of its kind
   (vowel or consonant), each digit by a digit;
3. a letter swap: the same, with any letter;
4. a swap with any letter or digit, whatever its case or kind: a word of narrow
   capitals, such as `III`, has no capital as narrow;
5. a swap for a word that the source draws tighter than its letters' widths, as a
   formula does: each new letter starts where the source's did;
6. a substitute font with more glyphs (see 5.3), and the ladder again;
7. a stop.

The manifest counts the words at each step, because invented words and swaps read
as nonsense: they keep the file private, but they are not the plausible content
that `training` asks for. A letter in steps 2 and 3 has no one fixed partner,
because a fixed partner is a substitution cipher, which letter frequencies break.

**Word starts** state what the distance to the next word shows.

- With `pinned`, each word keeps its start. The distance to the next word then
  still shows the width of the source word, whatever word now stands there.
- With `moved`, words are chosen without regard to the width of the source word,
  and the gaps take up the difference, each in proportion to its room. It applies
  only with scope `line`. A gap wider than 0.75 em is structure, such as the space
  between table cells, and stays where it is, so that words do not leave their cell.

Changing every word and hiding the source are separate properties. A file can pass
coverage and still show the width of every word. Under `moved`, some width still
shows: a line with one word, a stretch whose gaps have no room, and a number, whose
digits are often all one width.

The text-position tracker computes the boxes from font metrics (see 5.2).

| Profile | Scope | Box tolerance | Tracking limit | Word count | Minimum gap | Coverage | Word starts |
|---|---|---|---|---|---|---|---|
| `bug-repro` | not checked, unless the user sets it | | | | | `best-effort`: the oracle must be able to keep the trigger, and what stays goes in the evidence | `pinned` |
| `metamorphic` | `word` | `exact`, unless box position is the variable | 0 | `fixed` | defaults | does not apply: text changes only as the family says | `pinned` |
| `training` | `line`; `word` is allowed | `exact`; `contain` and `±d` are allowed because labels are rebuilt from the output | 0.02 em | `fixed`; `free` is allowed | defaults | `best-effort`; `all` is allowed | `pinned`; `moved` is allowed |
| `privacy` | `line` | `±1`; `exact` is allowed; at level 2, at least `±` half the grid step | 0.02 em | `fixed`; `free` is allowed | defaults | `all` | `moved` |

`privacy` uses `line` and `moved` because a replacement that keeps the width of
each word narrows down what the original word could have been. Its tolerance is
`±1`, less than a letter, because at `exact` a lone digit whose width no other glyph
of its font has cannot change: on the sample, one such stopped a receipt. On the sample, the
system dictionary holds a median of 83 words within 0.005 em of a source word's
width, and 10 or fewer for one word in five.

`all` makes scope `word` impractical: exact word boxes leave most documents with
words that cannot change. It is allowed but stops on most files.

A line that no replacement can fit is left unchanged and reported, as with any
mutation that breaks a kept signal (see 4.3). Under coverage `all`, it stops the
run.

The manifest records every setting with the geometry oracle result: the largest
edge movement found for lines and, with `fixed` word count, for words; the
largest tracking used; the smallest word and line gaps found; the number of pairs
that broke the gap rule; and the number of lines left unchanged or with a changed
word count. It also records the coverage result: the words in and out of scope,
the words changed, those at each step of the ladder, and the share of words still
at the source's distance from the next word.

The defaults come from a measurement on 12 sample documents (at most 5 pages
each; 3,774 words made only of letters, in one font, with `/ToUnicode`). It asked
whether each word had a different real word that the page's font could draw and
that fit the word's box. Fit was not checked for grammar or meaning, so sensible
text will fit less often.

| Fit | Words with a fit: documents' own 1,384 words | Whole lines | Words with a fit: 63,849-word dictionary | Whole lines |
|---|---|---|---|---|
| Exact width | 49% | 27% | 85% | 61% |
| Tracking up to 0.01 em per gap | 90% | 69% | 97% | 88% |
| Tracking up to 0.02 em per gap | 95% | 84% | 99% | 96% |

With scope `line`, words within one letter of the source length and the spaces
absorbing the difference, a random word changes each space by a median of about
0.2 em and at the 95th percentile by about 0.8 em, which is more than the space
itself (typically about 0.25 em). Choosing the best of 30 candidates against the
running difference lowers those figures to 0.002 em and 0.07 em. So `line` needs
a mutator that chooses words with the line in mind. About 10% of lines have one
word and no spaces. Whether 0.02 em of tracking is visible has not been checked
with renders yet.

**Possible later:** segments that span several lines, so that text can reflow
between lines inside a held region such as a paragraph or a table cell, and
sensible alternatives to the source text in place of words chosen only by fit.
Both would use the same settings on a larger box. They are not designed yet, and
the one-line-to-one-line rule above stays until they are.

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
have `/Widths` or are of the Helvetica, Times or Courier families, Type3 fonts (through their `/FontMatrix`), and composite fonts
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
Text comes from `/ToUnicode` (`bfchar` and `bfrange`) and, in a simple font, from
the encoding: a named base encoding and the glyph names of `/Differences`, read
through the Adobe Glyph List, or else TeX's names for the glyphs of its math fonts, with
sizes such as `parenleftBig` read as their base. A Type1 font with no named base uses the
encoding built into its program: the `dup 65 /A put` lines of a Type1 program's clear text,
or a compact (CFF) program's encoding. TeX's fonts often rely on it, with neither
`/Encoding` nor `/ToUnicode`, and a glyph without text draws nothing in a substitute font. A
name that says nothing, such as `g12`, gives no text. Each box is a rectangle in the frame of its line, as 4.4
requires, and a line box is the union of its word boxes.

A standard font may leave out `/Widths`, since every reader knows them. For Helvetica,
Times and Courier, `standard_fonts.py` gives their widths by character, through the
font's encoding, or the standard encoding if it names none; a name such as `Arial,Bold`
counts as its family. The widths come from the Liberation fonts, whose advance widths
match these families by design, and `scripts/standard_widths.py` writes the table again.
Five symbols where Arial and Times New Roman differ from Adobe's fonts (`¯ ± µ · ÷`) take
Adobe's widths. pdfium's built-in fonts agree on all 216 characters it measures in each
of the six tables, apart from `«` and `»` in Helvetica and `@` in Helvetica-Bold.
Symbol and ZapfDingbats with no widths still stop a run.

On the sample documents, 99% of the words match pdfium's exactly. In the rest,
pdfium joins words hyphenated across lines, expands ligatures or leaves out a
space, or the font's `/ToUnicode` maps a hyphen to U+0000. The sample has no gaps between 0.05 and 0.3 em, so it
confirms the word gap of 0.15 em but does not pin it down; that value comes from
typography (kerning stays under about 0.1 em, word spaces rarely fall under
0.18 em). Known limits: a font with no text for a space at an unknown code runs
words together, letter spacing (`Tc`) above 0.15 em splits a word into
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
- **Fitted replacement** (built: `fit.py`): writes other words into a line under
  the settings of 4.4. New codes flow from the start of the word. Every instruction
  that held a part of the word begins by moving the pen to where the flow needs it
  and ends with the pen where the source had it, by `TJ` numbers if needed, so
  nothing else moves, however it is positioned. With word starts `pinned`, an inner
  word must leave the minimum gap, and a word whose end is held fits within the
  tolerance, with tracking if the limit allows it. With `moved`, the gaps of each
  stretch between wide gaps take up the difference. The output goes through the
  geometry oracle, and a line that fails returns to its source text. Words are
  chosen by fit, not by meaning, so the vocabulary is curated: a general dictionary
  puts unsuitable words into the output. The bundled one (`data/vocabulary.txt`)
  holds SCOWL's American and English words up to level 35, the common words of a
  small dictionary: 38,937 of them, in lowercase only, so no proper name,
  abbreviation or possessive, and none of the LDNOOBW English list of offensive words
  or their simple inflections. The filter also removes a few harmless words, such as
  butter, and cannot know every slur. `scripts/vocabulary.py` makes the list again.

  Under coverage `best-effort`, a word keeps its source text, with the reason in the
  report, when its font gives no text, it holds no run of two or more letters, it is
  in a form XObject, it mixes fonts or sizes, or its line is printed over another
  line. Under `all`, each of these changes. A form gets a copy for each drawing with
  text, because other drawings and pages can share it. A line printed over another
  with the same text takes the same new text. A word in several fonts changes letter
  by letter, each letter in its own font, and its letters flow across the change of
  font. A glyph with no known text is swapped for another code of its font.

  Some converters set letter spacing (`Tc`) and take it back with a `TJ` number after
  each letter, so the letters sit as if there were none. When most letter pairs of an
  instruction sit so, within 0.05 em, the new letters get the same number between them;
  without it each new letter would stand apart as a word of its own. Spacing that is not
  taken back, as in a spaced-out heading, stays.

  On 38 sample pages with 6,954 words in scope, a system dictionary and scope `line`
  with 0.02 em tracking, no page failed the geometry oracle:

  | Settings | Words changed | Pages stopped | Words at the source's distance from the next |
  |---|---|---|---|
  | `best-effort`, `pinned` | 68.5% | not applicable | 100% |
  | `all`, `pinned`, no font source | 99.57% | 18 | 100% |
  | `all`, `moved`, Liberation fonts | 99.94% | 4 | 11.4% |
  | the same, tolerance `±1` | 100% | 0 | not measured |

  Under `all` and `moved`, 70% of the words changed were real words, 29% invented
  and 1% letter swaps. pdfium reads 40,517 of the 40,519 glyph origins within 0.2 pt
  of the tracker. The 4 words left are a lone `1` at a held line end, with no
  glyph of its width, in a font that cannot take a substitute, and three narrow words
  (`1.0`, `III`, `III,`) whose stretch has too little room for any wider letters.
  31 lines kept their word starts because their gaps had too little room. One further page cannot be
  read at all, because a font relies on built-in metrics (see 5.2).

  Since then, the narrow words take any letter, and the standard fonts have widths. On
  12 sample files as whole documents under `privacy`, 10 pass at `exact` and 11 at `±1`,
  up from 8 and 9; the receipt with the lone `1` passes at `±1` only. The one that
  stopped is a paper whose displayed formulas draw a fraction's parts as separate slanted
  glyphs, in a math font of a few letters: its formulas now become blocks (see 5.5).
- **Glyph availability** (partial): fitted replacement uses only codes that the
  document shows in that font, which needs no font parsing but leaves out glyphs
  a full font could draw. Planned: subset fonts lack glyphs that the document
  never used. fontTools reads the embedded program to tell which codes have
  glyphs, so a replacement uses only codes that render.
- **Metric-compatible substitution** (built: `substitute.py`): replaces the
  embedded program with an open font: Liberation Sans, Serif and Mono (for Arial,
  Times New Roman, Courier New), Carlito (Calibri), Caladea (Cambria), or else a
  Liberation font by the font's flags, which changes the look but not the layout.
  Under coverage `all`, a font of a word that could not change gets one, if a font
  source is given. It keeps every code the document shows, with its text, advance
  and height, and adds the letters and digits it lacks at codes it does not use,
  with widths scaled to the source font. A Type3 font becomes a TrueType font, its
  widths converted through its `/FontMatrix`. A font with a shown code of unknown
  text, or a composite font without Identity-H, stays. `/Widths` or `/W` and
  `/ToUnicode` are rewritten. The subset keeps the copyright and license entries
  and drops the font's names, because the OFL reserves some names for unmodified
  fonts. This also removes the source's font program, which the copyright and
  privacy claims need, but so far only for the fonts that needed more glyphs.

A page under coverage `all` gets four attempts, each with other words, because whether
a tight line has room for its last word depends on the words before it. A line that
the geometry oracle refuses keeps its word starts in the next attempt, and its box may
not grow: the words of a formula lie too close to move, and its lines can overlap. When
lines join or split, the oracle names no line, and the fitter finds one whose words
change the count on their own, by halving, instead of putting back the whole page.

A slanted letter's box leans past its width, so the fitter measures a glyph's scale
along the bottom of its box, and a word's end at the box that reaches furthest, which
need not be the last one: in a formula, a slanted letter can lean past a small one after
it. A word whose gap to the next is tighter than the minimum, as between slanted words
whose boxes overlap, may end as far as the source's did, within the oracle's rounding. A substitute draws a math letter as its plain letter.
A symbol that it lacks, or a code whose text is not known, draws nothing and keeps its
advance. A letter that it lacks refuses the substitute, so that a font of another
script does not lose its text.

### 5.4 Images (built, basic)

Samples are replaced at the same geometry. Planned: choose the treatment by
the image's class (logo, signature, photo, chart, scanned text) from Docling's
picture classification; for scanned text, replace the text in the image, not
only the pixels.

Under `privacy`, every image becomes flat and light at the same size, depth and colour
space, and a mask becomes empty: its shape can show a logo or a signature. An image with
a soft mask that no line of text crosses loses the mask instead, and is a block like any
other image. Otherwise an icon or a logo with a see-through background would vanish, and
on the sample so did a whole card. One that text crosses keeps its empty mask, and shows
nothing, so that it hides none of the text under it. So does one whose palette holds only dark
colours, since its mask is its shape: on the sample, five black icons became black squares
that the render difference passed, at 0.09% of a page.
The fill depends on the colour space, because a sample counts ink in CMYK, where the
number that is mid-grey in RGB is close to black. An image inside a soft mask counts as a
mask too. Its brightness says where a fill shows through, so a light one lets the fill show
everywhere: on the sample, it spread a shadow into a grey block across a whole card, which
every oracle passed until the render difference.

### 5.5 Vector graphics

The reduce mode keeps the paths that the oracle needs, exactly. The `training`
profile keeps paths too, outlines of text among them, because a model should see
what the source draws. `--keep-drawings` asks the same of `privacy`, and gives up
its claim.

Some producers draw all text as outlines, which the tracker cannot see. On such a
page every check of the words passes without having looked at anything. So under
`privacy`, each outline item becomes a paragraph of new text (`outlines.py`), the one
exception to holding path coordinates. An item is what a person would read as a run
of text:

- Its shapes are filled, of one colour, written in one direction, and side by side on
  a line, with gaps of at most their height. They need not be drawn in that order,
  and a clip around each short run does not divide them: producers do both.
- A shape is glyph-sized: under 72 pt high, and if it has one subpath or only boxes,
  under 72 pt wide and at most 2.5 times as wide as it is high.
- The shapes include one that a box is not: a curve, or a polygon of 6 points or
  more, as an H or a T has. Letters with no curves go with their word, and two bars
  are just two bars.
- Alone, a fill is an item only if it has 3 subpaths or more and is not all boxes. It
  can be a word; with fewer it can be a bullet, a ring or a check box.

The sizes come from the same 12 files: shapes of glyphs are almost all under 40 pt, and
backgrounds above 150 pt.

The paragraph is the item's width, by horizontal scaling, and takes its height from the
item. Its words are new: the number of letters follows from the width, and word breaks are
random, so that they say nothing of the source's. Each is marked as a paragraph of its own.

The shapes go, with any group left painting nothing, because a transform for each letter
records its advance, and an advance can identify a letter. A paragraph is drawn where the
item's last shape was, so that what covered the shapes still covers it, but outside the groups
open there, which open again after it. A clip for each short run would otherwise cut it down
to one run.

What stays, and the oracle `drawn-text` runs on the saved file:

- No item is left, and no page keeps more than 100 curved shapes. These would be too
  large to be glyphs. The count says that someone must look, not what is there.
- A lone glyph stays, because it spells little and a bullet looks like one.
- Curves drawn with strokes, such as a signature, stay. So does text used as a clip
  for a picture.

The manifest counts the paths that remain, and only a reviewer can say what they show.

**Machine-readable codes** (`codes.py`). A code stays readable when every word around
it has changed, and it can hold a URL or an ID, while OCR does not read one. It is
recognizable by what draws it: filled boxes of one colour, at least 15 of them and close
together, whose sides are whole multiples of one module of at most 8 pt. A matrix (a QR
code or a Data Matrix) is nearly square, at least 18 modules wide and 25 to 75% ink, with
its boxes on the module's grid. A barcode has bars of one height, at least 3 times as
tall as its widest bar, with bars and gaps of at most 6 modules. Each becomes a flat, light
block of the same size, and the oracle `machine-codes` looks for any left on the saved
file. On the sample this finds the one QR code, which still decoded in an earlier
output that had passed every other check, and nothing else, among tables and charts.
It does not find a code of round dots, which is an outline item instead; a skewed code; or
one in several colours. A code in an image goes with the image.

**Graphics** (`graphics.py`). A logo says whose document it is, whatever its words say. And
the symbol on an icon's badge looks like a short word to the outline items, which made two
letters of each one on the sample. So under `privacy`, each compact graphic becomes a flat,
light block of its extent, the grey of a flat image. A graphic is a group of marks within
2 pt of each other: fills, strokes and shadings, and the images among them. Fills, strokes
and shadings of two colours or more must draw it, and it must be 12 to 144 pt on its longer
side. These stay:

- a graphic that a line of text crosses: a block must not hide the source's words, which
  the run changes where they are;
- a mark over 144 pt, such as a card or a background, and a straight one no thicker than
  1.5 pt, such as a rule. Neither joins anything, so the line that links a column of icons
  does not make them one tall group;
- a group over 144 pt, such as a table, a banner or a chart, and one under 12 pt, such as a
  check box or a bullet;
- a group of one colour: an outlined word, and also a logo of one colour;
- what a soft mask or a pattern draws. The one is a shape, not a picture; the other repeats.

An outline item more than 1.25 times as wide as it is high is a word: it joins no graphic,
and becomes a paragraph. On the sample, words are 1.4 times as wide as high or more, and
symbols and emblems 1.1 or less. A symbol joins its badge. Images add no colour, because
each is flat already, and they stay on the block. Only what a clip let show counts, and
groups whose extents touch become one, because a block covers the corners of its extent.

A block is drawn where the graphic's first mark was, outside the groups open there, so a
label drawn on a button stays on its block. A paragraph drawn before a graphic that it
reaches into can be partly covered: on the sample, the word left of a logo's X. The oracle
`drawn-text` looks for graphics left on the saved file, and the render difference lets a
block get lighter than what it covers, but not darker.

On the sample, 22 graphics in 4 files become blocks: logos, icons on badges and a few buttons,
and the lesson plan's outline items fall from 19 to 11, since its icons no longer become
letters. 20 images in 2 files become solid. The same files pass and stop as before, under
`training` and `--keep-images` too. A logo of one colour stays, and so does an icon of one
colour, which can still become a letter. A block keeps a graphic's size and place.

**Formulas** (`formulas.py`). A displayed formula says which paper it comes from as surely
as its title, and swapping its letters keeps its shape: a fraction over a sum, its
subscripts and brackets. Nor do its letters swap well: a math font holds the few letters
that the formula uses, and the parts of a fraction are lines of their own, which join when
anything in them moves. So under `privacy`, before any word is fitted, the glyphs of each
formula go, with the pen left where each word left it, and its words join those that no
new word may be. A light block covers where it was, drawn last over the page, so that no
graphic takes it into a plain block of its own.

The block has the formula pattern: plus signs of grey 0.78, 3.2 pt across, on a grid of
12 pt staggered by half a step, over grey 0.9, anchored at the page's origin. It labels the
block as a formula without any text, for a tool or a reviewer that knows it; the pattern is
in this public repository, so it is a convention, not a secret. Other kinds of block may
take patterns of their own.

A formula line is one whose glyphs are 40% math or more, at least two of them: math letters,
Greek letters, operators and arrows, or glyphs of a math or symbol font. It needs a term of
math, a letter or digit, so a line of vector arrows over letters starts none; and a line with
six letters or more outside math is a sentence, however much math it holds. Lines whose bands
(0.2 em below the baseline to 0.7 em above) overlap a formula line's, or come within 1 em of
it, go with it: the parts of a fraction, a large bracket, an equation's number.
A block reaches 0.3 em below and 0.9 em above, and stops short of the band of any other line;
a formula whose block that leaves under half its height is too close to text to cover, and
goes to the fitter. A formula in a form stays, since the form can be drawn elsewhere too.

In a line of text, a word whose glyphs are half math or more is a formula of its own, with a
small block, when a letter of math in it is of a font that shows fewer than 8 letters in the
whole document: the fitter would have too few to swap. An arrow used as a bullet, or a sign
alone, is not. A math font that TeX uses for all the variables in the text shows more, and the
fitter changes its words like any other. A word of math that the fitter cannot change, such as
a letter under a bar, would stop the run; when every word left on every page is one, the run
starts again from the source with those words as formulas too, up to twice more.

The geometry and coverage oracles compare the source without the formulas' glyphs, grouped
again as the output is, with the output. The oracle `formulas` looks for any glyph that
starts inside a block, and the render difference lets a block get lighter than what it
covers, but not darker. `--keep-formulas` keeps them as text, and gives up the claim.

On the sample, one paper of 42 pages has 31 displayed formulas and 7 words of math in its
text. With them as blocks, it passes every check: its other 10,746 words change, 89% of
them to real words, and the reader finds no source word.

**Reading the render** (`reading.py`). Every other oracle reads the file's operators and
objects, so what they cannot see they pass, and three times a page has passed them with
source text in plain view. The oracle `readable-text` looks at the picture instead: it
reads the rendered pages of source and output through Tesseract, at 216 dpi, and stops
if a word of 4 letters or more:

- is a word of the source, anywhere in the document, from either the reader or the file,
  unless it is part of a new word that the run wrote there: the reader splits a long word
  now and then, and a new word can hold a short one, as `bookshops` holds `book`. A word
  that the run left unchanged is a source word, so it is never new;
- or is at least 80% part of a word that the reader saw at the same place in the source.
  This is what is left of a word when only some letters changed. A fragment elsewhere
  is chance, and common in new words, but layout holds, so here it is not. The fitter
  keeps to the same rule: no new word or run of a swap is mostly part of a source word
  on its line, and no run between punctuation is a source word, as a year in a range
  could be.

Two words are at the same place when their boxes share half the height of the shorter one,
and lie within 0.4 of that height of each other along the line. Readers draw boxes of their
own size: docTR's and RapidOCR's are some 12 pixels taller than Tesseract's, and with a
margin of fixed pixels they reached the next line, where a new word that shares 4 letters
with a source word below it looked like what was left of it. On the 29 maths papers that
pass, that was 22 of the 33 matches those two readers found.

A reader that sees nothing where the pages show at least 20 words also stops the run, so
that a broken one, or a blank output, cannot pass. Words stay out of the manifest, as
everywhere: only their counts go in.

Passing is evidence, not proof. OCR misses small and stylized text and words of under 4
letters, and it cannot tell a logo from a drawing, so review stays.

### 5.6 Hidden layers (built: `layers.py`)

Text lives in more places than the content stream. Producers add private keys that
no list can know (Acrobat Capture's `/CAPT_Info` holds a file path), so the cleaner
denies by default. It walks everything reachable from the trailer, and:

- rewrites any string not known to be data. Only `/DA`, `/Registry`, `/Ordering`,
  `/Lang`, `/Panose` and an indexed colour table are known to be data;
- removes any stream not known to be part of drawing a page;
- keeps only the listed keys of the catalog and of each page.

| Layer | Where | What happens |
|---|---|---|
| Structure tree | `/Alt`, `/E`, `/T`, `/ID` of structure elements | rewritten |
| | `/ActualText` | removed: a rewritten one would not match the glyphs, which then speak for themselves |
| | custom role names (a role name can hold text) | replaced by the standard role that they map to |
| Forms | field values `/V`, `/DV`, tooltips `/TU`, names `/T`, options | rewritten |
| | check boxes, radio buttons, chosen indexes, signatures | cleared: what a person chose or signed |
| | XFA | removed |
| Annotations | `/Contents`, `/T`, `/Subj` | rewritten |
| | `/RC`, `/M`, appearance streams that show text | removed; a widget asks for a new appearance |
| | files, stamps, sounds, handwriting and other media | the annotation is removed, and the tags let go of it |
| Navigation | outline titles, page label prefixes | rewritten |
| | named destinations (`cite.smith2018`) | resolved to their page |
| | web addresses, scripts and other actions | removed |
| Metadata | document info, XMP, `/PieceInfo`, thumbnails, dates, file identifier | removed |
| Other | embedded files, JavaScript, marked content properties (see 3.1) | removed |
| | optional-content group names | `Layer N` |

A rewritten string keeps its shape: letters stay letters and digits stay digits. The
same string always becomes the same string, so that a form value still matches its
options and a table header still matches its cell. It uses no word of the source, in
any layer.

The tag tree keeps its shape and its marked-content IDs, which an oracle checks.
That does not make the file conform to what its source did: the XMP that declares
PDF/UA or PDF/A goes with the rest, since no validator has checked the output, and a
file should not claim what nothing has checked.

The manifest does not name the source's file, which can name a person. It does hold
the source's hash, because a derived file must name its source: whoever holds both
files can link them.

Names are not rewritten: a font's name, or a check box's state, can still say
something. The fingerprint report lists the fonts that stay.

### 5.7 Semantic selection (planned; not needed for coverage `all`)

When every word changes, nothing depends on finding what is sensitive, so the
`privacy` profile does not need this. It matters for realistic output (a name for a
name, a date for a date), for `training`, and for any future mode that changes only
what is sensitive, where a miss is a leak.

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
| geometry: the word and line box settings of 4.4 (`geometry.py`) | built |
| coverage: every word in scope changed, by the tracker's words (`coverage.py`) | built |
| residual text: the words of the source that pdfium still reads on the page (`coverage.py`), which shares no code with the tracker | built |
| hidden text: no word of the source, in any layer, in any string of the output (`layers.py`) | built |
| other layers: what must be gone is gone, including forms with text that no page draws and text in patterns (`layers.py`) | built |
| file identifier: not the source's | built |
| structure: the tag tree has the same shape and the same marked-content IDs | built |
| image geometry: each page draws images of the same sizes and depths. The filter can change, under `privacy`: a flat image is Flate | built |
| drawn text: no outline item or graphic is left, and no page keeps so many curved shapes that someone should look (see 5.5) | built |
| machine codes: no QR code, Data Matrix or barcode drawn as paths is left (see 5.5) | built |
| formulas: no glyph starts inside a formula's block (see 5.5) | built |
| readable text: OCR of the render reads no source word in the output, and has read something (see 5.5) | built |
| render difference: pypdfium2 renders source and saved file, and what a run does not change must look the same (see below), with per-page `compared`, `changed_pixels_ratio`, `mean_abs_error`, `darkened` and `lines_lost` | built |
| render difference between two engines | planned |
| text-cell difference between two runs or two engines | planned |
| invariant check for metamorphic families | planned |

The anonymize mode runs the oracles above on the saved bytes, not on the file in
memory. Most of them read operators and objects, not pictures. On the sample they all
passed a page that a flat image had turned black, one where a word crossed a table
border, and one where an image in a soft mask spread a grey block across a card.

**The render difference** (`difference.py`) compares the pictures at 144 dpi. It leaves out
the places that the run means to change, all taken from the source: the lines of text, with
a place set by their font size because a font's own numbers can be wrong; outline items;
machine-readable codes; annotations that show an appearance; and images and graphics.
Whatever a run keeps stays in the comparison, so under `--keep-images` not a pixel of an
image may change.
It stops the run when:

- more than 100 pixels of a page changed by more than 32 grey levels outside those places.
  New text that strays out of the source's places counts, because the output's own places
  are not left out;
- more than 1% of a page got darker by more than 64 levels where an image or a graphic was.
  Those places are left out of the first measure, so a black image would otherwise pass.
  Getting lighter is what a flat image and a block do;
- a line of text keeps under 20% of its ink, unless it is over an image. White text on a dark
  picture cannot stand out on a flat, light one.

On the 9 sample files that pass, the most any page changed is 19 pixels, and the most that
got darker is 0.011% of a page. A soft mask left light stops one of them at 10%, and black
images stop two others, at up to 45% of a page.

It does not see what stays inside a place: a word that crosses a border within its own line,
a lost logo in a line's place, or how a replacement looks. On a page full of text and images
it compares little, as low as 19% of a page on the sample, and the manifest records this as
`compared`. Review stays.

The command oracles run the target in a child process. A target is any command with
`{pdf}` and `{page}` placeholders, so Java, Rust and C++ tools work as well as
Python ones.

### 5.9 Evidence (built, partial)

Built: words shared with the source page (pypdfium2 text), source images kept
(sample hashes), source font programs kept (program hashes), render dHash
distance, and a count of the remaining units by kind. Planned: SSIM, n-gram
overlap, region crops for reverse image search with the original as a positive
control, and a side-by-side render for the reviewer.

### 5.10 Manifest and claims (built: schema 0.2)

The manifest records tier, source, fonts, mutations, generator, oracle results,
family, evidence, attestation and takedown contact, and enforces the corpus
policy (see `CORPUS-LICENSES.md`). Schema 0.2 adds:

- `claims`: a list of `copyright`, `training` and `privacy`. A `privacy` claim needs
  the `privacy` profile, every oracle of 5.8 that the mode runs, a fingerprint report,
  evidence and a reviewer's privacy statement. A `training` claim is refused until
  there is a labels oracle;
- `profile`, with `settings`, `preserved` (each kept signal and its oracle) and
  `changed` (each changed signal and its mutators);
- `fingerprint` (`privacy` only): the normalization level, which is 0 for now, and
  the fingerprint report: page sizes, image sizes, the number of distinct transforms
  and painted paths, and the fonts that stay.

A manifest from the anonymize mode is a draft until a reviewer attests. A font
program that stays, from a source that may not be redistributed, breaks the corpus
policy, so that draft cannot be attested.

### 5.11 Attestation

A claim that needs judgment needs a named reviewer, a checklist version and a
statement version (`docs/ATTESTATION.md`). The copyright statement exists as a
draft, and so does the privacy statement. CI refuses corpus files whose claims lack the required review.

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
pydantic (MIT), fontTools (MIT), Pillow (MIT-CMU). Planned optional: docling (MIT), presidio (MIT),
Faker (MIT). Substitute fonts are not shipped: they are read from the system or a
directory the user names, under their own licenses (OFL for Liberation, Carlito
and Caladea). No dependency's code is copied into docmutant's files.

Data shipped: the vocabulary, a modified version of SCOWL under its permissive
notice, which `data/vocabulary-copyright.txt` holds with a note of the change; and
the widths of the standard fonts, measured from the Liberation fonts. The offensive
word list used to filter the vocabulary (LDNOOBW, CC BY 4.0) is not shipped.

## 9. Roadmap

| Milestone | Content | Status |
|---|---|---|
| M1 | Manifest, policy validation, CLI | done |
| M2 | Reduce mode for rendering bugs, crash/hang oracles, basic evidence | built; first real-document runs in progress |
| M3 | Text-position tracker; render-difference oracle; geometry (with the settings of 4.4) and image-geometry preservation oracles | built; the render difference compares source and output in one engine, not two |
| M4 | Font glyph availability and metric-compatible substitution | substitution built for fonts that need more glyphs; glyph availability from the font program next |
| M5 | Hidden layers; anonymize mode; profiles; fingerprint report; schema 0.2 | built; privacy normalization levels 1 and 2 are not |
| M6 | Fuzz and metamorphic modes; corpus packaging | |
| M7 | Semantic selection with Docling and a PII detector; replacement by type | |

## 10. Open questions

- Will the maintainers of corpora (docling-parse's regression dataset,
  opendataloader-bench) accept derived files as evidence, and with which claims?
- Counsel's review of `CORPUS-LICENSES.md` and the attestation statements.
- Text in images: which OCR to use for evidence and replacement, without making
  the core depend on a model.
- Tolerances for a render difference across renderers. Today's compares one engine with
  itself, where clean pages differ by almost nothing, so its limits say little about two.
- Whether the limits of the render difference are right. They come from 12 files, and the
  100 pixels, the 1% and the 20% have only met the damage those files could show.
- How much measurement difference to allow, on top of the box tolerance, when
  a text engine (pypdfium2 or the user's target) reports the word boxes
  instead of the tracker. A fixed allowance per glyph is not enough: an
  engine that rounds widths (pdfium truncates them) drifts further from the
  tracker with each glyph until the next positioning operator.
- How much tracking stays invisible. The default of 0.02 em per gap is an
  estimate. The render difference cannot test it: tracking stays inside a line's place.
- What counts as an outline item. Its sizes and limits come from 12 files, of which
  two draw all their text as outlines. A drawing of small shapes can pass for text.
- Whether 100 curved shapes is still the right limit. It comes from the same files, and
  now counts only what is too large to be a glyph.
- Whether paragraphs should hold a font's weight. All of them are one regular font.
- How much of a leak reading the render misses. On the 3 sample files with outlined text, it
  reads a leak in proportion to what stays, but short words and small print can go unread.
- Whether to read languages other than English. Tesseract runs with its default model,
  which reads other Latin scripts less well and other scripts not at all.
- What else could count as a code, such as a PDF417 stack of unequal rows.
- What counts as a formula. The limits come from one paper: a line half math, a word half
  math, bands of 0.2 to 0.7 em, 1 em between parts. A table of Greek-lettered values would
  become blocks too, and a formula set in an ordinary italic font, as many are, is not found
  and goes to the fitter like any text.
- Whether `±1` is the right tolerance for `privacy`. It lets a lone digit change, and no
  sample file needed more, but a line box that moves by a point is less exact than before.
- What counts as a graphic. Its limits come from 12 files: 12 to 144 pt, two colours, 1.25 for
  a word. A logo of one colour stays, and so does one under 12 pt, while a compact chart of
  two colours becomes a block. A block keeps a graphic's size and place, which can still say
  something to someone who knows the source.
- How wide a gap counts as structure. The 0.75 em for holding a gap under word
  starts `moved` is an estimate: a justified word space can come close to it, and
  table cells can be closer.
- Vocabularies beyond common English: other languages, and words by type (names,
  places, units) for replacement of the same type (see 5.7). The bundled list is
  English only, so a document in another language gets English words.
- How to model text-in-scan replacement so that OCR training data keeps the
  noise and skew statistics of the original scan.
