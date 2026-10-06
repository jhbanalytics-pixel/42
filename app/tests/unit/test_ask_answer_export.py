"""Export of a completed Ask answer, with its cited sources, as HTML and PDF.

The export is rebuilt on the server from the stored engine reply the question
worker reads back for the scope, never from anything the browser sends, and
the PDF goes through the reviewed renderer (pdf_exporter) unchanged.
"""

import copy
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src.api import ask_export, general_question_routes, main, pdf_exporter
from src.api.question_worker_process import WorkerProcessError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REPLY = json.loads(
    (FIXTURES / "general-question-stored-reply-partial.json").read_text(encoding="utf-8")
)
RID = REPLY["request_id"]
HTML_ROUTE = f"/api/chat/answer/{RID}/export.html"
PDF_ROUTE = f"/api/chat/answer/{RID}/export.pdf"
QUESTION = (
    "What were people in South Africa saying about amapiano and nightlife "
    "between 25 August and 7 September 2026?"
)
client = TestClient(main.app)


def stored_detail(reply=None, **changes):
    intelligence = copy.deepcopy(REPLY if reply is None else reply)
    detail = {
        "contract_version": "general_question_detail_v1",
        "request_id": intelligence["request_id"],
        "observed_state": intelligence["status"],
        "question": QUESTION,
        "history": [],
        "selected_market": "za",
        "requested_window": {"start": "2026-08-25", "end": "2026-09-07"},
        "response": {
            "answer": "Stored answer text.",
            "sources": [],
            "intelligence": intelligence,
        },
        "reserved_microusd": 100000,
        "missing_work": [],
    }
    detail.update(changes)
    return detail


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


def observe(monkeypatch, detail=None, *, error=None):
    calls = []

    async def read(request, request_id):
        calls.append(request_id)
        if error is not None:
            raise error
        return copy.deepcopy(stored_detail() if detail is None else detail)

    monkeypatch.setattr(general_question_routes, "read_question_observation", read)
    return calls


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.tags, self.links = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs).get("href"))

    def handle_data(self, data):
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


def squash(value):
    return " ".join(value.split())


def text_of(html):
    parser = Text()
    parser.feed(html)
    return " ".join(parser.parts), parser


def test_html_export_carries_the_answer_and_every_cited_source(monkeypatch):
    calls = observe(monkeypatch)
    response = client.get(HTML_ROUTE)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-disposition"] == (
        'attachment; filename="42-answer-60a5fd02.html"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert calls == [RID]
    text, parser = text_of(response.text)
    assert QUESTION in text
    assert "Partial response" in text
    assert "Review required before downstream use." in text
    for claim in REPLY["claims"]:
        if claim["claim_id"] != "c8":
            assert squash(claim["text"]) in text
    for receipt in REPLY["receipts"]:
        assert receipt["citation_label"] + " · " in text
        assert squash(receipt["excerpt"]) in text
    assert text.count("Published: not recorded by the source") == 23
    assert text.count("Source family: Not available in the released data") == 24
    assert text.count("Collected: 7 Sept 2026") == 24
    assert text.count("not recorded by the source") == 23
    assert "Unavailable" not in text
    assert "script" not in parser.tags
    assert all(re.match(r"https://", link) for link in parser.links)


def test_html_export_renders_the_known_record_count_in_words(monkeypatch):
    observe(monkeypatch)
    text, _ = text_of(client.get(HTML_ROUTE).text)
    assert "selected_receipt_count" not in text
    assert "Selected source records: 24 records (2026-08-25 to 2026-09-07)." in text


def test_html_export_shows_a_recorded_publication_date_and_family(monkeypatch):
    reply = copy.deepcopy(REPLY)
    reply["receipts"][0]["published_at"] = "2026-09-03T10:00:00Z"
    reply["receipts"][0]["source_family"] = "video"
    observe(monkeypatch, stored_detail(reply))
    text, _ = text_of(client.get(HTML_ROUTE).text)
    assert "Published: 3 Sept 2026" in text
    assert "Source family: video" in text
    assert text.count("Source family: Not available in the released data") == 23


