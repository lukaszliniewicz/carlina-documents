from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup

from ..reasoning_snapshot import (
    REASONING_SNAPSHOT_KEY,
    thinking_config_for_call,
)

logger = logging.getLogger(__name__)


# These limits are deliberately independent of the upload limit. A 512 MiB
# upload can be stored, but sending that much text to a model is not a useful
# or safe document-processing job. Settings may lower the defaults, but may
# not raise these hard ceilings.
DEFAULT_TXT_CHUNK_CHARS = 24_000
DEFAULT_TXT_CHUNK_LINES = 400
DEFAULT_TXT_MAX_CHUNKS = 4_096
DEFAULT_TXT_MAX_INPUT_CHARS = 32 * 1024 * 1024
DEFAULT_TXT_MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_TXT_CHUNK_CHARS = 256_000
MAX_TXT_CHUNK_LINES = 4_000
MAX_TXT_MAX_CHUNKS = 16_384
MAX_TXT_INPUT_CHARS = 64 * 1024 * 1024
MAX_TXT_INPUT_BYTES = 128 * 1024 * 1024
DEFAULT_TXT_MAX_RETRIES = 3
DEFAULT_TXT_RETRY_BACKOFF_SECONDS = 2.0
MAX_TXT_RETRY_BACKOFF_SECONDS = 30.0
MAX_PRIOR_STRUCTURE_CONTEXT_CHARS = 2_000


