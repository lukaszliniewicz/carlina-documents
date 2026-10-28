"""Apply edition details without reserializing the translated book body."""

from collections.abc import Mapping
from html import escape

from .block_contract import BlockContractError
from .block_planner import _LosslessParser, _Node


def _nodes(roots):
    for node in roots:
        if isinstance(node, _Node):
            yield node
            yield from _nodes(node.children)


def present_export(
    html: str,
    metadata: Mapping,
    *,
    cover_uri: str | None = None,
    omit_cover: bool = False,
    renew_cover_identity: bool = False,
) -> str:
    """Derive the HTML head and optionally replace one explicitly marked cover.

    Source and target revisions remain unchanged. Edits use original offsets;
    paragraphs, notes, identifiers, and links retain their exact body bytes.
    """
    nodes = list(_nodes(_LosslessParser(html).parse()[0]))
    root = next((node for node in nodes if node.tag == "html"), None)
    if root is None:
        html = f'<html lang="{escape(str(metadata.get("language") or "en"), quote=True)}"><body>{html}</body></html>'
        nodes = list(_nodes(_LosslessParser(html).parse()[0]))
        root = nodes[0]
    body = next((node for node in nodes if node.tag == "body"), None)
    if body is None:
        raise BlockContractError("export-presentation-body")

    head = next((node for node in nodes if node.tag == "head"), None)
    title = escape(str(metadata.get("title") or "Translation"))
    head_parts = [f"<title>{title}</title>"]

    def add(name, value):
        if value not in (None, "", []):
            text = " ".join(str(value).split())
            head_parts.append(
                f'<meta name="{escape(name, quote=True)}" content="{escape(text, quote=True)}">'
            )

    for key, name in (
        ("subtitle", "DC.title.alternative"),
        ("description", "description"),
        ("language", "DC.language"),
        ("publisher", "DC.publisher"),
        ("publication_date", "DC.date"),
        ("rights", "DC.rights"),
        ("journal_or_series", "DC.relation.isPartOf"),
        ("volume", "DC.relation.volume"),
    ):
        add(name, metadata.get(key))
    contributors = metadata.get("contributors") or {}
    # Pandoc maps these standard HTML names to native DOCX core properties;
    # the DC fields below retain explicit contributor roles in every edition.
    add("author", "; ".join(contributors.get("authors") or []))
    add("lang", metadata.get("language"))
    for role, name in (
        ("authors", "DC.creator"),
        ("translators", "DC.contributor.translator"),
        ("editors", "DC.contributor.editor"),
    ):
        for person in contributors.get(role, []):
            add(name, person)
    for person in contributors.get("other", []):
        add(f"DC.contributor.{person.get('role', 'ctb')}", person.get("name"))
    for subject in metadata.get("subjects") or []:
        add("DC.subject", subject)
    for identifier in metadata.get("identifiers") or []:
        add("DC.identifier", identifier.get("value"))

    edits = []
    if head is not None:
        for node in _nodes(head.children):
            if node.tag in {"title", "meta"} and (
                node.tag != "meta" or not node.has_attr("charset")
            ):
                edits.append((node.start, node.end or node.start_tag_end, ""))
        edits.append((head.start_tag_end, head.start_tag_end, "".join(head_parts)))
    else:
        edits.append(
            (
                root.start_tag_end,
                root.start_tag_end,
                '<head><meta charset="utf-8">' + "".join(head_parts) + "</head>",
            )
        )

    if cover_uri or omit_cover:
        cover_id = str(metadata.get("cover_image_id") or "")
        used_ids = {node.attr("id") for node in nodes if node.attr("id")}
        replacement_id = "epub-cover-image"
        suffix = 2
        while replacement_id in used_ids:
            replacement_id = f"epub-cover-image-{suffix}"
            suffix += 1
        renewed_image = (
            f'<img id="{replacement_id}" class="cover" data-epub-selected-cover="true" '
            f'src="{escape(cover_uri or "", quote=True)}" alt="{title} — Cover">'
        )

        def marked(node):
            return (
                "cover" in node.classes()
                or node.attr("role") == "doc-cover"
                or "cover" in (node.attr("epub:type") or "").split()
            )

        covers = []
        for node in nodes:
            if node.tag != "img":
                continue
            parent = node.parent
            in_cover = marked(node)
            while parent is not None and parent.tag != "body":
                in_cover = in_cover or marked(parent)
                parent = parent.parent
            if in_cover or (
                cover_id and cover_id in {node.attr("id"), node.attr("src")}
            ):
                covers.append(node)
        if len(covers) > 1:
            raise BlockContractError("export-cover-ambiguous")
        if covers:
            node = covers[0]
            if omit_cover:
                cover_id = node.attr("id")
                replacement = (
                    f'<span id="{escape(cover_id, quote=True)}"></span>'
                    if cover_id
                    else ""
                )
                edits.append((node.start, node.end or node.start_tag_end, replacement))
            elif renew_cover_identity:
                old_id = node.attr("id")
                anchor = (
                    f'<span id="{escape(old_id, quote=True)}"></span>' if old_id else ""
                )
                edits.append(
                    (node.start, node.end or node.start_tag_end, anchor + renewed_image)
                )
            else:
                attributes = []
                for attr in node.attrs:
                    if attr.name.lower() not in {"src", "class", "width", "height"}:
                        attributes.append(
                            html[
                                node.start + 1 + attr.full_start : node.start
                                + 1
                                + attr.full_end
                            ].strip()
                        )
                classes = " ".join(sorted(node.classes() | {"cover"}))
                replacement = f'<img class="{escape(classes, quote=True)}" src="{escape(cover_uri or "", quote=True)}" {" ".join(attributes)}>'
                edits.append((node.start, node.end or node.start_tag_end, replacement))
        elif cover_uri:
            image = (
                f'<figure class="cover" role="doc-cover">{renewed_image}</figure>'
                if renew_cover_identity
                else f'<figure class="cover" role="doc-cover"><img class="cover" src="{escape(cover_uri, quote=True)}" alt="Cover"></figure>'
            )
            edits.append((body.start_tag_end, body.start_tag_end, image))
    for start, end, replacement in sorted(
        edits, key=lambda edit: (edit[0], edit[1]), reverse=True
    ):
        html = html[:start] + replacement + html[end:]
    return html