# Ask redesign, 23 Sept 2026: the in app view writes a window through
# readableWindow, naming the year once when both ends share it ("25 Aug to
# 7 Sept 2026"), so the export writes the answer's window and each
# measurement's window the same way instead of as two full dates.
def test_html_export_writes_each_window_as_the_in_app_view_does(monkeypatch):
    observe(monkeypatch)
    text, _ = text_of(client.get(HTML_ROUTE).text)
    assert "South Africa · 25 Aug to 7 Sept 2026 · Closed observation window" in text
    assert "Selected source records · 25 Aug to 7 Sept 2026 · Closed window" in text
    assert "Aug 2026" not in text


def test_every_quoted_span_is_the_digest_bound_slice(monkeypatch):
    observe(monkeypatch)
    text, _ = text_of(client.get(HTML_ROUTE).text)
    for receipt in REPLY["receipts"]:
        for binding in receipt["quote_bindings"]:
            span = "".join(list(receipt["excerpt"])[binding["start"] : binding["end"]])
            assert hashlib.sha256(span.encode()).hexdigest() == binding["quote_sha256"]
            assert squash(f"Cited span: “{span}”") in text


def test_a_forged_quote_digest_withholds_the_export(monkeypatch):
    reply = copy.deepcopy(REPLY)
    reply["receipts"][0]["quote_bindings"][0]["quote_sha256"] = "0" * 64
    observe(monkeypatch, stored_detail(reply))
    response = client.get(HTML_ROUTE)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "answer_invalid"


def test_a_moved_span_under_the_same_digest_withholds_the_export(monkeypatch):
    reply = copy.deepcopy(REPLY)
    reply["receipts"][0]["quote_bindings"][0]["start"] = 1
    observe(monkeypatch, stored_detail(reply))
    assert client.get(HTML_ROUTE).json()["detail"]["code"] == "answer_invalid"


def test_markup_in_stored_text_is_escaped(monkeypatch):
    reply = copy.deepcopy(REPLY)
    reply["claims"][0]["text"] = '<script>alert("x")</script><img src=x onerror=1>'
    reply["receipts"][1]["url"] = "javascript:alert(1)"
    detail = stored_detail(reply, question='<b onmouseover="x">Question</b>')
    observe(monkeypatch, detail)
    body = client.get(HTML_ROUTE).text
    assert "<script>" not in body and "<img" not in body and "<b " not in body
    assert "&lt;script&gt;" in body
    assert "javascript:" not in body
    text, parser = text_of(body)
    assert "Source link: withheld because it is not a web address" in text
    assert set(parser.tags) <= {
        "html", "head", "meta", "title", "style", "body", "header", "main",
        "section", "article", "h1", "h2", "h3", "p", "blockquote", "a", "footer",
    }


def test_the_export_is_read_for_the_named_request_only(monkeypatch):
    observe(monkeypatch, stored_detail(request_id="00000000-0000-4000-8000-000000000009"))
    response = client.get(HTML_ROUTE)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "answer_invalid"


@pytest.mark.parametrize("state", ["unconfirmed", "unavailable", "refused", "needs_clarification"])
def test_only_a_completed_answer_is_exported(monkeypatch, state):
    detail = stored_detail(observed_state=state)
    if state == "unconfirmed":
        detail["response"] = None
    observe(monkeypatch, detail)
    response = client.get(HTML_ROUTE)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "answer_not_exportable"
    assert "completed answer" in detail["message"]


def test_a_held_answer_is_not_exported(monkeypatch):
    reply = copy.deepcopy(REPLY)
    reply["usage"].update(
        status="unresolved",
        reason="response_usage_unavailable",
        model_calls=None,
        input_tokens=None,
        output_tokens=None,
    )
    observe(monkeypatch, stored_detail(reply, observed_state="held"))
    response = client.get(HTML_ROUTE)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "answer_held"


def test_a_terminal_error_response_is_not_exported(monkeypatch):
    detail = stored_detail()
    detail["response"]["error"] = True
    observe(monkeypatch, detail)
    assert client.get(HTML_ROUTE).json()["detail"]["code"] == "answer_invalid"


