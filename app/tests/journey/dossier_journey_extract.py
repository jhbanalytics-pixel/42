"""What a reader extracts from the downloaded exports, for the browser journey to compare.

Run from app/ with the app's interpreter:

    python -m tests.journey.dossier_journey_extract HTML PDF ARTIFACT_READ_JSON QUESTION

The artifact read JSON is the approved read the page itself received, and
the question is the one the investigation was framed with. The
output is one JSON object: the lines the Client Read payload allows, the text
runs of the HTML as the renderer's own input audit reads them, the PDF page
text through the app's pypdf reader, the page count, and every metadata value
either file carries.
"""

from __future__ import annotations

import io
import json
import sys
from html.parser import HTMLParser
from pathlib import Path

from pypdf import PdfReader

from src.api import dossier_artifacts
from tests.unit import dossier_pdf_fakes


def allowed_lines(client_read: dict, title: str) -> list[str]:
    """The same allowed lines the handler journey test derives from the payload."""
    from tests.unit.test_main_dossier_journey_routes import allowed_lines as derive

    return derive(client_read, title)


class _Metadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = []
        self.title = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            self.meta.append(dict(attrs))
        self._in_title = tag == "title"

    def handle_endtag(self, tag):
        self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)


def extract(html_bytes: bytes, pdf_bytes: bytes, artifact: dict, title: str) -> dict:
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=True)
    info = {str(key): str(value) for key, value in (reader.metadata or {}).items()}
    metadata = _Metadata()
    metadata.feed(html_bytes.decode("utf-8"))
    metadata.close()
    return {
        "allowed": allowed_lines(artifact["client_read"], title),
        "html_runs": dossier_pdf_fakes.page_text(html_bytes),
        "pdf_text": dossier_artifacts.pdf_text(pdf_bytes),
        "pdf_pages": len(reader.pages),
        "pdf_info": info,
        "pdf_xmp": "/Metadata" in reader.trailer["/Root"],
        "html_meta": metadata.meta,
        "html_title": "".join(metadata.title),
        "tool_names": list(dossier_pdf_fakes.TOOL_NAMES),
    }


def main() -> None:
    html_path, pdf_path, artifact_path, title = sys.argv[1:5]
    artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    result = extract(
        Path(html_path).read_bytes(), Path(pdf_path).read_bytes(), artifact, title
    )
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
