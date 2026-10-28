# Reflowable EPUB publishing

Carlina's common packager targets EPUB 3.3 with OPF `version="3.0"`. Document
Processor and Translator consume the same owned projection and validator. This
contract covers reflowable editions; fixed layout, DRM, media overlays, EPUB 2
production and Kindle conversion remain outside its scope.

## Policy and historical compatibility

An absent `epub_options` field selects the retained exporter. A present object,
including `{}`, normalizes to `publishing-v2`. An explicit `publishing-v1` retains
its original eleven-field contract and layout; v2-only choices are rejected under
v1. Unknown fields, malformed choices,
stale anchors and out-of-bounds review maps fail closed. The options envelope is
separate from the 16 KiB publication metadata envelope and is limited to 128 KiB:
512 heading/table/image reviews, 2,048 page labels, and a bounded evaluation.
Historical request serialization, idempotency, passive instructions, assembly
bytes and report-less export readback remain valid. HTML/DOCX/PDF reject these
EPUB-only options and retain their existing projection contracts.

Source HTML and canonical metadata remain immutable during export. Reviewed
roles, note placement, table headers, image descriptions and printed folios are
edition choices. Source evidence takes precedence over conservative DOM fallback;
English chapter/title words are not classification rules. Unknown note ownership
keeps source placement and reports a review item. Every resolved reference gets
a distinguishable return link; printed note labels, IDs, citations and authored
return links are preserved. Global notes can have evidenced chapter group headings
without changing counters. Large chapters retain logical ownership across files.

Document-wide numbering is independent of chapter ownership. A shared printed
sequence can span chapters while each note belongs to the one export unit
containing all of its references. References outside a unit, across units, or
with conflicting scope declarations retain the collected placement and review
warning. Preflight and packaging run the same note projection.

Publishing editions give each surviving note collection a visible localized
heading, navigation entry, document title and named endnotes region. Derived
chapter headings identify their chapter; an existing reviewed heading is retained.
Generated contents labels and document titles omit live note/page markers while
the body heading keeps its citations. Generated return labels use the printed
note label and distinguish repeated references. Authored returns remain intact.
Internal validation checks these generated labels, visible notes headings and
reciprocal reference/return destinations in addition to ZIP/XML/link validity.
These checks do not replace EPUBCheck or publication accessibility evaluation.

Replacement covers receive a new image review identity. Preflight and packaging
use the same resource-free projection; old link destinations survive as anchors.
The dialogs discard only the old cover's description/decorative decision and prior
evaluation when the selected picture changes. Interior-image reviews remain.
Uploaded Translator covers also retire the old image semantics. Field-absent
historical requests keep their original cover projection.

## Saved editions and front matter

Each product owns one optional edition recipe per document/project, with its own
owner, monotonically increasing version and source witness. REST and MCP expose
get/save/clear through the same service. Saving requires both the expected version
and a fresh source revision; an equal save checks the version without advancing
it. Clearing retains a versioned tombstone, preventing an old window from recreating
forgotten choices. Recipes follow the root's ordinary-content encryption policy.
The additive migrations are Document Processor `006` and Translator `029`.

A recipe contains only normalized options, cover selection and image mode. It
does not contain source HTML, paths or publication metadata. Document witnesses
cover completed HTML and canonical metadata. Translator witnesses cover the
verified active source plan, target revision pointers/receipts, numbering and saved
export context. On a changed or unavailable source, effective choices discard
anchor reviews, omissions, edits, section breaks, order, PDF-page/crop selection and
accessibility evaluation. General preferences, including a generated cover, survive.
An unavailable source is represented by a null current revision; its owner can
still read or forget the saved recipe. Export requires a ready source and fresh
preflight. Historical delivered files retain their own identities.

The v2 outline offers at most 64 leading front-matter sections and 512 paragraphs.
It stops at evidenced body content. Section and paragraph omissions bind exact
SHA-256 witnesses; retained sections can be reordered explicitly. Plain paragraphs
can be edited with at most 4,096 UTF-8 bytes; editing replaces inline formatting,
so paragraphs containing links, images, tables, note targets or nested IDs are
excluded. Longer paragraphs appear as explicitly labelled, read-only excerpts;
the full source witness remains authoritative. Linked targets cannot be silently removed. Duplicate text is a review
hint, never an automatic deletion rule. Copyright and licensing remain an
editorial responsibility.

