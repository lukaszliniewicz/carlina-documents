from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from xml.etree.ElementTree import Element


@dataclass(frozen=True)
class EpubValidationResult:
    passed: bool
    message: str | None
    epubcheck: Literal["passed", "not_run", "failed"]
    version: str | None = None
    ace: Literal["passed", "not_run", "failed"] = "not_run"
    ace_version: str | None = None
    ace_findings: bool = False

    def as_legacy_tuple(self) -> tuple[bool, str | None]:
        return self.passed, self.message


def _publication_label(node: Element[str]) -> str:
    from xml.etree import ElementTree as ET

    from bs4 import BeautifulSoup

    from .epub_structure import publication_heading_text

    markup = BeautifulSoup(ET.tostring(node, encoding="unicode"), "html.parser")
    return publication_heading_text(markup)


def _publishing_semantic_error(
    roots: Mapping[str, Element[str]],
    nav_member: str,
    href_target: Callable[[str, str], tuple[str, str | None, bool]],
) -> str | None:
    """Qualify generated labels and reciprocal notes beyond archive validity."""
    xhtml = "{http://www.w3.org/1999/xhtml}"
    epub_type = "{http://www.idpf.org/2007/ops}type"
    if not any(
        node.get("data-epub-role") or node.get("data-epub-nav-level")
        for root in roots.values()
        for node in root.iter()
    ):
        return None
    ids = {
        member: {str(node.get("id")): node for node in root.iter() if node.get("id")}
        for member, root in roots.items()
    }

    def heading(node: Element[str]) -> bool:
        return node.tag in {f"{xhtml}h{level}" for level in range(1, 7)}

    def note(node: Element[str]) -> bool:
        return bool(
            set(str(node.get(epub_type) or "").split()) & {"footnote", "endnote"}
            or node.get("role") in {"doc-footnote", "doc-endnote"}
        )

    def target(member: str, href: str) -> tuple[str, Element[str] | None]:
        destination, fragment, external = href_target(member, href)
        return destination, (
            ids.get(destination, {}).get(fragment or "") if not external else None
        )

    for member, root in roots.items():
        if member == nav_member:
            continue
        first_heading = next((node for node in root.iter() if heading(node)), None)
        title = root.find(f"{xhtml}head/{xhtml}title")
        if (
            first_heading is not None
            and title is not None
            and _publication_label(title) != _publication_label(first_heading)
        ):
            return "Publishing EPUB document title does not match its heading."
        names: set[str] = set()
        for collection in root.iter():
            if collection.get("data-epub-role") != "endnotes" or heading(collection):
                continue
            if not any(note(child) for child in collection.iter()):
                continue
            label = str(collection.get("aria-label") or "")
            if collection.get("aria-labelledby"):
                label = " ".join(
                    _publication_label(ids[member][identifier])
                    for identifier in str(collection.get("aria-labelledby")).split()
                )
            if not label or label in names:
                return "Publishing EPUB notes landmarks need distinct non-empty labels."
            names.add(label)
            note_descendants = {child for child in collection.iter() if note(child)}
            note_content = {child for body in note_descendants for child in body.iter()}
            has_heading = any(
                heading(child) and child not in note_content
                for child in collection.iter()
            ) or any(
                heading(child)
                and child.get("data-epub-role") == "endnotes"
                and _publication_label(child) == label
                for content in roots.values()
                for child in content.iter()
            )
            if not has_heading:
                return (
                    "Publishing EPUB notes collection is missing its visible heading."
                )
        for reference in root.iter(f"{xhtml}a"):
            if (
                "noteref" not in str(reference.get(epub_type) or "").split()
                and reference.get("role") != "doc-noteref"
            ):
                continue
            destination, body = target(member, str(reference.get("href") or ""))
            if body is None or not note(body) or not reference.get("id"):
                return "Publishing EPUB note reference must identify a note and return target."
            if not any(
                target(destination, str(link.get("href") or "")) == (member, reference)
                for link in body.iter(f"{xhtml}a")
            ):
                return "Publishing EPUB note reference has no reciprocal return link."
    for navigation in roots[nav_member].iter(f"{xhtml}nav"):
        kind = str(navigation.get(epub_type) or "").split()
        for link in navigation.iter(f"{xhtml}a"):
            _, node = target(nav_member, str(link.get("href") or ""))
            if node is None:
                continue
            if (
                "toc" in kind
                and heading(node)
                and _publication_label(link) != _publication_label(node)
            ):
                return (
                    "Publishing EPUB contents label does not match its target heading."
                )
            if (
                "landmarks" in kind
                and "endnotes" in str(link.get(epub_type) or "").split()
            ):
                label = (
                    _publication_label(node)
                    if heading(node)
                    else str(node.get("aria-label") or "")
                )
                if node.get("aria-labelledby"):
                    destination, _, _ = href_target(
                        nav_member, str(link.get("href") or "")
                    )
                    label = " ".join(
                        _publication_label(ids[destination][identifier])
                        for identifier in str(node.get("aria-labelledby")).split()
                    )
                if not label or _publication_label(link) != label:
                    return "Publishing EPUB notes landmark label does not match its section."
    return None


