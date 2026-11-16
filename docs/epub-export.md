# Reflowable EPUB publishing

Carlina's common packager targets EPUB 3.3 with OPF `version="3.0"`. Document
Processor and Translator use the same owned projection and validator. This
contract covers reflowable editions. Fixed layout, DRM, media overlays, EPUB 2
production and Kindle conversion are outside its scope.

## Policy and historical compatibility

If the `epub_options` field is absent, the retained exporter runs. A present
object, including `{}`, normalises to `publishing-v2`. An explicit `publishing-v1`
keeps its original eleven-field contract and layout, and v2-only choices are
rejected under v1. Unknown fields, malformed choices, stale anchors and
out-of-bounds review maps fail closed. The options envelope is separate from the
16 KiB publication metadata envelope and is limited to 128 KiB: 512 heading,
table and image reviews, 2,048 page labels, and a bounded evaluation. Historical
request serialisation, idempotency, passive instructions, assembly bytes and
report-less export readback remain valid. HTML, DOCX and PDF reject these
EPUB-only options and keep their existing projection contracts.

Source HTML and canonical metadata stay immutable during export. Reviewed roles,
note placement, table headers, image descriptions and printed folios are edition
choices. Source evidence takes precedence over a conservative DOM fallback.
English chapter and title words are not classification rules. When note
ownership is unknown, the note keeps its source placement and the export reports
a review item. Every resolved reference gets a distinguishable return link.
Printed note labels, IDs, citations and authored return links are preserved.
Global notes can have evidenced chapter group headings without changing counters.
Large chapters keep logical ownership across files.

Document-wide numbering is independent of chapter ownership. A shared printed
sequence can span chapters, while each note belongs to the one export unit that
contains all of its references. References outside a unit, across units, or with
conflicting scope declarations keep the collected placement and a review warning.
Preflight and packaging run the same note projection.

Publishing editions give each surviving note collection a visible localised
heading, a navigation entry, a document title and a named endnotes region.
Derived chapter headings identify their chapter, and an existing reviewed heading
is kept. Generated contents labels and document titles omit live note and page
markers, while the body heading keeps its citations. Generated return labels use
the printed note label and distinguish repeated references. Authored returns stay
intact. Internal validation checks these generated labels, the visible notes
headings and reciprocal reference and return destinations, in addition to ZIP,
XML and link validity. These checks do not replace EPUBCheck or a publication
accessibility evaluation.

Replacement covers receive a new image review identity. Preflight and packaging
use the same resource-free projection, and old link destinations survive as
anchors. When the selected picture changes, the dialogs discard only the old
cover's description or decorative decision and its prior evaluation. Interior-image
reviews remain. Uploaded Translator covers also retire the old image semantics.
Historical requests without the field keep their original cover projection.

## Saved editions and front matter

Each product owns one optional edition recipe per document or project, with its
own owner, monotonically increasing version and source witness. REST and MCP
expose get, save and clear through the same service. Saving requires both the
expected version and a fresh source revision. An equal save checks the version
without advancing it. Clearing keeps a versioned tombstone, which stops an old
window from recreating forgotten choices. Recipes follow the root's
ordinary-content encryption policy. The additive migrations are Document
Processor `006` and Translator `029`.

A recipe contains only normalised options, cover selection and image mode. It
does not contain source HTML, paths or publication metadata. Document witnesses
cover completed HTML and canonical metadata. Translator witnesses cover the
verified active source plan, target revision pointers and receipts, numbering and
saved export context. When the source has changed or is unavailable, effective
choices discard anchor reviews, omissions, edits, section breaks, order, PDF-page
and crop selection, and accessibility evaluation. General preferences, including
a generated cover, survive. An unavailable source is represented by a null
current revision, and its owner can still read or forget the saved recipe. Export
requires a ready source and a fresh preflight. Historical delivered files keep
their own identities.

The v2 outline offers at most 64 leading front-matter sections and 512
paragraphs. It stops at evidenced body content. Section and paragraph omissions
bind exact SHA-256 witnesses, and retained sections can be reordered explicitly.
Plain paragraphs can be edited up to 4,096 UTF-8 bytes. Editing replaces inline
formatting, so paragraphs containing links, images, tables, note targets or
nested IDs are excluded. Longer paragraphs appear as explicitly labelled,
read-only excerpts, and the full source witness remains authoritative. Linked
targets cannot be silently removed. Duplicate text is a review hint, never an
automatic deletion rule. Copyright and licensing remain an editorial
responsibility.

