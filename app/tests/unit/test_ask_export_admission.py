"""The export admits exactly the stored replies the in app view shows.

The Ask view withholds a reply that validateIntelligenceReply rejects. The
export must withhold the same replies, so this suite runs the browser function
itself (under node) and the server's reading of it over one corpus of stored
replies, and requires the same verdict and the same failure name for each.
"""

import copy
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import ask_export, general_question_routes, intelligence_reply, main

APP = Path(__file__).resolve().parents[2]
FIXTURES = APP / "tests" / "fixtures"
REPLY = json.loads(
    (FIXTURES / "general-question-stored-reply-partial.json").read_text(encoding="utf-8")
)
RID = REPLY["request_id"]
HTML_ROUTE = f"/api/chat/answer/{RID}/export.html"
client = TestClient(main.app)


def detail_for(reply, **changes):
    detail = {
        "contract_version": "general_question_detail_v1",
        "request_id": RID,
        "observed_state": reply["status"] if isinstance(reply, dict) else "partial",
        "question": "What were people in South Africa saying about amapiano?",
        "history": [],
        "selected_market": "za",
        "requested_window": {"start": "2026-08-25", "end": "2026-09-07"},
        "response": {"answer": "Stored answer text.", "sources": [], "intelligence": reply},
        "reserved_microusd": 100000,
        "missing_work": [],
    }
    detail.update(changes)
    return detail


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


def serve(monkeypatch, detail):
    async def read(request, request_id):
        return copy.deepcopy(detail)

    monkeypatch.setattr(general_question_routes, "read_question_observation", read)


