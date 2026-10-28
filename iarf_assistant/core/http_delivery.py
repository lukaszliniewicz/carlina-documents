from __future__ import annotations

from typing import Any

from iarf_assistant.extensions import compress


IMMUTABLE_FRONTEND_ASSET_PREFIX = "/static/frontend/assets/"
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


def configure_http_delivery(app: Any) -> None:
    """Configures response compression without coupling it to app creation."""
    app.config.setdefault(
        "COMPRESS_MIMETYPES",
        [
            "application/javascript",
            "application/json",
            "application/manifest+json",
            "image/svg+xml",
            "text/css",
            "text/html",
            "text/javascript",
            "text/plain",
        ],
    )
    app.config.setdefault("COMPRESS_ALGORITHM", ["br", "gzip"])
    app.config.setdefault("COMPRESS_BR_LEVEL", 4)
    app.config.setdefault("COMPRESS_LEVEL", 6)
    app.config.setdefault("COMPRESS_MIN_SIZE", 1024)
    compress.init_app(app)


def apply_static_asset_cache_headers(response: Any, path: str) -> Any:
    """Marks only content-hashed Vite assets as immutable."""
    if str(path or "").startswith(IMMUTABLE_FRONTEND_ASSET_PREFIX):
        response.headers["Cache-Control"] = IMMUTABLE_CACHE_CONTROL
    return response


__all__ = [
    "IMMUTABLE_CACHE_CONTROL",
    "IMMUTABLE_FRONTEND_ASSET_PREFIX",
    "apply_static_asset_cache_headers",
    "configure_http_delivery",
]
