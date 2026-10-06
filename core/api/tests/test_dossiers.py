import copy
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from core.api import dossiers

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AT = "2026-09-29T10:00:00.000001+02:00"


def source():
    """ask_complete.json plus a Single source claim (c3) and a claim the checks cut (c4)."""
    record = json.loads((FIXTURES / "ask_complete.json").read_text(encoding="utf-8"))
    claims = record["answer"]["claims"]
    claims.append({"id": "c3", "text": "One Cape Town poster says the family pot is all you need.",
                   "label": "single_source", "kind": "observation", "evidence_ids": ["x_fixture_2"],
                   "quotes": [{"evidence_id": "x_fixture_2", "text": "just the family pot"}]})
    claims.append({"id": "c4", "text": "CUT CLAIM TEXT", "label": "observed", "kind": "observation",
                   "evidence_ids": ["tt_fixture_1"], "check": "cut"})
    return record


def draft(record=None, body=None, previous=None, version=1):
    record = record or source()
    sel = dossiers.selection(body or {}, record, previous)
    return dossiers.build(record, sel, dossier_id="d_0123456789ab", version=version, created_at=AT,
                          source={"ask_id": record["ask_id"]})


def by_id(body):
    return {c["claim_id"]: c for c in body["claims"]}


def test_build_carries_only_claims_that_passed_the_checks():
    record = source()
    body = draft(record)
    assert [c["claim_id"] for c in body["claims"]] == ["c1", "c2", "c3"]
    assert all(c["kept"] for c in body["claims"])
    assert body["left_out"] == [{"claim_id": "c4", "reason": "cut by the checks"}]
    assert "CUT CLAIM TEXT" not in json.dumps(body)
    c1 = by_id(body)["c1"]
    src = record["answer"]["claims"][0]
    assert (c1["text"], c1["label"], c1["evidence_ids"], c1["quotes"], c1["numbers"]) == (
        src["text"], src["label"], src["evidence_ids"], src["quotes"], src["numbers"])
    assert body["summary"] == record["answer"]["short_answer"]
    assert body["gaps"] == record["answer"]["gaps"]
    assert [e["id"] for e in body["evidence"]] == ["tt_fixture_1", "x_fixture_2"]
    assert body["state"] == "draft" and body["version"] == 1
    assert body["source_ask_id"] == record["ask_id"] and body["source"] == {"ask_id": record["ask_id"]}
    assert body["title"] == record["question"]


def test_unfinished_or_answerless_source_is_refused():
    for change in ({"status": "running"}, {"status": "failed"}, {"answer": None}):
        record = {**source(), **change}
        with pytest.raises(dossiers.Refused) as caught:
            dossiers.selection({}, record, None)
        assert caught.value.status == 409 and caught.value.code == "not_ready"


def test_client_claim_text_and_labels_are_ignored():
    forged = {"keep": ["c1", "c2"], "title": "Kitchen-table dance",
              "claims": [{"id": "c1", "text": "FORGED TEXT", "label": "observed"}],
              "label": "observed", "text": "FORGED TEXT", "evidence": [{"id": "fake"}],
              "quotes": [{"evidence_id": "tt_fixture_1", "text": "FORGED QUOTE"}],
              "numbers": [{"value": 9999}]}
    record = source()
    body = draft(record, forged)
    assert "FORGED" not in json.dumps(body) and "9999" not in json.dumps(body)
    assert by_id(body)["c1"]["label"] == "corroborated"
    assert by_id(body)["c2"]["label"] == "inferred"
    assert "fake" not in [e["id"] for e in body["evidence"]]


def test_keep_order_title_and_notes():
    body = draft(body={"keep": ["c1", "c3"], "order": ["c3", "c1"], "title": "Pot dance",
                       "notes": {"c3": "Ask the client whether one poster is enough"}})
    assert [(c["claim_id"], c["kept"]) for c in body["claims"]] == [("c3", True), ("c1", True), ("c2", False)]
    assert body["title"] == "Pot dance"
    assert by_id(body)["c3"]["note"] == "Ask the client whether one poster is enough"
    assert by_id(body)["c1"]["note"] is None
    # Evidence follows the kept claims only.
    assert [e["id"] for e in body["evidence"]] == ["tt_fixture_1", "x_fixture_2"]
    only_c3 = draft(body={"keep": ["c3"]})
    assert [e["id"] for e in only_c3["evidence"]] == ["x_fixture_2"]


