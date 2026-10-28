# Corpus license policy

docmutant's code is under the MIT License. The documents it produces are not
software, and each one carries its own license, recorded in its manifest. This
page explains the policy that `docmutant validate` enforces.

This is not legal advice. The policy is a draft until counsel has reviewed it.

## By source

| Source | Allowed? | License of files made from it |
|---|---|---|
| None (synthetic file) | yes | your choice; CC0-1.0 recommended |
| Public domain or CC0 | yes | your choice; CC0-1.0 recommended |
| CC-BY | yes | CC-BY, with the source's attribution line kept |
| CC-BY-SA | yes | CC-BY-SA; keep these files apart from the others |
| Any NC or ND license | **no** | ND forbids changes; NC blocks commercial users |
| All rights reserved, or unknown | the source itself is never shared | a derived file only, and only with evidence, a signed attestation and a takedown contact |

## Why CC0 for synthetic files

It is unclear whether a file that a program generates has an author in the
copyright sense. CC0 settles the question: whatever rights exist are waived, and
the file can go into any corpus.

## Fonts

A PDF's embedded fonts have their own licenses, separate from the document's.
The manifest records each font program and what happened to it. In a file
derived from a restricted source, a font program may stay only if it has a free
license (for example OFL-1.1). Otherwise it must be replaced or removed. Fonts
under the OFL with a Reserved Font Name must be renamed when they are changed.

## Derived files from restricted sources

The claim for such a file is that it **keeps no protectable expression** from
its source, so that it is not a derivative work. The claim needs human judgment
for every file:

1. docmutant records automatic evidence: what remains (for example one path or
   one image placement), what was replaced, and similarity measurements.
2. A named reviewer checks the file against a fixed checklist and signs the
   attestation in [docs/ATTESTATION.md](docs/ATTESTATION.md).
3. The manifest names the rights holder of the new file (normally the
   organization that made it) and a takedown contact.

The source document is never published. Its manifest entry holds its SHA-256
and, where public, its URL, so that anyone who has the original can verify the
relation.
