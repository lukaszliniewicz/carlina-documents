"""Byte-compatible image preparation, with no application imports.

Workers load this directly; retain the historical scale policy for replay.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import threading
import uuid
from pathlib import Path

try:
    import fitz
except ImportError:
    fitz = None

logger = logging.getLogger(__name__)
MAX_CROP_GUARD_EXPANSIONS = 64
CROP_GUARD_MIN_TEXT_CHARS = 24
CROP_GUARD_MARGIN_RATIO = 0.005
MCP_IMAGE_TRANSPORT_PNG_2000 = "png-2000-v1"
MCP_IMAGE_TRANSPORT_MAX_EDGE = 2000


def get_optimal_scale_factor(page: fitz.Page, target_longest_edge: int = 2048) -> float:
    rect = page.rect
    page_longest_edge = max(rect.width, rect.height)
    if page_longest_edge == 0:
        return 2.0

    try:
        text_blocks = page.get_text("dict")["blocks"]
    except Exception:
        text_blocks = []

    total_text_len = sum(
        len(span.get("text", "").strip())
        for b in text_blocks
        if b.get("type") == 0
        for l in b.get("lines", [])
        for span in l.get("spans", [])
    )

    image_list = page.get_images()
    total_image_area, max_image_res = 0, 0
    for img_info in image_list:
        try:
            base_image = fitz.Pixmap(page.doc, img_info[0])
            max_image_res = max(max_image_res, max(base_image.width, base_image.height))
            for r in page.get_image_rects(img_info[0]):
                total_image_area += r.width * r.height
        except Exception:
            continue

    page_area = page.rect.width * page.rect.height
    image_coverage_pct = (total_image_area / page_area) if page_area > 0 else 0

    is_native = total_text_len >= 50 and image_coverage_pct <= 0.85

    if is_native or (total_text_len < 50 and image_coverage_pct < 0.1):
        return target_longest_edge / page_longest_edge
    else:
        if max_image_res > 0:
            if max_image_res < target_longest_edge:
                return (max_image_res * 1.25) / page_longest_edge
            return target_longest_edge / page_longest_edge
        return target_longest_edge / page_longest_edge


class PdfImageRenderer:
    """Image-only state without providers, database or Flask."""

    def __init__(self, settings: dict, output_folder: Path | str):
        self.settings = settings
        self.output_folder = Path(output_folder)
        self.metrics = {"crop_guard_expansions": []}
        self.metrics_lock = threading.Lock()

    def _uses_png_image_transport(self) -> bool:
        settings = getattr(self, "settings", None)
        return (
            isinstance(settings, dict)
            and settings.get("mcp_image_transport") == MCP_IMAGE_TRANSPORT_PNG_2000
        )

    def _get_clip_rect(self, page: fitz.Page, crop_box_normalized):
        if not crop_box_normalized or len(crop_box_normalized) != 4:
            return None

        x0, y0, x1, y1 = crop_box_normalized
        x_min, x_max = sorted([x0, x1])
        y_min, y_max = sorted([y0, y1])

        rect = page.rect
        requested = fitz.Rect(
            rect.x0 + (x_min * rect.width),
            rect.y0 + (y_min * rect.height),
            rect.x0 + (x_max * rect.width),
            rect.y0 + (y_max * rect.height),
        )
        expanded, crossing_blocks = self._expand_clip_to_native_text(page, requested)
        if crossing_blocks:
            self._record_crop_guard_expansion(
                page, requested, expanded, crossing_blocks
            )
        return expanded

    def _expand_clip_to_native_text(
        self, page: fitz.Page, requested: fitz.Rect
    ) -> tuple[fitz.Rect, int]:
        """Prevent a selected crop from slicing through substantive text blocks."""

        page_rect = page.rect
        x0, y0, x1, y1 = requested
        requested_x0, requested_y0, requested_x1, requested_y1 = requested
        crossing_blocks = 0
        margin = max(
            1.0,
            min(float(page_rect.width), float(page_rect.height))
            * CROP_GUARD_MARGIN_RATIO,
        )
        try:
            native_blocks = page.get_text("blocks")
        except Exception:
            return fitz.Rect(requested), 0

        for block in native_blocks:
            if len(block) < 5:
                continue
            text = re.sub(r"\s+", "", str(block[4] or ""))
            if len(text) < CROP_GUARD_MIN_TEXT_CHARS:
                continue
            try:
                block_rect = fitz.Rect(*block[:4])
            except TypeError, ValueError:
                continue

            changed = False
            if block_rect.y1 > requested_y0 and block_rect.y0 < requested_y1:
                if block_rect.x0 < requested_x0 < block_rect.x1:
                    x0 = min(x0, max(page_rect.x0, block_rect.x0 - margin))
                    changed = True
                if block_rect.x0 < requested_x1 < block_rect.x1:
                    x1 = max(x1, min(page_rect.x1, block_rect.x1 + margin))
                    changed = True
            if block_rect.x1 > requested_x0 and block_rect.x0 < requested_x1:
                if block_rect.y0 < requested_y0 < block_rect.y1:
                    y0 = min(y0, max(page_rect.y0, block_rect.y0 - margin))
                    changed = True
                if block_rect.y0 < requested_y1 < block_rect.y1:
                    y1 = max(y1, min(page_rect.y1, block_rect.y1 + margin))
                    changed = True
            if changed:
                crossing_blocks += 1

        return fitz.Rect(x0, y0, x1, y1), crossing_blocks

    def _record_crop_guard_expansion(
        self,
        page: fitz.Page,
        requested: fitz.Rect,
        expanded: fitz.Rect,
        crossing_blocks: int,
    ) -> None:
        if not hasattr(self, "metrics") or not hasattr(self, "metrics_lock"):
            return
        page_rect = page.rect

        def normalized(box: fitz.Rect) -> list[float]:
            return [
                round((box.x0 - page_rect.x0) / page_rect.width, 4),
                round((box.y0 - page_rect.y0) / page_rect.height, 4),
                round((box.x1 - page_rect.x0) / page_rect.width, 4),
                round((box.y1 - page_rect.y0) / page_rect.height, 4),
            ]

        diagnostic = {
            "page": int(getattr(page, "number", -1)) + 1,
            "requested": normalized(requested),
            "expanded": normalized(expanded),
            "crossing_text_blocks": crossing_blocks,
        }
        with self.metrics_lock:
            expansions = self.metrics.setdefault("crop_guard_expansions", [])
            if diagnostic not in expansions:
                expansions.append(diagnostic)
                del expansions[:-MAX_CROP_GUARD_EXPANSIONS]

    def _render_page_to_png(self, page: fitz.Page, crop_box_normalized):
        scale = get_optimal_scale_factor(page)
        clip_rect = self._get_clip_rect(page, crop_box_normalized)
        if self._uses_png_image_transport():
            page_rect = page.rect
            longest_edge = max(float(page_rect.width), float(page_rect.height))
            if longest_edge > 0:
                scale = min(scale, MCP_IMAGE_TRANSPORT_MAX_EDGE / longest_edge)
        pix = page.get_pixmap(
            matrix=fitz.Matrix(scale, scale), clip=clip_rect, alpha=False
        )
        if self._uses_png_image_transport():
            while max(int(pix.width), int(pix.height)) > MCP_IMAGE_TRANSPORT_MAX_EDGE:
                scale *= MCP_IMAGE_TRANSPORT_MAX_EDGE / max(
                    int(pix.width), int(pix.height)
                )
                pix = page.get_pixmap(
                    matrix=fitz.Matrix(scale, scale), clip=clip_rect, alpha=False
                )

        png_path = self.output_folder / f"docproc_{uuid.uuid4().hex[:10]}.png"
        pix.save(png_path)
        return str(png_path)

    def _image_file_to_data_url(
        self,
        image_path: str,
        crop_region: str | None = None,
        *,
        max_width: int | None = 1400,
        quality: int = 88,
    ) -> str | None:
        import io

        from PIL import Image

        if not image_path or not os.path.exists(image_path):
            return None

        if self._uses_png_image_transport():
            try:
                with Image.open(image_path) as img:
                    width, height = img.size
                    crop = crop_region in {"top", "bottom"}
                    target_width = max_width if max_width is not None else width
                    scale = min(
                        1.0,
                        MCP_IMAGE_TRANSPORT_MAX_EDGE / max(width, height),
                        target_width / width if target_width > 0 else 1.0,
                    )
                    if (
                        not crop
                        and img.format == "PNG"
                        and width <= MCP_IMAGE_TRANSPORT_MAX_EDGE
                        and height <= MCP_IMAGE_TRANSPORT_MAX_EDGE
                        and (max_width is None or width <= max_width)
                    ):
                        encoded = base64.b64encode(
                            Path(image_path).read_bytes()
                        ).decode("ascii")
                        return f"data:image/png;base64,{encoded}"

                    if crop:
                        strip_height = max(1, int(height * 0.28))
                        box = (
                            (0, 0, width, strip_height)
                            if crop_region == "top"
                            else (0, height - strip_height, width, height)
                        )
                        img = img.crop(box)
                        width, height = img.size
                        scale = min(
                            1.0,
                            MCP_IMAGE_TRANSPORT_MAX_EDGE / max(width, height),
                            target_width / width if target_width > 0 else 1.0,
                        )
                    new_size = (
                        max(1, round(width * scale)),
                        max(1, round(height * scale)),
                    )
                    while max(new_size) > MCP_IMAGE_TRANSPORT_MAX_EDGE:
                        correction = MCP_IMAGE_TRANSPORT_MAX_EDGE / max(new_size)
                        new_size = (
                            max(1, round(new_size[0] * correction)),
                            max(1, round(new_size[1] * correction)),
                        )
                    if new_size != (width, height):
                        img = img.resize(new_size, resample=Image.Resampling.LANCZOS)
                    buffer = io.BytesIO()
                    img.save(buffer, format="PNG")
                    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                    return f"data:image/png;base64,{encoded}"
            except Exception as exc:
                logger.warning(
                    "Failed to encode boundary/metadata image '%s': %s", image_path, exc
                )
                return None

        try:
            with Image.open(image_path) as img:
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                if crop_region in {"top", "bottom"}:
                    width, height = img.size
                    strip_height = max(1, int(height * 0.28))
                    box = (
                        (0, 0, width, strip_height)
                        if crop_region == "top"
                        else (0, height - strip_height, width, height)
                    )
                    img = img.crop(box)

                if max_width is not None and img.width > max_width:
                    ratio = max_width / float(img.width)
                    img = img.resize((max_width, max(1, int(img.height * ratio))))

                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=quality)
                encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
                return f"data:image/jpeg;base64,{encoded}"
        except Exception as exc:
            logger.warning(
                "Failed to encode boundary/metadata image '%s': %s", image_path, exc
            )
            return None