@pytest.mark.parametrize("route", [HTML_ROUTE, PDF_ROUTE])
def test_authentication_precedes_the_stored_read(monkeypatch, route):
    calls = observe(monkeypatch)
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    monkeypatch.setenv("UI_PASSCODE", "fixture-passcode")
    assert client.get(route).status_code == 401
    assert calls == []


@pytest.mark.parametrize("suffix", ["export.html", "export.pdf"])
def test_a_malformed_reference_refuses_before_the_stored_read(monkeypatch, suffix):
    calls = observe(monkeypatch)
    response = client.get(f"/api/chat/answer/not-a-request/{suffix}")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "request_invalid"
    assert calls == []


def test_an_unknown_request_reads_as_unavailable(monkeypatch):
    observe(monkeypatch, error=WorkerProcessError("observation_request_unavailable"))
    response = client.get(HTML_ROUTE)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "request_unavailable"


def test_a_failed_stored_read_is_plain(monkeypatch):
    observe(monkeypatch, error=RuntimeError("bucket token leaked"))
    response = client.get(HTML_ROUTE)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "export_unavailable"
    assert "bucket" not in detail["message"]


def test_pdf_export_renders_the_pdf_variant_through_the_reviewed_renderer(monkeypatch):
    observe(monkeypatch)
    seen = []

    def render(html_bytes, *, asset_root):
        seen.append((html_bytes, asset_root))
        return b"%PDF-1.7 synthetic"

    monkeypatch.setattr(pdf_exporter, "render_stored_html_pdf", render)
    response = client.get(PDF_ROUTE)
    assert response.status_code == 200
    assert response.content == b"%PDF-1.7 synthetic"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        'attachment; filename="42-answer-60a5fd02.pdf"'
    )
    assert response.headers["cache-control"] == "no-store"
    (html_bytes, asset_root), = seen
    assert Path(asset_root) == Path("/app/web/dist/assets")
    html = html_bytes.decode("utf-8")
    assert "font-family:Newsreader" in html.replace(" ", "")
    assert "url('newsreader-variable-tcB4qQtO.woff2')" in html
    assert "<title>" not in html
    text, _ = text_of(html)
    assert QUESTION in text


def test_pdf_variant_passes_the_renderer_input_audit(tmp_path):
    html = ask_export.render_answer_html(stored_detail(), RID, pdf=True).encode("utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "newsreader-variable-tcB4qQtO.woff2").write_bytes(b"font")
    prepared = pdf_exporter._prepare_render_input(html, assets, tmp_path / "work")
    assert prepared["font_names"] == ["Newsreader"]
    assert prepared["expected_text"].startswith("42 Ogilvy Intelligence")


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [
        ("pdf_render_busy", 429, "pdf_busy"),
        ("pdf_browser_unavailable", 503, "pdf_unavailable"),
        ("pdf_runtime_invalid", 503, "pdf_unavailable"),
        ("pdf_text_missing", 503, "pdf_unavailable"),
    ],
)
def test_a_renderer_refusal_is_plain_and_offers_the_html_copy(monkeypatch, reason, status, code):
    observe(monkeypatch)

    def render(_html, *, asset_root):
        raise pdf_exporter.PdfExportError(reason)

    monkeypatch.setattr(pdf_exporter, "render_stored_html_pdf", render)
    response = client.get(PDF_ROUTE)
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert reason not in detail["message"]
    if code == "pdf_unavailable":
        assert "HTML" in detail["message"]


def test_pdf_is_not_attempted_for_an_answer_that_cannot_be_exported(monkeypatch):
    observe(monkeypatch, stored_detail(observed_state="refused"))
    monkeypatch.setattr(
        pdf_exporter,
        "render_stored_html_pdf",
        lambda *_a, **_k: pytest.fail("renderer reached"),
    )
    assert client.get(PDF_ROUTE).status_code == 409