def validate_epub_structure(output_path: str) -> tuple[bool, str | None]:
    import posixpath
    import re
    from urllib.parse import unquote, urlparse
    from xml.etree import ElementTree as ET

    from .epub_packager import CORE_IMAGE_MEDIA_TYPES, MARC_ROLE_VALUES

    container_ns = "urn:oasis:names:tc:opendocument:xmlns:container"
    opf_ns = "http://www.idpf.org/2007/opf"
    dc_ns = "http://purl.org/dc/elements/1.1/"
    xhtml_ns = "http://www.w3.org/1999/xhtml"
    epub_ns = "http://www.idpf.org/2007/ops"

    def resolve_member(base_member: str, href: str) -> str:
        parsed = urlparse(href)
        if parsed.scheme or href.startswith("//"):
            return ""
        path = unquote(parsed.path)
        if not path or path.startswith("/"):
            return ""
        member = posixpath.normpath(
            posixpath.join(posixpath.dirname(base_member), path)
        )
        return "" if member == ".." or member.startswith("../") else member

    def href_target(base_member: str, href: str) -> tuple[str, str | None, bool]:
        parsed = urlparse(href)
        if parsed.scheme or href.startswith("//"):
            return "", None, True
        member = resolve_member(base_member, href) if parsed.path else base_member
        fragment = unquote(parsed.fragment) if parsed.fragment else None
        return member, fragment, False

    def element_local_name(node: Element[str]) -> str:
        return str(node.tag).rsplit("}", 1)[-1]

    def validate_nav_list(ordered_list: Element[str]) -> str | None:
        children = list(ordered_list)
        if not children or any(element_local_name(child) != "li" for child in children):
            return "Navigation ordered lists must contain one or more li children."
        for item in children:
            item_children = list(item)
            if not item_children or element_local_name(item_children[0]) not in {
                "a",
                "span",
            }:
                return "Each navigation li must begin with exactly one a or span label."
            if not " ".join("".join(item_children[0].itertext()).split()):
                return "Navigation labels must not be empty."
            trailing = item_children[1:]
            if len(trailing) > 1 or (
                trailing and element_local_name(trailing[0]) != "ol"
            ):
                return "Navigation li elements may contain only one nested ol after their label."
            if element_local_name(item_children[0]) == "span" and not trailing:
                return "An unlinked navigation span must be followed by a nested ol."
            if trailing:
                error = validate_nav_list(trailing[0])
                if error:
                    return error
        return None

    try:
        with zipfile.ZipFile(output_path) as epub_zip:
            names = epub_zip.namelist()
            if len(names) != len(set(names)):
                return False, "EPUB contains duplicate ZIP member names."
            if not names or names[0] != "mimetype":
                return False, "EPUB mimetype entry is missing or not first."
            if epub_zip.read("mimetype") != b"application/epub+zip":
                return False, "EPUB mimetype is invalid."
            mimetype_info = epub_zip.getinfo("mimetype")
            if mimetype_info.compress_type != zipfile.ZIP_STORED:
                return False, "EPUB mimetype entry must be stored without compression."
            if mimetype_info.extra:
                return False, "EPUB mimetype entry must not contain a ZIP extra field."
            for info in epub_zip.infolist():
                if info.flag_bits & 0x1:
                    return (
                        False,
                        f"EPUB ZIP entries must not be encrypted: {info.filename}",
                    )
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    return (
                        False,
                        f"Unsupported EPUB ZIP compression method: {info.filename}",
                    )
            if "META-INF/container.xml" not in names:
                return False, "EPUB container.xml is missing."

            container = ET.fromstring(epub_zip.read("META-INF/container.xml"))
            if (
                container.tag != f"{{{container_ns}}}container"
                or container.get("version") != "1.0"
            ):
                return (
                    False,
                    "EPUB container.xml has an invalid root element or version.",
                )
            rootfile = container.find(f".//{{{container_ns}}}rootfile")
            if rootfile is None:
                return (
                    False,
                    "EPUB package document referenced by container.xml is missing.",
                )
            opf_path = rootfile.get("full-path")
            if (
                not opf_path
                or opf_path not in names
                or rootfile.get("media-type") != "application/oebps-package+xml"
            ):
                return (
                    False,
                    "EPUB package document referenced by container.xml is missing.",
                )

            package = ET.fromstring(epub_zip.read(opf_path))
            if package.tag != f"{{{opf_ns}}}package" or package.get("version") != "3.0":
                return False, "Package document is not EPUB 3."
            metadata = package.find(f"{{{opf_ns}}}metadata")
            manifest = package.find(f"{{{opf_ns}}}manifest")
            spine = package.find(f"{{{opf_ns}}}spine")
            if metadata is None or manifest is None or spine is None:
                return (
                    False,
                    "Package document is missing metadata, manifest, or spine.",
                )

            titles = metadata.findall(f"{{{dc_ns}}}title")
            languages = metadata.findall(f"{{{dc_ns}}}language")
            identifiers = metadata.findall(f"{{{dc_ns}}}identifier")
            if not titles or not (titles[0].text or "").strip():
                return False, "EPUB metadata is missing dc:title."
            if not languages:
                return False, "EPUB metadata is missing dc:language."
            for language in languages:
                if not re.fullmatch(
                    r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*",
                    (language.text or "").strip(),
                ):
                    return (
                        False,
                        f"Invalid BCP 47 language tag: {(language.text or '').strip()}",
                    )
            unique_identifier = package.get("unique-identifier")
            if not unique_identifier or not any(
                node.get("id") == unique_identifier and (node.text or "").strip()
                for node in identifiers
            ):
                return (
                    False,
                    "Package unique-identifier does not resolve to a dc:identifier.",
                )

            modified = [
                node
                for node in metadata.findall(f"{{{opf_ns}}}meta")
                if node.get("property") == "dcterms:modified"
            ]
            if len(modified) != 1 or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                (modified[0].text or "").strip(),
            ):
                return (
                    False,
                    "EPUB must contain exactly one valid UTC dcterms:modified value.",
                )

            dates = metadata.findall(f"{{{dc_ns}}}date")
            if len(dates) > 1:
                return False, "EPUB metadata must not contain more than one dc:date."

            package_ids: set[str] = set()
            for node in package.iter():
                node_id = node.get("id")
                if not node_id:
                    continue
                if re.search(r"\s", node_id):
                    return False, f"Package document id contains whitespace: {node_id}"
                if node_id in package_ids:
                    return False, f"Package document contains a duplicate id: {node_id}"
                package_ids.add(node_id)

            for node in list(metadata):
                node_name = element_local_name(node)
                if node_name != "link" and not " ".join(
                    "".join(node.itertext()).split()
                ):
                    return False, f"EPUB metadata value must not be empty: {node_name}"
                refines = str(node.get("refines") or "")
                if refines and (
                    not refines.startswith("#") or refines[1:] not in package_ids
                ):
                    return (
                        False,
                        f"EPUB metadata refines target does not resolve: {refines}",
                    )
                if (
                    node_name == "meta"
                    and node.get("property") == "group-position"
                    and not re.fullmatch(
                        r"[0-9]+(?:\.[0-9]+)*", (node.text or "").strip()
                    )
                ):
                    return (
                        False,
                        f"Invalid EPUB group-position value: {(node.text or '').strip()}",
                    )
                if (
                    node_name == "meta"
                    and node.get("property") == "role"
                    and node.get("scheme") == "marc:relators"
                    and (node.text or "").strip() not in MARC_ROLE_VALUES
                ):
                    return (
                        False,
                        f"Unsupported MARC relator code: {(node.text or '').strip()}",
                    )

            manifest_ids: dict[str, Element[str]] = {}
            manifest_paths: dict[str, Element[str]] = {}
            remote_manifest_ids: set[str] = set()
            nav_members: list[str] = []
            for item in manifest.findall(f"{{{opf_ns}}}item"):
                item_id = item.get("id")
                href = item.get("href")
                media_type = item.get("media-type")
                if not item_id or not href or not media_type:
                    return False, "Manifest item is missing id, href, or media-type."
                parsed_href = urlparse(href)
                is_remote = parsed_href.scheme in {"http", "https"} or href.startswith(
                    "//"
                )
                member = "" if is_remote else resolve_member(opf_path, href)
                if not is_remote and not member:
                    return (
                        False,
                        f"Manifest resource must use a valid relative path: {href}",
                    )
                if not is_remote and member not in names:
                    return (
                        False,
                        f"Manifest resource is missing from the EPUB container: {href}",
                    )
                if item_id in manifest_ids or (member and member in manifest_paths):
                    return False, "Manifest contains duplicate IDs or resource paths."
                manifest_ids[item_id] = item
                if is_remote:
                    remote_manifest_ids.add(item_id)
                else:
                    manifest_paths[member] = item
                if "nav" in str(item.get("properties") or "").split():
                    if is_remote:
                        return (
                            False,
                            "The EPUB navigation document must be a local container resource.",
                        )
                    nav_members.append(member)

            if len(nav_members) != 1:
                return (
                    False,
                    "EPUB must contain exactly one navigation document manifest item.",
                )
            raw_spine_refs = [
                itemref.get("idref")
                for itemref in spine.findall(f"{{{opf_ns}}}itemref")
            ]
            if not raw_spine_refs or any(
                idref is None or idref not in manifest_ids for idref in raw_spine_refs
            ):
                return (
                    False,
                    "EPUB spine is empty or references an unknown manifest item.",
                )
            spine_refs = [idref for idref in raw_spine_refs if idref is not None]
            if any(idref in remote_manifest_ids for idref in spine_refs):
                return False, "EPUB spine resources must not be remote."
            spine_members: set[str] = {
                member
                for member, item in manifest_paths.items()
                if item.get("id") in spine_refs
            }
            if any(
                manifest_ids[idref].get("media-type") != "application/xhtml+xml"
                for idref in spine_refs
            ):
                return (
                    False,
                    "This exporter requires every spine item to be an XHTML content document.",
                )

            cover_items = [
                item
                for item in manifest_ids.values()
                if "cover-image" in str(item.get("properties") or "").split()
            ]
            if len(cover_items) > 1 or any(
                item.get("media-type") not in CORE_IMAGE_MEDIA_TYPES
                for item in cover_items
            ):
                return (
                    False,
                    "EPUB cover-image must identify at most one supported image resource.",
                )

            embedded_refs = {
                "img": "src",
                "script": "src",
                "audio": "src",
                "video": "src",
                "source": "src",
                "track": "src",
                "object": "data",
                "link": "href",
            }
            xhtml_ids: dict[str, set[str]] = {}
            xhtml_roots: dict[str, Element[str]] = {}
            for member, item in manifest_paths.items():
                if item.get("media-type") != "application/xhtml+xml":
                    continue
                root = ET.fromstring(epub_zip.read(member))
                if root.tag != f"{{{xhtml_ns}}}html":
                    return (
                        False,
                        f"XHTML resource has an invalid root element: {member}",
                    )
                if root.find(f"{{{xhtml_ns}}}head/{{{xhtml_ns}}}title") is None:
                    return (
                        False,
                        f"XHTML resource is missing a document title: {member}",
                    )
                ids: list[str] = []
                id_references: dict[str, tuple[str, int | None]] = {}
                relationship_references: list[
                    tuple[str, tuple[str, ...], int | None]
                ] = []
                next_table_id = 0
                pending_nodes: list[tuple[Element[str], int | None]] = [(root, None)]
                while pending_nodes:
                    node, containing_table = pending_nodes.pop()
                    table_id = containing_table
                    if node.tag == f"{{{xhtml_ns}}}table":
                        next_table_id += 1
                        table_id = next_table_id
                    node_id = node.get("id")
                    if node_id:
                        if re.search(r"\s", node_id):
                            return (
                                False,
                                f"XHTML id contains whitespace: {member}#{node_id}",
                            )
                        ids.append(node_id)
                        id_references[node_id] = (node.tag, table_id)
                    for attribute in (
                        "aria-labelledby",
                        "aria-describedby",
                        "headers",
                    ):
                        raw_references = node.get(attribute)
                        if raw_references is not None:
                            relationship_references.append(
                                (attribute, tuple(raw_references.split()), table_id)
                            )
                    pending_nodes.extend(
                        (child, table_id) for child in reversed(list(node))
                    )
                if len(ids) != len(set(ids)):
                    return False, f"XHTML resource contains duplicate IDs: {member}"
                for attribute, references, source_table_id in relationship_references:
                    for reference in references:
                        target = id_references.get(reference)
                        if target is None:
                            return (
                                False,
                                "XHTML accessibility relationship contains an unresolved local ID reference.",
                            )
                        target_tag, target_table_id = target
                        if attribute == "headers" and (
                            target_tag != f"{{{xhtml_ns}}}th"
                            or (
                                source_table_id is not None
                                and target_table_id != source_table_id
                            )
                        ):
                            return (
                                False,
                                "XHTML headers relationship must reference a th in the same table.",
                            )
                xhtml_ids[member] = set(ids)
                xhtml_roots[member] = root

            for member, root in xhtml_roots.items():
                for embedded_name, attr_name in embedded_refs.items():
                    for node in root.findall(f".//{{{xhtml_ns}}}{embedded_name}"):
                        href = node.get(attr_name)
                        if not href or href.startswith("#"):
                            continue
                        parsed = urlparse(href)
                        if embedded_name == "link" and parsed.scheme in {
                            "http",
                            "https",
                        }:
                            continue
                        referenced_member = resolve_member(member, href)
                        if (
                            not referenced_member
                            or referenced_member not in manifest_paths
                        ):
                            return (
                                False,
                                f"XHTML resource reference is not manifested: {member} -> {href}",
                            )
                        if (
                            embedded_name == "img"
                            and manifest_paths[referenced_member].get("media-type")
                            not in CORE_IMAGE_MEDIA_TYPES
                        ):
                            return (
                                False,
                                f"EPUB image resource is not a supported core media type: {href}",
                            )

                for anchor in root.findall(f".//{{{xhtml_ns}}}a"):
                    href = anchor.get("href")
                    if not href:
                        continue
                    target_member, fragment, external = href_target(member, href)
                    if external:
                        continue
                    if not target_member or target_member not in manifest_paths:
                        return (
                            False,
                            f"EPUB hyperlink target is missing or unmanifested: {member} -> {href}",
                        )
                    if fragment and fragment not in xhtml_ids.get(target_member, set()):
                        return (
                            False,
                            f"EPUB hyperlink fragment does not resolve: {member} -> {href}",
                        )

            nav_root = ET.fromstring(epub_zip.read(nav_members[0]))
            nav_nodes_by_type: dict[str, list[Element[str]]] = {}
            for node in nav_root.findall(f".//{{{xhtml_ns}}}nav"):
                for nav_type in str(node.get(f"{{{epub_ns}}}type") or "").split():
                    nav_nodes_by_type.setdefault(nav_type, []).append(node)
                direct_children = list(node)
                headings = [
                    child
                    for child in direct_children
                    if element_local_name(child) in {"h1", "h2", "h3", "h4", "h5", "h6"}
                ]
                lists = [
                    child
                    for child in direct_children
                    if element_local_name(child) == "ol"
                ]
                if (
                    len(headings) > 1
                    or len(lists) != 1
                    or len(direct_children) != len(headings) + 1
                ):
                    return (
                        False,
                        "EPUB navigation elements must contain an optional heading followed by exactly one ol.",
                    )
                nav_error = validate_nav_list(lists[0])
                if nav_error:
                    return False, nav_error

            toc_nodes = nav_nodes_by_type.get("toc", [])
            if len(toc_nodes) != 1 or not toc_nodes[0].findall(f".//{{{xhtml_ns}}}a"):
                return False, "EPUB navigation document is missing a non-empty toc nav."
            if (
                len(nav_nodes_by_type.get("page-list", [])) > 1
                or len(nav_nodes_by_type.get("landmarks", [])) > 1
            ):
                return (
                    False,
                    "EPUB navigation document contains duplicate navigation aid types.",
                )
            for nav_type in ("toc", "page-list", "landmarks"):
                for nav_node in nav_nodes_by_type.get(nav_type, []):
                    for anchor in nav_node.findall(f".//{{{xhtml_ns}}}a"):
                        member, fragment, external = href_target(
                            nav_members[0], anchor.get("href") or ""
                        )
                        if external or member not in spine_members:
                            return (
                                False,
                                f"EPUB {nav_type} navigation target is not in the spine: {anchor.get('href')}",
                            )
                        if fragment and fragment not in xhtml_ids.get(member, set()):
                            return (
                                False,
                                f"EPUB {nav_type} navigation fragment does not resolve: {anchor.get('href')}",
                            )

            semantic_error = _publishing_semantic_error(
                xhtml_roots, nav_members[0], href_target
            )
            if semantic_error:
                return False, semantic_error

            expected_manifest_members = {
                name
                for name in names
                if name not in {"mimetype", "META-INF/container.xml", opf_path}
                and not name.startswith("META-INF/")
                and not name.endswith("/")
            }
            unmanifested = sorted(expected_manifest_members - set(manifest_paths))
            if unmanifested:
                return (
                    False,
                    "EPUB contains unmanifested publication resources: "
                    + ", ".join(unmanifested[:5]),
                )
    except Exception as exc:
        return False, str(exc)
    return True, None


