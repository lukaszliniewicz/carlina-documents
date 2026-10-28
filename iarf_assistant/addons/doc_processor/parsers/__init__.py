from __future__ import annotations

from pathlib import Path
import logging

from iarf_assistant.core.upload_paths import is_upload_reference, resolve_upload_reference

logger = logging.getLogger(__name__)


def get_parser(file_path: str, user_id: int | None, llm_service, settings: dict | None = None, state: dict | None = None):
    if is_upload_reference(file_path):
        resolved = resolve_upload_reference(file_path)
        if resolved is None:
            raise FileNotFoundError("Source document path is missing.")
        file_path = str(resolved)
    suffix = Path(str(file_path)).suffix.lower()
    settings = settings or {}

    if suffix == ".pdf":
        try:
            from .pdf_parser import PdfParser
        except ModuleNotFoundError as exc:
            if exc.name in {"fitz", "pymupdf"}:
                raise RuntimeError("PyMuPDF is required to parse PDF files. Install the 'pymupdf' package.") from exc
            raise
        except ImportError as exc:
            raise RuntimeError("PyMuPDF is required to parse PDF files. Install the 'pymupdf' package.") from exc
        return PdfParser(file_path, user_id, llm_service, settings=settings, state=state)

    if suffix in {".txt", ".csv"}:
        from .txt_parser import TxtParser

        return TxtParser(file_path, user_id, llm_service, settings=settings)

    if suffix == ".msg":
        from .msg_parser import MsgParser

        return MsgParser(file_path, user_id, llm_service, settings=settings)

    if suffix == ".fb2":
        from .fb2_parser import Fb2Parser

        return Fb2Parser(file_path, user_id, llm_service, settings=settings)

    if suffix in {".html", ".htm", ".epub", ".docx", ".doc", ".odt", ".rtf"}:
        from .html_epub_parser import HtmlEpubParser

        return HtmlEpubParser(file_path, user_id, llm_service, settings=settings)

    raise ValueError(f"Unsupported document type: {suffix or 'unknown'}")


def process_file_headlessly(
    file_path: str,
    user_id: int | None,
    llm_service,
    metrics_dict: dict | None = None,
    settings: dict | None = None,
    **_kwargs,
) -> str:
    try:
        parser = get_parser(file_path, user_id, llm_service, settings=settings)
        status, html_content = parser.process()
        if metrics_dict is not None:
            metrics_dict.update(getattr(parser, "metrics", {}) or {})
        if status != "COMPLETED":
            return f"Error: Document parsing ended with status {status}."

        from iarf_assistant.addons.doc_processor import format_utils

        return format_utils.html_to_markdown_on_the_fly(html_content or "")
    except Exception as exc:
        logger.exception("Headless document parsing failed for %s", file_path)
        return f"Error: {exc}"