def test_request_details_name_the_request_without_usage_money(monkeypatch):
    observe(monkeypatch)
    text, _ = text_of(client.get(HTML_ROUTE).text)
    assert f"Request reference: {RID}" in text
    assert "As of 20 Sept 2026, 16:28 UTC" in text
    assert "0.100000" not in text


def _hostile_receipt(reply, **fields):
    receipt = reply["receipts"][0]
    receipt["quote_bindings"] = []
    receipt.update(fields)
    return receipt


def test_markup_in_a_third_party_excerpt_is_escaped(monkeypatch):
    reply = copy.deepcopy(REPLY)
    excerpt = '</blockquote><script>alert("x")</script><b>bold</b>'
    _hostile_receipt(reply, excerpt=excerpt)
    observe(monkeypatch, stored_detail(reply))
    body = client.get(HTML_ROUTE).text
    assert "<script" not in body and "<b>" not in body
    assert "<blockquote>&lt;/blockquote&gt;&lt;script&gt;" in body
    text, parser = text_of(body)
    assert squash(excerpt) in text
    assert "script" not in parser.tags and "b" not in parser.tags


@pytest.mark.parametrize(
    "fields",
    [
        {"platform": "<img src=x onerror=1>"},
        {"platform": None, "source_label": "<img src=x onerror=1>"},
    ],
)
def test_markup_in_a_source_name_is_escaped(monkeypatch, fields):
    reply = copy.deepcopy(REPLY)
    _hostile_receipt(reply, **fields)
    observe(monkeypatch, stored_detail(reply))
    body = client.get(HTML_ROUTE).text
    assert "<img" not in body
    assert "<h3>R1 · &lt;img src=x onerror=1&gt;</h3>" in body
    _, parser = text_of(body)
    assert "img" not in parser.tags


def test_a_source_link_cannot_break_out_of_its_attribute(monkeypatch):
    reply = copy.deepcopy(REPLY)
    url = 'https://example.test/a"><script>alert(1)</script><a href="x'
    _hostile_receipt(reply, url=url)
    observe(monkeypatch, stored_detail(reply))
    body = client.get(HTML_ROUTE).text
    assert "<script" not in body
    _, parser = text_of(body)
    assert "script" not in parser.tags
    assert parser.links[0] == url
    assert parser.tags.count("a") == len(REPLY["receipts"])


@pytest.mark.parametrize(
    "url",
    ["javascript://example.test/%0Aalert(1)", "ftp://example.test/file", "data://example.test/x"],
)
def test_only_a_web_address_becomes_a_link(monkeypatch, url):
    reply = copy.deepcopy(REPLY)
    _hostile_receipt(reply, url=url)
    observe(monkeypatch, stored_detail(reply))
    body = client.get(HTML_ROUTE).text
    text, parser = text_of(body)
    assert url not in body
    assert "Source link: withheld because it is not a web address" in text
    assert all(link.startswith("https://") for link in parser.links)


def test_a_stored_reply_for_another_request_is_not_exported(monkeypatch):
    reply = copy.deepcopy(REPLY)
    reply["request_id"] = "00000000-0000-4000-8000-000000000009"
    observe(monkeypatch, stored_detail(reply, request_id=RID))
    response = client.get(HTML_ROUTE)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "answer_invalid"


def test_an_answer_with_unsettled_usage_is_held_whatever_its_state(monkeypatch):
    reply = copy.deepcopy(REPLY)
    reply["usage"].update(
        status="unresolved",
        reason="response_usage_unavailable",
        model_calls=None,
        input_tokens=None,
        output_tokens=None,
    )
    observe(monkeypatch, stored_detail(reply, observed_state="partial"))
    response = client.get(HTML_ROUTE)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "answer_held"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"claim_id": "c2"}, "the binding's claim does not cite the receipt"),
        ({"content_digest": "f" * 64}, "the binding names another receipt's content"),
        ({"end": 98}, "the span ends past the excerpt"),
        ({"end": 10**4}, "the span ends far past the excerpt"),
    ],
)
def test_a_quote_binding_that_does_not_hold_withholds_the_export(monkeypatch, change, reason):
    reply = copy.deepcopy(REPLY)
    binding = reply["receipts"][0]["quote_bindings"][0]
    assert (binding["start"], binding["end"]) == (0, len(reply["receipts"][0]["excerpt"]))
    binding.update(change)
    observe(monkeypatch, stored_detail(reply))
    response = client.get(HTML_ROUTE)
    assert response.status_code == 502, reason
    assert response.json()["detail"]["code"] == "answer_invalid"


