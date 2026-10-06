from copy import deepcopy
import json

import pytest

from core.agent.saved_findings import prepare_saved_ask_finding


RUN_ID = "r_saved_fixture"
RESULT_HASH = "sha256:" + "a" * 64


def _post(post_id, market="ZA", *, handle="@fixture", flags=None, source_market=None):
    return {
        "id": post_id,
        "platform": "x",
        "handle": handle,
        "url": f"https://example.invalid/{post_id}",
        "posted_at": "2026-10-01T09:00:00+02:00",
        "market": market,
        "text": f"Fixture post {post_id}.",
        "flags": [] if flags is None else flags,
        "source_market": source_market,
    }


def _claim(claim_id, text, post_id, *, check="pass", numbers=None):
    return {
        "id": claim_id,
        "check": check,
        "text": text,
        "label": "observed",
        "kind": "observation",
        "evidence_ids": [post_id],
        "numbers": numbers or [],
    }


def _record(answer_status="complete"):
    claims = [
        _claim("c1", "First checked fixture claim.", "post_1", numbers=[{
            "value": 2,
            "unit": "fixture posts",
            "query_id": "q_fixture_1",
            "run_id": RUN_ID,
            "result_hash": RESULT_HASH,
        }]),
        _claim("c2", "Second checked fixture claim.", "post_2"),
    ]
    return {
        "ask_id": "a_fixture_1",
        "question": "Which fixture pattern is present?",
        "parent_id": "a_fixture_parent",
        "market": "ZA",
        "skin_id": None,
        "status": "complete",
        "finished_at": "2026-10-01T10:00:00+02:00",
        "run": {
            "run_id": RUN_ID,
            "window": {"from": "2026-09-24", "to": "2026-09-30"},
            "notices": ["Fixture notice."],
            "source_status": [{"platform": "x", "route": "x/search", "status": "ok", "items": 2}],
        },
        "answer": {
            "status": answer_status,
            "as_of": "2026-10-01T09:59:00+02:00",
            "context": "Stored fixture context.",
            "short_answer": "Untrusted headline that must not be copied.",
            "gaps": [{"what": "Fixture gap.", "searched": "x/search", "why": "fixture"}],
            "claims": claims,
            "evidence": [_post("post_1"), _post("post_2", source_market="NG")],
        },
    }


def test_complete_answer_saves_only_admitted_claims_with_stored_provenance():
    record = _record()

    row = prepare_saved_ask_finding(record["ask_id"], record)

    envelope = json.loads(row["answer"])
    assert set(row) == {"finding_id", "question", "answer", "as_of", "claims", "valid_from", "valid_to", "status"}
    assert row["question"] == "Which fixture pattern is present?"
    assert envelope == {
        "schema_version": "saved-ask-finding-v1",
        "answer_text": "First checked fixture claim.\nSecond checked fixture claim.",
        "source_ask_id": "a_fixture_1",
        "source_run_id": RUN_ID,
        "parent_id": "a_fixture_parent",
        "answer_status": "complete",
        "context": "Stored fixture context.",
        "market": "ZA",
        "window": {"from": "2026-09-24", "to": "2026-09-30"},
        "notices": ["Fixture notice."],
        "gaps": [{"what": "Fixture gap.", "searched": "x/search", "why": "fixture"}],
        "source_status": [{"platform": "x", "route": "x/search", "status": "ok", "items": 2}],
        "evidence": [_post("post_1"), _post("post_2", source_market="NG")],
    }
    assert row["claims"] == [
        {"text": "First checked fixture claim.", "label": "observed", "item_ids": [],
         "evidence_post_ids": ["post_1"], "query_ids": ["q_fixture_1"], "run_ids": [RUN_ID],
         "result_hashes": [RESULT_HASH]},
        {"text": "Second checked fixture claim.", "label": "observed", "item_ids": [],
         "evidence_post_ids": ["post_2"], "query_ids": [], "run_ids": [RUN_ID], "result_hashes": []},
    ]
    assert row["as_of"] == "2026-10-01T09:59:00+02:00"
    assert row["valid_from"] == "2026-10-01T10:00:00+02:00"
    assert row["valid_to"] is None and row["status"] == "current"
    assert row["finding_id"].startswith("f_")
    assert "Untrusted headline" not in envelope["answer_text"]


def test_partial_answer_keeps_a_partial_qualifier_and_is_deterministic():
    record = _record("partial")

    first = prepare_saved_ask_finding(record["ask_id"], record)
    second = prepare_saved_ask_finding(record["ask_id"], deepcopy(record))

    assert first == second
    assert json.loads(first["answer"])["answer_text"] == (
        "Partial answer: First checked fixture claim.\nSecond checked fixture claim."
    )
    assert json.loads(first["answer"])["answer_status"] == "partial"


