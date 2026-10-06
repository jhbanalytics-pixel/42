import copy
import html
import json
from pathlib import Path

import pytest

from core.api.export import ExportRefused, export_filename, render_answer_html

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def complete():
    return load("ask_complete.json")


@pytest.fixture
def partial():
    return load("ask_partial.json")


def refused(record):
    with pytest.raises(ExportRefused) as caught:
        render_answer_html(record)
    assert caught.value.status == 409
    assert caught.value.code
    return caught.value


@pytest.mark.parametrize("name", ["ask_complete.json", "ask_partial.json"])
def test_finished_records_render_question_claims_and_urls(name):
    record = load(name)
    page = render_answer_html(record)
    assert page.startswith("<!doctype html>")
    assert "<title>42 answer</title>" in page
    assert html.escape(record["question"]) in page
    assert html.escape(record["answer"]["short_answer"]) in page
    for claim in record["answer"]["claims"]:
        assert html.escape(claim["text"]) in page
    for item in record["answer"]["evidence"]:
        assert f'href="{html.escape(item["url"])}"' in page
        assert html.escape(item["text"]) in page
        assert html.escape(item["handle"]) in page
    for gap in record["answer"]["gaps"]:
        assert html.escape(gap["what"]) in page
    assert "What we do not know" in page
    assert record["run"]["run_id"] in page


def test_meta_line_labels_numbers_and_footer(complete):
    page = render_answer_html(complete)
    assert "South Africa" in page
    assert "21 to 27 September 2026" in page
    assert "214 posts" in page
    assert "2 platforms" in page
    assert "Corroborated" in page
    assert "Inferred" in page
    assert "q_fixture_tt_tag_7d" in page
    assert "q_fixture_x_mentions_7d" in page
    assert "kitchen table, one pot, everybody dances" in page
    assert html.escape(complete["answer"]["so_what"][0]["text"]) in page
    assert html.escape(complete["answer"]["watch_next"][0]["text"]) in page
    assert "41 seconds" in page
    assert "214 credits" in page
    assert 'href="#source-1"' in page and 'id="source-1"' in page
    assert 'href="#source-2"' in page and 'id="source-2"' in page


def test_partial_shows_single_source_label(partial):
    page = render_answer_html(partial)
    assert "Single source" in page
    assert "Kenya" in page
    assert "9 posts" in page
    assert "1 platform" in page and "1 platforms" not in page


def test_context_is_labelled_as_context(complete):
    complete["answer"]["context"] = "Shared-pot meals are a Sunday habit in many homes."
    page = render_answer_html(complete)
    assert "Shared-pot meals are a Sunday habit in many homes." in page
    assert "Context" in page
    assert "not evidence" in page


def test_failed_record_refused():
    refused(load("ask_failed.json"))


def test_running_record_refused(complete):
    complete["status"] = "running"
    refused(complete)


def test_stopped_record_exports(complete):
    complete["status"] = "stopped"
    assert "42 answer" in render_answer_html(complete)


def test_null_answer_refused(complete):
    complete["answer"] = None
    refused(complete)


def test_mutated_quote_refused(complete):
    complete["answer"]["claims"][0]["quotes"][0]["text"] = "kitchen table, two pots, everybody dances"
    refused(complete)


def test_quote_tolerates_whitespace_and_curly_quotes(complete):
    record = copy.deepcopy(complete)
    record["answer"]["evidence"][1]["text"] = "Tried it, “no props needed,” just the family pot"
    record["answer"]["claims"][1]["quotes"][0]["text"] = '"no  props\nneeded," just'
    record["answer"]["claims"][0]["quotes"][1]["text"] = "the family pot"
    assert "42 answer" in render_answer_html(record)


def test_unknown_evidence_id_refused(complete):
    complete["answer"]["claims"][1]["evidence_ids"].append("tt_missing")
    refused(complete)


@pytest.mark.parametrize("cut", ["missing", "empty", "none"])
def test_claim_without_evidence_ids_refused(complete, cut):
    claim = complete["answer"]["claims"][1]
    if cut == "missing":
        del claim["evidence_ids"]
    else:
        claim["evidence_ids"] = [] if cut == "empty" else None
    error = refused(complete)
    assert error.code == "not_ready"
    assert "Claim c2" in error.message
    assert "cites no evidence" in error.message