def validate_epub_file(
    output_path: str, *, config: Mapping[str, object] | None = None
) -> EpubValidationResult:
    """Run mandatory archive checks, EPUBCheck, and Ace when available."""
    passed, message = validate_epub_structure(output_path)
    if not passed:
        return EpubValidationResult(False, message, "not_run")
    epubcheck_result = run_official_epubcheck(output_path, config=config)
    if not epubcheck_result.passed:
        return epubcheck_result

    ace_result = run_ace(output_path, config=config)
    optional_findings = ace_result.ace_findings and not _ace_required(config)
    return EpubValidationResult(
        epubcheck_result.passed and (ace_result.passed or optional_findings),
        epubcheck_result.message or ace_result.message,
        epubcheck_result.epubcheck,
        epubcheck_result.version,
        ace_result.ace,
        ace_result.ace_version,
        ace_result.ace_findings,
    )


_VERSION_PATTERN = re.compile(
    r"\bEPUBCheck(?:\s+version)?\s+v?"
    r"([0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z.-]+)?)",
    re.IGNORECASE,
)
_MAX_DIAGNOSTIC_CHARS = 4000


def _version_from_output(output: str) -> str | None:
    match = _VERSION_PATTERN.search(output)
    return match.group(1) if match else None


def _bounded_message(message: str) -> str:
    return message[-_MAX_DIAGNOSTIC_CHARS:]


