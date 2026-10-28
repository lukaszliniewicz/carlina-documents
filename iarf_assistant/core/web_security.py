"""Central local/server web-security policy for Carlina."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from flask import jsonify, request
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from carlina.product_identity import product_environment_value
from iarf_assistant.extensions import csrf

LOCAL_PROFILE = "local"
SERVER_PROFILE = "server"
DEFAULT_REQUEST_LIMIT = 64 * 1024 * 1024
LARGE_MEDIA_REQUEST_LIMIT = 5 * 1024 * 1024 * 1024
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LARGE_UPLOAD_PREFIXES = (
    "/addons/audio_core/",
    "/addons/doc_processor/",
    "/addons/library_importer/",
    "/addons/meeting_minutes/",
    "/addons/stt_dataset/",
    "/addons/subtitle_studio/",
    "/addons/subtitles/",
    "/addons/translator/",
)


class WebSecurityConfigurationError(RuntimeError):
    """The selected exposure profile is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class WebSecurityPolicy:
    profile: str
    trusted_hosts: tuple[str, ...]
    external_url: str | None
    proxy_hops: int
    secure_cookies: bool


def waitress_proxy_options(policy: WebSecurityPolicy) -> dict[str, bool]:
    """Let the configured ProxyFix own forwarding in server mode only."""
    # Waitress otherwise strips these headers before ProxyFix can apply the
    # explicit hop count. Local mode still strips them and never installs it.
    return {"clear_untrusted_proxy_headers": policy.profile != SERVER_PROFILE}


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(part.strip() for part in (value or "").split(",") if part.strip())
    )


def _parse_proxy_hops(value: str | None) -> int:
    if value is None or not value.strip():
        raise WebSecurityConfigurationError(
            "Server profile requires CARLINA_PROXY_HOPS to declare the exact "
            "number of trusted reverse-proxy hops."
        )
    try:
        hops = int(value)
    except ValueError as exc:
        raise WebSecurityConfigurationError(
            "CARLINA_PROXY_HOPS must be an integer."
        ) from exc
    if not 1 <= hops <= 4:
        raise WebSecurityConfigurationError(
            "CARLINA_PROXY_HOPS must be between 1 and 4."
        )
    return hops


def resolve_web_security_policy(
    environment: Mapping[str, str] | None = None,
) -> WebSecurityPolicy:
    profile = (
        (
            product_environment_value(
                "WEB_PROFILE",
                default=LOCAL_PROFILE,
                environment=environment,
            )
            or ""
        )
        .strip()
        .lower()
    )
    if profile not in {LOCAL_PROFILE, SERVER_PROFILE}:
        raise WebSecurityConfigurationError(
            "CARLINA_WEB_PROFILE must be 'local' or 'server'."
        )

    if profile == LOCAL_PROFILE:
        return WebSecurityPolicy(
            profile=profile,
            trusted_hosts=("localhost", "127.0.0.1", "[::1]"),
            external_url=None,
            proxy_hops=0,
            secure_cookies=False,
        )

    external_url = (
        product_environment_value("EXTERNAL_URL", environment=environment) or ""
    ).strip()
    try:
        parsed = urlsplit(external_url)
        # Accessing ``port`` also validates non-numeric and out-of-range values.
        _validated_port = parsed.port
    except ValueError as exc:
        raise WebSecurityConfigurationError(
            "Server profile requires CARLINA_EXTERNAL_URL as a valid HTTPS origin."
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(
            ord(character) < 32 or ord(character) == 127 for character in external_url
        )
    ):
        raise WebSecurityConfigurationError(
            "Server profile requires CARLINA_EXTERNAL_URL as an HTTPS origin without "
            "credentials, a path, query parameters, or a fragment."
        )
    trusted_hosts = _csv(
        product_environment_value("TRUSTED_HOSTS", environment=environment)
    )
    if not trusted_hosts:
        raise WebSecurityConfigurationError(
            "Server profile requires a comma-separated CARLINA_TRUSTED_HOSTS list."
        )
    if parsed.hostname not in {host.removeprefix(".") for host in trusted_hosts}:
        raise WebSecurityConfigurationError(
            "CARLINA_TRUSTED_HOSTS must include the CARLINA_EXTERNAL_URL hostname."
        )
    return WebSecurityPolicy(
        profile=profile,
        trusted_hosts=trusted_hosts,
        external_url=external_url.rstrip("/"),
        proxy_hops=_parse_proxy_hops(
            product_environment_value("PROXY_HOPS", environment=environment)
        ),
        secure_cookies=True,
    )


