from __future__ import annotations

import logging
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path
from xml.etree.ElementTree import Element, ParseError

from bs4 import BeautifulSoup
from sqlalchemy.orm.attributes import flag_modified

from .archive_safety import (
    MAX_ARCHIVE_XML_BYTES,
    parse_safe_xml,
    read_zip_member,
    validate_zip_archive,
)
from .cover_candidate import normalize_cover_candidate
from .heading_structure import normalize_heading_structure
from .models import DocumentMetadata
from .note_arrangement import normalize_note_arrangement

logger = logging.getLogger(__name__)

EPUB_OPF_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "opf": "http://www.idpf.org/2007/opf",
    "pkg": "http://www.idpf.org/2007/opf",
}
EPUB_CONTAINER_NS = {"ctr": "urn:oasis:names:tc:opendocument:xmlns:container"}
DOCX_CORE_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}
ODT_META_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "dc": "http://purl.org/dc/elements/1.1/",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
}
CORE_CONTRIBUTOR_ROLES = ("authors", "editors", "translators")
STANDARD_IDENTIFIER_SCHEMES = {
    "isbn": "ISBN",
    "issn": "ISSN",
    "doi": "DOI",
    "uuid": "UUID",
    "uri": "URI",
}
EPUB_ROLE_MAP = {
    "aut": "authors",
    "aui": "authors",
    "edt": "editors",
    "trl": "translators",
    "ill": "illustrator",
    "nrt": "narrator",
    "ann": "annotator",
    "cmp": "compiler",
    "ctb": "contributor",
}

COMMON_LANGUAGE_TAGS = {
    "arabic": "ar",
    "chinese": "zh",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "finnish": "fi",
    "french": "fr",
    "german": "de",
    "greek": "el",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "norwegian": "no",
    "polish": "pl",
    "portuguese": "pt",
    "romanian": "ro",
    "russian": "ru",
    "slovak": "sk",
    "slovenian": "sl",
    "spanish": "es",
    "swedish": "sv",
    "thai": "th",
    "turkish": "tr",
    "ukrainian": "uk",
    "vietnamese": "vi",
}

ONIX_CODELIST5_SCHEMES = {
    "02": "ISBN",
    "06": "DOI",
    "15": "ISBN",
    "22": "URN",
}


def _clean_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return re.sub(r"\s+", " ", text)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen = set()
    normalized = []
    for value in values:
        text = _clean_text(value)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _split_text_values(value, split_commas: bool = False) -> list[str]:
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value if item not in (None, "")]
    else:
        text = str(value)
        parts = [text]
        for separator in ("\r\n", "\n", ";", "|"):
            next_parts = []
            for part in parts:
                next_parts.extend(part.split(separator))
            parts = next_parts
        if split_commas:
            next_parts = []
            for part in parts:
                next_parts.extend(part.split(","))
            parts = next_parts

    return _dedupe_strings(parts)