def _probe_epubcheck_version(
    command: list[str], *, scrub_paths: tuple[str, ...]
) -> str | None:
    probe_command = [*command[:-1], "--version"]
    try:
        result = subprocess.run(
            probe_command,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None

    output = "\n".join((result.stdout or "", result.stderr or ""))
    for path in (*scrub_paths, *probe_command):
        if path:
            output = output.replace(path, "EPUBCheck")
    return _version_from_output(output[:_MAX_DIAGNOSTIC_CHARS])


def run_official_epubcheck(
    output_path: str, *, config: Mapping[str, object] | None = None
) -> EpubValidationResult:
    """Run EPUBCheck when installed or explicitly configured."""
    app_config = config or {}
    executable = app_config.get("EPUBCHECK_EXECUTABLE") or os.environ.get(
        "EPUBCHECK_EXECUTABLE"
    )
    jar_path = app_config.get("EPUBCHECK_JAR") or os.environ.get("EPUBCHECK_JAR")
    required_value = app_config.get("EPUBCHECK_REQUIRED")
    if "EPUBCHECK_REQUIRED" not in app_config:
        required_value = os.environ.get("EPUBCHECK_REQUIRED")
    epubcheck_required = required_value is True or str(
        required_value
    ).strip().lower() in {"1", "true", "yes", "on"}
    command: list[str] | None = None
    configured = bool(executable or jar_path)

    if executable:
        resolved = shutil.which(str(executable))
        if resolved is None and Path(str(executable)).is_file():
            resolved = str(Path(str(executable)))
        if resolved is None:
            return EpubValidationResult(
                False, "Configured EPUBCheck executable was not found.", "failed"
            )
        command = [resolved, output_path]
    elif jar_path:
        jar = Path(str(jar_path))
        java = shutil.which("java")
        if not jar.is_file():
            return EpubValidationResult(
                False, "Configured EPUBCheck JAR was not found.", "failed"
            )
        if java is None:
            return EpubValidationResult(
                False,
                "EPUBCheck JAR is configured, but Java is not available.",
                "failed",
            )
        command = [java, "-jar", str(jar), output_path]
    else:
        discovered = shutil.which("epubcheck")
        if discovered:
            command = [discovered, output_path]

    if command is None:
        if epubcheck_required:
            return EpubValidationResult(
                False,
                "Official EPUBCheck validation is required, but no EPUBCHECK_EXECUTABLE, "
                "EPUBCHECK_JAR, or epubcheck command is available.",
                "failed",
            )
        return EpubValidationResult(True, None, "not_run")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        details = str(exc)
        scrub_paths = [output_path]
        if executable:
            scrub_paths.append(str(executable))
        if jar_path:
            scrub_paths.append(str(jar_path))
        scrub_paths.extend(command)
        for path in scrub_paths:
            if path:
                details = details.replace(path, "EPUBCheck")
        details = _bounded_message(details)
        prefix = (
            "Unable to run configured EPUBCheck: "
            if configured
            else "Unable to run EPUBCheck discovered on PATH: "
        )
        return EpubValidationResult(False, _bounded_message(prefix + details), "failed")

    output = "\n".join((result.stdout or "", result.stderr or ""))
    version = _version_from_output(output)
    if version is None:
        scrub_paths = [output_path]
        if executable:
            scrub_paths.append(str(executable))
        if jar_path:
            scrub_paths.append(str(jar_path))
        version = _probe_epubcheck_version(command, scrub_paths=tuple(scrub_paths))
    if result.returncode != 0:
        details = (
            result.stderr or result.stdout or "EPUBCheck reported validation errors."
        ).strip()
        if output_path:
            details = details.replace(output_path, "EPUB")
        return EpubValidationResult(False, _bounded_message(details), "failed", version)
    return EpubValidationResult(True, None, "passed", version)


_ACE_OUTCOMES = frozenset({"pass", "fail", "cantTell", "inapplicable", "untested"})
_ACE_VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z.-]+)?\Z")
_ACE_MAX_REPORT_BYTES = 8 * 1024 * 1024
_ACE_MAX_VERSION_CHARS = 128


