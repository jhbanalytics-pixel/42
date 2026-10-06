"""A stand-in for the reviewed PDF renderer, for tests that cannot run the pinned browser.

The pinned runtime needs the packaged browser and font assets of the final
image, which a unit test host does not have. This double keeps the parts of
its contract a route can observe: it reads the same HTML the renderer would,
takes the page text the renderer's own input audit takes, and returns a real
PDF whose extractable text is that page text. It also writes the metadata a
headless browser writes, a Producer and a Creator naming the tool, so a test
can prove the export path removes them rather than never meeting them.
"""

from __future__ import annotations

from src.api import pdf_exporter

BROWSER_PRODUCER = "Skia/PDF m141"
BROWSER_CREATOR = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/141.0.0.0 Safari/537.36"
)
TOOL_NAMES = ("skia", "chrome", "chromium", "mozilla", "webkit", "playwright", "pypdf")


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def synthetic_pdf(
    lines, *, producer=BROWSER_PRODUCER, creator=BROWSER_CREATOR, title="Client Read"
) -> bytes:
    """One page of Helvetica text, one line per item, with a browser's Info dictionary."""
    content = "BT /F1 9 Tf 11 TL 36 806 Td " + " ".join(
        f"({_escape(line)}) Tj T*" for line in lines
    ) + " ET"
    stream = content.encode("latin-1")
    info = []
    for key, value in (("Producer", producer), ("Creator", creator), ("Title", title)):
        if value is not None:
            info.append(f"/{key} ({_escape(value)})")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        ("<< " + " ".join(info) + " >>").encode("latin-1"),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def page_text(html_bytes: bytes) -> list[str]:
    """The text runs the reviewed renderer's input audit reads from the page."""
    audit = pdf_exporter._HtmlAudit()
    audit.feed(html_bytes.decode("utf-8"))
    audit.close()
    return list(audit.text)


class FakeRenderer:
    """Records every call and renders the page text, or refuses as the renderer would."""

    def __init__(self):
        self.calls = []
        self.refusal = None

    def __call__(self, html_bytes, *, asset_root):
        self.calls.append((html_bytes, asset_root))
        if self.refusal is not None:
            raise pdf_exporter.PdfExportError(self.refusal)
        return synthetic_pdf(page_text(html_bytes))


# Stands in for the approved Newsreader face, which a unit test host does not
# carry at the renderer's asset root. The export embeds whatever this returns.
FAKE_FONT = b"wOF2 stand-in face for unit tests"


def install_font(monkeypatch) -> bytes:
    from src.api import dossier_artifacts

    monkeypatch.setattr(dossier_artifacts, "_export_font_bytes", lambda: FAKE_FONT)
    return FAKE_FONT


def install(monkeypatch) -> FakeRenderer:
    renderer = FakeRenderer()
    monkeypatch.setattr(pdf_exporter, "render_stored_html_pdf", renderer)
    install_font(monkeypatch)
    return renderer