def _normalize_language_tag(value) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    mapped = COMMON_LANGUAGE_TAGS.get(text.casefold())
    if mapped:
        return mapped
    if not re.fullmatch(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*", text):
        return None
    parts = re.split(r"[-_]", text)
    if not parts:
        return None

    normalized_parts = []
    for index, part in enumerate(parts):
        if not part:
            continue
        if index == 0:
            normalized_parts.append(part.lower())
        elif len(part) == 2 and part.isalpha():
            normalized_parts.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            normalized_parts.append(part.title())
        else:
            normalized_parts.append(part)
    return "-".join(normalized_parts) or None


def _normalize_subjects(value) -> list[str]:
    return _split_text_values(value, split_commas=True)


def _normalize_other_contributors(raw_other) -> list[dict]:
    entries = []
    if isinstance(raw_other, dict):
        raw_other = [raw_other]

    if not isinstance(raw_other, list):
        return entries

    seen = set()
    for entry in raw_other:
        if not isinstance(entry, Mapping):
            continue
        role = _clean_text(entry.get("role"))
        name = _clean_text(entry.get("name"))
        if not role or not name:
            continue
        dedupe_key = (role.casefold(), name.casefold())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        entries.append({"role": role, "name": name})
    return entries


def _normalize_contributors(raw_contributors, fallback_author=None) -> dict:
    normalized = {
        "authors": [],
        "editors": [],
        "translators": [],
        "other": [],
    }

    if isinstance(raw_contributors, Mapping):
        normalized["authors"] = _split_text_values(raw_contributors.get("authors"))
        normalized["editors"] = _split_text_values(raw_contributors.get("editors"))
        normalized["translators"] = _split_text_values(raw_contributors.get("translators"))
        normalized["other"] = _normalize_other_contributors(raw_contributors.get("other"))

        for raw_role, raw_people in raw_contributors.items():
            role = _clean_text(raw_role)
            if not role or role in (*CORE_CONTRIBUTOR_ROLES, "other"):
                continue
            for name in _split_text_values(raw_people):
                normalized["other"].append({"role": role, "name": name})
    elif isinstance(raw_contributors, list):
        normalized["authors"] = _split_text_values(raw_contributors)
    elif isinstance(raw_contributors, str):
        normalized["authors"] = _split_text_values(raw_contributors)

    if fallback_author and not normalized["authors"]:
        normalized["authors"] = _split_text_values(fallback_author)

    normalized["other"] = _normalize_other_contributors(normalized["other"])
    if any(normalized.values()):
        return normalized
    return {}


def _normalize_identifier_scheme(value) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    return STANDARD_IDENTIFIER_SCHEMES.get(text.casefold(), text)


def _normalize_identifiers(value) -> list[dict]:
    if value is None:
        return []

    if isinstance(value, Mapping):
        raw_entries = [{"scheme": key, "value": raw_value} for key, raw_value in value.items()]
    elif isinstance(value, (list, tuple, set)):
        raw_entries = list(value)
    else:
        raw_entries = _split_text_values(value)

    identifiers = []
    seen = set()
    for entry in raw_entries:
        scheme = None
        label = None
        identifier_value = None

        if isinstance(entry, Mapping):
            scheme = _normalize_identifier_scheme(
                entry.get("scheme") or entry.get("type") or entry.get("identifier_type")
            )
            label = _clean_text(entry.get("label") or entry.get("display"))
            identifier_value = _clean_text(entry.get("value") or entry.get("identifier") or entry.get("content"))
        else:
            match = re.match(r"^\s*([^:=|]+)\s*[:=|]\s*(.+?)\s*$", str(entry))
            if match:
                scheme = _normalize_identifier_scheme(match.group(1))
                identifier_value = _clean_text(match.group(2))
            else:
                identifier_value = _clean_text(entry)

        if not identifier_value:
            continue
        lowered_value = identifier_value.casefold()
        if not scheme:
            if lowered_value.startswith("urn:isbn:") or re.fullmatch(r"(?:97[89])?[0-9Xx-]{10,17}", identifier_value):
                scheme = "ISBN"
            elif lowered_value.startswith("urn:issn:"):
                scheme = "ISSN"
            elif lowered_value.startswith(("urn:doi:", "doi:")) or re.match(r"^10\.\d{4,9}/\S+$", identifier_value):
                scheme = "DOI"
            elif lowered_value.startswith("urn:uuid:"):
                scheme = "UUID"
            elif lowered_value.startswith(("http://", "https://")):
                scheme = "URI"
        dedupe_key = ((scheme or "").casefold(), identifier_value.casefold(), (label or "").casefold())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        normalized_entry = {"value": identifier_value}
        if scheme:
            normalized_entry["scheme"] = scheme
        if label:
            normalized_entry["label"] = label
        identifiers.append(normalized_entry)

    return identifiers


def _normalize_positive_int_list(value) -> list[int]:
    if value is None:
        return []

    values = value if isinstance(value, (list, tuple, set)) else [value]
    normalized = []
    seen = set()
    for item in values:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number <= 0 or number in seen:
            continue
        seen.add(number)
        normalized.append(number)
    return normalized


def _normalize_table_of_contents(value) -> list[dict]:
    if not isinstance(value, list):
        return []

    toc_entries = []
    for raw_entry in value:
        if not isinstance(raw_entry, Mapping):
            continue
        title = _clean_text(raw_entry.get("title"))
        if not title:
            continue
        entry = {"title": title}

        try:
            level = int(raw_entry.get("level", 1) or 1)
        except (TypeError, ValueError):
            level = 1
        entry["level"] = max(1, level)

        page = _clean_text(raw_entry.get("page"))
        page_label = _clean_text(raw_entry.get("page_label")) or page
        if page:
            entry["page"] = page
        if page_label:
            entry["page_label"] = page_label

        try:
            pdf_page = int(raw_entry.get("pdf_page"))
        except (TypeError, ValueError):
            pdf_page = None
        if pdf_page and pdf_page > 0:
            entry["pdf_page"] = pdf_page
        toc_entries.append(entry)

    return toc_entries


def _normalize_pages_to_remove(value) -> list[dict]:
    if not isinstance(value, list):
        return []

    normalized = []
    seen = set()
    for raw_entry in value:
        page = None
        reason = None

        if isinstance(raw_entry, Mapping):
            try:
                page = int(raw_entry.get("page"))
            except (TypeError, ValueError):
                page = None
            reason = _clean_text(raw_entry.get("reason")) or "unspecified"
            has_safety_decision = "safe_to_remove" in raw_entry
            safe_to_remove = raw_entry.get("safe_to_remove") is True
            try:
                confidence = float(raw_entry.get("confidence"))
            except (TypeError, ValueError):
                confidence = None
        else:
            try:
                page = int(raw_entry)
            except (TypeError, ValueError):
                page = None
            reason = "unspecified"
            has_safety_decision = False
            safe_to_remove = False
            confidence = None

        if not page or page <= 0 or page in seen:
            continue
        seen.add(page)
        entry = {"page": page, "reason": reason}
        if has_safety_decision:
            entry["safe_to_remove"] = safe_to_remove
        if confidence is not None:
            entry["confidence"] = max(0.0, min(1.0, confidence))
        normalized.append(entry)
    return normalized


def _has_meaningful_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_has_meaningful_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_meaningful_value(item) for item in value)
    return True