def _ace_required(config: Mapping[str, object] | None) -> bool:
    if config is not None and "ACE_REQUIRED" in config:
        required_value = config["ACE_REQUIRED"]
    else:
        required_value = os.environ.get("ACE_REQUIRED")
    return required_value is True or str(required_value).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _ace_outcome(assertion: Mapping[str, object]) -> str | None:
    result = assertion.get("earl:result", assertion.get("result"))
    if not isinstance(result, Mapping):
        return None
    result_mapping = cast(Mapping[str, object], result)
    outcome = result_mapping.get("earl:outcome", result_mapping.get("outcome"))
    if not isinstance(outcome, str):
        return None
    normalized = outcome.removeprefix("earl:")
    return normalized if normalized in _ACE_OUTCOMES else None


def _ace_report_message(report: object) -> tuple[str | None, bool]:
    malformed = "Ace accessibility report is malformed or unsupported."
    if not isinstance(report, Mapping):
        return malformed, False
    report_mapping = cast(Mapping[str, object], report)
    if report_mapping.get("@type") != "earl:report":
        return malformed, False
    documents_value = report_mapping.get("assertions")
    if not isinstance(documents_value, list):
        return malformed, False
    documents = cast(list[object], documents_value)

    failed = False
    incomplete = False
    for document_value in documents:
        if not isinstance(document_value, Mapping):
            return malformed, False
        document = cast(Mapping[str, object], document_value)
        tests_value = document.get("assertions")
        document_outcome = _ace_outcome(document)
        if not isinstance(tests_value, list) or document_outcome is None:
            return malformed, False
        tests = cast(list[object], tests_value)

        outcomes = [document_outcome]
        for test_value in tests:
            if not isinstance(test_value, Mapping):
                return malformed, False
            test = cast(Mapping[str, object], test_value)
            if "assertions" in test:
                return malformed, False
            test_outcome = _ace_outcome(test)
            if test_outcome is None:
                return malformed, False
            outcomes.append(test_outcome)

        failed = failed or "fail" in outcomes
        incomplete = incomplete or any(
            outcome in {"cantTell", "untested"} for outcome in outcomes
        )

    if incomplete:
        return "Ace accessibility validation has incomplete results.", False
    if failed:
        return "Ace reported accessibility findings.", True
    return None, False