RELEASED = "Not available in the released data"


def test_gaps_in_the_released_data_are_not_blamed_on_the_source(monkeypatch):
    reply = copy.deepcopy(REPLY)
    _hostile_receipt(
        reply, market=None, platform=None, excerpt=None, url=None, collected_at=None
    )
    reply["receipts"][0]["published_at"] = "2026-09-03T10:00:00Z"
    observe(monkeypatch, stored_detail(reply))
    body = client.get(HTML_ROUTE).text
    record = body.split("<article>")[-len(REPLY["receipts"])].split("</article>")[0]
    text, _ = text_of(record)
    assert "Content record · Market: " + RELEASED in text
    assert "Platform: " + RELEASED in text
    assert "Excerpt: " + RELEASED in text
    assert "Source link: " + RELEASED in text
    assert "Published: 3 Sept 2026 · Collection time: " + RELEASED in text
    assert "Source family: " + RELEASED in text
    assert "Collected" not in text
    assert "not recorded by the source" not in text


# The engine lists an unfulfilled requirement by its planned id ahead of the
# readable "Missing required evidence" line. The in app view shows the readable
# line and keeps the id only as its requirement reference, so the export must
# never print the id as a needed item of its own, in either variant.
NEEDED_QUESTION = (
    "Missing required evidence: What topics, cultural events, and conversations "
    "are trending in South Africa during the observation window?"
)
NEEDED_CHALLENGE = "Challenge search incomplete: no challenge requirement was planned."


def still_needed(html):
    start = html.index("<h2>What is still needed</h2>")
    return html[start : html.index("</section>", start)]


@pytest.mark.parametrize("pdf", [False, True])
def test_a_requirement_id_is_kept_as_the_reference_of_its_readable_line(pdf):
    assert REPLY["missing_work"][0] == "req_trending_topics_za"
    html = ask_export.render_answer_html(stored_detail(), RID, pdf=pdf)
    section = still_needed(html)
    assert "<p>req_trending_topics_za</p>" not in section
    assert section.count("req_trending_topics_za") == 1
    assert section.count(NEEDED_QUESTION) == 1
    question = section.index(f"<p>{NEEDED_QUESTION}</p>")
    reference = section.index(
        '<p class="note">Requirement reference: req_trending_topics_za</p>'
    )
    challenge = section.index(f"<p>{NEEDED_CHALLENGE}</p>")
    assert question < reference < challenge
    text, _ = text_of(section)
    assert text == squash(
        f"What is still needed {NEEDED_QUESTION} "
        f"Requirement reference: req_trending_topics_za {NEEDED_CHALLENGE}"
    )


def test_the_pdf_audit_reads_the_requirement_reference_as_page_text(tmp_path):
    html = ask_export.render_answer_html(stored_detail(), RID, pdf=True)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "newsreader-variable-tcB4qQtO.woff2").write_bytes(b"font")
    pdf_exporter._prepare_render_input(html.encode("utf-8"), assets, tmp_path / "work")
    audit = pdf_exporter._HtmlAudit()
    audit.feed(html)
    audit.close()
    page = squash(" ".join(audit.text))
    assert f"{NEEDED_QUESTION} Requirement reference: req_trending_topics_za" in page
    assert page.count("req_trending_topics_za") == 1


BSA_LENS = {
    "lens_binding_version": "client_lens_binding_v1",
    "client_lens_id": "bsa_pulse_lens",
    "configuration_digest": "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf",
}