def test_finding_id_changes_when_ask_or_accepted_claim_content_changes():
    record = _record()
    original = prepare_saved_ask_finding(record["ask_id"], record)["finding_id"]
    other_ask = deepcopy(record)
    other_ask["ask_id"] = "a_fixture_2"
    changed_claim = deepcopy(record)
    changed_claim["answer"]["claims"][0]["text"] = "Changed checked fixture claim."

    assert prepare_saved_ask_finding(other_ask["ask_id"], other_ask)["finding_id"] != original
    assert prepare_saved_ask_finding(record["ask_id"], changed_claim)["finding_id"] != original


def test_no_answer_is_refused():
    record = _record("insufficient_evidence")
    record["answer"]["claims"] = []

    with pytest.raises(ValueError):
        prepare_saved_ask_finding(record["ask_id"], record)


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(status="failed"),
    lambda r: r.update(status="stopped"),
    lambda r: r.update(skin_id=""),
    lambda r: r.update(skin_id="skin_fixture"),
    lambda r: r.update(ask_id="a_other"),
    lambda r: r["answer"].update(status="insufficient_evidence"),
    lambda r: r["answer"]["claims"].pop(),
    lambda r: r["answer"]["claims"][1].update(check="cut"),
    lambda r: r["answer"]["evidence"][0].update(handle=""),
    lambda r: r["answer"]["evidence"][0].update(flags=["market_assumed"]),
    lambda r: r["answer"]["claims"][0].update(evidence_ids=["missing_post"]),
    lambda r: r["answer"]["evidence"][1].update(market="NG"),
    lambda r: r["answer"]["claims"][0]["numbers"][0].update(result_hash="invented"),
    lambda r: r["answer"]["claims"][0]["numbers"][0].update(run_id="r_other"),
    lambda r: r["answer"]["claims"][1].update(evidence_ids=["post_1"]),
])
def test_invalid_or_unsupported_answer_is_refused(mutate):
    record = _record()
    mutate(record)

    with pytest.raises(ValueError):
        prepare_saved_ask_finding("a_fixture_1", record)


def test_present_item_ids_are_copied_verbatim_and_malformed_ids_are_refused():
    record = _record()
    record["answer"]["claims"][0]["item_ids"] = ["item_fixture_1"]

    row = prepare_saved_ask_finding(record["ask_id"], record)
    assert row["claims"][0]["item_ids"] == ["item_fixture_1"]

    record["answer"]["claims"][0]["item_ids"] = ["item_fixture_1", 7]
    with pytest.raises(ValueError):
        prepare_saved_ask_finding(record["ask_id"], record)


def test_context_defaults_and_inputs_are_serialized_without_aliasing():
    record = _record()
    del record["run"]["notices"]
    del record["run"]["source_status"]
    del record["answer"]["gaps"]

    first = prepare_saved_ask_finding(record["ask_id"], record)
    expected_evidence = deepcopy(record["answer"]["evidence"])
    record["answer"]["evidence"][1]["source_market"] = "ZA"
    record["run"]["notices"] = ["Changed after producer call."]
    envelope = json.loads(first["answer"])

    assert envelope["notices"] == []
    assert envelope["source_status"] == []
    assert envelope["gaps"] == []
    assert envelope["evidence"] == expected_evidence


def test_missing_parent_and_context_are_preserved_as_null():
    record = _record()
    del record["parent_id"]
    del record["answer"]["context"]

    envelope = json.loads(prepare_saved_ask_finding(record["ask_id"], record)["answer"])

    assert envelope["parent_id"] is None
    assert envelope["context"] is None


@pytest.mark.parametrize("mutate", [
    lambda r: r["run"].update(window={"from": "2026-10-01", "to": "2026-09-30"}),
    lambda r: r["run"].update(window={"from": "yesterday", "to": "2026-09-30"}),
    lambda r: r["run"].update(notices=None),
    lambda r: r["run"].update(source_status=None),
    lambda r: r["answer"].update(gaps=None),
    lambda r: r["answer"]["evidence"][0].update(flags=None),
    lambda r: r["run"].update(notices=[float("nan")]),
])
def test_invalid_saved_context_or_evidence_is_refused(mutate):
    record = _record()
    mutate(record)

    with pytest.raises(ValueError):
        prepare_saved_ask_finding(record["ask_id"], record)