def has_meaningful_document_metadata(metadata) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    return any(_has_meaningful_value(value) for value in metadata.values())


def normalize_document_metadata_payload(payload: Mapping | None) -> dict:
    if not isinstance(payload, Mapping):
        return {}

    normalized = {}

    for field in (
        "title",
        "subtitle",
        "publication_type",
        "publisher",
        "publication_date",
        "isbn",
        "issn",
        "rights",
        "source",
        "description",
        "journal_or_series",
        "volume",
        "issue",
        "cover_image_id",
    ):
        text = _clean_text(payload.get(field))
        if text:
            normalized[field] = text

    language = _normalize_language_tag(payload.get("language") or payload.get("lang"))
    if language:
        normalized["language"] = language

    contributors = _normalize_contributors(payload.get("contributors"), payload.get("author"))
    if contributors:
        normalized["contributors"] = contributors

    subjects = _normalize_subjects(payload.get("subjects"))
    if subjects:
        normalized["subjects"] = subjects

    identifiers = _normalize_identifiers(payload.get("identifiers"))
    if identifiers:
        normalized["identifiers"] = identifiers

    table_of_contents = _normalize_table_of_contents(payload.get("table_of_contents"))
    if table_of_contents:
        normalized["table_of_contents"] = table_of_contents

    heading_structure = normalize_heading_structure(
        payload.get("heading_structure"),
        table_of_contents=table_of_contents,
    )
    if heading_structure:
        normalized["heading_structure"] = heading_structure

    pages_to_remove = _normalize_pages_to_remove(payload.get("pages_to_remove"))
    if pages_to_remove:
        normalized["pages_to_remove"] = pages_to_remove

    extraction_notes = _split_text_values(payload.get("extraction_notes"))
    if extraction_notes:
        normalized["extraction_notes"] = extraction_notes

    source_pages = _normalize_positive_int_list(payload.get("source_pages"))
    if source_pages:
        normalized["source_pages"] = source_pages

    note_arrangement = payload.get("note_arrangement")
    if isinstance(note_arrangement, Mapping):
        normalized["note_arrangement"] = normalize_note_arrangement(note_arrangement)

    cover_candidate = normalize_cover_candidate(payload.get("cover_candidate"))
    if cover_candidate:
        normalized["cover_candidate"] = cover_candidate

    for identifier in identifiers:
        scheme = str(identifier.get("scheme") or "").casefold()
        if scheme == "isbn" and "isbn" not in normalized:
            normalized["isbn"] = re.sub(r"^urn:isbn:", "", identifier["value"], flags=re.IGNORECASE)
        if scheme == "issn" and "issn" not in normalized:
            normalized["issn"] = re.sub(r"^urn:issn:", "", identifier["value"], flags=re.IGNORECASE)

    if (
        not has_meaningful_document_metadata(normalized)
        and "note_arrangement" not in normalized
        and "cover_candidate" not in normalized
    ):
        return {}

    return normalized


