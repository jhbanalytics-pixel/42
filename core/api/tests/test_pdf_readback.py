"""The PDF readback (core/api/pdf.py) holds the whole page, not only its header (audit W21-04). These tests write
small PDFs with pypdf and stand them in for the browser, so they run where no Chromium is installed."""
import io

import pytest

from core.api import pdf

PAGE = ("<!doctype html><html><head><title>T</title></head><body>"
        "<header><p>42 Ogilvy Intelligence</p><h1>Amapiano in Lagos</h1><p>Question: Why now?</p></header>"
        "<main><section><h2>What we found</h2><p>The ﬁrst claim holds \U0001F525 for now.</p></section>"
        "<section><h2>Sources</h2><article id=\"source-1\"><h3>[1] TikTok · @fixture_ng_1</h3>"
        "<blockquote>Ẹ ku àárọ̀ final-source-marker-7431</blockquote></article></section></main></body></html>")


def pdf_of(lines):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(595, 842)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    ops = "".join(f"BT /F1 12 Tf 72 {800 - 20 * i} Td ({text}) Tj ET\n" for i, text in enumerate(lines))
    stream = DecodedStreamObject()
    stream.set_data(ops.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


HEADER = ["42 Ogilvy Intelligence", "Amapiano in Lagos", "Question: Why now?"]
# What a browser writes: the ligature spelt out, the emoji gone, the Yoruba marks dropped by the font, the heading's
# words broken over two lines.
WHOLE = HEADER + ["What we found", "The first claim holds for now.", "Sources",
                  "[1] TikTok @fixture_ng_1", "E ku aaro final-source-", "marker-7431"]


def test_a_pdf_holding_only_the_header_is_refused(monkeypatch):
    monkeypatch.setattr(pdf, "_render", lambda html: pdf_of(HEADER))
    with pytest.raises(pdf.PdfError) as raised:
        pdf.render_pdf(PAGE)
    assert raised.value.reason == "pdf_text_missing"


def test_a_pdf_missing_the_last_source_is_refused(monkeypatch):
    monkeypatch.setattr(pdf, "_render", lambda html: pdf_of(WHOLE[:-2]))
    with pytest.raises(pdf.PdfError) as raised:
        pdf.render_pdf(PAGE)
    assert raised.value.reason == "pdf_text_missing"


def test_a_pdf_holding_every_line_of_the_page_is_returned(monkeypatch):
    data = pdf_of(WHOLE)
    monkeypatch.setattr(pdf, "_render", lambda html: data)
    assert pdf.render_pdf(PAGE) == data
