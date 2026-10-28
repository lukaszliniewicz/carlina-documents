"""Small HTTP client primitives with fail-closed network defaults."""

from __future__ import annotations

from collections.abc import Iterable

import requests

DEFAULT_HTTP_TIMEOUT = (10.0, 60.0)


class BoundedSession(requests.Session):
    """A requests session that cannot wait forever by omission.

    Callers may still select a different finite timeout per operation. Passing
    ``None`` deliberately does not disable the bound because add-on network
    calls run in shared host or managed-worker capacity.
    """

    def __init__(self, *, default_timeout=DEFAULT_HTTP_TIMEOUT):
        super().__init__()
        self.default_timeout = default_timeout

    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.default_timeout
        return super().request(method, url, **kwargs)


def read_bounded_response(
    response: requests.Response,
    *,
    max_bytes: int,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """Read a streamed response without trusting Content-Length."""

    maximum = max(1, int(max_bytes))
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            declared_length = int(declared)
        except TypeError, ValueError:
            declared_length = 0
        if declared_length > maximum:
            raise ValueError("The remote response is too large.")

    payload = bytearray()
    chunks: Iterable[bytes] = response.iter_content(chunk_size=max(1, chunk_size))
    for chunk in chunks:
        if not chunk:
            continue
        payload.extend(chunk)
        if len(payload) > maximum:
            raise ValueError("The remote response is too large.")
    return bytes(payload)


__all__ = ["DEFAULT_HTTP_TIMEOUT", "BoundedSession", "read_bounded_response"]
