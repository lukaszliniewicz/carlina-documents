"""Keep repeated semantic note references attached to one native Word footnote."""

from __future__ import annotations

import io
import zipfile
from collections import Counter

from bs4 import BeautifulSoup
from defusedxml.minidom import parseString

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def preserve_repeated_docx_notes(data: bytes, prepared_html: str) -> bytes:
    """Replace Pandoc's duplicate notes with bookmarked NOTEREF fields.

    Pandoc emits one native footnote per HTML noteref, even when several refs
    share one target. The prepared HTML supplies the reference order/identity;
    the generated DOCX must match it exactly before any duplicate is removed.
    Single-reference documents keep their original artifact bytes.
    """
    soup = BeautifulSoup(prepared_html, "html.parser")
    targets = [str(ref.get("href")) for ref in soup.select("a.footnote-ref")]
    counts = Counter(targets)
    if not any(count > 1 for count in counts.values()):
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document = parseString(archive.read("word/document.xml"))
        notes = parseString(archive.read("word/footnotes.xml"))
        refs = list(document.getElementsByTagNameNS(WORD_NS, "footnoteReference"))
        by_id = {
            note.getAttributeNS(WORD_NS, "id"): note
            for note in notes.getElementsByTagNameNS(WORD_NS, "footnote")
        }
        native_ids = [ref.getAttributeNS(WORD_NS, "id") for ref in refs]
        if (
            len(refs) != len(targets)
            or len(set(native_ids)) != len(refs)
            or any(note_id not in by_id for note_id in native_ids)
        ):
            raise ValueError("semantic-export-docx-note-reference-shape")

        bookmarks = list(document.getElementsByTagNameNS(WORD_NS, "bookmarkStart"))
        bookmark_id = (
            max(
                (int(node.getAttributeNS(WORD_NS, "id")) for node in bookmarks),
                default=0,
            )
            + 1
        )
        bookmark_names = {node.getAttributeNS(WORD_NS, "name") for node in bookmarks}
        first = {}
        for target, ref in zip(targets, refs, strict=True):
            run = ref.parentNode
            if (
                run.namespaceURI != WORD_NS
                or run.localName != "r"
                or any(
                    child.nodeType == child.ELEMENT_NODE
                    and (
                        child.namespaceURI != WORD_NS
                        or child.localName not in {"rPr", "footnoteReference"}
                    )
                    for child in run.childNodes
                )
            ):
                raise ValueError("semantic-export-docx-note-reference-shape")
            if target not in first:
                number = len(first) + 1
                name = f"CarlinaNote{bookmark_id}"
                while name in bookmark_names:
                    bookmark_id += 1
                    name = f"CarlinaNote{bookmark_id}"
                first[target] = (name, number)
                if counts[target] > 1:
                    start = document.createElementNS(WORD_NS, "w:bookmarkStart")
                    start.setAttributeNS(WORD_NS, "w:id", str(bookmark_id))
                    start.setAttributeNS(WORD_NS, "w:name", name)
                    end = document.createElementNS(WORD_NS, "w:bookmarkEnd")
                    end.setAttributeNS(WORD_NS, "w:id", str(bookmark_id))
                    run.parentNode.insertBefore(start, run)
                    run.parentNode.insertBefore(end, run.nextSibling)
                    bookmark_names.add(name)
                    bookmark_id += 1
                continue

            name, number = first[target]
            field = document.createElementNS(WORD_NS, "w:fldSimple")
            field.setAttributeNS(WORD_NS, "w:instr", f" NOTEREF {name} \\h \\f ")
            field.setAttributeNS(WORD_NS, "w:dirty", "true")
            display_run = run.cloneNode(deep=True)
            copied_ref = display_run.getElementsByTagNameNS(
                WORD_NS, "footnoteReference"
            )[0]
            text = document.createElementNS(WORD_NS, "w:t")
            text.appendChild(document.createTextNode(str(number)))
            display_run.replaceChild(text, copied_ref)
            field.appendChild(display_run)
            run.parentNode.replaceChild(field, run)
            duplicate = by_id[ref.getAttributeNS(WORD_NS, "id")]
            duplicate.parentNode.removeChild(duplicate)

        replacements = {
            "word/document.xml": document.toxml(encoding="UTF-8"),
            "word/footnotes.xml": notes.toxml(encoding="UTF-8"),
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as updated:
            for member in archive.infolist():
                updated.writestr(
                    member, replacements.get(member.filename, archive.read(member))
                )
        return output.getvalue()