def test_later_selection_defaults_to_the_previous_version():
    first = draft(body={"keep": ["c1", "c3"], "order": ["c3", "c1"], "title": "Pot dance",
                        "notes": {"c1": "Lead with this"}})
    second = draft(body={"title": "Pot dance, take two"}, previous=first, version=2)
    assert [(c["claim_id"], c["kept"]) for c in second["claims"]] == [("c3", True), ("c1", True), ("c2", False)]
    assert by_id(second)["c1"]["note"] == "Lead with this"
    assert second["title"] == "Pot dance, take two" and second["version"] == 2
    third = draft(body={"order": ["c1", "c3"]}, previous=second, version=3)
    assert [c["claim_id"] for c in third["claims"] if c["kept"]] == ["c1", "c3"]


@pytest.mark.parametrize("body", [
    {"keep": ["c9"]},
    {"keep": ["c4"]},
    {"keep": ["c1", "c1"]},
    {"keep": "c1"},
    {"keep": ["c1"], "order": ["c2"]},
    {"keep": ["c1", "c2"], "order": ["c2", "c2"]},
    {"notes": {"c9": "x"}},
    {"notes": {"c1": "x" * 1001}},
    {"notes": {"c1": 5}},
    {"notes": ["c1"]},
    {"title": ""},
    {"title": "x" * 201},
    {"title": 5},
])
def test_bad_selections_give_400(body):
    with pytest.raises(dossiers.Refused) as caught:
        dossiers.selection(body, source(), None)
    assert caught.value.status == 400 and caught.value.code == "bad_request"


def test_latest_tick_row_per_claim_counts():
    rows = [{"claim_id": "c2", "ticked": True, "note": None, "who": "passcode", "at": "1"},
            {"claim_id": "c3", "ticked": True, "note": "fine", "who": "passcode", "at": "2"},
            {"claim_id": "c3", "ticked": False, "note": "not sure", "who": "passcode", "at": "3"}]
    ticks = dossiers.latest_ticks(rows)
    assert ticks["c2"]["ticked"] is True
    assert ticks["c3"]["ticked"] is False and ticks["c3"]["note"] == "not sure"


def test_single_source_and_inferred_need_a_current_tick():
    body = draft()
    assert [n["claim_id"] for n in dossiers.needs_tick(body, {})] == ["c2", "c3"]
    ticks = dossiers.latest_ticks([{"claim_id": "c2", "ticked": True, "note": None, "who": "p", "at": "1"},
                                   {"claim_id": "c3", "ticked": True, "note": None, "who": "p", "at": "2"},
                                   {"claim_id": "c3", "ticked": False, "note": None, "who": "p", "at": "3"}])
    assert [n["claim_id"] for n in dossiers.needs_tick(body, ticks)] == ["c3"]
    # A claim that is not kept needs nothing.
    assert dossiers.needs_tick(draft(body={"keep": ["c1", "c2"]}), ticks) == []


def test_recheck_passes_on_an_untouched_source():
    record = source()
    assert dossiers.recheck(draft(record), record) == []


def test_recheck_names_a_quote_that_left_its_evidence_text():
    record = source()
    body = draft(record)
    record["answer"]["evidence"][1]["text"] = "Tried the #fixture thing with my cousins, everybody dances"
    problems = dossiers.recheck(body, record)
    assert sorted({p["claim_id"] for p in problems}) == ["c2", "c3"]
    assert all("verbatim" in p["reason"] for p in problems)


def test_recheck_names_a_changed_result_hash_label_or_unresolved_evidence():
    record = source()
    body = draft(record)
    changed = copy.deepcopy(record)
    changed["answer"]["claims"][0]["numbers"][1]["result_hash"] = "sha256:" + "0" * 64
    assert [(p["claim_id"], "result_hash" in p["reason"]) for p in dossiers.recheck(body, changed)] == [("c1", True)]

    changed = copy.deepcopy(record)
    changed["answer"]["claims"][2]["label"] = "corroborated"
    assert [p["claim_id"] for p in dossiers.recheck(body, changed)] == ["c3"]

    changed = copy.deepcopy(record)
    changed["answer"]["evidence"] = changed["answer"]["evidence"][:1]
    assert sorted({p["claim_id"] for p in dossiers.recheck(body, changed)}) == ["c1", "c2", "c3"]

    changed = copy.deepcopy(record)
    changed["answer"]["claims"][1]["check"] = "cut"
    assert [p["claim_id"] for p in dossiers.recheck(body, changed)] == ["c2"]


