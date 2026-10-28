"""Small, source-free EPUB cover presentation options shared by exporters."""

import math
from collections.abc import Mapping
from decimal import Decimal
from typing import cast


def _normalize_crop(value: object) -> list[float]:
    """Return a bounded, stable PDF-page crop in normalized coordinates."""

    if not isinstance(value, (list, tuple)):
        raise ValueError("Invalid EPUB cover crop.")

    raw = cast(list[object] | tuple[object, ...], value)
    if len(raw) != 4:
        raise ValueError("Invalid EPUB cover crop.")
    coordinates: list[float] = []
    decimal_coordinates: list[Decimal] = []
    for coordinate in raw:
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
            raise ValueError("Invalid EPUB cover crop.")
        if coordinate < 0 or coordinate > 1 or not math.isfinite(coordinate):
            raise ValueError("Invalid EPUB cover crop.")
        coordinates.append(float(coordinate))
        decimal_coordinates.append(Decimal(str(coordinate)))

    minimum_dimension = Decimal("0.01")
    left, top, right, bottom = decimal_coordinates
    if right - left < minimum_dimension or bottom - top < minimum_dimension:
        raise ValueError("Invalid EPUB cover crop.")

    normalized = [
        rounded if rounded else 0.0
        for rounded in (round(coordinate, 6) for coordinate in coordinates)
    ]
    normalized_decimals = [Decimal(str(coordinate)) for coordinate in normalized]
    normalized_left, normalized_top, normalized_right, normalized_bottom = (
        normalized_decimals
    )
    if (
        normalized_left < 0
        or normalized_top < 0
        or normalized_right > 1
        or normalized_bottom > 1
        or normalized_right - normalized_left < minimum_dimension
        or normalized_bottom - normalized_top < minimum_dimension
    ):
        raise ValueError("Invalid EPUB cover crop.")

    return normalized


def normalize_epub_cover(value: object = None) -> dict[str, object]:
    """Keep the historical export by default; reject ambiguous explicit choices."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Invalid EPUB cover option.")
    options = cast(Mapping[str, object], value)
    if not set(options).issubset({"mode", "page", "crop"}):
        raise ValueError("Invalid EPUB cover option.")
    mode = options.get("mode", "existing")
    if not isinstance(mode, str) or mode not in {"existing", "generated", "pdf_page"}:
        raise ValueError("Invalid EPUB cover mode.")
    page = options.get("page")
    if mode != "pdf_page":
        if page is not None:
            raise ValueError("A cover page requires the PDF page cover mode.")
        if "crop" in options:
            raise ValueError("A cover crop requires the PDF page cover mode.")
        return {} if mode == "existing" else {"mode": "generated"}
    if type(page) is not int or not 1 <= page <= 1_000_000:
        raise ValueError("The cover page must be a positive physical PDF page number.")
    normalized: dict[str, object] = {"mode": "pdf_page", "page": page}
    if "crop" in options:
        normalized["crop"] = _normalize_crop(options["crop"])
    return normalized
