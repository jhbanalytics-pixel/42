from core.eval.demo_selection import select_pair
from core.eval.demo_scope import _DEMO_SOURCES


IOL_ID = next(iter(_DEMO_SOURCES["DEMO-05"]))
IOL_QUOTE = (
    "WhatsApp messages presented to the Madlanga Commission have detailed allegations that taxi "
    "bosses Joe “Ferrari” Sibanyoni and Bafana “King of the Sky” Sindane used threats, intimidation "
    "and the prospect of violent disruption to extract money from businessmen operating in Mpumalanga."
)


def _number_claim(value):
    return {
        "id": "c1",
        "text": f"The post has {value} views.",
        "kind": "observation",
        "label": "observed",
        "evidence_ids": ["e1"],
        "numbers": [
            {
                "value": value,
                "unit": "views",
                "query_id": "q30",
                "run_id": "source-run",
                "result_hash": "sha256:" + "a" * 64,
            }
        ],
    }


def _quote_claim(text, wording):
    return {
        "id": "c1",
        "text": wording,
        "kind": "observation",
        "label": "single_source",
        "evidence_ids": ["e1"],
        "quotes": [{"evidence_id": "e1", "text": text}],
    }


def _evidence(text="Bro is funny"):
    return {
        "id": "e1",
        "platform": "tiktok",
        "handle": "@creator",
        "url": "https://example.test/post/1",
        "posted_at": "2026-09-24T10:00:00Z",
        "market": "NG",
        "text": text,
    }


def _answer(claims, evidence=None, status="complete"):
    return {
        "status": status,
        "as_of": "2026-10-01T10:00:00Z",
        "short_answer": "Answer text that must not enter the handoff.",
        "claims": claims,
        "evidence": evidence if evidence is not None else [_evidence()],
        "so_what": [{"text": "Unrequested implication.", "claim_ids": ["c1"]}],
        "watch_next": [],
        "gaps": [],
        "context": "Private run context.",
    }


def _receipt(attempt, answer, run_id=None, question_id="DEMO-03", context=None):
    return {
        "question_id": question_id,
        "attempt": attempt,
        "raw_answer": answer,
        "context": context if context is not None else "Receipt context must stay out of the saved payload.",
        "run_id": run_id or f"run-{attempt}",
        "call_costs": {"usd": 0.01},
    }


def _admit_all(receipt, question_id):
    return {
        "admitted_claim_ids": [claim["id"] for claim in receipt["raw_answer"]["claims"]],
        "dropped": [],
        "safe": True,
        "reasons": [],
    }


def test_flipped_source_numbers_are_unstable_and_never_full_pass():
    first = _receipt(1, _answer([_number_claim(14680)]))
    second = _receipt(2, _answer([_number_claim(14608)]))

    result = select_pair("DEMO-03", first, second, assessor=_admit_all)

    assert result["selected_attempt"] == 1
    assert result["stability_status"] == "unstable"
    assert result["full_pass"] is False
    assert result["run_ids"] == {"1": "run-1", "2": "run-2"}
    assert all(value.startswith("sha256:") for value in result["raw_hashes"].values())


def test_unadmitted_uncited_claim_is_excluded_from_handoff_payload():
    supported = _number_claim(14680)
    unsupported = {
        "id": "c2",
        "text": "This post proves a national trend.",
        "kind": "interpretation",
        "label": "inferred",
        "evidence_ids": [],
    }
    first = _receipt(1, _answer([supported, unsupported]))
    second = _receipt(2, _answer([supported]))

    def assessor(receipt, question_id):
        if receipt["attempt"] == 1:
            return {
                "admitted_claim_ids": ["c1"],
                "dropped": [{"claim_id": "c2", "reason": "No source support"}],
                "safe": False,
                "reasons": ["One claim was dropped."],
            }
        return _admit_all(receipt, question_id)

    result = select_pair("DEMO-03", first, second, assessor=assessor)

    assert result["selected_attempt"] == 1
    assert result["rejections"]["1"] == [{"claim_id": "c2", "reason": "No source support"}]
    assert [claim["id"] for claim in result["saved_payload"]["claims"]] == ["c1"]
    assert result["stability_status"] == "unproven"
    assert "short_answer" not in result["saved_payload"]
    assert "so_what" not in result["saved_payload"]
    assert "context" not in result["saved_payload"]
    assert "watch_next" not in result["saved_payload"]