An eligible top-level paragraph can explicitly start a title, copyright or
contributors section. The projection adds a localized heading and preserves the
paragraph and its anchors. Changing section starts invalidates section omission
and order decisions for another review. The source remains intact. V2 places
navigation after the cover/title/copyright sequence and before contributors/body.

PDF cover crops use normalized `[left, top, right, bottom]` coordinates relative
to the visible rotated page, including its inherited CropBox. Percent controls,
full/half-page presets and a preview avoid a mouse-drag requirement. Crops must
retain at least one percent of each dimension. Source-page selection, source
digest and permission checks remain authoritative.

## Publishing layout and images

V2 packages one shared stylesheet for content and navigation. Relative sizes,
logical spacing, a readable maximum line length, wrapping links/code and scalable
images permit reader font, spacing, direction and theme overrides. A chapter title
may paginate naturally instead of keeping a long heading and the next paragraph
as an oversized block. Title and copyright pages have distinct spacing. Simple
two-column tables receive balanced columns; a numeric final column aligns to the
end. Spanning or complex tables keep their authored structure. EPUB reflow is not
an attempt to reproduce PDF page geometry.

With Auto selected, the balanced image policy optimizes an opaque photographic cover and
explicitly reviewed photographic interior images. It bounds covers to 1,800 pixels
and interior photographs to 2,400 pixels on the longest edge, with high-quality
JPEG and no enlargement. Transparent/animated images and low-colour PNG diagrams
remain lossless. Untouched interior images keep their original bytes; the same
source used as a cover and interior has independent packaging identities. A larger
re-encoding is discarded. `image_policy="preserve"` disables this optimization.
New EPUB exports default to `epub_image_mode="jpeg"` at quality 90. Auto retains
the selective behavior above. Explicit `jpeg` and
`webp` choices take priority over the optional balanced/preserve policy. Recipes,
direct exports and Translator semantic exports carry the same mode, and review
and queued-request witnesses bind that choice. Saved editions retain their mode.
Omitted historical requests remain Auto; an omitted retry retains the admitted
request's choice.

Explicit JPEG converts opaque static rasters, including diagrams, at quality 90.
Google Jpegli is provided by the pinned `pyjpegli` 0.3.2 wheel in Document
Processor's Pixi and host-vendor environments. Its current binding uses baseline
JPEG with 4:2:0 sampling; it does not expose progressive or sampling controls.
ICC-bearing RGB images use Pillow quality 90, 4:4:4, optimized progressive JPEG
to retain their colour profile. Existing JPEGs within the size bounds are not
recompressed. SVG, animation and transparent PNG/GIF images retain their format;
other transparent rasters, including WebP, use lossless PNG. Images whose colour profile cannot
be preserved safely remain in their supported original format. Covers and body
images use the same 1,800/2,400-pixel bounds without enlargement. JPEG encoding
failure is explicit, rather than silently substituting an unreported encoder.

Explicit WebP retains quality 84 and method 6, with lossless encoding for
transparency or limited-colour artwork and retained animation timing. WebP is an
EPUB 3.3 core image type, but older reading systems may lack support. Auto's
selective JPEG optimization remains quality 88, 4:4:4, optimized progressive JPEG;
choosing a new format does not alter Auto's settings. Receipts record original
and packaged image bytes without including paths or image content.

## Preflight and delivery witnesses

Document Processor exposes owner-scoped REST and MCP preflight; Translator exposes
target preflight through its pinned semantic-export provider. REST Translator
preflight includes unsaved edition metadata and cover choices; MCP uses the saved
edition. Preflight performs no resource capture, model inference, archive build
or validator execution. Its checker states are `not_run`, rather than passes.
The bounded outline contains stable anchor IDs and review text, never source HTML,
resource paths or credentials. Invalid provider responses leave the review usable.

