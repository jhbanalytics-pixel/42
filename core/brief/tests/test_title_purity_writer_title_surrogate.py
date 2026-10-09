import json

from core.brief import explain, job, title_purity
from core.brief.tests import test_title_purity as cases


def test_schema_valid_surrogate_writer_title_is_contained_at_the_actual_job_boundary(monkeypatch):
    snapshot, pack = cases.reader(monkeypatch, n=3)
    pack["title_snapshot"] = snapshot
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 1000.0)
    candidate = {"row": cases.ROW, "pack": pack, "market": "ZA", "rerun": None}
    control = job._explain_one(candidate, model=cases.MemberModel(), spent_before=0, d=cases.DAY,
                               model_call_guard=lambda: True)
    model = cases.MemberModel()
    model.drafts[0]["title"] += " " + json.loads('"\ud800"')
    assert title_purity.jsonschema.Draft202012Validator(explain.WRITER_SCHEMA).is_valid(model.drafts[0])
    result = job._explain_one(candidate, model=model, spent_before=0, d=cases.DAY, model_call_guard=lambda: True)
    assert result["reason"] is None and result["numbers_only"] is False and result["title_written"] is None
    audit = result["title_majority"]
    assert audit["decision"] == "unknown" and audit["N"] == audit["U"] == 3
    assert audit["title_hash"] is None and audit["request_hash"] is None and audit["response_hash"] is None
    assert "title" not in audit and len(model.calls) == 6
    json.dumps(audit, ensure_ascii=False).encode("utf-8")
    for key in ("explanation", "claims", "explanation_claim_ids", "specificity", "news_driven"):
        assert result[key] == control[key]
    assert [x for x in result["checks"] if x["rule"] != "title"] == [
        x for x in control["checks"] if x["rule"] != "title"]
