"""Render one export page to PDF in memory with headless Chromium (contract.md section 13.2, Export).

Lifted from app/src/api/pdf_exporter.py and pdf_renderer_worker.py: one render
at a time, the HTML audit that refuses scripts, frames, forms, handlers and any
resource outside the page, a browser with JavaScript off, offline and with every
request aborted, a 15 second ceiling, size limits, and the output read back with
pypdf before it is returned. Dropped: the asset copying (the export page carries
its own style and no fonts or images), the /proc process-tree and seccomp
monitor, and the worker subprocess, because nothing is written to disk here:
the bytes go straight back to the caller and are never stored.

F42_CHROMIUM names a Chromium or headless-shell executable; unset, Playwright's
own build is used (the image installs it).
"""

from __future__ import annotations

import io
import os
import threading
from html.parser import HTMLParser
from urllib.parse import urlsplit

MAX_INPUT_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 25 * 1024 * 1024
TIMEOUT_MS = 15_000
_PERMIT = threading.Lock()
_BLOCKED_TAGS = {"base", "embed", "form", "frame", "iframe", "link", "object", "script", "img", "video",
                 "audio", "source", "svg", "input", "button"}
_CSS_BLOCKED = ("url(", "@import", "expression(", "behavior", "-moz-binding", "image-set(")


class PdfError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _Audit(HTMLParser):
    """Refuses anything that could reach outside the page; keeps the visible text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.in_style = self.in_title = False

    def handle_starttag(self, tag, attrs):
        values = {str(key).lower(): value or "" for key, value in attrs}
        if tag in _BLOCKED_TAGS or any(key.startswith("on") for key in values):
            raise PdfError("pdf_html_unsafe")
        if tag == "meta" and values.get("http-equiv", "").lower() == "refresh":
            raise PdfError("pdf_html_unsafe")
        for key in ("src", "srcset", "poster", "background", "action", "formaction", "data", "xlink:href"):
            if key in values:
                raise PdfError("pdf_html_unsafe")
        if "href" in values:
            href = values["href"].strip()
            parts = urlsplit(href)
            if tag != "a" or not (href.startswith("#") or (parts.scheme in ("http", "https") and parts.netloc)):
                raise PdfError("pdf_html_unsafe")
        if "style" in values:
            self.css(values["style"])
        self.in_style = tag == "style"
        self.in_title = tag == "title"

    def handle_endtag(self, tag):
        self.in_style = self.in_title = False

    def handle_data(self, data):
        if self.in_style:
            self.css(data)
        elif data.split() and not self.in_title:
            self.text.append(" ".join(data.split()))

    @staticmethod
    def css(value):
        lowered = value.lower().replace(" ", "")
        if any(item in lowered for item in _CSS_BLOCKED):
            raise PdfError("pdf_html_unsafe")


def _render(html):
    from playwright.sync_api import Error, sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=os.environ.get("F42_CHROMIUM") or None, timeout=TIMEOUT_MS)
            try:
                context = browser.new_context(java_script_enabled=False, service_workers="block",
                                              accept_downloads=False, offline=True)
                page = context.new_page()
                page.set_default_timeout(TIMEOUT_MS)
                blocked = []

                def refuse(route):
                    blocked.append(route.request.url)
                    route.abort()

                page.route("**/*", refuse)
                page.set_content(html, wait_until="load", timeout=TIMEOUT_MS)
                if blocked:
                    raise PdfError("pdf_network_blocked")
                return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
            finally:
                browser.close()
    except Error:
        raise PdfError("pdf_browser_failed") from None


def _check(data, expected):
    from pypdf import PdfReader

    if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
        raise PdfError("pdf_output_invalid")
    if len(data) > MAX_OUTPUT_BYTES:
        raise PdfError("pdf_output_too_large")
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or not reader.pages:
            raise ValueError()
        text = "".join("".join((page.extract_text() or "").split()) for page in reader.pages)
    except Exception:
        raise PdfError("pdf_output_invalid") from None
    if expected not in text:
        raise PdfError("pdf_text_missing")


def render_pdf(html: str) -> bytes:
    """The PDF of one export page, rendered in memory, or PdfError with a reason code."""
    if not _PERMIT.acquire(blocking=False):
        raise PdfError("pdf_render_busy")
    try:
        if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_INPUT_BYTES:
            raise PdfError("pdf_input_invalid")
        audit = _Audit()
        audit.feed(html)
        audit.close()
        if not audit.text:
            raise PdfError("pdf_text_missing")
        data = _render(html)
        # The first lines of the page must come back out of the PDF; whitespace is where layout differs.
        _check(data, "".join(" ".join(audit.text[:3]).split()))
        return data
    finally:
        _PERMIT.release()