def test_recheck_names_a_stored_body_that_no_longer_matches_its_source():
    record = source()
    body = draft(record)
    by_id(body)["c1"]["quotes"][0]["text"] = "kitchen table"
    assert [p["claim_id"] for p in dossiers.recheck(body, record)] == ["c1"]
    body = draft(record)
    by_id(body)["c2"]["evidence_ids"] = ["tt_fixture_1"]
    assert [p["claim_id"] for p in dossiers.recheck(body, record)] == ["c2"]


def test_recheck_skips_claims_that_are_not_kept():
    record = source()
    body = draft(record, {"keep": ["c1"]})
    record["answer"]["claims"][2]["label"] = "corroborated"
    assert dossiers.recheck(body, record) == []


def test_freeze_copies_the_draft_and_records_the_ticks():
    body = draft()
    ticks = {"c2": {"claim_id": "c2", "ticked": True, "note": "ok", "who": "passcode", "at": "x"}}
    frozen = dossiers.freeze(body, version=2, created_at="2026-09-29T11:00:00+02:00", ticks=ticks)
    assert frozen["state"] == "frozen" and frozen["version"] == 2 and frozen["frozen_from"] == 1
    assert frozen["claims"] == body["claims"] and frozen["evidence"] == body["evidence"]
    assert frozen["reviews"] == {"c2": {"ticked": True, "note": "ok", "at": "x"}}
    assert body["state"] == "draft" and body["version"] == 1


def test_an_edit_made_from_an_older_version_is_refused_as_stale():
    dossiers.check_current(3, 3)
    dossiers.check_current(None, 3)
    for given in (2, 4):
        with pytest.raises(dossiers.Refused) as caught:
            dossiers.check_current(given, 3)
        assert caught.value.status == 409 and caught.value.code == "stale_version"
        assert caught.value.message == ("This dossier changed since you opened it. "
                                        "Reload to see the latest version.")


@pytest.mark.parametrize("given", [0, -1, "3", 3.0, True, [3]])
def test_a_from_version_that_is_not_a_version_number_gives_400(given):
    with pytest.raises(dossiers.Refused) as caught:
        dossiers.check_current(given, 3)
    assert caught.value.status == 400 and caught.value.code == "bad_request"


def test_content_hash_is_stable_and_follows_the_body():
    body = draft()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", dossiers.content_hash(body))
    assert dossiers.content_hash(copy.deepcopy(body)) == dossiers.content_hash(body)
    body["title"] = "Changed"
    assert dossiers.content_hash(body) != dossiers.content_hash(draft())


def frozen_fixture():
    record = source()
    record["answer"]["evidence"][0]["url"] = "https://example.invalid/tiktok/@fixture_za_1/video/7431?a=1&b=<2>"
    record["answer"]["evidence"][1]["url"] = "javascript:alert(1)"
    record["answer"]["evidence"][1]["text"] += " <script>alert('x')</script>"
    body = draft(record, {"keep": ["c1", "c2"], "order": ["c2", "c1"], "title": "Pot <b>dance</b>",
                          "notes": {"c2": "Checked with <i>Thapelo</i>"}})
    ticks = {"c2": {"claim_id": "c2", "ticked": True, "note": None, "who": "passcode", "at": AT}}
    return dossiers.freeze(body, version=2, created_at="2026-09-29T11:00:00+02:00", ticks=ticks)


def test_html_export_refuses_a_draft():
    with pytest.raises(dossiers.Refused) as caught:
        dossiers.render_html(draft())
    assert caught.value.status == 409 and caught.value.code == "not_ready"


