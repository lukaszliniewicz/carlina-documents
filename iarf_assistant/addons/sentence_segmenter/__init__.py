import logging
import math
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests
from flask import has_app_context

from carlina.addons.api import AddonBase
from carlina.runtime_environment import is_worker_process
from iarf_assistant.models import Setting

from .container.batch_protocol import (
    SplitBatchUnsupported,
    check_batch_response,
    exceeds_batch_bytes,
    validate_batch_texts,
)
from .routes import blueprint

logger = logging.getLogger(__name__)
SERVICE_TIMEOUT = (5, 300)


class SentenceSegmenterClient:
    """Request-local capability adapter for the shared segmenter service."""

    def __init__(self, addon):
        self._addon = addon

    def split_text(self, *args, **kwargs):
        return self._addon.split_text(*args, **kwargs)

    def split_paragraphs(self, *args, **kwargs):
        return self._addon.split_paragraphs(*args, **kwargs)

    def predict_boundaries(self, *args, **kwargs):
        return self._addon.predict_boundaries(*args, **kwargs)

    def service_health(self):
        return self._addon.service_health()

    def source_splitter(self):
        return self._addon.source_splitter()

    def split_texts(self, *args, **kwargs):
        return self._addon.split_texts(*args, **kwargs)


class SentenceSegmenterAddon(AddonBase):
    def __init__(self, context, manifest):
        super().__init__(context, manifest)
        self.blueprint = blueprint
        self.blueprint.addon_context = context
        self.blueprint.url_prefix = f"/addons/{self.id}"
        self._container_url = None
        self._native_client = None

    def _uses_native_backend(self) -> bool:
        application = getattr(self.context, "application", None)
        config = getattr(application, "config", {})
        profile = config.get("CARLINA_PROFILE", "workbench")
        backend = "auto"
        if has_app_context():
            setting = Setting.query.filter_by(key=f"{self.id}_backend").first()
            backend = setting.value if setting and setting.value else "auto"
        if backend not in {"auto", "native", "container"}:
            raise ValueError("Sentence Segmenter backend selection is invalid.")
        if backend == "container" and profile == "documents":
            raise ValueError("Container segmentation is unavailable in the document app.")
        return backend == "native" or (backend == "auto" and profile == "documents")

    def _ensure_native_worker(self):
        if not is_worker_process():
            self.context.ensure_worker_pool("native_worker.py", num_workers=1)

    def _get_native_client(self):
        if self._native_client is None:
            from .native_client import NativeQueueClient

            self._native_client = NativeQueueClient(
                Path(self.context.application.instance_path),
                ensure_worker=self._ensure_native_worker,
            )
        return self._native_client

    def on_register_jobs(self, scheduler):
        if self._uses_native_backend():
            self._ensure_native_worker()

    def initialize_client(self, config):
        return SentenceSegmenterClient(self)

    def _get_container_url(self, *, refresh: bool = False) -> str:
        if refresh or not self._container_url:
            self._container_url = self.context.get_container_url()
        return self._container_url

    @contextmanager
    def _heartbeat_activity(self):
        """Best-effort marker refresh for contexts predating container_use.

        No database/session is used, and the thread is stopped on every
        exit path. Touch failures never fail the wrapped request.
        """
        touch = getattr(self.context, "keep_container_alive", None)
        if not callable(touch):  # capability adapters predating the host method
            yield
            return

        def _safe_touch():
            try:
                touch()
            except Exception:  # noqa: BLE001 - activity marker must never fail segmentation
                logger.warning("Could not refresh segmenter activity marker.")

        stopped = threading.Event()
        _safe_touch()

        def heartbeat():
            while not stopped.wait(15):
                _safe_touch()

        thread = threading.Thread(target=heartbeat, name="segmenter-activity", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)
            _safe_touch()

    @contextmanager
    def _request_activity(self):
        """Hold the host guard for the entire request, including cached URLs.

        Current hosts must acquire the guard before any request is attempted.
        Only old contexts lacking this API use the compatibility heartbeat;
        contention or lock I/O errors must not silently bypass coordination.
        """
        guard = getattr(self.context, "container_use", None)
        if callable(guard):
            with guard():
                yield
            return
        with self._heartbeat_activity():
            yield

    def _post(self, path: str, payload: dict):
        if self._uses_native_backend():
            return self._get_native_client().request(path.strip("/"), payload)
        with self._request_activity():
            endpoint = f"{self._get_container_url()}/{path.lstrip('/')}"
            try:
                return requests.post(endpoint, json=payload, timeout=SERVICE_TIMEOUT)
            except requests.ConnectionError, requests.Timeout:
                endpoint = f"{self._get_container_url(refresh=True)}/{path.lstrip('/')}"
                return requests.post(endpoint, json=payload, timeout=SERVICE_TIMEOUT)

    def split_text(
        self,
        text: str,
        stride: int = None,
        block_size: int = None,
        *,
        raise_on_failure: bool = False,
        expected_identity: str | None = None,
    ) -> list[str]:
        """
        Public API for other addons to use.
        Requests segmentation from the selected native or container backend.
        """
        if not text or not text.strip():
            return []

        try:
            # 1. Fetch settings (fallback to defaults if not set)
            if stride is None:
                s_setting = Setting.query.filter_by(key=f"{self.id}_stride").first()
                stride = int(s_setting.value) if s_setting and s_setting.value else 128

            if block_size is None:
                b_setting = Setting.query.filter_by(key=f"{self.id}_block_size").first()
                block_size = (
                    int(b_setting.value) if b_setting and b_setting.value else 256
                )

            # 2. Call the microservice. Reuse the URL during batch segmentation.
            payload = {
                "text": text,
                "stride": stride,
                "block_size": block_size,
                "weighting": "hat",
                "treat_newline_as_space": True,
            }

            if expected_identity is not None:
                payload["expected_identity"] = expected_identity
            response = self._post("/split", payload)
            response.raise_for_status()

            data = response.json()
            if expected_identity is not None and (
                not isinstance(data, dict) or data.get("segmentation_identity") != expected_identity
            ):
                raise RuntimeError("The Sentence Segmenter identity changed during planning.")
            sentences = data.get("sentences") if isinstance(data, dict) else None
            if (
                not isinstance(sentences, list)
                or not sentences
                or any(
                    not isinstance(item, str) or not item.strip() for item in sentences
                )
            ):
                raise RuntimeError(
                    "Sentence Segmenter returned an invalid sentence response."
                )
            return sentences

        except Exception as e:
            logger.error(f"Sentence Segmenter microservice failed: {e}")
            if raise_on_failure:
                raise
            return [text]  # Fail gracefully by returning the unsplit text

    def split_texts(
        self,
        texts,
        stride: int = None,
        block_size: int = None,
        *,
        raise_on_failure: bool = False,
        expected_identity: str | None = None,
        max_429_retries: int = 2,
    ) -> list[list[str]]:
        """Segment one bounded batch with a single container call.

        Raises SplitBatchUnsupported only when the server predates
        /split_batch (HTTP 404/405) so the caller can fall back to per-text
        /split calls. Identity drift (409), other HTTP errors and malformed
        bodies never fall back: strict callers raise, lenient callers keep
        each input whole. Capacity exhaustion (429) is retried a bounded
        number of times honoring Retry-After (default 2s, capped at 5s);
        after the budget is spent it raises like any other failure.
        """
        items = list(texts) if isinstance(texts, (list, tuple)) else texts
        if isinstance(items, list) and not items:
            return []
        if (
            len(items) == 1
            and isinstance(items[0], str)
            and exceeds_batch_bytes(items[0])
        ):
            # One valid projection can exceed the batch byte total while
            # staying within the legacy per-text character bound. Route it
            # to /split explicitly instead of failing batch validation.
            return [self.split_text(
                items[0], stride=stride, block_size=block_size,
                raise_on_failure=raise_on_failure,
                expected_identity=expected_identity,
            )]
        batch = validate_batch_texts(items)

        try:
            if stride is None:
                s_setting = Setting.query.filter_by(key=f"{self.id}_stride").first()
                stride = int(s_setting.value) if s_setting and s_setting.value else 128

            if block_size is None:
                b_setting = Setting.query.filter_by(key=f"{self.id}_block_size").first()
                block_size = (
                    int(b_setting.value) if b_setting and b_setting.value else 256
                )

            payload = {
                "texts": batch,
                "stride": stride,
                "block_size": block_size,
                "weighting": "hat",
                "treat_newline_as_space": True,
            }
            if expected_identity is not None:
                payload["expected_identity"] = expected_identity
            attempts = 3
            if isinstance(max_429_retries, int) and not isinstance(max_429_retries, bool):
                attempts = min(max(1, max_429_retries + 1), 4)
            response = None
            for attempt in range(attempts):
                response = self._post("/split_batch", payload)
                try:
                    response.raise_for_status()
                    break
                except Exception as http_exc:
                    status = getattr(getattr(http_exc, "response", None), "status_code", None)
                    if status is None:
                        status = getattr(response, "status_code", None)
                    if status in (404, 405):
                        raise SplitBatchUnsupported(
                            "The Sentence Segmenter server predates batch segmentation."
                        ) from http_exc
                    if status == 429 and attempt + 1 < attempts:
                        retry_after = None
                        headers = getattr(response, "headers", None)
                        getter = getattr(headers, "get", None)
                        if callable(getter):
                            try:
                                retry_after = getter("Retry-After")
                            except Exception:
                                retry_after = None
                        try:
                            delay = float(retry_after) if retry_after is not None else 2.0
                        except (TypeError, ValueError):
                            delay = 2.0
                        if not math.isfinite(delay):
                            # NaN must not reach sleep (raises) and Inf must
                            # not stall the worker: fall back to the default.
                            delay = 2.0
                        try:
                            response.close()
                        except Exception:
                            pass
                        time.sleep(min(max(delay, 0.0), 5.0))
                        continue
                    raise
            data = response.json()
            if expected_identity is not None and (
                not isinstance(data, dict) or data.get("segmentation_identity") != expected_identity
            ):
                raise RuntimeError("The Sentence Segmenter identity changed during planning.")
            return check_batch_response(data, len(batch))
        except SplitBatchUnsupported:
            raise
        except Exception as e:
            logger.error(f"Sentence Segmenter batch microservice failed: {e}")
            if raise_on_failure:
                raise
            return [[text] for text in batch]

    def split_paragraphs(
        self,
        text: str,
        stride: int = None,
        block_size: int = None,
        *,
        threshold: float = None,
        paragraph_threshold: float = None,
        raise_on_failure: bool = False,
    ) -> list[str]:
        if not text or not text.strip():
            return []

        try:
            if stride is None:
                s_setting = Setting.query.filter_by(key=f"{self.id}_stride").first()
                stride = int(s_setting.value) if s_setting and s_setting.value else 128

            if block_size is None:
                b_setting = Setting.query.filter_by(key=f"{self.id}_block_size").first()
                block_size = (
                    int(b_setting.value) if b_setting and b_setting.value else 256
                )

            if threshold is None:
                t_setting = Setting.query.filter_by(key=f"{self.id}_threshold").first()
                if t_setting and t_setting.value not in (None, ""):
                    threshold = float(t_setting.value)

            payload = {
                "text": text,
                "stride": stride,
                "block_size": block_size,
                "weighting": "hat",
                "treat_newline_as_space": True,
                "threshold": threshold,
                "paragraph_threshold": paragraph_threshold,
            }
            response = self._post("/paragraphs", payload)
            response.raise_for_status()
            data = response.json()
            paragraphs = data.get("paragraphs") if isinstance(data, dict) else None
            if not isinstance(paragraphs, list):
                raise RuntimeError(
                    "Sentence Segmenter returned an invalid paragraph response."
                )
            cleaned = [
                str(paragraph).strip()
                for paragraph in paragraphs
                if str(paragraph or "").strip()
            ]
            return cleaned or [text.strip()]
        except Exception as e:
            logger.error(f"Sentence Segmenter paragraph split failed: {e}")
            if raise_on_failure:
                raise
            return [text.strip()]

    def source_splitter(self):
        """Freeze effective settings once, not two database queries per paragraph."""
        health = self.service_health()
        runtime = health.get("segmentation_identity") or {}
        fingerprint = runtime.get("fingerprint") if isinstance(runtime, dict) else None
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            if health.get("backend") == "native" and health.get("preparation_required"):
                raise RuntimeError("Prepare the native Sentence Segmenter model before source planning.")
            raise RuntimeError("Update the Sentence Segmenter service before source planning: effective identity is unavailable.")
        settings = {row.key: row.value for row in Setting.query.filter(
            Setting.key.in_([f"{self.id}_stride", f"{self.id}_block_size"])
        ).all()}
        stride = int(settings.get(f"{self.id}_stride") or 128)
        block_size = int(settings.get(f"{self.id}_block_size") or 256)
        if not (1 <= stride <= 512 and 1 <= block_size <= 512):
            raise ValueError("Sentence Segmenter stride and block size must be between 1 and 512.")

        def split(text):
            return self.split_text(text, stride=stride, block_size=block_size,
                                   raise_on_failure=True, expected_identity=fingerprint)

        def split_batch(batch):
            return self.split_texts(batch, stride=stride, block_size=block_size,
                                    raise_on_failure=True, expected_identity=fingerprint)

        split.split_batch = split_batch

        split.segmentation_profile = {
            "runtime": fingerprint, "stride": stride, "block_size": block_size,
            "weighting": "hat", "treat_newline_as_space": True,
        }
        return split

    def service_health(self) -> dict:
        if self._uses_native_backend():
            response = self._get_native_client().request("health", {})
            response.raise_for_status()
            return response.json()
        container_url = self._get_container_url()
        endpoint = f"{container_url}/health"
        try:
            response = requests.get(endpoint, timeout=SERVICE_TIMEOUT)
        except requests.ConnectionError, requests.Timeout:
            endpoint = f"{self._get_container_url(refresh=True)}/health"
            response = requests.get(endpoint, timeout=SERVICE_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(
                "Sentence Segmenter health endpoint returned invalid data."
            )
        return payload

    def predict_boundaries(
        self,
        text: str,
        stride: int = None,
        block_size: int = None,
        threshold: float = None,
    ) -> dict:
        """
        Return SaT sentence-boundary probabilities for character-level alignment.

        Unlike split_text(), this method raises on failure so callers that depend
        on sentence-complete timing do not silently fall back to heuristic splits.
        """
        if not text or not text.strip():
            return {
                "threshold": threshold,
                "probabilities": [],
                "boundaries": [],
                "sentence_spans": [],
            }

        if stride is None:
            s_setting = Setting.query.filter_by(key=f"{self.id}_stride").first()
            stride = int(s_setting.value) if s_setting and s_setting.value else 128

        if block_size is None:
            b_setting = Setting.query.filter_by(key=f"{self.id}_block_size").first()
            block_size = int(b_setting.value) if b_setting and b_setting.value else 256

        t_setting = Setting.query.filter_by(key=f"{self.id}_threshold").first()
        if threshold is None and t_setting and t_setting.value not in (None, ""):
            threshold = float(t_setting.value)

        payload = {
            "text": text,
            "stride": stride,
            "block_size": block_size,
            "weighting": "hat",
            "treat_newline_as_space": True,
            "threshold": threshold,
        }
        response = self._post("/boundary-probabilities", payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(
                "Sentence Segmenter returned an invalid boundary response."
            )
        return data