def _validated_ace_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    version = value.strip()
    if len(version) > _ACE_MAX_VERSION_CHARS:
        return None
    return version if _ACE_VERSION_PATTERN.fullmatch(version) else None


def _ace_version_from_report(report: object) -> str | None:
    if not isinstance(report, Mapping):
        return None
    report_mapping = cast(Mapping[str, object], report)
    if report_mapping.get("@type") != "earl:report":
        return None
    asserted_by_value = report_mapping.get("earl:assertedBy")
    if not isinstance(asserted_by_value, Mapping):
        return None
    asserted_by = cast(Mapping[str, object], asserted_by_value)
    release_value = asserted_by.get("doap:release")
    if not isinstance(release_value, Mapping):
        return None
    release = cast(Mapping[str, object], release_value)
    revision: object = release.get("doap:revision")
    return _validated_ace_version(revision)


def _read_ace_report(report_path: Path) -> object | None:
    try:
        with report_path.open("rb") as report_file:
            raw_report = report_file.read(_ACE_MAX_REPORT_BYTES + 1)
    except OSError:
        return None
    if len(raw_report) > _ACE_MAX_REPORT_BYTES:
        return None

    def reject_constant(value: str) -> None:
        raise ValueError(f"Invalid JSON constant: {value}")

    try:
        return cast(object, json.loads(raw_report, parse_constant=reject_constant))
    except UnicodeDecodeError, ValueError, RecursionError:
        return None