def browser_verdicts(cases, tmp_path):
    """validateIntelligenceReply, the function the Ask view calls, on each case."""
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps(cases, allow_nan=False), encoding="utf-8")
    validator = (APP / "frontend" / "src" / "generalIntelligence.js").as_uri()
    script = tmp_path / "verdicts.mjs"
    script.write_text(
        f"""
import {{readFileSync}} from 'node:fs';
import {{validateIntelligenceReply}} from '{validator}';
const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));
console.log(JSON.stringify(cases.map(([value, terminalError]) => {{
  const result = validateIntelligenceReply(value, {{terminalError}});
  return result.ok ? {{ok: true}} : {{ok: false, field: result.field}};
}})));
""",
        encoding="utf-8",
    )
    run = subprocess.run(
        ["node", str(script), str(corpus)],
        cwd=APP,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(run.stdout)


def server_verdict(value, terminal_error=False):
    result = intelligence_reply.validate_intelligence_reply(
        copy.deepcopy(value), terminal_error=terminal_error
    )
    return {"ok": True} if result["ok"] else {"ok": False, "field": result["field"]}


# The six answers a review built from the stored reply: the in app view
# withholds every one, and the export printed every one.
def _proposal_as_observation(reply):
    reply["claims"][0]["support_state"] = "proposed"


def _market_outside_scope(reply):
    reply["receipts"][0]["market"] = "ng"


def _published_after_as_of(reply):
    reply["receipts"][0]["published_at"] = "2027-01-01T00:00:00Z"


def _bindings_without_purposes(reply):
    reply["receipts"][0].pop("evidence_purposes")


def _snapshot_mismatch(reply):
    reply["receipts"][0]["snapshot_id"] = "other"


def _unknown_receipt_key(reply):
    reply["receipts"][0]["injected"] = "<b>x</b>"


REVIEWED = {
    "proposal_printed_as_observation": (_proposal_as_observation, "proposal_type"),
    "receipt_market_outside_scope": (_market_outside_scope, "receipt_market"),
    "published_after_as_of": (_published_after_as_of, "receipt_dates"),
    "bindings_without_purposes": (_bindings_without_purposes, "receipt"),
    "receipt_snapshot_mismatch": (_snapshot_mismatch, "receipt_identity"),
    "unknown_receipt_key": (_unknown_receipt_key, "receipt"),
}


def reviewed(name):
    reply = copy.deepcopy(REPLY)
    REVIEWED[name][0](reply)
    return reply


def test_the_in_app_view_withholds_each_reviewed_answer(tmp_path):
    names = list(REVIEWED)
    verdicts = browser_verdicts([[reviewed(name), False] for name in names], tmp_path)
    assert verdicts == [{"ok": False, "field": REVIEWED[name][1]} for name in names]


@pytest.mark.parametrize("name", list(REVIEWED))
def test_the_export_withholds_each_answer_the_in_app_view_withholds(monkeypatch, name):
    serve(monkeypatch, detail_for(reviewed(name)))
    response = client.get(HTML_ROUTE)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "answer_invalid"
    assert server_verdict(reviewed(name)) == {"ok": False, "field": REVIEWED[name][1]}


def test_a_proposal_is_never_printed_as_an_observation(monkeypatch):
    serve(monkeypatch, detail_for(reviewed("proposal_printed_as_observation")))
    body = client.get(HTML_ROUTE).text
    assert "Observation" not in body
    assert "Welcome to South Africa" not in body


# The view reads a receipt time as coverageDate(new Date(value).toISOString())
# (GeneralIntelligence.jsx dateText), a window day through coverageDate, and a
# window (the answer's and each measurement's) through readableWindow.
# askPresentation.js reaches react through router.js only for its hooks, so
# the script stands react in with an empty module and needs no install.
VIEW_DATES = """
import {register} from 'node:module';
const hooks = 'export const useState = () => {}; export const useEffect = () => {};';
register('data:text/javascript,' + encodeURIComponent(
  'export async function resolve(specifier, context, next) {' +
  "  if (specifier === 'react') return {url: 'data:text/javascript,' + " +
  JSON.stringify(encodeURIComponent(hooks)) + ', shortCircuit: true};' +
  '  return next(specifier, context);' +
  '}'
));
const {coverageDate, readableWindow} = await import(process.argv[2]);
const values = JSON.parse(process.argv[3]);
const windows = JSON.parse(process.argv[4]);
console.log(JSON.stringify({
  dates: values.map(value =>
    value.length === 10 ? coverageDate(value) : coverageDate(new Date(value).toISOString())),
  windows: windows.map(([start, end]) => readableWindow(start, end)),
}));
"""

# A window inside one year names the year once, inside one month names the
# month once, on one day is that date, and across years is two full dates.
WINDOWS = {
    "same_year": (("2026-08-25", "2026-09-07"), "25 Aug to 7 Sept 2026"),
    "same_month": (("2026-09-01", "2026-09-07"), "1 to 7 Sept 2026"),
    "same_day": (("2026-09-07", "2026-09-07"), "7 Sept 2026"),
    "across_years": (("2025-12-29", "2026-01-04"), "29 Dec 2025 to 4 Jan 2026"),
}


def test_the_export_writes_each_date_as_the_in_app_view_does(tmp_path):
    values = [f"2026-{month:02d}-07" for month in range(1, 13)]
    values += ["2026-09-01", "2026-09-10", "2026-12-31"]
    values += ["2026-09-03T10:00:00Z", "2026-09-08T00:30:00+02:00", "2026-09-07T23:59:59.999Z"]
    windows = [pair for pair, _ in WINDOWS.values()]
    script = tmp_path / "dates.mjs"
    script.write_text(VIEW_DATES, encoding="utf-8")
    run = subprocess.run(
        [
            "node",
            str(script),
            (APP / "frontend" / "src" / "askPresentation.js").as_uri(),
            json.dumps(values),
            json.dumps(windows),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    read = json.loads(run.stdout)
    view = read["dates"]
    assert view[8] == "7 Sept 2026"
    assert [ask_export._date_text(value) for value in values] == view
    assert read["windows"] == [text for _, text in WINDOWS.values()]
    assert [ask_export._window_text(start, end) for start, end in windows] == read["windows"]



# The view pairs each requirement id with its readable line through
# missingWorkItems (generalIntelligence.js, rendered by GeneralIntelligence.jsx
# under "What is still needed"). Each case is one the view tests name: the
# stored reply, several pairs around a known code, too few readable lines,
# repeated readable lines, repeated ids, a paired id repeated after the
# readable lines, and lists with nothing to pair.
VIEW_MISSING_WORK = """
import {missingWorkItems} from '%s';
const lists = JSON.parse(process.argv[2]);
console.log(JSON.stringify(lists.map(list => missingWorkItems(list))));
"""
Q = "Missing required evidence: "
MISSING_WORK_CASES = {
    "stored": REPLY["missing_work"],
    "pairs_around_a_code": [
        "req_za_trending_topics_sep_2026",
        "req_za_brand_mentions",
        "parent_source_unavailable",
        Q + "What was trending?",
        Q + "Which brands were named?",
    ],
    "too_few_readable_lines": ["req_orphan", "req_second", Q + "Only one question?"],
    "repeated_readable_lines": ["req_a", "req_b", Q + "Q", Q + "Q"],
    "repeated_ids": ["req_a", "req_a", Q + "Q1", Q + "Q2"],
    "id_repeated_after_readable": ["req_a", Q + "Q", "req_a"],
    "ids_without_readable_lines": ["req_a", "coverage_incomplete"],
    "codes_and_prose_only": ["retrieval_incomplete", "Challenge search incomplete: none planned."],
    "prose_before_readable": ["Not an id at all", Q + "Q"],
    "code_would_fill_the_count": ["coverage_incomplete", Q + "Q"],
}


def test_the_export_lists_what_is_still_needed_as_the_in_app_view_does(tmp_path):
    names = list(MISSING_WORK_CASES)
    script = tmp_path / "missing.mjs"
    script.write_text(
        VIEW_MISSING_WORK % (APP / "frontend" / "src" / "generalIntelligence.js").as_uri(),
        encoding="utf-8",
    )
    run = subprocess.run(
        ["node", str(script), json.dumps([MISSING_WORK_CASES[name] for name in names])],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    view = dict(zip(names, json.loads(run.stdout)))
    assert view["stored"] == [
        {"text": REPLY["missing_work"][1], "reference": "req_trending_topics_za"},
        {"text": REPLY["missing_work"][2], "reference": None},
    ]
    assert all(item["reference"] is None for item in view["repeated_ids"])
    for name in names:
        assert ask_export._missing_work_items(MISSING_WORK_CASES[name]) == view[name], name
        reply = copy.deepcopy(REPLY)
        reply["missing_work"] = MISSING_WORK_CASES[name]
        assert server_verdict(reply) == {"ok": True}, name
        for pdf in (False, True):
            html = ask_export.render_answer_html(detail_for(reply), RID, pdf=pdf)
            start = html.index("<h2>What is still needed</h2>")
            section = html[start : html.index("</section>", start)]
            shown = "".join(
                ask_export._p(item["text"])
                + (
                    ""
                    if item["reference"] is None
                    else ask_export._p("Requirement reference: " + item["reference"], "note")
                )
                for item in view[name]
            )
            assert section == "<h2>What is still needed</h2>" + shown, name


def _set(path, value):
    def change(reply):
        target = reply
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return change


def _pop(path):
    def change(reply):
        target = reply
        for key in path[:-1]:
            target = target[key]
        target.pop(path[-1])

    return change


def _both(*changes):
    def change(reply):
        for item in changes:
            item(reply)

    return change


def _unresolved(reply):
    reply["usage"].update(
        status="unresolved",
        reason="response_usage_unavailable",
        model_calls=None,
        input_tokens=None,
        output_tokens=None,
    )


def _self_parent(reply):
    reply["claims"][8]["parent_claim_ids"].append("c9")


def _loop(reply):
    reply["claims"][0]["parent_claim_ids"] = ["c9"]


def _ungrounded(reply):
    claim = reply["claims"][9]
    claim["parent_claim_ids"] = []


def _proposal_in_evidence(reply):
    reply["sections"][1]["claim_ids"].append("c12")
    reply["sections"][3]["claim_ids"] = []


def _reverse_sections(reply):
    reply["sections"].reverse()


def _span(start, end):
    return _both(
        _set(("receipts", 0, "quote_bindings", 0, "start"), start),
        _set(("receipts", 0, "quote_bindings", 0, "end"), end),
    )


# One stored reply changed one way at a time, across every check the Ask view
# makes. Each entry is (change, terminal error flag).
CORPUS = {
    "stored": (lambda reply: None, False),
    "stored_terminal": (lambda reply: None, True),
    "complete_terminal": (_set(("status",), "complete"), True),
    **{name: (change, False) for name, (change, _) in REVIEWED.items()},
    "extra_reply_key": (_set(("extra",), 1), False),
    "missing_reply_key": (_pop(("clarification",)), False),
    "wrong_version": (_set(("contract_version",), "general_cultural_question_v2"), False),
    "request_id_not_uuid": (_set(("request_id",), "not-a-uuid"), False),
    "request_id_upper": (_set(("request_id",), RID.upper()), False),
    "request_id_list": (_set(("request_id",), [RID]), False),
    "request_digest_short": (_set(("request_digest",), "ab"), False),
    "status_unknown": (_set(("status",), "done"), False),
    "as_of_offset": (_set(("as_of",), "2026-09-20T18:28:23+02:00"), False),
    "as_of_zero_offset": (_set(("as_of",), "2026-09-20T16:28:23+00:00"), False),
    "as_of_no_zone": (_set(("as_of",), "2026-09-20T16:28:23"), False),
    "as_of_hour_24": (_set(("as_of",), "2026-09-20T24:00:00Z"), False),
    "as_of_bad_day": (_set(("as_of",), "2026-02-30T10:00:00Z"), False),
    "as_of_trailing_newline": (_set(("as_of",), "2026-09-20T16:28:23Z\n"), False),
    "scope_extra_key": (_set(("resolved_scope", "extra"), None), False),
    "scope_upper_market": (_set(("resolved_scope", "market_scope"), ["ZA"]), False),
    "scope_no_market": (_set(("resolved_scope", "market_scope"), []), False),
    "scope_repeated_market": (_set(("resolved_scope", "market_scope"), ["za", "za"]), False),
    "scope_blank_client": (_set(("resolved_scope", "client_scope_id"), " \u00a0"), False),
    "scope_bom_client": (_set(("resolved_scope", "client_scope_id"), "\ufeff"), False),
    "scope_separator_client": (_set(("resolved_scope", "client_scope_id"), "\x1c"), False),
    "scope_blank_brand": (_set(("resolved_scope", "brand_config_id"), ""), False),
    "window_null": (_set(("window",), None), False),
    "window_reversed": (_set(("window", "start"), "2026-09-08"), False),
    "window_closed_not_bool": (_set(("window", "closed"), 1), False),
    "window_bad_day": (_set(("window", "end"), "2026-02-30"), False),
    "window_extra_key": (_set(("window", "extra"), 1), False),
    "window_closed_on_as_of_day": (_set(("window", "end"), "2026-09-20"), False),
    "window_open_on_as_of_day": (
        _both(_set(("window", "end"), "2026-09-20"), _set(("window", "closed"), False)),
        False,
    ),
    "snapshot_blank": (_set(("snapshot_id",), ""), False),
    "snapshot_null": (_set(("snapshot_id",), None), False),
    "limitation_blank": (_set(("limitations",), ["ok", " "]), False),
    "missing_work_not_list": (_set(("missing_work",), "x"), False),
    "clarification_blank": (_set(("clarification",), ""), False),
    "review_and_ready": (_set(("ready_for_downstream",), True), False),
    "review_not_bool": (_set(("review_required",), 1), False),
    "clarification_needed_without_text": (_set(("status",), "needs_clarification"), False),
    "unavailable_ready": (
        _both(
            _set(("status",), "unavailable"),
            _set(("review_required",), False),
            _set(("ready_for_downstream",), True),
        ),
        False,
    ),
    "usage_extra_key": (_set(("usage", "extra"), 1), False),
    "usage_status_unknown": (_set(("usage", "status"), "settled"), False),
    "usage_cost_short": (_set(("usage", "reserved_cost_usd"), "0.1"), False),
    "usage_negative_count": (_set(("usage", "input_tokens"), -1), False),
    "usage_float_count": (_set(("usage", "input_tokens"), 10989.0), False),
    "usage_fraction_count": (_set(("usage", "input_tokens"), 10989.5), False),
    "usage_bool_count": (_set(("usage", "output_tokens"), True), False),
    "usage_unsafe_count": (_set(("usage", "output_tokens"), 2**53), False),
    "usage_reason_when_resolved": (_set(("usage", "reason"), "response_usage_unavailable"), False),
    "usage_unresolved_partial": (_unresolved, False),
    "usage_unresolved_complete": (_both(_unresolved, _set(("status",), "complete")), False),
    "usage_unresolved_unknown_reason": (_both(_unresolved, _set(("usage", "reason"), "x")), False),
    "usage_unresolved_without_calls": (_both(_unresolved, _set(("usage", "call_receipt_ids"), [])), False),
    "usage_measured_without_receipts": (_set(("usage", "usage_receipt_ids"), []), False),
    "usage_fewer_calls_than_counted": (_set(("usage", "model_calls"), 3), False),
    "usage_no_calls": (
        _both(_set(("usage", "model_calls"), 0), _set(("usage", "usage_receipt_ids"), [])),
        False,
    ),
    "claims_not_list": (_set(("claims",), {}), False),
    "claim_repeated_id": (_set(("claims", 1, "claim_id"), "c1"), False),
    "receipt_blank_id": (_set(("receipts", 1, "receipt_id"), " "), False),
    "reading_not_record": (_set(("readings", 0), None), False),
    "receipt_label_zero": (_set(("receipts", 0, "citation_label"), "R0"), False),
    "receipt_label_repeated": (_set(("receipts", 1, "citation_label"), "R1"), False),
    "receipt_label_list": (_set(("receipts", 0, "citation_label"), ["R1"]), False),
    "receipt_kind_unknown": (_set(("receipts", 0, "kind"), "post"), False),
    "receipt_digest_short": (_set(("receipts", 0, "content_digest"), "ab"), False),
    "receipt_source_label_blank": (_set(("receipts", 0, "source_label"), ""), False),
    "receipt_market_null": (_set(("receipts", 0, "market"), None), False),
    "receipt_author_blank": (_set(("receipts", 0, "author"), ""), False),
    "receipt_url_number": (_set(("receipts", 0, "url"), 5), False),
    "receipt_limitations_text": (_set(("receipts", 0, "limitations"), "x"), False),
    "receipt_unknown_reading": (_set(("receipts", 0, "reading_ids"), ["gqrd_x"]), False),
    "receipt_content_without_row": (_set(("receipts", 0, "source_row_id"), None), False),
    "receipt_aggregate_without_row": (
        _both(_set(("receipts", 0, "kind"), "aggregate"), _set(("receipts", 0, "source_row_id"), None)),
        False,
    ),
    "receipt_collected_after_as_of": (_set(("receipts", 0, "collected_at"), "2026-09-20T16:28:24Z"), False),
    "receipt_collected_at_as_of": (_set(("receipts", 0, "collected_at"), "2026-09-20T16:28:23.050Z"), False),
    "receipt_collected_within_millisecond": (
        _set(("receipts", 0, "collected_at"), "2026-09-20T16:28:23.0509Z"),
        False,
    ),
    "receipt_collected_in_the_truncated_millisecond": (
        _both(
            _set(("as_of",), "2026-09-20T16:28:23.0509Z"),
            _set(("receipts", 0, "collected_at"), "2026-09-20T16:28:23.051Z"),
        ),
        False,
    ),
    "receipt_collected_bad": (_set(("receipts", 0, "collected_at"), "yesterday"), False),
    "receipt_published_null": (_set(("receipts", 0, "published_at"), None), False),
    "purpose_unknown": (_set(("receipts", 0, "evidence_purposes", 0, "evidence_purpose"), "proof"), False),
    "purpose_claim_not_citing": (_set(("receipts", 0, "evidence_purposes", 0, "claim_id"), "c2"), False),
    "purpose_extra_key": (_set(("receipts", 0, "evidence_purposes", 0, "extra"), 1), False),
    "purposes_not_list": (_set(("receipts", 0, "evidence_purposes"), None), False),
    "binding_claim_not_citing": (_set(("receipts", 0, "quote_bindings", 0, "claim_id"), "c2"), False),
    "binding_claim_unknown": (_set(("receipts", 0, "quote_bindings", 0, "claim_id"), "c99"), False),
    "binding_claim_list": (_set(("receipts", 0, "quote_bindings", 0, "claim_id"), ["c1"]), False),
    "binding_digest_other": (_set(("receipts", 0, "quote_bindings", 0, "content_digest"), "f" * 64), False),
    "binding_sha_upper": (
        lambda reply: reply["receipts"][0]["quote_bindings"][0].update(
            quote_sha256=reply["receipts"][0]["quote_bindings"][0]["quote_sha256"].upper()
        ),
        False,
    ),
    "binding_sha_other": (_set(("receipts", 0, "quote_bindings", 0, "quote_sha256"), "0" * 64), False),
    "binding_field_other": (_set(("receipts", 0, "quote_bindings", 0, "source_field"), "text"), False),
    "binding_extra_key": (_set(("receipts", 0, "quote_bindings", 0, "extra"), 1), False),
    "binding_empty_span": (_span(3, 3), False),
    "binding_float_span": (_span(0.0, 97.0), False),
    "binding_negative_start": (_span(-1, 97), False),
    "binding_end_past_excerpt": (_span(0, 10**4), False),
    "binding_excerpt_null": (_set(("receipts", 0, "excerpt"), None), False),
    "bindings_not_list": (_set(("receipts", 0, "quote_bindings"), {}), False),
    "bindings_empty": (_set(("receipts", 0, "quote_bindings"), []), False),
    "reading_extra_key": (_set(("readings", 0, "extra"), 1), False),
    "reading_value_record": (_set(("readings", 0, "value"), {}), False),
    "reading_value_text": (_set(("readings", 0, "value"), "24"), False),
    "reading_value_huge": (_set(("readings", 0, "value"), 10**400), False),
    "reading_unit_blank": (_set(("readings", 0, "unit"), ""), False),
    "reading_window_bad": (_set(("readings", 0, "window"), {"start": "x", "end": "y", "closed": True}), False),
    "reading_closed_late": (_set(("readings", 0, "window", "end"), "2026-09-20"), False),
    "reading_denominator_negative": (_set(("readings", 0, "denominator"), -1), False),
    "reading_denominator_bool": (_set(("readings", 0, "denominator"), False), False),
    "reading_denominator_zero": (_set(("readings", 0, "denominator"), 0), False),
    "reading_no_sources": (_set(("readings", 0, "source_receipt_ids"), []), False),
    "reading_repeated_source": (
        lambda reply: reply["readings"][0]["source_receipt_ids"].append(
            reply["readings"][0]["source_receipt_ids"][0]
        ),
        False,
    ),
    "claim_extra_key": (_set(("claims", 0, "extra"), 1), False),
    "claim_kind_unknown": (_set(("claims", 0, "kind"), "fact"), False),
    "claim_text_blank": (_set(("claims", 0, "text"), "\u2028"), False),
    "claim_unknown_receipt": (_set(("claims", 0, "receipt_ids"), ["gqctx_x"]), False),
    "claim_falsifier_blank": (_set(("claims", 0, "falsifier"), ""), False),
    "claim_observation_proposed_kind": (_set(("claims", 0, "kind"), "proposal"), False),
    "proposal_supported": (_set(("claims", 11, "support_state"), "derived"), False),
    "proposal_without_review": (_set(("review_required",), False), False),
    "proposal_in_evidence": (_proposal_in_evidence, False),
    "claim_self_parent": (_self_parent, False),
    "claim_loop": (_loop, False),
    "claim_ungrounded": (_ungrounded, False),
    "sections_not_list": (_set(("sections",), {}), False),
    "sections_reversed": (_reverse_sections, False),
    "section_null": (_set(("sections", 1), None), False),
    "section_text": (_set(("sections", 1), "evidence"), False),
    "section_extra_key": (_set(("sections", 0, "extra"), 1), False),
    "section_repeated_claim": (lambda reply: reply["sections"][1]["claim_ids"].append("c11"), False),
    "section_empty": (_set(("sections", 2, "claim_ids"), []), False),
    "sections_without_answer": (lambda reply: reply["sections"].pop(0), False),
    "answer_ungrounded": (
        _both(
            _set(("claims", 10, "parent_claim_ids"), []),
            _set(("sections", 0, "claim_ids"), ["c11"]),
        ),
        False,
    ),
}


def corpus():
    cases = []
    for name, (change, terminal) in CORPUS.items():
        reply = copy.deepcopy(REPLY)
        change(reply)
        cases.append((name, reply, terminal))
    return cases


def test_the_server_reads_every_stored_reply_as_the_in_app_view_does(tmp_path):
    cases = corpus()
    browser = browser_verdicts([[reply, terminal] for _, reply, terminal in cases], tmp_path)
    server = [server_verdict(reply, terminal) for _, reply, terminal in cases]
    differ = {
        name: {"browser": seen, "server": ours}
        for (name, _, _), seen, ours in zip(cases, browser, server)
        if seen != ours
    }
    assert differ == {}
    # The corpus reaches both verdicts and most of the failure names.
    assert {"ok": True} in browser
    fields = {item["field"] for item in browser if not item["ok"]}
    assert len(fields) >= 40


def test_the_export_refuses_every_stored_reply_the_in_app_view_withholds(tmp_path):
    cases = corpus()
    browser = browser_verdicts([[reply, terminal] for _, reply, terminal in cases], tmp_path)
    exported = {}
    for (name, reply, terminal), seen in zip(cases, browser):
        detail = detail_for(reply)
        if terminal:
            detail["response"]["error"] = True
        try:
            ask_export.render_answer_html(detail, RID)
            exported[name] = True
        except ask_export.AskExportRefused:
            exported[name] = False
        if not seen["ok"]:
            assert exported[name] is False, name
    assert exported["stored"] is True