def get_primary_creator_names(metadata: Mapping | None) -> list[str]:
    normalized = normalize_document_metadata_payload(metadata)
    contributors = normalized.get("contributors") if isinstance(normalized.get("contributors"), Mapping) else {}
    authors = contributors.get("authors") if isinstance(contributors.get("authors"), list) else []
    editors = contributors.get("editors") if isinstance(contributors.get("editors"), list) else []
    translators = contributors.get("translators") if isinstance(contributors.get("translators"), list) else []
    return authors or editors or translators or []


def get_effective_identifier_entries(metadata: Mapping | None, fallback_document_id: str | None = None) -> list[dict]:
    normalized = normalize_document_metadata_payload(metadata)
    identifiers = list(normalized.get("identifiers", [])) if isinstance(normalized.get("identifiers"), list) else []
    seen = {
        (
            str(entry.get("scheme") or "").casefold(),
            str(entry.get("value") or "").casefold(),
            str(entry.get("label") or "").casefold(),
        )
        for entry in identifiers
        if isinstance(entry, Mapping)
    }

    for scheme_key in ("isbn", "issn"):
        value = _clean_text(normalized.get(scheme_key))
        scheme = scheme_key.upper()
        dedupe_key = (scheme.casefold(), (value or "").casefold(), "")
        if value and dedupe_key not in seen:
            seen.add(dedupe_key)
            identifiers.append({"scheme": scheme, "value": value})

    if fallback_document_id:
        uuid_value = f"urn:uuid:{fallback_document_id}"
        dedupe_key = ("uuid", uuid_value.casefold(), "")
        if dedupe_key not in seen:
            identifiers.insert(0, {"scheme": "UUID", "value": uuid_value})

    return identifiers


def get_document_display_title(document, metadata: Mapping | None = None) -> str:
    effective_metadata = normalize_document_metadata_payload(metadata) if metadata is not None else get_canonical_document_metadata(document)
    title = _clean_text(effective_metadata.get("title"))
    if title:
        return title
    filename = getattr(document, "filename", "") or ""
    name = Path(filename).stem.strip()
    return name or "Untitled Document"


def get_canonical_document_metadata(document) -> dict:
    settings_metadata = {}
    settings = getattr(document, "settings", None)
    if isinstance(settings, Mapping):
        settings_metadata = normalize_document_metadata_payload(settings.get("document_metadata"))

    record_metadata = {}
    metadata_record = getattr(document, "metadata_record", None)
    if metadata_record is not None:
        record_metadata = normalize_document_metadata_payload(metadata_record.to_dict())

    if not record_metadata:
        return settings_metadata
    if not settings_metadata:
        return record_metadata

    merged = dict(settings_metadata)
    for key, value in record_metadata.items():
        if _has_meaningful_value(value) or key not in merged:
            merged[key] = value
    return normalize_document_metadata_payload(merged)