@pytest.mark.parametrize("pdf", [False, True])
def test_the_export_names_the_client_lens_the_stored_request_carries(pdf):
    lensed, _ = text_of(
        ask_export.render_answer_html(
            stored_detail(client_lens=dict(BSA_LENS)), RID, pdf=pdf, client_scope_id="bsa_pulse"
        )
    )
    assert "Client lens: Brand South Africa Pulse" in lensed
    assert (
        "Client lens configuration: bsa_pulse_lens, " + BSA_LENS["configuration_digest"]
    ) in lensed
    general, _ = text_of(ask_export.render_answer_html(stored_detail(), RID, pdf=pdf))
    assert "Client lens: None (general 42)" in general
    assert "Brand South Africa" not in general
    assert "Client lens configuration" not in general


def server_scope(monkeypatch, client_scope_id):
    """The server scope the stored request is read under, as the route resolves it."""
    configured = general_question_routes._current_scope
    monkeypatch.setattr(
        general_question_routes,
        "_current_scope",
        lambda: dict(configured(), client_scope_id=client_scope_id),
    )


def test_the_exported_lens_is_the_stored_one_whatever_the_route_would_select(monkeypatch):
    server_scope(monkeypatch, "bsa_pulse")
    observe(monkeypatch, stored_detail(client_lens=dict(BSA_LENS)))
    text, _ = text_of(client.get(HTML_ROUTE + "?lens=").text)
    assert "Client lens: Brand South Africa Pulse" in text


def test_a_lens_the_registry_no_longer_lists_is_named_by_its_stored_identity():
    moved = dict(BSA_LENS, configuration_digest="0" * 64)
    text, _ = text_of(ask_export.render_answer_html(stored_detail(client_lens=moved), RID))
    assert "Client lens: bsa_pulse_lens" in text
    assert "Brand South Africa Pulse" not in text
    assert "Client lens configuration: bsa_pulse_lens, " + "0" * 64 in text


def test_a_lens_of_another_client_scope_is_named_by_its_stored_identity(monkeypatch):
    text, _ = text_of(
        ask_export.render_answer_html(
            stored_detail(client_lens=dict(BSA_LENS)), RID, client_scope_id="ogilvy_default"
        )
    )
    assert "Client lens: bsa_pulse_lens" in text
    assert "Brand South Africa Pulse" not in text
    server_scope(monkeypatch, "other_scope")
    observe(monkeypatch, stored_detail(client_lens=dict(BSA_LENS)))
    routed, _ = text_of(client.get(HTML_ROUTE).text)
    assert "Client lens: bsa_pulse_lens" in routed
    assert "Brand South Africa Pulse" not in routed
    assert "Client lens configuration: bsa_pulse_lens, " + BSA_LENS["configuration_digest"] in routed


def test_a_disabled_lens_is_named_by_its_stored_identity(tmp_path, monkeypatch):
    from src.api import client_lenses

    document = yaml.safe_load(client_lenses.CLIENT_LENSES_PATH.read_text(encoding="utf-8"))
    document["lenses"]["bsa_pulse_lens"]["enabled"] = False
    registry = tmp_path / "client_lenses.yaml"
    registry.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(client_lenses, "CLIENT_LENSES_PATH", registry)
    monkeypatch.setattr(
        client_lenses, "CONFIG_DIGEST", hashlib.sha256(registry.read_bytes()).hexdigest()
    )
    text, _ = text_of(
        ask_export.render_answer_html(
            stored_detail(client_lens=dict(BSA_LENS)), RID, client_scope_id="bsa_pulse"
        )
    )
    assert "Client lens: bsa_pulse_lens" in text
    assert "Brand South Africa Pulse" not in text


@pytest.mark.parametrize(
    "lens",
    [
        None,
        {},
        "bsa_pulse_lens",
        dict(BSA_LENS, lens_binding_version="client_lens_binding_v0"),
        dict(BSA_LENS, client_lens_id=""),
        dict(BSA_LENS, configuration_digest="E" * 64),
        dict(BSA_LENS, extra="x"),
    ],
)
def test_a_malformed_stored_lens_withholds_the_export(lens):
    with pytest.raises(ask_export.AskExportRefused) as refused:
        ask_export.render_answer_html(stored_detail(client_lens=lens), RID)
    assert refused.value.code == "answer_invalid"