`review_hash` binds normalized edition inputs. Direct exports bind the owned raw
revision, canonical metadata, choices, declared cover and image mode. Semantic
exports bind the serialized immutable DTO with HTML represented by its digest,
including metadata, resource manifest and provider identity. The job-specific
operation witness JSON and its digest are excluded because delivery does not exist
at preflight; they remain in the delivered DTO. Admission/worker checks reject a
changed edition with `epub-review-stale`. The separate source/numbering revision
fence still applies. Field-absent old jobs bypass the new witness check.

The final `epub-quality-v1` receipt binds the input, normalized options, final ZIP
bytes and a stable content digest. Content identity covers serialized XHTML/nav and
resource hashes before variable OPF modification time/identifier fields. Owner-
scoped delivery/readback validates persisted receipt hashes; it does not reject a
historical completed export simply because its live source later changed.

## Structure and accessibility

Native headings, tables and list semantics remain authoritative. Reviewed headers
become `th` with row/column scope; untouched tables require review rather than an
invented first header row. Informative alternatives require review; confirmed
decorative images have empty alternatives. DPUB roles describe eligible section,
navigation and note containers. Chapter/subheading rank, reading-start landmarks
and endnote headings follow the export projection. Wrapper anchors survive spine
splitting; local IDREFs remain local, and unsupported cross-document accessibility
relationships block export rather than becoming broken references.

