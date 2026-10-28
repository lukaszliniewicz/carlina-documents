from __future__ import annotations

from email.utils import parseaddr
from html import escape


class MsgParser:
    def __init__(self, file_path: str, user_id: int | None, llm_service, settings: dict | None = None):
        self.file_path = file_path
        self.user_id = user_id
        self.llm_service = llm_service
        self.settings = settings or {}
        self.metrics = {}

    def process(self):
        try:
            import extract_msg
        except ImportError as exc:
            raise RuntimeError("The 'extract_msg' package is required to parse Outlook MSG files.") from exc

        msg = extract_msg.openMsg(str(self.file_path))
        try:
            subject = msg.subject or "Message"
            sender_name, sender_addr = parseaddr(msg.sender or "")
            sender = sender_name or sender_addr or msg.sender or ""
            sent = msg.date or ""
            body = msg.htmlBody
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            if not body:
                plain = msg.body or ""
                body = "<br>".join(escape(line) for line in str(plain).splitlines())

            header = [
                f"<h1>{escape(subject)}</h1>",
                "<dl>",
                f"<dt>From</dt><dd>{escape(sender)}</dd>" if sender else "",
                f"<dt>Date</dt><dd>{escape(str(sent))}</dd>" if sent else "",
                "</dl>",
            ]
            return "COMPLETED", "\n".join(part for part in header if part) + str(body)
        finally:
            try:
                msg.close()
            except Exception:
                pass