def sync_document_metadata(document, metadata: Mapping | None = None, *, ensure_record: bool = True, mark_stale: bool | None = None) -> dict:
    normalized = normalize_document_metadata_payload(
        metadata if metadata is not None else get_canonical_document_metadata(document)
    )

    settings = dict(document.settings or {})
    if normalized:
        settings["document_metadata"] = normalized
    else:
        settings.pop("document_metadata", None)

    if mark_stale is not None:
        settings["metadata_stale"] = bool(mark_stale)

    document.settings = settings
    flag_modified(document, "settings")

    metadata_record = getattr(document, "metadata_record", None)
    if metadata_record is None and ensure_record:
        metadata_record = DocumentMetadata(document_id=document.id)
        document.metadata_record = metadata_record

    if metadata_record is None:
        return normalized

    contributors = normalized.get("contributors") if isinstance(normalized.get("contributors"), Mapping) else {}
    authors = contributors.get("authors") if isinstance(contributors.get("authors"), list) else []

    metadata_record.title = normalized.get("title")
    metadata_record.subtitle = normalized.get("subtitle")
    metadata_record.author = "; ".join(authors) if authors else None
    metadata_record.publication_type = normalized.get("publication_type")
    metadata_record.publisher = normalized.get("publisher")
    metadata_record.publication_date = normalized.get("publication_date")
    metadata_record.isbn = normalized.get("isbn")
    metadata_record.issn = normalized.get("issn")
    metadata_record.language = normalized.get("language")
    metadata_record.rights = normalized.get("rights")
    metadata_record.source = normalized.get("source")
    metadata_record.description = normalized.get("description")
    metadata_record.journal_or_series = normalized.get("journal_or_series")
    metadata_record.volume = normalized.get("volume")
    metadata_record.issue = normalized.get("issue")
    metadata_record.cover_image_id = normalized.get("cover_image_id")
    metadata_record.contributors = normalized.get("contributors")
    metadata_record.subjects = normalized.get("subjects")
    metadata_record.identifiers = normalized.get("identifiers")
    metadata_record.table_of_contents = normalized.get("table_of_contents")
    metadata_record.pages_to_remove = normalized.get("pages_to_remove")
    metadata_record.source_pages = normalized.get("source_pages")
    metadata_record.extraction_notes = normalized.get("extraction_notes")

    return normalized


def initialize_document_metadata(document, source_path: Path | str | None = None) -> dict:
    if source_path is None:
        source_path = getattr(document, "file_path", None)

    metadata = extract_source_document_metadata(source_path, filename=getattr(document, "filename", None))
    return sync_document_metadata(document, metadata, ensure_record=True, mark_stale=False)


def extract_source_document_metadata(source_path: Path | str | None, filename: str | None = None) -> dict:
    if not source_path:
        return {}

    path = Path(source_path)
    if not path.exists() or not path.is_file():
        return {}

    suffix = path.suffix.lower()
    metadata = {}
    try:
        if suffix == ".epub":
            metadata = _extract_epub_metadata(path)
        elif suffix == ".fb2":
            from .parsers.fb2_parser import extract_fb2_metadata

            metadata = extract_fb2_metadata(path)
        elif suffix in {".html", ".htm"}:
            metadata = _extract_html_metadata(path)
        elif suffix == ".docx":
            metadata = _extract_docx_metadata(path)
        elif suffix == ".odt":
            metadata = _extract_odt_metadata(path)
        elif suffix == ".msg":
            metadata = _extract_msg_metadata(path)
        elif suffix in {".txt", ".csv"}:
            metadata = _extract_txt_metadata(path)
    except Exception:
        logger.warning("Unable to extract source metadata from '%s'.", path, exc_info=True)
        metadata = {}

    normalized = normalize_document_metadata_payload(metadata)
    if normalized:
        return normalized
    return {}


def _load_archive_xml(archive: zipfile.ZipFile, member_name: str) -> Element | None:
    try:
        return parse_safe_xml(read_zip_member(archive, member_name, max_bytes=MAX_ARCHIVE_XML_BYTES))
    except (KeyError, ParseError):
        return None