An eligible top-level paragraph can explicitly start a title, copyright or
contributors section. The projection adds a localised heading and preserves the
paragraph and its anchors. Changing section starts invalidates section omission
and order decisions, so they need another review. The source remains intact. V2
places navigation after the cover, title and copyright sequence and before
contributors and body.

PDF cover crops use normalised `[left, top, right, bottom]` coordinates relative
to the visible rotated page, including its inherited CropBox. Percent controls,
full-page and half-page presets and a preview mean no mouse drag is required.
Crops must keep at least one percent of each dimension. Source-page selection,
source digest and permission checks remain authoritative.

## Publishing layout and images

V2 packages one shared stylesheet for content and navigation. Relative sizes,
logical spacing, a readable maximum line length, wrapping links and code, and
scalable images let readers override font, spacing, direction and theme. A
chapter title may paginate naturally instead of keeping a long heading and the
next paragraph together as an oversized block. Title and copyright pages have
distinct spacing. Simple two-column tables get balanced columns, and a numeric
final column aligns to the end. Spanning or complex tables keep their authored
structure. EPUB reflow does not try to reproduce PDF page geometry.

With Auto selected, the balanced image policy optimises an opaque photographic
cover and explicitly reviewed photographic interior images. It limits covers to
1,800 pixels and interior photographs to 2,400 pixels on the longest edge, using
high-quality JPEG and no enlargement. Transparent or animated images and
low-colour PNG diagrams remain lossless. Untouched interior images keep their
original bytes. The same source used as a cover and as an interior image gets
independent packaging identities. A larger re-encoding is discarded.
`image_policy="preserve"` disables this optimisation.

New EPUB exports default to `epub_image_mode="jpeg"` at quality 90. Auto keeps
the selective behaviour above. Explicit `jpeg` and `webp` choices take priority
over the optional balanced or preserve policy. Recipes, direct exports and
Translator semantic exports carry the same mode, and review and queued-request
witnesses bind that choice. Saved editions keep their mode. Historical requests
that omit it remain Auto, and a retry that omits it keeps the admitted request's
choice.

Explicit JPEG converts opaque static rasters, including diagrams, at quality 90.
Google Jpegli comes from the pinned `pyjpegli` 0.3.2 wheel in Document
Processor's Pixi and host-vendor environments. Its current binding uses baseline
JPEG with 4:2:0 sampling and does not expose progressive or sampling controls.
ICC-bearing RGB images use Pillow quality 90, 4:4:4, optimised progressive JPEG
to keep their colour profile. Existing JPEGs within the size bounds are not
recompressed. SVG, animation and transparent PNG and GIF images keep their
format. Other transparent rasters, including WebP, use lossless PNG. Images whose
colour profile cannot be preserved safely stay in their supported original
format. Covers and body images use the same bounds of 1,800 and 2,400 pixels,
without enlargement. A JPEG encoding failure is reported explicitly, instead of
silently substituting an unreported encoder.

Explicit WebP keeps quality 84 and method 6, with lossless encoding for
transparency or limited-colour artwork and retained animation timing. WebP is an
EPUB 3.3 core image type, but older reading systems may lack support. Auto's
selective JPEG optimisation remains quality 88, 4:4:4, optimised progressive
JPEG. Choosing a new format does not alter Auto's settings. Receipts record
original and packaged image bytes without including paths or image content.

## Preflight and delivery witnesses

Document Processor exposes owner-scoped REST and MCP preflight. Translator exposes
target preflight through its pinned semantic-export provider. REST Translator
preflight includes unsaved edition metadata and cover choices, while MCP uses the
saved edition. Preflight performs no resource capture, model inference, archive
build or validator execution. Its checker states are `not_run`, not passes. The
bounded outline contains stable anchor IDs and review text, never source HTML,
resource paths or credentials. Invalid provider responses leave the review usable.

`review_hash` binds normalised edition inputs. Direct exports bind the owned raw
revision, canonical metadata, choices, declared cover and image mode. Semantic
exports bind the serialised immutable DTO with HTML represented by its digest,
including metadata, resource manifest and provider identity. The job-specific
operation witness JSON and its digest are excluded because delivery does not
exist at preflight. They remain in the delivered DTO. Admission and worker checks
reject a changed edition with `epub-review-stale`. The separate source and
numbering revision fence still applies. Old jobs without the field bypass the new
witness check.