def _probe_ace_version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    return _validated_ace_version((result.stdout or "")[:_ACE_MAX_VERSION_CHARS])


def run_ace(
    output_path: str, config: Mapping[str, object] | None = None
) -> EpubValidationResult:
    """Run local DAISY Ace and validate its bounded EARL JSON report."""
    app_config = config or {}
    executable = app_config.get("ACE_EXECUTABLE") or os.environ.get("ACE_EXECUTABLE")
    ace_required = _ace_required(config)

    configured = bool(executable)
    if executable:
        resolved = shutil.which(str(executable))
        if resolved is None and Path(str(executable)).is_file():
            resolved = str(Path(str(executable)))
        if resolved is None:
            return EpubValidationResult(
                False,
                "Configured Ace executable was not found.",
                "not_run",
                ace="failed",
            )
    else:
        resolved = shutil.which("ace")

    if resolved is None:
        if ace_required:
            return EpubValidationResult(
                False,
                "Ace accessibility validation is required, but Ace is unavailable.",
                "not_run",
                ace="failed",
            )
        return EpubValidationResult(True, None, "not_run", ace="not_run")

    try:
        scratch_context = tempfile.TemporaryDirectory(prefix="carlina-ace-")
    except OSError:
        return EpubValidationResult(
            False,
            "Unable to prepare temporary files for Ace accessibility validation.",
            "not_run",
            ace="failed",
        )

    with scratch_context as scratch:
        scratch_path = Path(scratch)
        report_dir = scratch_path / "report"
        work_dir = scratch_path / "work"
        command = [
            resolved,
            "--silent",
            "--doNotReportMedia",
            "--timeout",
            "30000",
            "--outdir",
            str(report_dir),
            "--tempdir",
            str(work_dir),
            output_path,
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return EpubValidationResult(
                False,
                "Ace accessibility validation timed out.",
                "not_run",
                ace="failed",
            )
        except OSError, subprocess.SubprocessError:
            message = (
                "Unable to run configured Ace accessibility validation."
                if configured
                else "Unable to run Ace accessibility validation discovered on PATH."
            )
            return EpubValidationResult(False, message, "not_run", ace="failed")

        report_path = report_dir / "report.json"
        report = _read_ace_report(report_path)
        version = _ace_version_from_report(report) or _probe_ace_version(resolved)

        if result.returncode != 0:
            return EpubValidationResult(
                False,
                "Ace exited with an error during accessibility validation.",
                "not_run",
                ace="failed",
                ace_version=version,
            )
        if report is None:
            return EpubValidationResult(
                False,
                "Ace accessibility report is missing, invalid, or too large.",
                "not_run",
                ace="failed",
                ace_version=version,
            )

        report_message, ace_findings = _ace_report_message(report)
        if report_message is not None:
            return EpubValidationResult(
                False,
                report_message,
                "not_run",
                ace="failed",
                ace_version=version,
                ace_findings=ace_findings,
            )
        return EpubValidationResult(
            True,
            None,
            "not_run",
            ace="passed",
            ace_version=version,
        )