Extraction distinguishes the image's purpose from its printed caption. Short
alternatives identify the useful information in context; clearly decorative or
fully redundant retained images use `alt=""`. Printed captions remain exact
transcription. A complex cropped diagram or chart receives ordinary, translatable
prose, lists or tables in a `div.image-description-v1` with a unique ID and an
adjacent `p.image-description-link` containing a local link. The description is
visible and navigable; structured content is not flattened into `aria-describedby`.
This follows the [WAI complex-image guidance](https://www.w3.org/WAI/tutorials/images/complex/)
and [DAISY description guidance](https://kb.daisy.org/publishing/docs/html/images-desc.html).

These optional markers are a closed semantic contract. Each description is
limited to 128 KiB of UTF-8 HTML and 2,048 descendant elements, with at most 512
described figures per document. Marked short alternatives and description-link text have
a 4,096-byte limit. Hidden descriptions, duplicate or broken targets, nested
images/diagrams, and an image role that conceals the structured alternative are
rejected. Unmarked historical content retains its existing validation contract.
The markers identify accessibility adaptations; they do not assert human review.

Editable flowcharts may carry a plain `p.image-alt-v1` and the same extended
description. Translation preserves literal graph code, IDs and caption structure,
while translating labels, alternatives and description text. Every rendered
flowchart, including older figures, receives node and connection tables with
scoped headers, stable local navigation and actual language tags. The tables
include every node, shape, connection, direction and edge label, including loops,
disconnected nodes and identical labels. They supplement any authored explanation.
SVG title/description fields also include the validated topology. Derived output
preserves the stored source and removes a legacy figure image role that would
hide these tables from assistive technology.
Label IDs target the actual description cells, so retained links and accessibility
references preserve their text as well as their destination. Captions occur once,
at a valid figure edge; the publishing projection moves a middle caption to the
start without changing its text, formatting or ID.

The publication review outline includes both cropped images and editable
flowcharts. Its short-alt confirmation and decorative decision are explicit edition
choices; the panel links to the source and full description for content review.
Confirming an alt does not confirm the full description's equivalence or complete
the edition accessibility evaluation. Generated descriptions remain unreviewed.
V2 exposes a separate extended-description confirmation bound to the authored
description, graph topology and translatable labels. A changed description rejects
the old witness. This confirmation clears its review item; it does not assert
equivalent access for the publication.
Presence of one extended description does not automatically assert the
[`longDescription` discovery feature](https://kb.daisy.org/publishing/docs/metadata/schema.org/accessibilityFeature/longDescription.html),
which describes coverage of all complex content in the edition.

Relative CSS permits reader fonts, spacing, direction and themes. Images/SVG scale
inside the viewport; atomic content remains intact when it exceeds the chunk target
and reports a compatibility warning. Package/content language follows edition
metadata, preserving inner language changes. Explicit direction overrides source;
edition-language changes update RTL progression. Generated labels are localized for
English, Polish, German, French, Spanish and Arabic; other editions use explicitly
language-tagged English fallback labels. Physical PDF positions remain distinct
from reviewed printed folios and do not imply a complete source-page list.

Observed features do not prove equivalent access. Unreviewed output omits
`schema:accessModeSufficient` and formal conformance, and reports unknown hazards.
An evaluation binds both content and projection options, with evaluator, scope,
sufficient modes and hazards. Stale evaluations omit assertions and warn. Optional
conformance follows the exact EPUB Accessibility 1.1/WCAG pattern and identifies
the evaluating party; credentials are optional and are never invented. This
mechanism records assertions, not an automatic WCAG certification.

## Validation and qualification

Both routes require the Flask-free ZIP/XML/container/manifest/spine/link/IDREF
validator. Official EPUBCheck is optional for ordinary local drafts; set
`EPUBCHECK_REQUIRED=true` for a qualified publishing lane and select an executable
or JAR explicitly. Unavailable optional checks remain `not_run`; required absence,
execution failure or format failure blocks delivery. Diagnostics omit source text
and paths; process output, timeout and version probing are bounded.

The integrated Ace runner accepts `ACE_EXECUTABLE` or an installed `ace` command,
with `ACE_REQUIRED=true` for the publishing gate. It runs locally in a temporary
output directory, bounds runtime and report size, validates EARL outcomes and
records the actual version. Exit zero alone is insufficient. Missing optional Ace
is `not_run`; operational, malformed or incomplete results block delivery.
Completed accessibility findings may accompany an ordinary draft, with `ace=failed`
and an explicit review issue. Required mode blocks those findings. Raw reports can
contain book text and stay outside ordinary application logs and quality receipts.

The qualification pin is official EPUBCheck 5.3.0 (EPUB 3.3), JAR SHA-256
`f7f96617c929371821609b88c8484d6dc9f24fe916499863c46094c5fb778a65`, with
OpenJDK 25.0.4.1. EPUBCheck 5.4.0 has a
[maintainer-confirmed navigation-label false positive](https://github.com/w3c/epubcheck/issues/1726).
Its negative result is retained separately; labels are not removed to appease it.
Ace 1.4.6 runs locally on Node 24.19.0 with Chromium 148.0.7778.97. Tool artifacts,
lockfiles and private reports are kept with the qualification output, outside Git.
Never filter a validator error into a pass. Renew versions deliberately.

Regression fixtures cover global/unknown/chapter/shared notes, restarted and
symbolic labels, multiple references, long/atomic chunks, parts/front/back matter,
page labels, table headers, images, RTL/multilingual and non-PDF sources. Replay the
bounded engine lane with:

```sh
pixi run python -m pytest -q tests/test_epub_export_contract.py tests/test_doc_processor_epub_publishing.py tests/test_doc_processor_epub_validation.py tests/test_doc_processor_epub_note_placement.py tests/test_translator_block_export.py tests/test_translator_epub_preflight.py tests/test_translator_epub_quality.py
```

Description-specific fixtures also cover bounded inert markup, dangling links,
hidden alternatives, complete graph topology, mixed languages and actual
block-translation transport through the final EPUB archive:

```sh
pixi run python -m pytest -q tests/test_semantic_image_descriptions.py tests/test_doc_processor_prompting.py tests/test_translator_block_rendering.py
```

Publishing acceptance adds exact-artifact EPUBCheck/Ace reports, source preservation
witnesses and actual reader checks. Automated accessibility findings, human content
equivalence, screen-reader use and legal compliance are distinct claims. Reader
qualification uses Thorium 3.5.0 and Calibre 9.16.0 on Linux. Other platforms and a
full assistive-technology/WCAG evaluation require separate acceptance. Ordinary
source or package tests do not renew a managed installation or public release.
