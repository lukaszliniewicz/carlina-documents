import html
from html.parser import HTMLParser
from urllib.parse import urlparse


ALLOWED_HTML_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
ALLOWED_GLOBAL_ATTRIBUTES = {"class", "title", "role", "aria-label", "aria-hidden"}
ALLOWED_TAG_ATTRIBUTES = {
    "a": {"href", "target", "rel"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
ALLOWED_URI_SCHEMES = {"http", "https", "mailto"}
ALLOWED_LINK_TARGETS = {"_blank", "_self"}
VOID_TAGS = {"br"}
STRIP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript"}


def _is_safe_uri(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate:
        return False
    lowered = candidate.lower()
    if lowered.startswith(("javascript:", "data:", "vbscript:")):
        return False
    parsed = urlparse(candidate)
    if parsed.scheme:
        return parsed.scheme.lower() in ALLOWED_URI_SCHEMES
    return True


class _AllowlistSanitizerParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._open_tags: list[str] = []
        self._strip_depth = 0

    def sanitized_html(self) -> str:
        # Close still-open tags for a stable fragment.
        while self._open_tags:
            tag = self._open_tags.pop()
            self._parts.append(f"</{tag}>")
        return "".join(self._parts)

    def handle_starttag(self, tag: str, attrs):
        normalized_tag = (tag or "").lower()
        if normalized_tag in STRIP_CONTENT_TAGS:
            self._strip_depth += 1
            return
        if self._strip_depth > 0:
            return
        if normalized_tag not in ALLOWED_HTML_TAGS:
            return

        safe_attrs = self._sanitize_attrs(normalized_tag, attrs or [])
        attr_segment = "".join(
            f' {name}="{html.escape(value, quote=True)}"' for name, value in safe_attrs
        )
        self._parts.append(f"<{normalized_tag}{attr_segment}>")
        if normalized_tag not in VOID_TAGS:
            self._open_tags.append(normalized_tag)

    def handle_endtag(self, tag: str):
        normalized_tag = (tag or "").lower()
        if normalized_tag in STRIP_CONTENT_TAGS:
            if self._strip_depth > 0:
                self._strip_depth -= 1
            return
        if self._strip_depth > 0:
            return
        if normalized_tag not in ALLOWED_HTML_TAGS or normalized_tag in VOID_TAGS:
            return

        if normalized_tag in self._open_tags:
            while self._open_tags:
                opened = self._open_tags.pop()
                self._parts.append(f"</{opened}>")
                if opened == normalized_tag:
                    break

    def handle_startendtag(self, tag: str, attrs):
        self.handle_starttag(tag, attrs)
        normalized_tag = (tag or "").lower()
        if normalized_tag in ALLOWED_HTML_TAGS and normalized_tag not in VOID_TAGS:
            if self._open_tags and self._open_tags[-1] == normalized_tag:
                self._open_tags.pop()
                self._parts.append(f"</{normalized_tag}>")

    def handle_data(self, data: str):
        if self._strip_depth > 0:
            return
        self._parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str):
        if self._strip_depth > 0:
            return
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str):
        if self._strip_depth > 0:
            return
        self._parts.append(f"&#{name};")

    def _sanitize_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
        allowed_specific = ALLOWED_TAG_ATTRIBUTES.get(tag, set())
        safe_attrs: list[tuple[str, str]] = []
        for raw_name, raw_value in attrs:
            name = (raw_name or "").lower()
            if not name or name.startswith("on"):
                continue
            if name == "style":
                continue
            if name not in ALLOWED_GLOBAL_ATTRIBUTES and name not in allowed_specific:
                continue
            value = (raw_value or "").strip()
            if not value:
                continue

            if name == "href":
                if not _is_safe_uri(value):
                    continue
            elif name == "target":
                value = value.lower()
                if value not in ALLOWED_LINK_TARGETS:
                    continue
            elif name == "rel":
                value = " ".join(part for part in value.split() if part)
                if not value:
                    continue
            elif name in {"colspan", "rowspan"} and not value.isdigit():
                continue

            safe_attrs.append((name, value))

        if tag == "a":
            attrs_dict = dict(safe_attrs)
            if attrs_dict.get("target") == "_blank":
                rel_tokens = set((attrs_dict.get("rel") or "").split())
                rel_tokens.update({"noopener", "noreferrer"})
                rel_value = " ".join(sorted(token for token in rel_tokens if token))
                safe_attrs = [(name, value) for name, value in safe_attrs if name != "rel"]
                safe_attrs.append(("rel", rel_value))

        return safe_attrs


def sanitize_html_fragment(value: str | None) -> str:
    if value is None:
        return ""
    parser = _AllowlistSanitizerParser()
    parser.feed(str(value))
    parser.close()
    return parser.sanitized_html()
