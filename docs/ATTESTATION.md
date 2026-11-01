# Reviewer attestation

**Status: draft 0.1, pending legal review. Do not rely on this text yet.**

A file that docmutant derives from a source that may not be redistributed needs
a signed attestation by a named reviewer. The manifest's `attestation` field
records the reviewer, the date, the checklist version and the statement version
below.

## Statement (version 0.1-draft)

> I reviewed the file named in this manifest, its recorded evidence and a
> side-by-side render with its source, against checklist version
> `<checklist_version>`.
>
> In my good-faith judgment, the file keeps no protectable expression from its
> source: no text, images, drawings, font programs or other creative content of
> the source remain, apart from the functional elements listed under
> `evidence.kept`.
>
> This statement is my opinion, made in good faith. It is not a warranty.

## Checklist (version 0.1)

1. The text layer has no words, names or numbers from the source.
2. Every image has replaced samples. No logo, photo or drawing of the source
   can be recognized in the render.
3. Every vector drawing that remains is a functional fragment (for example one
   path that triggers the bug), not a recognizable illustration.
4. Every font program is replaced or removed, or has a free license.
5. Metadata, XMP, outlines, annotations, attachments, JavaScript, form values
   and thumbnails are removed.
6. If a reverse image search was run and it found the original (positive
   control), it found neither the file nor any searched region of it.
7. The oracle still gives the result that the manifest claims.

## Privacy statement (version privacy-0.1-draft)

A file with a `privacy` claim needs this statement instead. It is for what the oracles
cannot see: they do not read pictures or drawings, and they do not know what is
personal.

> I reviewed the file named in this manifest, its recorded evidence, its fingerprint
> report and a side-by-side render with its source, against checklist version
> `<checklist_version>`.
>
> In my good-faith judgment, the file shows no personal data from its source, in any
> layer, apart from what is listed under `evidence.kept` and in the fingerprint report,
> which I judged not to identify anyone.
>
> This statement is my opinion, made in good faith. It is not a warranty.

## Privacy checklist (version privacy-0.1)

1. In the render, no word of the source can be read, anywhere on any page.
2. Every drawing that stays shows nothing personal or identifying: no logo, signature,
   QR code or text drawn as outlines. The evidence counts them; only a person can say
   what they show.
3. Every image is flat, as the manifest says.
4. The values in the fingerprint report (page sizes, image sizes, fonts that stay) do
   not point to one source, given who will receive the file.
5. Each oracle that the manifest lists stayed silent.

## Signing

Sign the commit that adds the file and its manifest, and put the commit's
reference in `attestation.signature`.