def test_html_export_escapes_and_keeps_citations():
    page = dossiers.render_html(frozen_fixture())
    assert page.startswith("<!doctype html>")
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "<b>dance</b>" not in page and "Pot &lt;b&gt;dance&lt;/b&gt;" in page
    assert "<i>Thapelo</i>" not in page and "Reviewer&#x27;s note: Checked with &lt;i&gt;Thapelo&lt;/i&gt;" in page
    assert 'href="javascript:' not in page and "javascript:alert(1) (not linked)" in page
    safe = "https://example.invalid/tiktok/@fixture_za_1/video/7431?a=1&amp;b=&lt;2&gt;"
    assert f'<a href="{safe}">{safe}</a>' in page
    for n in (1, 2):
        assert f'id="source-{n}"' in page and f'href="#source-{n}"' in page
    # Kept claims only, in the chosen order; the unkept claim's text is not exported.
    assert page.index("The format likely spreads") < page.index("TikTok posts tagged #fixture in South Africa")
    assert "One Cape Town poster" not in page
    assert "q_fixture_tt_tag_7d" in page
    assert "Inferred" in page and "Corroborated" in page
    assert "Reviewed" in page
    assert "CUT CLAIM TEXT" not in page


def test_export_filename():
    assert dossiers.export_filename("d_0123456789ab", 2, "pdf") == "42-dossier-d_0123456789ab-v2.pdf"
    assert dossiers.export_filename("d_../x", 2, "html") == "42-dossier-d_x-v2.html"


@pytest.fixture
def chromium(monkeypatch):
    """Point the renderer at the newest installed headless Chromium when F42_CHROMIUM is not set.

    The image installs Playwright's own build; a PC can hold a newer build than its Playwright expects."""
    if os.environ.get("F42_CHROMIUM"):
        return
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                or (Path(os.environ["LOCALAPPDATA"]) if os.name == "nt" else Path.home() / ".cache") / "ms-playwright")
    shells = sorted(root.glob("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell*"),
                    key=lambda p: int(p.parts[-3].rsplit("-", 1)[1]))
    shells = [p for p in shells if p.suffix in ("", ".exe")]
    if shells:
        monkeypatch.setenv("F42_CHROMIUM", str(shells[-1]))


def test_pdf_export_is_a_valid_pdf_with_every_citation(chromium):
    from pypdf import PdfReader

    from core.api import pdf

    body = frozen_fixture()
    data = pdf.render_pdf(dossiers.render_html(body))
    assert data.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) >= 1
    text = "".join("".join(page.extract_text().split()) for page in reader.pages)
    annots = [annot.get_object() for page in reader.pages for annot in page.get("/Annots") or []]
    # Chromium percent-encodes the URI it writes (< becomes %3C); decoded, it must be the evidence link.
    uris = [unquote(str(a["/A"]["/URI"])) for a in annots if "/A" in a]
    link = "https://example.invalid/tiktok/@fixture_za_1/video/7431?a=1&b=<2>"
    assert link in text
    assert uris == [link]
    assert "javascript:alert(1)" in text
    assert "[1]" in text and "[2]" in text
    assert {str(a["/Dest"]) for a in annots if "/Dest" in a} == {"/source-1", "/source-2"}
    assert "".join("Pot <b>dance</b>".split()) in text
    for claim in body["claims"]:
        if claim["kept"]:
            for quote in claim["quotes"]:
                assert "".join(quote["text"].split()) in text


@pytest.mark.parametrize("page", [
    "<p>x</p><script>alert(1)</script>",
    '<p onclick="x()">x</p>',
    '<img src="https://example.invalid/a.png"><p>x</p>',
    '<iframe src="about:blank"></iframe><p>x</p>',
    '<meta http-equiv="refresh" content="0;url=https://example.invalid"><p>x</p>',
    "<style>p{background:url(https://example.invalid/a.png)}</style><p>x</p>",
    '<link rel="stylesheet" href="https://example.invalid/a.css"><p>x</p>',
    '<p><a href="file:///etc/passwd">x</a></p>',
    "",
])
def test_pdf_refuses_unsafe_or_empty_html_before_any_browser(page, monkeypatch):
    from core.api import pdf

    monkeypatch.setattr(pdf, "_render", lambda html: pytest.fail("the browser must not start"))
    with pytest.raises(pdf.PdfError):
        pdf.render_pdf(page)


def test_pdf_refuses_a_second_render_while_one_runs(monkeypatch):
    from core.api import pdf

    assert pdf._PERMIT.acquire(blocking=False)
    try:
        with pytest.raises(pdf.PdfError) as caught:
            pdf.render_pdf("<p>x</p>")
        assert caught.value.reason == "pdf_render_busy"
    finally:
        pdf._PERMIT.release()


