"""Executable capability adapter for Carlina's local audio-engine spokes."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class AudioSpokeCapabilityClient:
    """Request-local client for an engine's declared async spoke transport.

    Audio Core discovers the same engine metadata for its model catalog. This
    adapter additionally makes the capability executable through the generic
    provider handle, so declarations such as ``audio.transcription/transcribe``
    and ``audio.alignment/align`` are truthful outside Audio Core as well.
    """

    def __init__(
        self,
        context: Any,
        addon_id: str,
        manifest: Mapping[str, Any],
        config: Mapping[str, Any] | None = None,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.context = context
        self.addon_id = str(addon_id)
        self.manifest = dict(manifest)
        self.config = dict(config or {})
        self.session = session or requests.Session()
        self._sleep = sleep
        self._monotonic = monotonic
        self.provider_capability_id = str(
            self.config.get("capability_provider_id") or ""
        ).strip()
        self._capability = self._select_capability()
        self.allowed_operations = frozenset(
            str(operation) for operation in self._capability.get("operations") or ()
        )
        self._audio_provider = self._select_audio_provider()
        self.transport = dict(self._audio_provider.get("transport") or {})

    def close(self) -> None:
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False

    def _select_capability(self) -> dict[str, Any]:
        capabilities = tuple(
            item
            for item in (self.manifest.get("provides") or {}).get(
                "capabilities", ()
            )
            if isinstance(item, Mapping)
        )
        if self.provider_capability_id:
            selected = next(
                (
                    item
                    for item in capabilities
                    if str(item.get("id")) == self.provider_capability_id
                ),
                None,
            )
            if selected is None:
                raise LookupError(
                    f"Audio engine {self.addon_id!r} does not provide capability "
                    f"{self.provider_capability_id!r}."
                )
            return dict(selected)
        if len(capabilities) == 1:
            return dict(capabilities[0])
        if not capabilities:
            raise LookupError(
                f"Audio engine {self.addon_id!r} declares no capabilities."
            )
        operations = {
            str(operation)
            for item in capabilities
            for operation in item.get("operations") or ()
        }
        return {"id": "all", "operations": sorted(operations)}

    def _select_audio_provider(self) -> dict[str, Any]:
        providers = tuple(
            item
            for item in (self.manifest.get("audio") or {}).get("providers", ())
            if isinstance(item, Mapping)
        )
        selected = next(
            (
                item
                for item in providers
                if str(item.get("source_addon_id") or self.addon_id)
                == self.addon_id
            ),
            providers[0] if providers else None,
        )
        if selected is None:
            raise LookupError(
                f"Audio engine {self.addon_id!r} has no audio provider transport."
            )
        if str(selected.get("adapter") or "iarf_async_spoke") != "iarf_async_spoke":
            raise ValueError(
                f"Audio engine {self.addon_id!r} does not use the supported "
                "iarf_async_spoke adapter."
            )
        return dict(selected)

    def models(self, operation: str | None = None) -> list[dict[str, Any]]:
        """Return copied model metadata for capability-aware catalog consumers."""

        suffix = str(operation or "").strip().lower()
        models = []
        for raw_model in self._audio_provider.get("models") or ():
            if not isinstance(raw_model, Mapping):
                continue
            model_operation = str(raw_model.get("operation") or "").lower()
            if suffix and model_operation.rsplit(".", 1)[-1] != suffix:
                continue
            models.append(dict(raw_model))
        return models

    def _require_operation(self, operation: str) -> None:
        if operation not in self.allowed_operations:
            raise PermissionError(
                f"Capability {self._capability.get('id')!r} does not expose "
                f"operation {operation!r}."
            )

    def _base_url(self) -> str:
        container_url = str(self.context.get_container_url(self.addon_id)).rstrip("/")
        template = str(
            self.transport.get("api_base")
            or self._audio_provider.get("api_base")
            or "{container_url}"
        )
        return template.replace("{container_url}", container_url).rstrip("/")

    @staticmethod
    def _url(base_url: str, path: str) -> str:
        return f"{base_url.rstrip('/')}/{str(path).lstrip('/')}"

    @staticmethod
    def _number(mapping: Mapping[str, Any], key: str, default: float) -> float:
        value = mapping.get(key, default)
        if value in (None, ""):
            value = default
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _audio_path(value: str | Path) -> Path:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Audio file not found: {value}")
        return path

    def _notify(
        self,
        callback: Callable[[Any, str, dict[str, Any]], None] | None,
        payload: Mapping[str, Any],
        default_message: str,
    ) -> None:
        if not callable(callback):
            return
        metadata = {
            str(key): value for key, value in payload.items() if key != "result"
        }
        try:
            callback(
                payload.get("progress"),
                str(payload.get("message") or default_message),
                metadata,
            )
        except Exception:
            logger.warning("Audio spoke progress callback failed.", exc_info=True)

    def _touch(self) -> None:
        keep_alive = getattr(self.context, "keep_container_alive", None)
        if callable(keep_alive):
            try:
                keep_alive(self.addon_id)
            except Exception:
                logger.debug(
                    "Could not refresh container activity for %s.",
                    self.addon_id,
                    exc_info=True,
                )

    def _post_file(
        self,
        *,
        path: str,
        audio_path: Path,
        data: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        with audio_path.open("rb") as audio_file:
            response = self.session.post(
                self._url(self._base_url(), path),
                files={
                    "file": (
                        audio_path.name,
                        audio_file,
                        "application/octet-stream",
                    )
                },
                data=dict(data),
                timeout=timeout,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise TypeError("Audio spoke returned a non-object JSON response.")
        return dict(payload)

    def _run_job(
        self,
        *,
        audio_path: Path,
        submit_path: str,
        status_path: str,
        data: Mapping[str, Any],
        timeout_key: str,
        timeout_default: float,
        heartbeat_key: str,
        heartbeat_default: float,
        progress_stall_key: str,
        progress_stall_default: float,
        submit_timeout_key: str,
        progress_callback: Callable[[Any, str, dict[str, Any]], None] | None,
        progress_message: str,
    ) -> dict[str, Any]:
        transport = self.transport
        try:
            payload = self._post_file(
                path=submit_path,
                audio_path=audio_path,
                data=data,
                timeout=self._number(transport, submit_timeout_key, 300),
            )
            raw_job_id = payload.get("job_id")
            if raw_job_id is None:
                return {"status": "success", **payload}
            job_id = str(raw_job_id)
            if not _JOB_ID_PATTERN.fullmatch(job_id):
                raise ValueError("Audio spoke returned an invalid job identifier.")

            submitted = {
                "job_id": job_id,
                "service_job_id": job_id,
                "status": "processing",
                "stage": "submitted",
                "progress": 0,
            }
            self._notify(progress_callback, submitted, progress_message)

            poll_interval = max(
                0.05, self._number(transport, "poll_interval_seconds", 2)
            )
            timeout_seconds = self._number(
                transport, timeout_key, timeout_default
            )
            heartbeat_seconds = self._number(
                transport, heartbeat_key, heartbeat_default
            )
            stall_seconds = self._number(
                transport, progress_stall_key, progress_stall_default
            )
            poll_timeout = max(
                1.0, self._number(transport, "poll_timeout_seconds", 15)
            )
            max_failures = max(
                1,
                int(self._number(transport, "connection_failure_threshold", 30)),
            )
            started = self._monotonic()
            deadline = started + timeout_seconds if timeout_seconds > 0 else None
            heartbeat_seen = started
            progress_seen = started
            last_heartbeat = None
            last_progress = None
            last_payload: dict[str, Any] = {}
            failures = 0

            while deadline is None or self._monotonic() < deadline:
                self._touch()
                try:
                    response = self.session.get(
                        self._url(
                            self._base_url(), status_path.format(job_id=job_id)
                        ),
                        timeout=poll_timeout,
                    )
                    response.raise_for_status()
                    raw_status = response.json()
                    if not isinstance(raw_status, Mapping):
                        raise TypeError(
                            "Audio spoke job status was not a JSON object."
                        )
                    job = dict(raw_status)
                    last_payload = job
                    failures = 0
                except requests.Timeout:
                    if heartbeat_seconds > 0 and (
                        self._monotonic() - heartbeat_seen >= heartbeat_seconds
                    ):
                        return {
                            "error": "Audio spoke job heartbeat stopped.",
                            "service_job": last_payload,
                        }
                    self._sleep(poll_interval)
                    continue
                except (requests.RequestException, TypeError, ValueError) as exc:
                    failures += 1
                    if failures >= max_failures:
                        return {
                            "error": "Audio spoke connection failed during polling.",
                            "details": str(exc),
                            "service_job": last_payload,
                        }
                    self._sleep(poll_interval)
                    continue

                now = self._monotonic()
                heartbeat = job.get("heartbeat_at") or job.get("updated_at")
                if heartbeat is None or heartbeat != last_heartbeat:
                    last_heartbeat = heartbeat
                    heartbeat_seen = now
                progress = job.get("progress")
                if progress is not None and progress != last_progress:
                    last_progress = progress
                    progress_seen = now
                self._notify(progress_callback, job, progress_message)

                if heartbeat_seconds > 0 and now - heartbeat_seen >= heartbeat_seconds:
                    return {
                        "error": "Audio spoke job heartbeat stopped.",
                        "service_job": last_payload,
                    }
                if (
                    stall_seconds > 0
                    and last_progress is not None
                    and now - progress_seen >= stall_seconds
                ):
                    return {
                        "error": "Audio spoke job progress stalled.",
                        "service_job": last_payload,
                    }

                status = str(job.get("status") or "").lower()
                if status == "completed":
                    result = job.get("result")
                    if result is None:
                        result = {}
                    if not isinstance(result, Mapping):
                        raise TypeError("Audio spoke job result was not a JSON object.")
                    return {
                        "status": "success",
                        **dict(result),
                        "service_job": {
                            key: value for key, value in job.items() if key != "result"
                        },
                    }
                if status in {"failed", "error"}:
                    return {
                        "error": str(job.get("error") or "Audio spoke job failed."),
                        "service_job": job,
                    }
                self._sleep(poll_interval)

            return {"error": "Audio spoke job timed out."}
        except (OSError, requests.RequestException, TypeError, ValueError) as exc:
            logger.warning("Audio spoke operation failed: %s", exc)
            return {"error": str(exc)}

    def _validate_model(self, operation: str, model_id: str | None) -> None:
        if model_id is None:
            return
        available = {
            str(model.get("id") or model.get("model_id"))
            for model in self.models(operation)
        }
        normalized = str(model_id)
        if normalized not in available:
            raise ValueError(
                f"Model {normalized!r} is not declared for {operation!r}."
            )

    def transcribe(
        self,
        audio_file_path: str | Path,
        *,
        model_id: str | None = None,
        language: str | None = None,
        config: Mapping[str, Any] | None = None,
        prompt: str | None = None,
        progress_callback: Callable[[Any, str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Transcribe one local audio file through the engine's async service."""

        self._require_operation("transcribe")
        self._validate_model("transcribe", model_id)
        data: dict[str, Any] = {"config": json.dumps(dict(config or {}))}
        if language:
            data["language"] = str(language)
        if prompt:
            data["prompt"] = str(prompt)
        return self._run_job(
            audio_path=self._audio_path(audio_file_path),
            submit_path=str(
                self.transport.get("submit_path")
                or "/audio/transcriptions/async"
            ),
            status_path=str(
                self.transport.get("status_path") or "/jobs/{job_id}"
            ),
            data=data,
            timeout_key="timeout_seconds",
            timeout_default=3600,
            heartbeat_key="heartbeat_timeout_seconds",
            heartbeat_default=300,
            progress_stall_key="progress_stall_timeout_seconds",
            progress_stall_default=0,
            submit_timeout_key="submit_timeout_seconds",
            progress_callback=progress_callback,
            progress_message="Transcribing audio...",
        )

    def align(
        self,
        audio_file_path: str | Path,
        transcript: str,
        *,
        model_id: str | None = None,
        language: str | None = None,
        config: Mapping[str, Any] | None = None,
        progress_callback: Callable[[Any, str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Align a transcript to one local audio file through the engine service."""

        self._require_operation("align")
        self._validate_model("align", model_id)
        normalized_transcript = str(transcript or "").strip()
        if not normalized_transcript:
            raise ValueError("A non-empty transcript is required for alignment.")
        data: dict[str, Any] = {
            "transcript": normalized_transcript,
            "config": json.dumps(dict(config or {})),
        }
        if language:
            data["language"] = str(language)
        audio_path = self._audio_path(audio_file_path)
        submit_path = self.transport.get("align_submit_path")
        if submit_path:
            return self._run_job(
                audio_path=audio_path,
                submit_path=str(submit_path),
                status_path=str(
                    self.transport.get("align_status_path")
                    or self.transport.get("status_path")
                    or "/jobs/{job_id}"
                ),
                data=data,
                timeout_key="align_timeout_seconds",
                timeout_default=0,
                heartbeat_key="align_heartbeat_timeout_seconds",
                heartbeat_default=300,
                progress_stall_key="align_progress_stall_timeout_seconds",
                progress_stall_default=0,
                submit_timeout_key="align_submit_timeout_seconds",
                progress_callback=progress_callback,
                progress_message="Aligning audio to transcript...",
            )
        try:
            payload = self._post_file(
                path=str(
                    self.transport.get("align_path") or "/audio/alignments"
                ),
                audio_path=audio_path,
                data=data,
                timeout=self._number(
                    self.transport, "align_timeout_seconds", 900
                ),
            )
            return {"status": "success", **payload}
        except (OSError, requests.RequestException, TypeError, ValueError) as exc:
            logger.warning("Audio spoke alignment failed: %s", exc)
            return {"error": str(exc)}


__all__ = ["AudioSpokeCapabilityClient"]