The final `epub-quality-v1` receipt binds the input, normalised options, final ZIP
bytes and a stable content digest. Content identity covers serialised XHTML and
navigation and resource hashes, taken before the variable OPF modification-time
and identifier fields. Owner-scoped delivery and readback validate persisted
receipt hashes. They do not reject a historical completed export simply because
its live source later changed.

## Structure and accessibility

Native headings, tables and list semantics remain authoritative. Reviewed headers
become `th` with row and column scope. Untouched tables require review rather
than an invented first header row. Informative alternatives require review, and
confirmed decorative images have empty alternatives. DPUB roles describe eligible
section, navigation and note containers. Chapter and subheading rank,
reading-start landmarks and endnote headings follow the export projection.
Wrapper anchors survive spine splitting. Local IDREFs remain local, and
unsupported cross-document accessibility relationships block export instead of
becoming broken references.

Extraction distinguishes the image's purpose from its printed caption. Short
alternatives identify the useful information in context. Clearly decorative or
fully redundant retained images use `alt=""`. Printed captions remain exact
transcription. A complex cropped diagram or chart receives ordinary, translatable
prose, lists or tables in a `div.image-description-v1` with a unique ID, plus an
adjacent `p.image-description-link` containing a local link. The description is
visible and navigable. Structured content is not flattened into
`aria-describedby`.
This follows the [WAI complex-image guidance](https://www.w3.org/WAI/tutorials/images/complex/)
and [DAISY description guidance](https://kb.daisy.org/publishing/docs/html/images-desc.html).

These optional markers are a closed semantic contract. Each description is
limited to 128 KiB of UTF-8 HTML and 2,048 descendant elements, with at most 512
described figures per document. Marked short alternatives and description-link
text have a 4,096-byte limit. The following are rejected: hidden descriptions,
duplicate or broken targets, nested images or diagrams, and an image role that
conceals the structured alternative. Unmarked historical content keeps its
existing validation contract. The markers identify accessibility adaptations.
They do not assert human review.

Editable flowcharts may carry a plain `p.image-alt-v1` and the same extended
description. Translation preserves literal graph code, IDs and caption structure,
and translates labels, alternatives and description text. Every rendered
flowchart, including older figures, receives node and connection tables with
scoped headers, stable local navigation and actual language tags. The tables
include every node, shape, connection, direction and edge label, including loops,
disconnected nodes and identical labels. They supplement any authored
explanation. SVG title and description fields also include the validated
topology. Derived output preserves the stored source and removes a legacy figure
image role that would hide these tables from assistive technology. Label IDs
target the actual description cells, so retained links and accessibility
references keep their text as well as their destination. Captions occur once, at
a valid figure edge. The publishing projection moves a middle caption to the
start without changing its text, formatting or ID.

The publication review outline includes both cropped images and editable
flowcharts. Its short-alt confirmation and decorative decision are explicit
edition choices. The panel links to the source and full description for content
review. Confirming an alt does not confirm that the full description is
equivalent, and it does not complete the edition accessibility evaluation.
Generated descriptions remain unreviewed. V2 exposes a separate
extended-description confirmation bound to the authored description, graph
topology and translatable labels. A changed description rejects the old witness.
This confirmation clears its review item. It does not assert equivalent access
for the publication. Presence of one extended description does not automatically
assert the
[`longDescription` discovery feature](https://kb.daisy.org/publishing/docs/metadata/schema.org/accessibilityFeature/longDescription.html),
which describes coverage of all complex content in the edition.

Relative CSS lets readers override fonts, spacing, direction and themes. Images
and SVG scale inside the viewport. Atomic content stays intact when it exceeds
the chunk target, and the export reports a compatibility warning. Package and
content language follow the edition metadata, preserving inner language changes.
Explicit direction overrides the source. Edition-language changes update RTL
progression. Generated labels are localised for English, Polish, German, French,
Spanish and Arabic. Other editions use explicitly language-tagged English
fallback labels. Physical PDF positions remain distinct from reviewed printed
folios and do not imply a complete source-page list.

Observed features do not prove equivalent access. Unreviewed output omits
`schema:accessModeSufficient` and formal conformance, and reports unknown hazards.
An evaluation binds both content and projection options, with evaluator, scope,
sufficient modes and hazards. Stale evaluations omit assertions and warn.
Optional conformance follows the exact EPUB Accessibility 1.1/WCAG pattern and
identifies the evaluating party. Credentials are optional and are never invented.
This mechanism records assertions. It is not an automatic WCAG certification.

## Validation and qualification

Both routes require the Flask-free validator, which checks the ZIP, XML,
container, manifest, spine, links and IDREFs. Official EPUBCheck is optional for
ordinary local drafts. For a qualified publishing lane, set
`EPUBCHECK_REQUIRED=true` and select an executable or JAR explicitly. Unavailable
optional checks remain `not_run`. A required check that is absent, fails to
execute or reports a format failure blocks delivery. Diagnostics omit source text
and paths. Process output, timeout and version probing are bounded.

The integrated Ace runner accepts `ACE_EXECUTABLE` or an installed `ace` command,
with `ACE_REQUIRED=true` for the publishing gate. It runs locally in a temporary
output directory, bounds runtime and report size, validates EARL outcomes and
records the actual version. Exit zero alone is insufficient. Missing optional Ace
is `not_run`. Operational, malformed or incomplete results block delivery.
Completed accessibility findings may accompany an ordinary draft, with
`ace=failed` and an explicit review issue. Required mode blocks those findings.
Raw reports can contain book text and stay outside ordinary application logs and
quality receipts.

### Bilingual reading EPUBs

Translator can export an alternating bilingual EPUB from a verified source plan
and its current translated target. The source and target passages appear one
after the other, with explicit `lang` and `xml:lang` per passage, and the
translated heading keeps the navigational heading role. The OPF records the
target language first and adds the source language as a second `dc:language` when
known. This uses standard static XHTML and CSS, not scripted language switches,
alternate renditions or reader-specific layers. A bilingual EPUB uses the same
source-attested preflight as a monolingual edition, but it reviews the paired
content and needs its own input and options hash before export. It deliberately
rejects structural mismatches instead of inserting guessed pairings. EPUBCheck
and EPUB reader acceptance remain required for publication qualification.

The qualification pin is official EPUBCheck 5.3.0 (EPUB 3.3), JAR SHA-256
`f7f96617c929371821609b88c8484d6dc9f24fe916499863c46094c5fb778a65`, with
OpenJDK 25.0.4.1. EPUBCheck 5.4.0 has a
[maintainer-confirmed navigation-label false positive](https://github.com/w3c/epubcheck/issues/1726).
Its negative result is retained separately, and labels are not removed to appease
it. Ace 1.4.6 runs locally on Node 24.19.0 with Chromium 148.0.7778.97. Tool
artifacts, lockfiles and private reports are kept with the qualification output,
outside Git. Never filter a validator error into a pass. Renew versions
deliberately.

Regression fixtures cover global, unknown, chapter and shared notes, restarted
and symbolic labels, multiple references, long and atomic chunks, parts, front
and back matter, page labels, table headers, images, RTL and multilingual
content, and non-PDF sources. Replay the bounded engine lane with:

```sh
pixi run python -m pytest -q tests/test_epub_export_contract.py tests/test_doc_processor_epub_publishing.py tests/test_doc_processor_epub_validation.py tests/test_doc_processor_epub_note_placement.py tests/test_translator_block_export.py tests/test_translator_epub_preflight.py tests/test_translator_epub_quality.py
```

Description-specific fixtures also cover bounded inert markup, dangling links,
hidden alternatives, complete graph topology, mixed languages and actual
block-translation transport through the final EPUB archive:

```sh
pixi run python -m pytest -q tests/test_semantic_image_descriptions.py tests/test_doc_processor_prompting.py tests/test_translator_block_rendering.py
```

Publishing acceptance adds exact-artifact EPUBCheck and Ace reports, source
preservation witnesses and actual reader checks. Automated accessibility
findings, human content equivalence, screen-reader use and legal compliance are
distinct claims. Reader qualification uses Thorium 3.5.0 and Calibre 9.16.0 on
Linux. Other platforms and a full assistive-technology and WCAG evaluation
require separate acceptance. Ordinary source or package tests do not renew a
managed installation or public release.