_WRAPPER_TAGS = {"html", "head", "body", "main_content", "footnotes"}
_UNSAFE_URI_SCHEMES = ("javascript:", "vbscript:", "data:")
_ALLOWED_TAGS = frozenset(
    {
        "a",
        "abbr",
        "address",
        "article",
        "aside",
        "b",
        "blockquote",
        "br",
        "caption",
        "cite",
        "code",
        "col",
        "colgroup",
        "dd",
        "del",
        "div",
        "dl",
        "dt",
        "em",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "html",
        "i",
        "ins",
        "kbd",
        "li",
        "main",
        "main_content",
        "mark",
        "nav",
        "ol",
        "p",
        "pre",
        "q",
        "s",
        "samp",
        "section",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "time",
        "u",
        "ul",
        "var",
        "wbr",
        "body",
        "footnotes",
        "title",
        "head",
    }
)
_GLOBAL_ATTRIBUTES = frozenset(
    {
        "aria-describedby",
        "aria-hidden",
        "aria-label",
        "aria-labelledby",
        "class",
        "data-footnote-label",
        "data-footnote-scope",
        "data-label",
        "data-subtitle-unit",
        "dir",
        "epub:type",
        "id",
        "lang",
        "hidden",
        "role",
        "title",
        "xml:lang",
    }
)
_TAG_ATTRIBUTES = {
    "a": frozenset({"href", "name", "rel", "target"}),
    "blockquote": frozenset({"cite"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "li": frozenset({"value"}),
    "ol": frozenset({"reversed", "start", "type"}),
    "table": frozenset({"summary"}),
    "td": frozenset({"abbr", "colspan", "headers", "rowspan"}),
    "th": frozenset({"abbr", "colspan", "headers", "rowspan", "scope"}),
    "time": frozenset({"datetime"}),
}
_MAX_ATTRIBUTE_VALUE_CHARS = 4_096
_SAFE_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")
_TERMINAL_FINISH_REASON_TOKENS = frozenset(
    {
        "length",
        "max_token",
        "max_tokens",
        "token_limit",
        "recitation",
        "safety",
        "prohibited_content",
        "spii",
        "blocklist",
        "content_filter",
        "contentfilter",
        "blocked",
    }
)


class TxtParserError(RuntimeError):
    """Raised when a TXT/CSV model conversion cannot produce a complete book."""


class _UnsafeChunkError(TxtParserError):
    """A model response contains unsafe HTML and must not be retried blindly."""


class _InvalidChunkError(TxtParserError):
    """A model response is empty or otherwise not an HTML fragment."""


class _TerminalModelError(TxtParserError):
    """The provider stopped for a terminal safety, truncation, or policy reason."""

    def __init__(self, finish_reason: str):
        self.finish_reason = finish_reason
        super().__init__(f"terminal provider finish reason: {finish_reason}")


@dataclass(frozen=True)
class _SourceLine:
    number: int
    text: str


@dataclass(frozen=True)
class _TxtChunk:
    start_line: int
    end_line: int
    lines: tuple[_SourceLine, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def numbered_text(self) -> str:
        return "\n".join(f"[{line.number}] {line.text}" for line in self.lines)

    @property
    def character_count(self) -> int:
        return len(self.text)

    @property
    def line_count(self) -> int:
        return len(self.lines)


class TxtParser:
    def __init__(
        self,
        file_path: str,
        user_id: int | None,
        llm_service,
        settings: dict | None = None,
    ):
        self.file_path = file_path
        self.user_id = user_id
        self.llm_service = llm_service
        self.settings = settings or {}
        self.metrics = {}

    def _get_setting(self, key: str, default=None):
        if key in self.settings:
            return self.settings[key]
        if key == "prompt_txt_structuring":
            from iarf_assistant.addons.doc_processor.prompts import (
                DEFAULT_TXT_STRUCTURING_PROMPT,
            )

            return DEFAULT_TXT_STRUCTURING_PROMPT
        return default

    def _record_model(self, stage: str, model: str | None) -> None:
        if not model:
            return
        models_used = self.settings.setdefault("models_used", {})
        values = models_used.setdefault(stage, [])
        if model not in values:
            values.append(model)

    def _read_text(self) -> str:
        path = Path(self.file_path)
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8", errors="replace")

    def _fallback_html(self, text: str) -> str:
        """Make one deterministic fallback document, not one heading per chunk."""
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
        if not paragraphs:
            return "<p></p>"
        first, rest = paragraphs[0], paragraphs[1:]
        body = [f"<h1>{escape(first)}</h1>"]
        body.extend(f"<p>{escape(line)}</p>" for line in rest)
        return "\n".join(body)

    def _setting_int(
        self, aliases: tuple[str, ...], default: int, hard_max: int
    ) -> int:
        raw = next(
            (self.settings[key] for key in aliases if key in self.settings), default
        )
        try:
            value = int(raw)
        except TypeError, ValueError:
            value = default
        return max(1, min(value, hard_max))

    def _limits(self) -> tuple[int, int, int, int, int]:
        return (
            self._setting_int(
                ("txt_max_chunk_chars", "txt_chunk_chars", "max_chunk_chars"),
                DEFAULT_TXT_CHUNK_CHARS,
                MAX_TXT_CHUNK_CHARS,
            ),
            self._setting_int(
                ("txt_max_chunk_lines", "txt_chunk_lines", "max_chunk_lines"),
                DEFAULT_TXT_CHUNK_LINES,
                MAX_TXT_CHUNK_LINES,
            ),
            self._setting_int(
                ("txt_max_chunks", "max_txt_chunks", "max_chunks"),
                DEFAULT_TXT_MAX_CHUNKS,
                MAX_TXT_MAX_CHUNKS,
            ),
            self._setting_int(
                ("txt_max_input_chars", "txt_max_input_size", "max_input_chars"),
                DEFAULT_TXT_MAX_INPUT_CHARS,
                MAX_TXT_INPUT_CHARS,
            ),
            self._setting_int(
                ("txt_max_input_bytes", "max_input_bytes"),
                DEFAULT_TXT_MAX_INPUT_BYTES,
                MAX_TXT_INPUT_BYTES,
            ),
        )

    def _init_metrics(self, text: str, limits: tuple[int, int, int, int, int]) -> None:
        chunk_chars, chunk_lines, max_chunks, max_input_chars, max_input_bytes = limits
        self.metrics = {
            "chunk_count": 0,
            "chunks_completed": 0,
            "model_calls": 0,
            "chunk_retries": 0,
            "input_characters": len(text),
            "input_bytes": len(text.encode("utf-8")),
            "prompt_characters": 0,
            "output_characters": 0,
            "max_chunk_characters": chunk_chars,
            "max_chunk_lines": chunk_lines,
            "max_chunks": max_chunks,
            "max_input_characters": max_input_chars,
            "max_input_bytes": max_input_bytes,
        }

    def _preflight(self, text: str, limits: tuple[int, int, int, int, int]) -> None:
        _chunk_chars, _chunk_lines, _max_chunks, max_input_chars, max_input_bytes = (
            limits
        )
        input_chars = len(text)
        input_bytes = len(text.encode("utf-8"))
        if input_chars > max_input_chars or input_bytes > max_input_bytes:
            raise TxtParserError(
                "TXT/CSV input is too large for the LLM workflow "
                f"({input_chars:,} characters, {input_bytes:,} UTF-8 bytes; "
                f"limits are {max_input_chars:,} characters/{max_input_bytes:,} bytes). "
                "No model call was made; reduce the file or lower its scope before retrying."
            )

    def _preflight_file_size(self, limits: tuple[int, int, int, int, int]) -> None:
        """Reject oversized files from metadata before decoding the payload."""
        max_input_bytes = limits[4]
        try:
            file_size = Path(self.file_path).stat().st_size
        except OSError:
            # Let the normal read path report the actionable missing/unreadable
            # file error. A stat failure cannot prove that the file is large.
            return
        if file_size > max_input_bytes:
            raise TxtParserError(
                "TXT/CSV input file is too large for the LLM workflow "
                f"({file_size:,} bytes on disk; limit is {max_input_bytes:,} bytes). "
                "No model call was made and the file was not read; reduce the file "
                "or lower its scope before retrying."
            )

    @staticmethod
    def _line_records(text: str) -> list[_SourceLine]:
        raw_lines = text.splitlines()
        if text and not raw_lines:
            raw_lines = [text]
        return [
            _SourceLine(index, line.rstrip("\r\n"))
            for index, line in enumerate(raw_lines, start=1)
        ]

    def _split_long_line(self, line: _SourceLine, max_chars: int) -> list[_SourceLine]:
        if len(line.text) <= max_chars:
            return [line]
        return [
            _SourceLine(line.number, line.text[start : start + max_chars])
            for start in range(0, len(line.text), max_chars)
        ]

    def _units(self, lines: list[_SourceLine]) -> list[list[_SourceLine]]:
        """Emit non-empty paragraphs, retaining blank separator line numbers."""
        units: list[list[_SourceLine]] = []
        current: list[_SourceLine] = []
        pending_blanks: list[_SourceLine] = []
        for line in lines:
            if not line.text.strip():
                if current:
                    current.append(line)
                    units.append(current)
                    current = []
                elif units:
                    units[-1].append(line)
                else:
                    pending_blanks.append(line)
                continue
            if not current and pending_blanks:
                current.extend(pending_blanks)
                pending_blanks = []
            current.append(line)
        if current:
            units.append(current)
        elif pending_blanks and units:
            units[-1].extend(pending_blanks)
        return units

    def _partition(self, text: str, max_chars: int, max_lines: int) -> list[_TxtChunk]:
        lines = self._line_records(text)
        if not any(line.text.strip() for line in lines):
            return []

        chunks: list[_TxtChunk] = []
        current: list[_SourceLine] = []

        def flush() -> None:
            nonlocal current
            meaningful = [line for line in current if line.text.strip()]
            if meaningful:
                chunks.append(
                    _TxtChunk(current[0].number, current[-1].number, tuple(current))
                )
                current = []
            else:
                current = []

        def add_line(line: _SourceLine) -> None:
            nonlocal current
            current_chars = len("\n".join(item.text for item in current))
            if current and (
                len(current) >= max_lines
                or current_chars + 1 + len(line.text) > max_chars
            ):
                flush()
            current.append(line)

        for unit in self._units(lines):
            # A line longer than the character budget is split deterministically;
            # all fragments retain the original global line number.
            expanded: list[_SourceLine] = []
            for line in unit:
                expanded.extend(self._split_long_line(line, max_chars))
            unit_chars = len("\n".join(item.text for item in expanded))
            if current and (
                len(current) + len(expanded) > max_lines
                or len("\n".join(item.text for item in current)) + 1 + unit_chars
                > max_chars
            ):
                flush()
            for line in expanded:
                add_line(line)
        flush()
        return chunks

    @staticmethod
    def _extract_content(response) -> str:
        try:
            choices = (
                response.get("choices")
                if isinstance(response, dict)
                else response.choices
            )
            if not choices:
                return ""
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message") or {}
                content = (
                    message.get("content", "") if isinstance(message, dict) else ""
                )
            else:
                content = first.message.content
        except AttributeError, IndexError, KeyError, TypeError:
            return ""
        if content is None:
            return ""
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content)

    @staticmethod
    def _response_finish_reason(response) -> str:
        try:
            choices = (
                response.get("choices")
                if isinstance(response, dict)
                else response.choices
            )
            if not choices:
                return ""
            first = choices[0]
            if isinstance(first, dict):
                return str(first.get("finish_reason", "") or "")
            return str(getattr(first, "finish_reason", "") or "")
        except AttributeError, IndexError, KeyError, TypeError:
            return ""

    def _record_response_metrics(self, response) -> None:
        usage = (
            response.get("usage")
            if isinstance(response, dict)
            else getattr(response, "usage", None)
        )
        if usage is None:
            return
        for source_name, metric_name in (
            ("prompt_tokens", "prompt_tokens"),
            ("completion_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            value = (
                usage.get(source_name)
                if isinstance(usage, dict)
                else getattr(usage, source_name, 0)
            )
            try:
                self.metrics[metric_name] = self.metrics.get(metric_name, 0) + int(
                    value or 0
                )
            except TypeError, ValueError:
                continue

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
        message = str(exc).casefold()
        return (
            isinstance(exc, (TimeoutError, ConnectionError))
            or status_code in {408, 409, 425, 429}
            or (isinstance(status_code, int) and status_code >= 500)
            or any(
                token in message
                for token in (
                    "timeout",
                    "timed out",
                    "temporar",
                    "rate limit",
                    "connection",
                    "overloaded",
                    "unavailable",
                )
            )
        )

    @staticmethod
    def _normalize_finish_reason(finish_reason: object) -> str:
        return re.sub(
            r"[^a-z0-9]+", "_", str(finish_reason or "").strip().casefold()
        ).strip("_")

    @classmethod
    def _terminal_finish_reason(cls, finish_reason: object) -> str | None:
        normalized = cls._normalize_finish_reason(finish_reason)
        if normalized and any(
            token in normalized for token in _TERMINAL_FINISH_REASON_TOKENS
        ):
            return normalized
        return None

    @classmethod
    def _reason_category(cls, exc: Exception) -> str:
        category_match = re.search(r"reason_category=([a-z0-9_]+)", str(exc))
        if category_match:
            return category_match.group(1)
        if isinstance(exc, _UnsafeChunkError):
            return "unsafe_model_output"
        if isinstance(exc, _InvalidChunkError):
            return "invalid_model_output"
        if isinstance(exc, _TerminalModelError):
            return f"terminal_finish_reason_{exc.finish_reason}"
        if isinstance(exc, (TimeoutError, ConnectionError)) or cls._retryable(exc):
            status_code = getattr(exc, "status_code", None)
            if isinstance(status_code, int) and status_code >= 400:
                return f"provider_http_{status_code}"
            return "transient_provider_failure"
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int) and status_code >= 400:
            return f"provider_http_{status_code}"
        return "permanent_provider_failure"

    def _render_prompt(
        self,
        template: str,
        chunk: _TxtChunk,
        chunk_index: int,
        chunk_count: int,
        prior_context: str,
    ) -> str:
        # Deliberately use named replacement rather than str.format: custom
        # prompts often contain literal JSON/CSS braces that must survive.
        replacements = {
            "numbered_lines": chunk.numbered_text,
            "prior_structure_context": (
                "[PRIOR_STRUCTURE_CONTEXT_BEGIN]\n"
                "Context only. Do not repeat, translate, or output this context. "
                "It describes the structure immediately before the current source chunk.\n"
                f"{prior_context}\n"
                "[PRIOR_STRUCTURE_CONTEXT_END]"
                if prior_context
                else ""
            ),
            "chunk_index": str(chunk_index),
            "chunk_count": str(chunk_count),
        }
        prompt = str(template)
        for key, value in replacements.items():
            prompt = prompt.replace("{" + key + "}", value)
        if prior_context and "{prior_structure_context}" not in str(template):
            prompt += "\n\n" + replacements["prior_structure_context"]
        return prompt

    @staticmethod
    def _structure_context(html: str) -> str:
        """Summarize tags only, avoiding prior source text in later prompts."""
        soup = BeautifulSoup(html, "html.parser")
        tags = [tag.name.lower() for tag in soup.find_all(True)]
        if not tags:
            return (
                "Previous chunk had a text-only fragment; continue with semantic HTML."
            )
        counts: dict[str, int] = {}
        for tag_name in tags:
            counts[tag_name] = counts.get(tag_name, 0) + 1
        ordered = ", ".join(f"<{name}> x{counts[name]}" for name in sorted(counts))
        heading_levels = [
            tag.name.lower() for tag in soup.find_all(re.compile(r"^h[1-6]$"))
        ]
        heading_summary = ", ".join(heading_levels[:12]) or "none"
        context = f"Element inventory: {ordered}. Heading sequence: {heading_summary}."
        return context[:MAX_PRIOR_STRUCTURE_CONTEXT_CHARS]

    @staticmethod
    def _normalize_chunk_html(content: str) -> str:
        cleaned = str(content or "").strip()
        if not cleaned:
            raise _InvalidChunkError("Model returned an empty TXT/CSV chunk.")
        fenced = re.fullmatch(
            r"```(?:html|xml)?\s*\n([\s\S]*?)\n```\s*", cleaned, flags=re.IGNORECASE
        )
        if fenced:
            cleaned = fenced.group(1).strip()
        elif cleaned.startswith("```") or cleaned.endswith("```"):
            raise _InvalidChunkError(
                "Model returned an unterminated markdown fence instead of HTML."
            )

        soup = BeautifulSoup(cleaned, "html.parser")
        for tag in soup.find_all(True):
            tag_name = str(tag.name or "").casefold()
            if tag_name not in _ALLOWED_TAGS:
                raise _UnsafeChunkError(
                    f"Model returned unsupported or active HTML tag <{tag_name}>."
                )
            allowed_attributes = _GLOBAL_ATTRIBUTES | _TAG_ATTRIBUTES.get(
                tag_name, frozenset()
            )
            for attr_name, attr_value in tag.attrs.items():
                lowered = str(attr_name).casefold()
                if lowered not in allowed_attributes:
                    raise _UnsafeChunkError(
                        f"Model returned unsupported HTML attribute on <{tag_name}>."
                    )
                value = (
                    " ".join(str(item) for item in attr_value)
                    if isinstance(attr_value, list)
                    else str(attr_value or "")
                )
                if len(value) > _MAX_ATTRIBUTE_VALUE_CHARS:
                    raise _UnsafeChunkError(
                        "Model returned an oversized HTML attribute."
                    )
                if any(
                    ord(character) < 0x20 or ord(character) == 0x7F
                    for character in value
                ):
                    raise _UnsafeChunkError(
                        "Model returned an HTML attribute containing control characters."
                    )
                if lowered in {"lang", "xml:lang"} and not _SAFE_LANG_RE.fullmatch(
                    value.strip()
                ):
                    raise _UnsafeChunkError(
                        "Model returned an invalid language attribute."
                    )
                if lowered not in {"href", "src", "cite"}:
                    continue
                value_lowered = value.strip().casefold()
                if value_lowered.startswith(
                    _UNSAFE_URI_SCHEMES
                ) or value_lowered.startswith("file:"):
                    raise _UnsafeChunkError("Model returned an unsafe HTML URI.")

        if not soup.get_text(strip=True):
            raise _InvalidChunkError("Model returned an empty TXT/CSV HTML chunk.")
        if not soup.find(True):
            # A plain-text response is still a valid, safe fallback fragment,
            # but escape it before placing it in the HTML contract.
            return f"<p>{escape(soup.get_text())}</p>"

        root = soup.body if soup.body is not None else soup
        fragment = BeautifulSoup(
            "".join(str(child) for child in root.contents), "html.parser"
        )
        for wrapper in list(fragment.find_all(_WRAPPER_TAGS)):
            wrapper.unwrap()
        normalized = str(fragment).strip()
        if not normalized:
            raise _InvalidChunkError("Model returned an empty TXT/CSV HTML fragment.")
        return normalized

    def _call_chunk(self, prompt: str, model: str | None, chunk_index: int) -> str:
        max_retries = self._setting_int(
            ("txt_max_retries", "max_retries"), DEFAULT_TXT_MAX_RETRIES, 6
        )
        try:
            backoff = float(
                next(
                    (
                        self.settings[key]
                        for key in (
                            "txt_retry_backoff_seconds",
                            "retry_backoff_seconds",
                        )
                        if key in self.settings
                    ),
                    DEFAULT_TXT_RETRY_BACKOFF_SECONDS,
                )
            )
        except TypeError, ValueError:
            backoff = DEFAULT_TXT_RETRY_BACKOFF_SECONDS
        backoff = max(0.0, min(backoff, MAX_TXT_RETRY_BACKOFF_SECONDS))

        for attempt in range(max_retries):
            try:
                self.metrics["model_calls"] += 1
                response = self.llm_service.completion(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    source="doc_processor.txt_parser",
                    reasoning_addon_id="doc_processor",
                    reasoning_slot="parsing",
                    thinking_config=thinking_config_for_call(
                        self.settings.get(REASONING_SNAPSHOT_KEY),
                        "parsing",
                        model,
                    ),
                )
                self._record_response_metrics(response)
                finish_reason = self._response_finish_reason(response)
                terminal_reason = self._terminal_finish_reason(finish_reason)
                if terminal_reason:
                    raise _TerminalModelError(terminal_reason)
                content = self._extract_content(response)
                if "content_filter" in content.casefold():
                    raise _UnsafeChunkError(
                        f"Model blocked TXT/CSV chunk {chunk_index} with a safety filter."
                    )
                return self._normalize_chunk_html(content)
            except _UnsafeChunkError, _InvalidChunkError, _TerminalModelError:
                raise
            except Exception as exc:
                if not self._retryable(exc):
                    raise TxtParserError(
                        "TXT/CSV structuring failed for chunk "
                        f"{chunk_index}; reason_category={self._reason_category(exc)}"
                    ) from exc
                if attempt >= max_retries - 1:
                    raise TxtParserError(
                        "TXT/CSV structuring failed for chunk "
                        f"{chunk_index} after {max_retries} attempts; "
                        f"reason_category={self._reason_category(exc)}"
                    ) from exc
                self.metrics["chunk_retries"] += 1
                logger.warning(
                    "Transient TXT/CSV structuring error on chunk %s attempt %s/%s; reason_category=%s",
                    chunk_index,
                    attempt + 1,
                    max_retries,
                    self._reason_category(exc),
                )
                if backoff:
                    time.sleep(backoff * (attempt + 1))
        raise TxtParserError(f"TXT/CSV structuring failed for chunk {chunk_index}.")

    def process(self):
        limits = self._limits()
        if self.llm_service:
            self._preflight_file_size(limits)
        text = self._read_text()
        if not self.llm_service or not text.strip():
            return "COMPLETED", self._fallback_html(text)

        self._init_metrics(text, limits)
        self._preflight(text, limits)
        max_chars, max_lines, max_chunks, _max_input_chars, _max_input_bytes = limits
        chunks = self._partition(text, max_chars, max_lines)
        self.metrics["chunk_count"] = len(chunks)
        if len(chunks) > max_chunks:
            raise TxtParserError(
                "TXT/CSV input requires "
                f"{len(chunks):,} model chunks, exceeding the configured maximum of {max_chunks:,}. "
                "No model call was made; increase the chunk budgets or reduce the file before retrying."
            )
        if not chunks:
            return "COMPLETED", self._fallback_html(text)

        template = (
            self._get_setting("prompt_txt_structuring", "{numbered_lines}")
            or "{numbered_lines}"
        )
        model = self.settings.get("parsing_model")
        self._record_model("structuring", model)
        fragments: list[str] = []
        prior_context = ""
        for index, chunk in enumerate(chunks, start=1):
            prompt = self._render_prompt(
                str(template), chunk, index, len(chunks), prior_context
            )
            self.metrics["prompt_characters"] += len(prompt)
            try:
                fragment = self._call_chunk(prompt, model, index)
            except Exception as exc:
                self.metrics["failed_chunk"] = index
                self.metrics["failed_chunk_start_line"] = chunk.start_line
                # Never return fragments from a failed book. The worker will
                # mark the document failed and the caller can retry explicitly.
                raise TxtParserError(
                    "TXT/CSV structuring failed at stage=structuring "
                    f"chunk={index}/{len(chunks)} "
                    f"(source lines {chunk.start_line}-{chunk.end_line}); "
                    f"reason_category={self._reason_category(exc)}; "
                    "no partial document was returned."
                ) from exc
            fragments.append(fragment)
            self.metrics["chunks_completed"] = index
            self.metrics["output_characters"] += len(fragment)
            prior_context = self._structure_context(fragment)
        return "COMPLETED", "\n".join(fragments)