def test_quote_on_unknown_evidence_refused(complete):
    complete["answer"]["claims"][1]["quotes"][0]["evidence_id"] = "tt_missing"
    refused(complete)


def test_javascript_url_not_linked(complete):
    complete["answer"]["evidence"][0]["url"] = "javascript:alert(1)"
    page = render_answer_html(complete)
    assert 'href="javascript' not in page
    assert "javascript:alert(1)" in page


def test_credentialed_url_not_linked(complete):
    complete["answer"]["evidence"][0]["url"] = "https://user:pw@example.invalid/x"
    page = render_answer_html(complete)
    assert 'href="https://user:pw@' not in page


def test_script_in_post_text_escaped(complete):
    evil = "<script>alert(1)</script> kitchen table, one pot, everybody dances everybody dances"
    complete["answer"]["evidence"][0]["text"] = evil
    complete["question"] = "<b>bold</b> question"
    page = render_answer_html(complete)
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<b>bold</b>" not in page


def test_filename_sanitised():
    assert export_filename("a_20260928_0000000a") == "42-answer-a_20260928_0000000a.html"
    assert export_filename('a_1/../x"<y>\r\n z') == "42-answer-a_1xyz.html"


def test_source_collected_as_twitter_is_named_x(complete):
    complete["answer"]["evidence"][1]["platform"] = "twitter"
    handle = html.escape(complete["answer"]["evidence"][1]["handle"])
    page = render_answer_html(complete)
    assert f"<h3>[2] X · {handle}</h3>" in page
    assert "twitter" not in page.lower()


# Visual QA, 5 October 2026 (A04, A05): the export printed "59 located_posts (query q_14)", an empty Short answer,
# record ids in its gaps and "Not enough evidence" over a budget stop.
def test_figures_read_as_words_and_their_queries_move_to_one_line(complete):
    record = copy.deepcopy(complete)
    claim = record["answer"]["claims"][0]
    base = claim["numbers"][0] if claim.get("numbers") else {"run_id": "r1", "result_hash": "sha256:x"}
    claim["numbers"] = [{**base, "value": 59, "unit": "located_posts", "query_id": "q_14"},
                        {**base, "value": 1, "unit": "located_creators", "query_id": "q_14"}]
    page = render_answer_html(record)
    assert "59 posts with a known location" in page
    assert "1 creator with a known location" in page
    assert "located_" not in page
    assert "(query q_14)" not in page
    assert 'title="Query q_14"' in page
    assert "Figures come from queries" in page or "Figures come from query" in page
    assert page.count("q_14") >= 2


def test_empty_short_answer_says_why(partial):
    record = copy.deepcopy(partial)
    record["answer"]["short_answer"] = ""
    page = render_answer_html(record)
    assert "No short answer: too little passed the checks to sum up safely." in page


def test_gap_record_ids_become_a_count(partial):
    record = copy.deepcopy(partial)
    record["answer"]["gaps"].append({
        "what": "A claim was removed: a narrower claim did not pass every trust check",
        "searched": "cited posts obs1_7bfd7de8e71252031cc34ad053aee005, obs1_66faeb419bbd118deb6bbaecfed8f213",
        "why": "the narrowed wording did not pass all required checks",
    })
    page = render_answer_html(record)
    assert "Searched: 2 cited posts" in page
    assert "obs1_" not in page


def test_budget_stop_is_not_headed_as_missing_evidence(partial):
    record = copy.deepcopy(partial)
    record["answer"]["status"] = "insufficient_evidence"
    record["answer"]["gaps"].append({
        "what": "The answer stopped because a model call could not be safely reserved",
        "searched": "stored posts and live sources, 29 September to 5 October",
        "why": "model cost or usage could not be verified within the per-question budget",
    })
    page = render_answer_html(record)
    assert "Stopped at this question&#x27;s model budget, not for lack of evidence" in page
    assert "Not enough evidence to answer" not in page