def _collect_epub_refinements(metadata_root: Element) -> dict[str, dict[str, list[dict]]]:
    refinements = {}
    for meta in metadata_root.findall("{http://www.idpf.org/2007/opf}meta") + metadata_root.findall("meta"):
        refines = meta.attrib.get("refines")
        if not refines or not refines.startswith("#"):
            continue
        property_name = _clean_text(meta.attrib.get("property"))
        if not property_name:
            continue
        refinements.setdefault(refines[1:], {}).setdefault(property_name, []).append(
            {
                "value": _clean_text(meta.text),
                "scheme": _clean_text(meta.attrib.get("scheme")),
            }
        )
    return refinements


def _epub_role_from_refinements(refinements: dict[str, list[dict]], fallback: str | None = None) -> str | None:
    role_entries = refinements.get("role", [])
    for role_entry in role_entries:
        value = _clean_text(role_entry.get("value"))
        if not value:
            continue
        return EPUB_ROLE_MAP.get(value.casefold(), value)
    return fallback


def _append_named_contributor(contributors: dict, role: str | None, name: str) -> None:
    if not name:
        return
    normalized_role = (role or "contributors").casefold()
    if normalized_role == "authors":
        contributors.setdefault("authors", []).append(name)
    elif normalized_role == "editors":
        contributors.setdefault("editors", []).append(name)
    elif normalized_role == "translators":
        contributors.setdefault("translators", []).append(name)
    else:
        display_role = role or "contributor"
        contributors.setdefault("other", []).append({"role": display_role, "name": name})


