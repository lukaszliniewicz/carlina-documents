import ipaddress
import socket
from urllib.parse import urlparse


def validate_external_url(url: str) -> str:
    """Allows only public HTTP(S) URLs for assistant-controlled network fetches."""
    cleaned = str(url or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid external URL: {url}")

    try:
        address_info = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve URL host: {parsed.hostname}") from exc

    for entry in address_info:
        ip_text = str(entry[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if any(
            [
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_unspecified,
                ip.is_reserved,
                ip.is_multicast,
            ]
        ):
            raise ValueError(f"Blocked non-public URL host: {parsed.hostname}")
    return cleaned


def validate_external_urls(urls) -> list[str]:
    return [validate_external_url(url) for url in (urls or [])]


__all__ = ["validate_external_url", "validate_external_urls"]
