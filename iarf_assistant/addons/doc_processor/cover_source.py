"""Resolve cover source files at the owned document boundary."""

from pathlib import Path

from iarf_assistant.core.upload_paths import resolve_upload_reference, uploads_root


def owned_pdf_source(document: object, *, user_id: int | None = None) -> Path:
    owner = getattr(document, "user_id", None)
    if owner is None or (user_id is not None and int(owner) != int(user_id)):
        raise ValueError("The source PDF is unavailable for this cover.")
    try:
        path = resolve_upload_reference(
            getattr(document, "file_path", None),
            uploads_dir=uploads_root(),
            require_within_uploads=True,
        )
        if path is None or path.suffix.lower() != ".pdf" or not path.is_file():
            raise ValueError("unavailable")
        path = path.resolve()
        path.relative_to((uploads_root() / str(owner)).resolve())
    except OSError, RuntimeError, TypeError, ValueError:
        raise ValueError("The source PDF is unavailable for this cover.") from None
    return path