def test_refusal_does_not_outscore_a_supported_partial_answer():
    refusal = _receipt(1, _answer([], evidence=[], status="refused"))
    supported = _receipt(2, _answer([_number_claim(14680)], status="partial"))

    result = select_pair("DEMO-03", refusal, supported, assessor=_admit_all)

    assert result["selected_attempt"] == 2
    assert result["saved_payload"]["status"] == "partial"
    assert result["stability_status"] == "unproven"
    assert result["full_pass"] is False


def test_matching_source_quotes_are_stable_across_different_answer_wording():
    quote = "Bro is funny"
    first = _receipt(1, _answer([_quote_claim(quote, "The post says Bro is funny.")]), question_id="DEMO-01")
    second = _receipt(2, _answer([_quote_claim(quote, "The recorded line is Bro is funny.")]), question_id="DEMO-01")

    result = select_pair("DEMO-01", first, second, assessor=_admit_all)

    assert result["selected_attempt"] == 1
    assert result["stability_status"] == "stable"
    assert result["stability_reason"]
    assert result["full_pass"] is False
    assert result["l5_handoff"]["target"] == "L5"
    assert result["l5_handoff"]["persistence_owner"] == "W6"
    assert result["l5_handoff"]["persisted"] is False


def test_missing_attempt_preserves_available_receipt_but_stays_unproven():
    first = _receipt(1, _answer([_number_claim(14680)]))

    result = select_pair("DEMO-03", first, None, assessor=_admit_all)

    assert result["selected_attempt"] == 1
    assert result["run_ids"] == {"1": "run-1", "2": None}
    assert result["raw_hashes"]["1"].startswith("sha256:")
    assert result["raw_hashes"]["2"] is None
    assert result["stability_status"] == "unproven"
    assert result["full_pass"] is False


def test_duplicate_admitted_claim_ids_do_not_create_an_empty_selected_payload():
    claim = _number_claim(14680)
    duplicate = {**claim, "text": "Duplicate claim identifier with different wording."}
    receipt = _receipt(1, _answer([claim, duplicate]))

    result = select_pair("DEMO-03", receipt, None, assessor=_admit_all)

    assert result["selected_attempt"] is None
    assert result["saved_payload"] is None
    assert result["receipt_validity"]["1"] is False
    assert "admitted_claim_id_ambiguous" in result["receipt_reasons"]["1"]


def test_default_assessor_handoff_uses_frozen_demo05_quote_and_downgrades_dropped_narrative():
    claim_text = "The post reports allegations of threats, intimidation and violent disruption to extract money from businessmen in Mpumalanga."
    source = {
        "id": IOL_ID,
        "published_at": "2026-09-28T12:00:00+02:00",
        "text": IOL_QUOTE,
    }
    context = {
        "window_start": "2026-09-24",
        "window_end": "2026-09-30",
        "evidence": [{
            "post_id": IOL_ID,
            "published_at": "2026-09-28T12:00:00+02:00",
            "text": IOL_QUOTE,
        }],
    }
    answer = {
        "status": "complete",
        "as_of": "2026-10-01T10:00:00Z",
        "short_answer": "This paraphrase is deliberately outside the quoted source wording.",
        "claims": [{
            "id": "c1",
            "label": "observed",
            "text": claim_text,
            "evidence_ids": [IOL_ID],
            "quotes": [{"evidence_id": IOL_ID, "text": IOL_QUOTE}],
            "numbers": [],
        }],
        "evidence": [source],
        "so_what": [],
        "watch_next": [],
        "gaps": [],
    }
    first = _receipt(1, answer, run_id="fixture_demo05_attempt1", question_id="DEMO-05", context=context)
    second = _receipt(2, answer, run_id="fixture_demo05_attempt2", question_id="DEMO-05", context=context)

    result = select_pair("DEMO-05", first, second)

    assert result["run_ids"] == {"1": "fixture_demo05_attempt1", "2": "fixture_demo05_attempt2"}
    assert result["admitted_claim_ids"] == {"1": ["c1"], "2": ["c1"]}
    assert any(row["claim_id"] == "short_answer" for row in result["rejections"]["1"])
    assert result["saved_payload"]["status"] == "partial"
    assert [claim["id"] for claim in result["saved_payload"]["claims"]] == ["c1"]
    assert result["stability_status"] == "stable"
    assert "context" not in result["saved_payload"]
    assert "short_answer" not in result["saved_payload"]
