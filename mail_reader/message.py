"""Fetch + sanitize a single message for rendering in the UI.

HTML mail goes through `nh3` with a restrictive allowlist. Remote images
are stripped entirely in v1 — no `<img>` in the allowlist, so tracking
pixels and external resources cannot load.

Plain-text bodies are escaped and wrapped in `<p>` / `<br>` so they look
like normal prose rather than monospace dumps.
"""
from __future__ import annotations

import email
import html as _html
import re
import subprocess
from email.policy import default as email_default
from typing import TypedDict

import nh3


class Attachment(TypedDict):
    filename: str
    mime: str
    size: int


class Message(TypedDict):
    message_id: str
    date: str
    from_addr: str
    to: str
    subject: str
    body_html: str
    body_is_plain: bool
    attachments: list[Attachment]


_ALLOWED_TAGS: set[str] = {
    "a", "p", "br", "div", "span", "strong", "b", "em", "i", "u", "s",
    "ul", "ol", "li", "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "td", "th",
    "hr",
    # img intentionally omitted — block remote tracking pixels in v1.
}
_ALLOWED_ATTRS: dict[str, set[str]] = {
    "a": {"href", "title"},
}


def _plain_to_html(text: str) -> str:
    """Escape text, turn blank lines into paragraphs, single newlines into <br>."""
    escaped = _html.escape(text)
    paragraphs = re.split(r"\n\s*\n", escaped.strip())
    return "\n".join(
        f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs if p
    )


def fetch_message(notmuch_msg_id: str) -> Message:
    raw = subprocess.run(
        ["notmuch", "show", "--format=raw", f"id:{notmuch_msg_id}"],
        check=True, capture_output=True,
    ).stdout
    msg = email.message_from_bytes(raw, policy=email_default)

    body = msg.get_body(preferencelist=("plain", "html"))
    is_plain = body is not None and body.get_content_type() != "text/html"
    if body is None:
        body_html = "<p><em>(no readable body)</em></p>"
    elif is_plain:
        body_html = _plain_to_html(body.get_content())
    else:
        body_html = nh3.clean(
            body.get_content(),
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRS,
            link_rel="noopener noreferrer",
        )

    attachments: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        disp = (part.get_content_disposition() or "").lower()
        if not fn and disp != "attachment":
            continue
        payload = part.get_payload(decode=True)
        size = len(payload) if isinstance(payload, bytes) else 0
        attachments.append({
            "filename": fn or "(unnamed)",
            "mime": part.get_content_type(),
            "size": size,
        })

    return {
        "message_id": notmuch_msg_id,
        "date": msg.get("Date", ""),
        "from_addr": msg.get("From", ""),
        "to": msg.get("To", ""),
        "subject": msg.get("Subject", ""),
        "body_html": body_html,
        "body_is_plain": is_plain,
        "attachments": attachments,
    }