def _extract_epub_metadata(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        validate_zip_archive(archive)
        container_root = _load_archive_xml(archive, "META-INF/container.xml")
        opf_path = None
        if container_root is not None:
            rootfile = container_root.find(".//ctr:rootfile", EPUB_CONTAINER_NS)
            if rootfile is not None:
                opf_path = rootfile.attrib.get("full-path")
        if not opf_path:
            opf_candidates = [name for name in archive.namelist() if name.lower().endswith(".opf")]
            opf_path = opf_candidates[0] if opf_candidates else None
        if not opf_path:
            return {}

        opf_root = _load_archive_xml(archive, opf_path)
        if opf_root is None:
            return {}

        metadata_root = opf_root.find("{http://www.idpf.org/2007/opf}metadata")
        if metadata_root is None:
            metadata_root = opf_root.find("metadata")
        if metadata_root is None:
            return {}

        refinements = _collect_epub_refinements(metadata_root)
        metadata = {
            "subjects": [],
            "identifiers": [],
        }

        titles = []
        for title_node in metadata_root.findall("dc:title", EPUB_OPF_NS):
            text = _clean_text(title_node.text)
            if not text:
                continue
            titles.append(
                {
                    "id": title_node.attrib.get("id"),
                    "title": text,
                    "refinements": refinements.get(title_node.attrib.get("id") or "", {}),
                }
            )

        main_title = None
        subtitle = None
        for title_entry in titles:
            refinement_titles = title_entry["refinements"].get("title-type", [])
            if any((item.get("value") or "").casefold() == "subtitle" for item in refinement_titles):
                subtitle = title_entry["title"]
            elif any((item.get("value") or "").casefold() == "main" for item in refinement_titles):
                main_title = title_entry["title"]

        if not main_title and titles:
            main_title = titles[0]["title"]
        if not subtitle and len(titles) > 1:
            subtitle = titles[1]["title"]
        if main_title:
            metadata["title"] = main_title
        if subtitle and subtitle != main_title:
            metadata["subtitle"] = subtitle

        language_node = metadata_root.find("dc:language", EPUB_OPF_NS)
        if language_node is not None:
            metadata["language"] = _clean_text(language_node.text)

        for field, tag in (
            ("publisher", "dc:publisher"),
            ("publication_date", "dc:date"),
            ("description", "dc:description"),
            ("rights", "dc:rights"),
            ("source", "dc:source"),
            ("publication_type", "dc:type"),
        ):
            node = metadata_root.find(tag, EPUB_OPF_NS)
            if node is not None and _clean_text(node.text):
                metadata[field] = _clean_text(node.text)

        contributors = {"authors": [], "editors": [], "translators": [], "other": []}
        for creator_tag, default_role in (("dc:creator", "authors"), ("dc:contributor", "contributor")):
            for contributor_node in metadata_root.findall(creator_tag, EPUB_OPF_NS):
                name = _clean_text(contributor_node.text)
                if not name:
                    continue
                role = _epub_role_from_refinements(
                    refinements.get(contributor_node.attrib.get("id") or "", {}),
                    fallback=EPUB_ROLE_MAP.get(
                        str(contributor_node.attrib.get(f"{{{EPUB_OPF_NS['opf']}}}role", "")).casefold(),
                        default_role,
                    ),
                )
                _append_named_contributor(contributors, role, name)

        if has_meaningful_document_metadata({"contributors": contributors}):
            metadata["contributors"] = contributors

        for subject_node in metadata_root.findall("dc:subject", EPUB_OPF_NS):
            text = _clean_text(subject_node.text)
            if text:
                metadata["subjects"].append(text)
        metadata["subjects"] = _dedupe_strings(metadata["subjects"])

        for identifier_node in metadata_root.findall("dc:identifier", EPUB_OPF_NS):
            value = _clean_text(identifier_node.text)
            if not value:
                continue
            identifier_refinements = refinements.get(identifier_node.attrib.get("id") or "", {})
            identifier_type_entries = identifier_refinements.get("identifier-type", [])
            identifier_type_entry = next((item for item in identifier_type_entries if item.get("value")), None)
            identifier_type = _clean_text(identifier_type_entry.get("value")) if identifier_type_entry else None
            identifier_type_scheme = _clean_text(identifier_type_entry.get("scheme")) if identifier_type_entry else None
            if identifier_type_scheme and identifier_type_scheme.casefold() == "onix:codelist5":
                identifier_type = ONIX_CODELIST5_SCHEMES.get(identifier_type or "", identifier_type)
            scheme = _normalize_identifier_scheme(identifier_node.attrib.get(f"{{{EPUB_OPF_NS['opf']}}}scheme") or identifier_type)
            entry = {"value": value}
            if scheme:
                entry["scheme"] = scheme
            metadata["identifiers"].append(entry)

        metadata["identifiers"] = _normalize_identifiers(metadata.get("identifiers"))

        for meta_node in metadata_root.findall("{http://www.idpf.org/2007/opf}meta") + metadata_root.findall("meta"):
            name = _clean_text(meta_node.attrib.get("name"))
            content = _clean_text(meta_node.attrib.get("content"))
            if not name or not content:
                continue
            lowered_name = name.casefold()
            if lowered_name == "calibre:series" and not metadata.get("journal_or_series"):
                metadata["journal_or_series"] = content
            elif lowered_name == "calibre:series_index" and not metadata.get("volume"):
                metadata["volume"] = content
            elif lowered_name == "cover" and not metadata.get("cover_image_id"):
                metadata["cover_image_id"] = content

    return metadata


def _extract_docx_metadata(path: Path) -> dict:
    metadata = {}
    with zipfile.ZipFile(path) as archive:
        validate_zip_archive(archive)
        core_root = _load_archive_xml(archive, "docProps/core.xml")
        if core_root is None:
            return metadata

        for field, tag in (
            ("title", "dc:title"),
            ("description", "dc:description"),
            ("publication_date", "dcterms:created"),
            ("language", "dc:language"),
        ):
            node = core_root.find(tag, DOCX_CORE_NS)
            if node is not None and _clean_text(node.text):
                metadata[field] = _clean_text(node.text)

        creator = core_root.find("dc:creator", DOCX_CORE_NS)
        if creator is not None and _clean_text(creator.text):
            metadata["contributors"] = {"authors": [_clean_text(creator.text)], "editors": [], "translators": [], "other": []}

        subject = core_root.find("dc:subject", DOCX_CORE_NS)
        if subject is not None and _clean_text(subject.text):
            metadata["subjects"] = _normalize_subjects(subject.text)

    return metadata


def _extract_odt_metadata(path: Path) -> dict:
    metadata = {}
    with zipfile.ZipFile(path) as archive:
        validate_zip_archive(archive)
        meta_root = _load_archive_xml(archive, "meta.xml")
        if meta_root is None:
            return metadata

        office_meta = meta_root.find("office:meta", ODT_META_NS)
        if office_meta is None:
            return metadata

        for field, tag in (
            ("title", "dc:title"),
            ("description", "dc:description"),
            ("publication_date", "dc:date"),
            ("language", "dc:language"),
        ):
            node = office_meta.find(tag, ODT_META_NS)
            if node is not None and _clean_text(node.text):
                metadata[field] = _clean_text(node.text)

        creator = office_meta.find("dc:creator", ODT_META_NS)
        if creator is not None and _clean_text(creator.text):
            metadata["contributors"] = {"authors": [_clean_text(creator.text)], "editors": [], "translators": [], "other": []}

        keywords = [_clean_text(node.text) for node in office_meta.findall("meta:keyword", ODT_META_NS)]
        keywords = [value for value in keywords if value]
        if keywords:
            metadata["subjects"] = _dedupe_strings(keywords)

    return metadata


def _meta_content_by_names(soup: BeautifulSoup, names: tuple[str, ...]) -> str | None:
    lowered_names = {name.casefold() for name in names}
    for meta in soup.find_all("meta"):
        for attr_name in ("name", "property", "http-equiv"):
            attr_value = _clean_text(meta.get(attr_name))
            if attr_value and attr_value.casefold() in lowered_names:
                content = _clean_text(meta.get("content"))
                if content:
                    return content
    return None


def _extract_html_metadata(path: Path) -> dict:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    metadata = {}

    title = _clean_text(soup.title.string if soup.title and soup.title.string else None)
    title = title or _meta_content_by_names(soup, ("og:title", "dc.title", "citation_title"))
    if title:
        metadata["title"] = title

    language = _normalize_language_tag(
        (soup.html.get("lang") if soup.html else None)
        or _meta_content_by_names(soup, ("dc.language", "language"))
    )
    if language:
        metadata["language"] = language

    for field, keys in (
        ("description", ("description", "dc.description", "og:description")),
        ("publisher", ("publisher", "dc.publisher")),
        ("publication_date", ("article:published_time", "citation_publication_date", "dc.date")),
        ("rights", ("copyright", "dc.rights")),
        ("source", ("source", "dc.source", "og:url")),
        ("publication_type", ("dc.type", "og:type")),
    ):
        value = _meta_content_by_names(soup, keys)
        if value:
            metadata[field] = value

    author = _meta_content_by_names(soup, ("author", "dc.creator", "citation_author"))
    if author:
        metadata["contributors"] = {"authors": _split_text_values(author), "editors": [], "translators": [], "other": []}

    subjects = _meta_content_by_names(soup, ("keywords", "dc.subject"))
    if subjects:
        metadata["subjects"] = _normalize_subjects(subjects)

    return metadata


def _extract_msg_metadata(path: Path) -> dict:
    try:
        import extract_msg
    except ImportError:
        return {}

    msg = extract_msg.openMsg(str(path))
    try:
        metadata = {}
        subject = _clean_text(msg.subject)
        if subject:
            metadata["title"] = subject

        sender = _clean_text(msg.sender)
        if sender:
            metadata["contributors"] = {"authors": [sender], "editors": [], "translators": [], "other": []}

        sent_date = _clean_text(msg.date)
        if sent_date:
            metadata["publication_date"] = sent_date

        recipients = _clean_text(msg.to)
        if recipients:
            metadata["source"] = recipients

        return metadata
    finally:
        try:
            msg.close()
        except Exception:
            pass


def _extract_txt_metadata(path: Path) -> dict:
    try:
        contents = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}

    first_non_empty = next((line.strip() for line in contents.splitlines() if line.strip()), "")
    if first_non_empty and len(first_non_empty) <= 160:
        return {"title": first_non_empty}
    return {}
