from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from PIL import Image
except ImportError:  # pragma: no cover - depends on optional runtime extras
    Image = None


MIN_REMOTE_IMAGE_BYTES = 100 * 1024
MAX_REMOTE_IMAGE_BYTES = 10 * 1024 * 1024
MIN_IMAGE_LONG_EDGE = 500
REMOTE_IMAGE_CONNECT_TIMEOUT = 2.0
REMOTE_IMAGE_READ_TIMEOUT = 5.0
REMOTE_IMAGE_MAX_RETRIES = 1
REMOTE_IMAGE_BACKOFF_FACTOR = 0.25
REMOTE_IMAGE_HOST_FAILURE_LIMIT = 3
REMOTE_IMAGE_USER_AGENT = "Carlina-Email-Image-Fetcher/1.0"
TRANSIENT_HTTP_STATUSES = (429, 500, 502, 503, 504)


@dataclass(frozen=True)
class RemoteImage:
    data: bytes
    mime_type: str


def image_bytes_worth_keeping(
    img_data: bytes,
    *,
    source: str = "image",
    min_bytes: int = MIN_REMOTE_IMAGE_BYTES,
    min_long_edge: int = MIN_IMAGE_LONG_EDGE,
    verbose: bool = False,
) -> bool:
    if not img_data:
        return False

    if min_bytes and len(img_data) < min_bytes:
        if verbose:
            logging.info("Skipping small %s: %s bytes", source, len(img_data))
        return False

    if Image and min_long_edge:
        try:
            with Image.open(BytesIO(img_data)) as img:
                if max(img.width, img.height) < min_long_edge:
                    if verbose:
                        logging.info("Skipping small-dimension %s: %sx%s", source, img.width, img.height)
                    return False
        except Exception as exc:
            logging.warning("Could not read dimensions for %s: %s", source, exc)
            return False

    return True


def image_file_worth_keeping(
    path: Path,
    *,
    source: str = "image",
    min_bytes: int = MIN_REMOTE_IMAGE_BYTES,
    min_long_edge: int = MIN_IMAGE_LONG_EDGE,
    verbose: bool = False,
) -> bool:
    try:
        size = path.stat().st_size
    except OSError as exc:
        logging.warning("Could not stat %s %s: %s", source, path, exc)
        return False

    if min_bytes and size < min_bytes:
        if verbose:
            logging.info("Skipping small %s: %s bytes", source, size)
        return False

    if Image and min_long_edge:
        try:
            with Image.open(path) as img:
                if max(img.width, img.height) < min_long_edge:
                    if verbose:
                        logging.info("Skipping small-dimension %s: %sx%s", source, img.width, img.height)
                    return False
        except Exception as exc:
            logging.warning("Could not read dimensions for %s %s: %s", source, path, exc)
            return False

    return True


class RemoteImageFetcher:
    """Bounded, cached remote image downloader for email sync."""

    def __init__(
        self,
        *,
        verbose: bool = False,
        min_bytes: int = MIN_REMOTE_IMAGE_BYTES,
        max_bytes: int = MAX_REMOTE_IMAGE_BYTES,
        min_long_edge: int = MIN_IMAGE_LONG_EDGE,
        connect_timeout: float = REMOTE_IMAGE_CONNECT_TIMEOUT,
        read_timeout: float = REMOTE_IMAGE_READ_TIMEOUT,
        max_retries: int = REMOTE_IMAGE_MAX_RETRIES,
        backoff_factor: float = REMOTE_IMAGE_BACKOFF_FACTOR,
        host_failure_limit: int = REMOTE_IMAGE_HOST_FAILURE_LIMIT,
        session: Any | None = None,
    ):
        self.verbose = verbose
        self.min_bytes = min_bytes
        self.max_bytes = max_bytes
        self.min_long_edge = min_long_edge
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_factor = backoff_factor
        self.host_failure_limit = max(1, int(host_failure_limit))
        self._session = session
        self._cache: dict[str, RemoteImage | None] = {}
        self._host_failures: dict[str, int] = {}

    def fetch(self, img_url: str) -> RemoteImage | None:
        normalized_url = self._normalize_url(img_url)
        if not normalized_url:
            return None

        if normalized_url in self._cache:
            return self._cache[normalized_url]

        host = self._host_for(normalized_url)
        if host and self._host_failures.get(host, 0) >= self.host_failure_limit:
            if self.verbose:
                logging.info("Skipping remote images from %s after repeated transport failures.", host)
            self._cache[normalized_url] = None
            return None

        try:
            image = self._fetch_uncached(normalized_url)
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in TRANSIENT_HTTP_STATUSES:
                self._record_host_failure(host)
            logging.warning("Could not download remote image %s: %s", normalized_url, exc)
            image = None
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.RetryError) as exc:
            self._record_host_failure(host)
            logging.warning("Could not download remote image %s: %s", normalized_url, exc)
            image = None
        except requests.RequestException as exc:
            logging.warning("Could not download remote image %s: %s", normalized_url, exc)
            image = None
        except Exception as exc:
            logging.warning("Could not process remote image %s: %s", normalized_url, exc)
            image = None

        self._cache[normalized_url] = image
        return image

    def image_is_worth_keeping(self, img_data: bytes, *, source: str = "image") -> bool:
        return image_bytes_worth_keeping(
            img_data,
            source=source,
            min_bytes=self.min_bytes,
            min_long_edge=self.min_long_edge,
            verbose=self.verbose,
        )

    def _fetch_uncached(self, img_url: str) -> RemoteImage | None:
        response = self._get_session().get(
            img_url,
            timeout=(self.connect_timeout, self.read_timeout),
            stream=True,
            allow_redirects=True,
            headers={"User-Agent": REMOTE_IMAGE_USER_AGENT},
        )

        try:
            response.raise_for_status()

            raw_content_type = response.headers.get("Content-Type", "")
            mime_type = raw_content_type.split(";", 1)[0].strip().lower()
            if mime_type and not mime_type.startswith("image/"):
                return None
            if not mime_type:
                mime_type = "image/png"

            content_length = self._parse_content_length(response.headers.get("Content-Length"))
            if content_length is not None:
                if self.min_bytes and content_length < self.min_bytes:
                    return None
                if self.max_bytes and content_length > self.max_bytes:
                    return None

            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if self.max_bytes and total > self.max_bytes:
                    return None
                chunks.append(chunk)

            img_data = b"".join(chunks)
            if not self.image_is_worth_keeping(img_data, source="remote image"):
                return None
            return RemoteImage(data=img_data, mime_type=mime_type)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def _get_session(self):
        if self._session is None:
            retry = Retry(
                total=self.max_retries,
                connect=self.max_retries,
                read=0,
                status=self.max_retries,
                backoff_factor=self.backoff_factor,
                status_forcelist=TRANSIENT_HTTP_STATUSES,
                allowed_methods=frozenset(["GET"]),
                respect_retry_after_header=True,
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
            session = requests.Session()
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self._session = session
        return self._session

    @staticmethod
    def _normalize_url(img_url: str) -> str | None:
        url = (img_url or "").strip()
        if not url:
            return None
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        return url

    @staticmethod
    def _host_for(img_url: str) -> str:
        return (urlsplit(img_url).hostname or "").lower()

    @staticmethod
    def _parse_content_length(raw_value: str | None) -> int | None:
        if not raw_value:
            return None
        value = raw_value.strip()
        if not value.isdigit():
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _record_host_failure(self, host: str) -> None:
        if not host:
            return
        self._host_failures[host] = self._host_failures.get(host, 0) + 1
