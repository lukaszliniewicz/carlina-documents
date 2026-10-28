import logging
from pathlib import Path
from typing import Optional

from flask import current_app, has_app_context, has_request_context
from flask_login import current_user

from carlina.core.text_formats import (
    html_to_markdown_on_the_fly,
    markdown_to_html_on_the_fly,
)

logger = logging.getLogger(__name__)


def _get_doc_processor_addon():
    """Fetches the universal Document Processor Hub if it is active."""
    if not has_app_context():
        return None

    addon_manager = getattr(current_app, "addon_manager", None)
    if not addon_manager:
        return None

    loaded_addons = getattr(addon_manager, "loaded_addons", {})
    return loaded_addons.get("doc_processor")


def _get_current_user_id() -> Optional[int]:
    if not has_request_context():
        return None

    try:
        if getattr(current_user, "is_authenticated", False):
            return getattr(current_user, "id", None)
    except Exception:
        logger.debug("Could not resolve current user for doc_processor request.", exc_info=True)

    return None


def _extract_with_basic_pypdf(path: Path) -> Optional[str]:
    """Absolute last resort fallback if no addons are installed."""
    logger.info("Doc Processor Hub not found. Using bare-bones pypdf fallback for: %s", path.name)

    try:
        from pypdf import PdfReader
    except ImportError:
        logger.error("pypdf is not installed. PDF fallback failed.")
        return None

    page_chunks = []

    try:
        document = PdfReader(path, strict=False)
        for page_number, page in enumerate(document.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if not page_text:
                continue

            page_chunks.append(f"<!-- Page {page_number} -->\n{page_text}")
    except Exception as exc:
        logger.error("Basic PDF extraction failed for %s: %s", path.name, exc, exc_info=True)
        return None

    if not page_chunks:
        return None

    return "\n\n---\n\n".join(page_chunks)


def extract_document_for_rag(file_path: str, engine: str = "auto", **kwargs) -> Optional[str]:
    """
    Extracts markdown from a document by routing through the universal Document Processor Hub.
    Maps legacy OCR intent into a concrete engine when needed.
    """
    path = Path(file_path)

    if path.suffix.lower() not in [".pdf", ".epub", ".mobi", ".docx", ".doc", ".rtf", ".odt", ".txt", ".html", ".htm", ".csv"]:
        return None

    processor_addon = _get_doc_processor_addon()

    if processor_addon:
        logger.info("Routing %s through universal Document Processor Hub...", path.name)
        try:
            client = processor_addon.initialize_client({"current_user_id": _get_current_user_id()})

            if engine == "auto":
                if kwargs.get("force_ocr"):
                    engine = "marker"
                elif kwargs.get("no_ocr"):
                    engine = "pymupdf"

            md_content = client.parse_document(
                filepath=str(path),
                engine=engine,
                **kwargs,
            )

            if md_content and not md_content.startswith("Error:"):
                import re

                if re.search(r"<(?:main|html|body|article|section)\b", md_content, re.IGNORECASE):
                    clean_html = md_content
                else:
                    clean_html = markdown_to_html_on_the_fly(md_content)

                pure_markdown = html_to_markdown_on_the_fly(clean_html)

                def replace_md_image(match):
                    alt_text = match.group(1) or "Image"
                    return f"[Visual Element: {alt_text}]"

                pure_markdown = re.sub(r'!\[(.*?)\]\(.*?\)', replace_md_image, pure_markdown)
                pure_markdown = re.sub(r'<img[^>]*alt=["\'](.*?)["\'][^>]*>', replace_md_image, pure_markdown)
                pure_markdown = re.sub(r'<img[^>]*>', '[Visual Element]', pure_markdown)
                return pure_markdown

            logger.error("Document Processor returned an error or no content: %s", md_content)
        except Exception as exc:
            logger.error("Document Processor Hub failed for %s: %s", path.name, exc, exc_info=True)

    if path.suffix.lower() == ".pdf":
        return _extract_with_basic_pypdf(path)
    return None
