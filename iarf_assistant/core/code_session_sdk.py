"""Small runtime SDK copied into governed Python code sessions.

This module intentionally uses only the Python standard library. Generated code
imports ``runtime`` from the session-local copy named ``iarf_runtime.py``.
Authenticated clients and provider credentials remain in the host process.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _product_environment_value(
    suffix: str,
    *,
    default: str | None = None,
) -> str | None:
    """Read a session contract value without depending on the host package."""

    canonical_name = f"CARLINA_{suffix}"
    if canonical_name in os.environ:
        return os.environ[canonical_name]
    return os.environ.get(f"IARF_{suffix}", default)


class HostActionPending(RuntimeError):
    def __init__(self, request_id: str):
        super().__init__(f"Host action {request_id} is pending.")
        self.request_id = request_id


class HostActionError(RuntimeError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _safe_relative_path(value: Any) -> Path:
    text = str(value or "").strip().replace("\\", "/")
    candidate = Path(text)
    if not text or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("A non-empty session-relative path is required.")
    return candidate


class _RuntimeBase:
    def __init__(self):
        root = _product_environment_value("SESSION_ROOT")
        if not root:
            raise RuntimeError(
                "CARLINA_SESSION_ROOT is unavailable "
                "(the legacy IARF_SESSION_ROOT alias is also accepted)."
            )
        self.root = Path(root).resolve()

    def _inside(self, base: Path, value: Any) -> Path:
        path = (base / _safe_relative_path(value)).resolve()
        path.relative_to(base.resolve())
        return path


class Resources(_RuntimeBase):
    def _manifest(self) -> dict[str, Any]:
        return json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                key: value
                for key, value in row.items()
                if key not in {"source_path"}
            }
            for row in self._manifest().get("resources") or []
            if isinstance(row, dict)
        ]

    def path(self, name: str) -> str:
        match = next(
            (
                row
                for row in self._manifest().get("resources") or []
                if isinstance(row, dict) and row.get("name") == name
            ),
            None,
        )
        if not match:
            raise KeyError(f"Session resource '{name}' is not declared.")
        path = self._inside(self.root, match.get("path"))
        if not path.is_file():
            raise FileNotFoundError(f"Session resource '{name}' is unavailable.")
        return str(path)

    def read_text(
        self,
        name: str,
        *,
        encoding: str = "utf-8",
        max_chars: int = 5_000_000,
    ) -> str:
        text = Path(self.path(name)).read_text(encoding=encoding, errors="replace")
        limit = max(1, min(int(max_chars or 1), 20_000_000))
        if len(text) > limit:
            raise ValueError(
                f"Resource '{name}' contains {len(text)} characters; limit is {limit}."
            )
        return text

    def read_json(self, name: str) -> Any:
        return json.loads(self.read_text(name))

    def output_path(self, name: str) -> str:
        path = self._inside(self.root / "outputs", name)
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def write_text(
        self,
        name: str,
        content: Any,
        *,
        encoding: str = "utf-8",
    ) -> str:
        path = Path(self.output_path(name))
        path.write_text(str(content), encoding=encoding)
        return str(path)

    def write_json(self, name: str, value: Any) -> str:
        path = Path(self.output_path(name))
        path.write_text(
            json.dumps(value, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return str(path)


class _HostActions(_RuntimeBase):
    def __init__(self):
        super().__init__()
        self._ordinal = 0
        self._execution_id = (
            _product_environment_value("EXECUTION_ID") or "execution"
        )

    def _request(self, action: str, payload: dict[str, Any]) -> Any:
        self._ordinal += 1
        identity = {
            "execution_id": self._execution_id,
            "ordinal": self._ordinal,
            "action": action,
            "payload": payload,
        }
        request_id = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()[:32]
        requests_dir = self.root / "requests"
        responses_dir = self.root / "responses"
        requests_dir.mkdir(parents=True, exist_ok=True)
        responses_dir.mkdir(parents=True, exist_ok=True)
        response_path = responses_dir / f"{request_id}.json"
        if response_path.is_file():
            response = json.loads(response_path.read_text(encoding="utf-8"))
            if not response.get("ok", False):
                raise HostActionError(
                    str(response.get("error") or "The host action failed.")
                )
            return response.get("result")

        request_path = requests_dir / f"{request_id}.json"
        if not request_path.exists():
            request = {
                **identity,
                "request_id": request_id,
            }
            request_path.write_text(
                json.dumps(request, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        raise HostActionPending(request_id)


class Tools(_HostActions):
    def call(
        self,
        *,
        arguments: dict[str, Any] | None = None,
        tool_view_id: str | None = None,
        tool_id: str | None = None,
        function_name: str | None = None,
        call_id: str | None = None,
    ) -> Any:
        call = {
            "call_id": call_id,
            "tool_view_id": tool_view_id,
            "tool_id": tool_id,
            "function_name": function_name,
            "arguments": dict(arguments or {}),
        }
        return self._request(
            "tool.call",
            {key: value for key, value in call.items() if value not in (None, "")},
        )

    def call_many(
        self,
        calls: list[dict[str, Any]],
        *,
        continue_on_error: bool = True,
    ) -> Any:
        if not isinstance(calls, list) or not calls:
            raise ValueError("calls must be a non-empty list.")
        if len(calls) > 50:
            raise ValueError("A code session can request at most 50 tool calls per batch.")
        return self._request(
            "tool.call_many",
            {
                "calls": calls,
                "continue_on_error": bool(continue_on_error),
            },
        )


class Agents(_HostActions):
    def map(
        self,
        items: list[Any],
        *,
        instructions: str,
        concurrency: int = 4,
        model_role: str = "fast",
        output_schema: dict[str, Any] | None = None,
        title: str = "Mapped code-session work",
    ) -> Any:
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty list.")
        if len(items) > 100:
            raise ValueError("A code session can map at most 100 items per request.")
        return self._request(
            "agents.map",
            {
                "items": items,
                "instructions": str(instructions or "").strip(),
                "concurrency": max(1, min(int(concurrency or 1), 8)),
                "model_role": str(model_role or "fast"),
                "output_schema": output_schema if isinstance(output_schema, dict) else None,
                "title": str(title or "Mapped code-session work"),
            },
        )


class Artifacts(_HostActions):
    def create(
        self,
        output_path: str,
        *,
        label: str | None = None,
        metadata: dict[str, Any] | None = None,
        review_status: str = "draft",
    ) -> Any:
        relative = _safe_relative_path(output_path)
        if relative.parts and relative.parts[0] == "outputs":
            relative = Path(*relative.parts[1:])
        candidate = self._inside(self.root / "outputs", relative)
        if not candidate.is_file():
            raise FileNotFoundError(
                "Artifacts can only be created from an existing session output file."
            )
        return self._request(
            "artifact.create",
            {
                "output_path": relative.as_posix(),
                "label": label,
                "metadata": metadata if isinstance(metadata, dict) else {},
                "review_status": str(review_status or "draft"),
            },
        )


class Progress(_RuntimeBase):
    def __init__(self):
        super().__init__()
        self._ordinal = 0

    def emit(
        self,
        message: str,
        *,
        progress: float | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ordinal += 1
        execution_id = _product_environment_value("EXECUTION_ID")
        event = {
            "id": hashlib.sha256(
                _canonical_json(
                    {
                        "execution_id": execution_id,
                        "ordinal": self._ordinal,
                        "message": message,
                        "progress": progress,
                        "data": data,
                    }
                ).encode("utf-8")
            ).hexdigest()[:24],
            "execution_id": execution_id,
            "message": str(message or ""),
            "progress": (
                max(0.0, min(float(progress), 1.0))
                if progress is not None
                else None
            ),
            "data": data if isinstance(data, dict) else {},
        }
        path = self.root / "progress.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical_json(event) + "\n")
        return event


class State(_RuntimeBase):
    @property
    def path(self) -> Path:
        return self.root / "state.json"

    def all(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.all().get(str(key), default)

    def set(self, key: str, value: Any) -> Any:
        state = self.all()
        state[str(key)] = value
        self.path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return value


class Runtime(_RuntimeBase):
    def __init__(self):
        super().__init__()
        self.resources = Resources()
        self.tools = Tools()
        self.agents = Agents()
        self.artifacts = Artifacts()
        self.progress = Progress()
        self.state = State()

    @property
    def input(self) -> Any:
        path_value = _product_environment_value("EXECUTION_INPUT")
        if not path_value:
            return None
        path = Path(path_value).resolve()
        path.relative_to(self.root)
        return json.loads(path.read_text(encoding="utf-8"))


runtime = Runtime()