def test_pdf_rejects_output_that_is_not_a_pdf(monkeypatch):
    from core.api import pdf

    monkeypatch.setattr(pdf, "_render", lambda html: b"<html>not a pdf</html>")
    with pytest.raises(pdf.PdfError) as caught:
        pdf.render_pdf("<p>x</p>")
    assert caught.value.reason == "pdf_output_invalid"


# A skin report (contract section 15.1): nobody who may not be named shows in the JSON, the HTML or the PDF, and
# the stored quotes stay verbatim so the freeze re-check still passes.

SKIN_PEOPLE = {"skin_id": "sk_0123456789ab", "approved": ["x:brandsa"], "allowed": ["tiktok:fixture_ng_macro"]}


def skin_leaks(text):
    from core.api.tests.test_skins import leaks
    return leaks(text)


def skin_source():
    from core.api.tests.test_skins import skin_record

    record = skin_record()
    record.update(question="Weekly report for Brand South Africa", market="ZA")
    return record


def skin_draft(record=None):
    record = record or skin_source()
    sel = dossiers.selection({}, record, None)
    return dossiers.build(record, sel, dossier_id="d_0123456789ab", version=1, created_at=AT,
                          source={"investigation_id": "i_0123456789ab"}, people=SKIN_PEOPLE)


def test_a_dossier_without_a_skin_is_shown_as_stored():
    body = draft()
    assert "people" not in body
    assert dossiers.shown(body) == body


def test_skin_dossier_json_names_no_sub_tier_author_and_masks_their_handles():
    from core.api import skins

    record = skin_source()
    body = skin_draft(record)
    assert body["people"] == SKIN_PEOPLE
    shown = dossiers.shown(body)
    assert skin_leaks(json.dumps(shown)) == []
    evidence = {e["id"]: e for e in shown["evidence"]}
    assert "handle" not in evidence["x_micro"] and "url" not in evidence["x_micro"]
    assert "author_name" not in evidence["x_micro"]
    assert evidence["x_org"]["handle"] == "@BrandSA"
    assert shown["claims"][0]["quotes"][0]["text"] == f"with {skins.MASK} and @BrandSA"
    # The stored body keeps the quotes verbatim, so the K1 and freeze re-checks pass.
    assert body["claims"][0]["quotes"] == record["answer"]["claims"][0]["quotes"]
    assert "@pal_of_micro" in body["claims"][0]["quotes"][0]["text"]
    assert dossiers.recheck(body, record) == []


def test_the_skin_travels_through_freeze_and_the_html_export_is_masked():
    record = skin_source()
    frozen = dossiers.freeze(skin_draft(record), version=2, created_at="2026-09-29T11:00:00+02:00", ticks={})
    assert frozen["people"] == SKIN_PEOPLE
    assert frozen["claims"][0]["quotes"] == record["answer"]["claims"][0]["quotes"]
    page = dossiers.render_html(frozen)
    assert skin_leaks(page) == []
    assert "@BrandSA" in page and "https://x.com/BrandSA/status/1" in page
    assert 'href="#source-1"' in page and 'href="#source-2"' in page


def test_skin_pdf_export_names_no_sub_tier_author(chromium):
    from pypdf import PdfReader

    from core.api import pdf

    frozen = dossiers.freeze(skin_draft(), version=2, created_at="2026-09-29T11:00:00+02:00", ticks={})
    data = pdf.render_pdf(dossiers.render_html(frozen))
    reader = PdfReader(io.BytesIO(data))
    text = "".join(page.extract_text() for page in reader.pages)
    squeezed = "".join(text.split()).lower()
    assert "brandsa" in squeezed and "fixture_ng_macro" in squeezed
    from core.api.tests.test_skins import LEAKS
    assert [leak for leak in LEAKS if leak in squeezed] == []
    annots = [annot.get_object() for page in reader.pages for annot in page.get("/Annots") or []]
    uris = [unquote(str(a["/A"]["/URI"])) for a in annots if "/A" in a]
    assert uris == ["https://x.com/BrandSA/status/1", "https://www.tiktok.com/@fixture_ng_macro/video/2"]
