import json

import pytest

from core.brief import explain, job, title_purity
from core.brief.tests import test_title_purity as cases


def test_schema_valid_surrogate_response_is_contained_at_the_actual_job_boundary(monkeypatch):
    snapshot, pack = cases.reader(monkeypatch, n=3)
    pack["title_snapshot"] = snapshot
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 1000.0)
    malformed_reason = json.loads('"\\ud800"')
    assert title_purity.jsonschema.Draft202012Validator(title_purity.SCHEMA).is_valid({
        "verdict": "supported", "reason": malformed_reason, "member_checks": []})
    original = title_purity.digest
    attempts = []

    def counted(value):
        if isinstance(value, dict) and value.get("reason") == malformed_reason:
            attempts.append(value)
        return original(value)
    monkeypatch.setattr(title_purity, "digest", counted)

    def broken(out):
        out["reason"] = malformed_reason
    candidate = {"row": cases.ROW, "pack": pack, "market": "ZA", "rerun": None}
    control = job._explain_one(candidate, model=cases.MemberModel(), spent_before=0, d=cases.DAY,
                               model_call_guard=lambda: True)
    result = job._explain_one(candidate, model=cases.MemberModel(broken), spent_before=0, d=cases.DAY,
                              model_call_guard=lambda: True)
    assert result["reason"] is None and result["numbers_only"] is False
    assert result["title_written"] is None and result["title_majority"]["decision"] == "unknown"
    assert result["title_majority"]["N"] == 3 and result["title_majority"]["U"] == 3
    assert result["title_majority"]["title_hash"] == control["title_majority"]["title_hash"]
    assert result["title_majority"]["response_hash"] is None and len(attempts) == 1
    json.dumps(result["title_majority"], ensure_ascii=False).encode("utf-8")
    for key in ("explanation", "claims", "explanation_claim_ids", "specificity", "news_driven"):
        assert result[key] == control[key]
    assert [x for x in result["checks"] if x["rule"] != "title"] == [
        x for x in control["checks"] if x["rule"] != "title"]


@pytest.mark.parametrize("text, expected", [
    ("\U0001f9d1\U0001f3fd\u200d\U0001f4bb", "8590632f87f06f405ce795417da9ee45d5b668986106241f62199a9fac88fc9a"),
    ("e\u0301", "3d68ce21f2899a475713cdbe7562ba9bdb6b1dfde8af1f221bdff4a0935b53b2"),
    ("\U0001f9d1\U0001f3fd\u200d\U0001f4bb prefix e\u0301 and \"proof\" \u6f22\u5b57",
     "32b807687666c579bd9809cc9082bc951d28cc4d587f3d6ba0427390821cd15f"),
])
def test_valid_unicode_job_responses_keep_the_frozen_v2_digest(monkeypatch, text, expected):
    assert title_purity.digest(text) == expected
    snapshot, pack = cases.reader(monkeypatch, n=3)
    pack["title_snapshot"] = snapshot
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 1000.0)

    def valid(out):
        out["reason"] = text
    model = cases.MemberModel(valid)
    result = job._explain_one({"row": cases.ROW, "pack": pack, "market": "ZA", "rerun": None},
                              model=model, spent_before=0, d=cases.DAY, model_call_guard=lambda: True)
    assert result["reason"] is None and result["title_written"] is not None
    assert result["title_majority"]["decision"] == "pass" and len(model.calls) == 7
    assert model.calls[-1]["max_tokens"] == 400
