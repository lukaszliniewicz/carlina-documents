"""Shared reader-overridable publishing-v2 stylesheet."""

BOOK_CSS = """html { overflow-wrap: break-word; }
body { margin: 0; padding: .75em; line-height: 1.5; }
main, body > nav { max-width: 42em; max-inline-size: 42em; margin-inline: auto; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; overflow-wrap: anywhere; break-inside: auto; }
h1 { font-size: 1.5em; margin-block: .5em .75em; break-after: auto; }
h2 { font-size: 1.25em; margin-block: 1.25em .6em; break-after: avoid; }
h3 { font-size: 1.1em; margin-block: 1em .5em; break-after: avoid; }
h4, h5, h6 { font-size: 1em; break-after: avoid; }
p { margin-block: .65em; orphans: 2; widows: 2; }
img, svg { max-width: 100%; max-inline-size: 100%; height: auto; }
figure { margin: 1em 0; max-inline-size: 100%; }
figure.cover { margin: 0; text-align: center; }
figure.cover img { max-height: 95vh; object-fit: contain; }
.epub-cover-page { padding: 0; }
.epub-cover-page main { max-width: none; max-inline-size: none; }
.epub-titlepage { text-align: center; padding-block-start: 1.5em; }
.epub-titlepage h1 { font-size: 1.8em; }
.epub-copyright-page { font-size: .95em; }
table { border-collapse: collapse; max-width: 100%; max-inline-size: 100%; margin-block: 1em; }
th, td { padding: .35em; text-align: start; vertical-align: top; overflow-wrap: anywhere; }
caption { text-align: start; margin-block-end: .5em; }
.epub-two-column-table { width: 100%; table-layout: fixed; }
.epub-two-column-table th, .epub-two-column-table td { width: 50%; }
.epub-two-column-table .epub-number { width: 28%; text-align: end; font-variant-numeric: tabular-nums; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
a { overflow-wrap: anywhere; }
.epub-backlink { display: inline-block; padding: .2em .4em; }
.epub-note-group { margin-block-start: 1.5em; }
nav ol { list-style: none; padding-inline-start: 0; }
nav ol ol { padding-inline-start: 1em; }
nav li { margin-block: .4em; }
[hidden] { display: none; }
"""