def normalized_request_id(candidate: str | None) -> str | None:
    value = str(candidate or "")
    return value if REQUEST_ID_PATTERN.fullmatch(value) else None


def request_size_limit(limit: int):
    """Annotate a view with a request-size limit applied before body parsing."""

    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("Request-size limits must be positive integers.")

    def decorate(view):
        view._carlina_request_size_limit = limit
        return view

    return decorate


def _effective_request_limit(app: Any) -> int:
    endpoint = request.endpoint
    view = app.view_functions.get(endpoint) if endpoint else None
    explicit = getattr(view, "_carlina_request_size_limit", None)
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    if any(request.path.startswith(prefix) for prefix in LARGE_UPLOAD_PREFIXES):
        return int(app.config["CARLINA_LARGE_MEDIA_REQUEST_LIMIT"])
    return int(app.config["MAX_CONTENT_LENGTH"])


def _wants_json_error() -> bool:
    return (
        request.path.startswith("/api/")
        or request.path.startswith("/addons/")
        or request.is_json
    )


def _content_security_policy() -> str:
    # Legacy add-on pages still contain inline scripts/styles. Keeping those
    # allowances explicit is safer than omitting CSP and makes their eventual
    # nonce migration a narrow, measurable compatibility task.
    return (
        "default-src 'self'; base-uri 'self'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https:; "
        "img-src 'self' data: blob: https:; font-src 'self' data: https:; "
        "media-src 'self' blob: https:; connect-src 'self' https: wss:"
    )


def apply_security_headers(response: Any, policy: WebSecurityPolicy) -> Any:
    response.headers.setdefault("Content-Security-Policy", _content_security_policy())
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), geolocation=(), microphone=(self), payment=(), usb=()",
    )
    if policy.profile == SERVER_PROFILE:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


def configure_web_security(app: Any) -> WebSecurityPolicy:
    """Resolve and install Carlina's complete HTTP security policy."""

    policy = resolve_web_security_policy()
    app.extensions["carlina_web_security"] = policy
    app.config.update(
        CARLINA_WEB_PROFILE=policy.profile,
        CARLINA_EXTERNAL_URL=policy.external_url,
        CARLINA_LARGE_MEDIA_REQUEST_LIMIT=LARGE_MEDIA_REQUEST_LIMIT,
        MAX_CONTENT_LENGTH=DEFAULT_REQUEST_LIMIT,
        MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024,
        MAX_FORM_PARTS=1_000,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=policy.secure_cookies,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        TRUSTED_HOSTS=list(policy.trusted_hosts),
        WTF_CSRF_CHECK_DEFAULT=False,
        WTF_CSRF_TIME_LIMIT=12 * 60 * 60,
    )
    if policy.profile == SERVER_PROFILE:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=policy.proxy_hops,
            x_proto=policy.proxy_hops,
            x_host=policy.proxy_hops,
            x_port=policy.proxy_hops,
            x_prefix=0,
        )

    csrf.init_app(app)

    @app.before_request
    def enforce_request_boundaries():
        request.max_content_length = _effective_request_limit(app)
        content_length = request.content_length
        if content_length is not None and content_length > request.max_content_length:
            raise RequestEntityTooLarge()
        enforce_in_tests = bool(app.config.get("CARLINA_ENFORCE_CSRF_IN_TESTS"))
        if (
            request.method in UNSAFE_METHODS
            and app.config.get("WTF_CSRF_ENABLED", True)
            and (not app.testing or enforce_in_tests)
        ):
            csrf.protect(apply_exemptions=True)

    @app.after_request
    def add_security_headers(response):
        return apply_security_headers(response, policy)

    @app.errorhandler(CSRFError)
    def csrf_error(_error):
        if _wants_json_error():
            return jsonify({"error": "CSRF validation failed."}), 400
        return "CSRF validation failed. Reload the page and try again.", 400

    return policy


__all__ = [
    "DEFAULT_REQUEST_LIMIT",
    "LARGE_MEDIA_REQUEST_LIMIT",
    "LOCAL_PROFILE",
    "SERVER_PROFILE",
    "WebSecurityConfigurationError",
    "WebSecurityPolicy",
    "apply_security_headers",
    "configure_web_security",
    "normalized_request_id",
    "request_size_limit",
    "resolve_web_security_policy",
    "waitress_proxy_options",
]
