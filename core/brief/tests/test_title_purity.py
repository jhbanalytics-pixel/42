import copy
import importlib
import json
import re
from datetime import date, datetime, timezone

import pytest

from core.brief import explain, job
from core.brief.tests import test_brief_job as job_tests
from core.brief.tests.test_brief_explain import FakeModel, good, make_pack, run
from core.brief.tests.test_brief_written_title import same_outcome, titled
from core.detect import sqlrun
from core.detect.tests import duck


DAY = date(2026, 9, 28)
ROW = {"item_id": "it_1", "kind": "topic", "title": "old topic", "metric_date": DAY,
       "run_id": "detect-1"}


def purity():
    try:
        return importlib.import_module("core.brief.title_purity")
    except ModuleNotFoundError:
        pytest.fail("title producer snapshot and majority validation are not implemented")


def reader(monkeypatch, n=13, mutate=None):
    module = purity()
    pack = make_pack()
    ids = [r["id"] for r in pack["evidence"]] + [f"p{i:03d}" for i in range(n - 3)]
    cid = "20260928-za-000"
    plan = [{"n": 1, "written_ids": [cid], "plan_id": "plan-1", "batch_index": 0,
             "checkpoint": {"market": "za", "batch_count": 1, "summary": {}},
             "batch": {"map_rows": [], "cluster_rows": [{"cluster_id": cid, "item_id": "it_1"}],
                       "member_rows": [{"cluster_id": cid, "post_id": i, "probability": 0.0} for i in ids]}}]
    written = [{"cluster_id": cid, "cluster_date": DAY, "market": "za", "item_id": "it_1", "post_id": i}
               for i in ids]
    by_id = {r["id"]: r for r in pack["evidence"]}
    contents = [{"post_id": i, "record_exists": True, "suppressed": False, "platform": "tiktok",
                 "creator_id": f"creator-{i}", "url": f"https://x/{i}",
                 "published_at": "2026-09-26T12:00:00+02:00", "content_read_at": "2026-09-28T13:00:00Z",
                 "quote_text": by_id.get(i, {}).get("text", "Shaya step at holiday braais")}
                for i in ids]
    if mutate:
        mutate(plan, written, contents)

    def query(client, sql, params, **kwargs):
        if "name: plan" in sql:
            return copy.deepcopy(plan)
        if "name: members" in sql:
            return copy.deepcopy(written)
        assert "name: content" in sql
        assert isinstance(params["post_ids"], str), "sqlrun accepts scalar parameters only"
        return copy.deepcopy([r for r in contents if r["post_id"] in json.loads(params["post_ids"])])

    monkeypatch.setattr(sqlrun, "query", query)
    return module.read_snapshot(object(), ROW, DAY, "ZA", pack, core="core", agent="agent"), pack


def test_a_topic_without_a_producer_snapshot_loses_only_its_written_title():
    result = run(FakeModel([titled()]), candidate=ROW)
    plain = run(FakeModel([good()]), candidate=ROW)
    assert result["title_written"] is None
    assert result["reason"] is None
    same_outcome(result, plain)
    assert result["checks"][-1]["rule"] == "title"


def test_full_producer_membership_is_not_the_sampled_pack(monkeypatch):
    snapshot, pack = reader(monkeypatch)
    assert snapshot["status"] == "complete"
    assert snapshot["N"] == 13 and len(snapshot["member_ids"]) == 13
    assert len(pack["evidence"]) == 3
    assert len(snapshot["records"]) == 12
    assert snapshot["producer_plan_id"] == "plan-1"
    assert snapshot["detect_run_id"] == "detect-1"


@pytest.mark.parametrize("case", ["missing_batch", "missing_cluster", "duplicate_written", "missing_member",
                                  "ambiguous", "wrong_date", "pooled", "conflicting_content"])
def test_incomplete_or_conflicting_snapshots_are_unknown(monkeypatch, case):
    def mutate(plan, written, contents):
        if case == "missing_batch":
            plan[0]["checkpoint"]["batch_count"] = 2
        elif case == "missing_cluster":
            plan[0]["written_ids"] = []
        elif case == "duplicate_written":
            written.append(dict(written[0]))
        elif case == "missing_member":
            written.pop()
        elif case == "ambiguous":
            plan[0]["batch"]["cluster_rows"].append({"cluster_id": "20260928-za-001", "item_id": "it_1"})
            plan[0]["written_ids"].append("20260928-za-001")
            plan[0]["n"] = 2
        elif case == "wrong_date":
            written[0]["cluster_date"] = "2026-09-27"
        elif case == "pooled":
            written[0]["market"] = "pan"
        else:
            next(x for x in contents if x["post_id"] == "tt_1")["quote_text"] = "changed source"
    snapshot, _ = reader(monkeypatch, mutate=mutate)
    assert snapshot["status"] == "unknown"


class MemberModel(FakeModel):
    def __init__(self, change=None):
        super().__init__([titled()])
        self.change = change

    def complete_json(self, **kwargs):
        out, usage = super().complete_json(**kwargs)
        if "member_checks" in kwargs["schema"]["properties"]:
            tail = kwargs["user"].split("Cluster members for individual title support:\n", 1)[1]
            members = json.loads(re.search(r"<untrusted_content>\n(.*?)\n</untrusted_content>", tail, re.S).group(1))
            out["member_checks"] = [{"post_id": x["post_id"], "verdict": "supported", "quote": x["text"]}
                                    for x in members]
            if self.change:
                self.change(out)
        return out, usage


def test_one_existing_title_call_can_prove_a_full_cluster_majority(monkeypatch):
    snapshot, pack = reader(monkeypatch)
    pack["title_snapshot"] = snapshot
    model = MemberModel()
    result = run(model, pack=pack, candidate=ROW)
    assert result["title_written"] is not None
    assert len(model.calls) == 7
    assert model.calls[-1]["max_tokens"] == explain.SUPPORT_MAX_TOKENS == 400
    audit = result["title_majority"]
    assert audit["N"] == 13 and audit["S"] == 12 and audit["U"] == 1
    assert audit["decision"] == "pass" and audit["request_hash"] and audit["response_hash"]
    assert len(audit["member_receipts"]) == 12


@pytest.mark.parametrize("case", ["quote", "id", "duplicate", "missing", "aggregate"])
def test_malicious_or_incomplete_member_receipts_never_manufacture_support(monkeypatch, case):
    snapshot, pack = reader(monkeypatch, n=3)
    pack["title_snapshot"] = snapshot

    def change(out):
        if case == "quote":
            for x in out["member_checks"]:
                x["quote"] = "not in source"
        elif case == "id":
            out["member_checks"][0]["post_id"] = "outside-plan"
        elif case == "duplicate":
            out["member_checks"].append(dict(out["member_checks"][0]))
        elif case == "missing":
            out["member_checks"] = []
        else:
            out["verdict"] = "unsupported"
    result = run(MemberModel(change), pack=pack, candidate=ROW)
    assert result["title_written"] is None and result["reason"] is None
    assert result["title_majority"]["decision"] != "pass"


def test_suppressed_and_missing_sources_stay_in_the_denominator(monkeypatch):
    def mutate(plan, written, contents):
        contents[0]["suppressed"] = True
        contents[1]["record_exists"] = False
    snapshot, pack = reader(monkeypatch, n=3, mutate=mutate)
    assert snapshot["N"] == 3 and len(snapshot["records"]) == 1
    pack["title_snapshot"] = snapshot
    result = run(MemberModel(), pack=pack, candidate=ROW)
    assert result["title_written"] is None
    assert result["title_majority"]["N"] == 3 and result["title_majority"]["S"] == 1


def test_an_optional_validation_error_cannot_hold_the_explanation(monkeypatch):
    snapshot, pack = reader(monkeypatch, n=3)
    pack["title_snapshot"] = snapshot
    module = purity()

    def fail(*args, **kwargs):
        raise ValueError("invalid optional receipt")
    monkeypatch.setattr(module, "assess", fail)
    result = run(MemberModel(), pack=pack, candidate=ROW)
    assert result["reason"] is None and result["title_written"] is None
    assert result["checks"][-1]["rule"] == "title"


def test_forged_source_hashes_cannot_back_a_title(monkeypatch):
    snapshot, pack = reader(monkeypatch, n=3)
    snapshot["records"][0]["content_hash"] = "forged"
    pack["title_snapshot"] = snapshot
    result = run(MemberModel(), pack=pack, candidate=ROW)
    assert result["reason"] is None and result["title_written"] is None


def test_the_existing_title_cap_leaves_a_private_unknown_receipt(monkeypatch):
    snapshot, pack = reader(monkeypatch, n=3)
    pack["title_snapshot"] = snapshot
    model = MemberModel()
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 1000.0 if len(model.calls) < 6 else 0.0)
    result = run(model, pack=pack, candidate=ROW)
    assert result["reason"] is None and result["title_written"] is None
    assert len(model.calls) == 6
    assert result["title_majority"]["decision"] == "unknown"
    assert result["title_majority"]["response_hash"] is None


def test_private_receipts_reach_run_counts_without_public_card_fields(monkeypatch):
    original = job._explain_one

    def receipt(cand, **kwargs):
        result = original(cand, **kwargs)
        result["title_majority"] = {"decision": "unknown", "N": 13, "S": 0, "R": 0, "U": 13}
        return result
    monkeypatch.setattr(job, "_explain_one", receipt)
    result = job_tests.brief(job_tests.world(n=1, markets=("ZA",)))
    assert result.counts["title_majority_receipts"]
    for card in job_tests.all_cards(job_tests.payload(result, "ZA")):
        assert "title_majority" not in card and "member_receipts" not in card


def test_real_snapshot_sql_uses_first_ready_plan_and_preserves_the_complete_denominator():
    module = purity()
    con = duck.connect(views=False)
    con.execute("CREATE OR REPLACE VIEW core.v_suppressed_creators AS SELECT 'hidden' AS creator_id")
    pack = make_pack()
    ids = [r["id"] for r in pack["evidence"]] + [f"p{i:03d}" for i in range(10)]
    cid = "20260928-za-000"
    for index, plan_id in enumerate(("first-plan", "later-plan")):
        members = ids if index == 0 else ids[:2]
        stamp = datetime(2026, 9, 28, index + 1, tzinfo=timezone.utc)
        counts = {"market": "za", "batch_count": 1, "summary": {}, "batch_index": -1}
        duck.load(con, "agent.runs", [{"run_id": plan_id, "stage": "understand_cluster_plan", "run_date": DAY,
                                       "status": "ready", "started_at": stamp, "counts": json.dumps(counts)}])
        counts.update(batch_index=0, map_rows=[], cluster_rows=[{"cluster_id": cid, "item_id": "it_1"}],
                      member_rows=[{"cluster_id": cid, "post_id": i, "probability": 0.0} for i in members])
        duck.load(con, "agent.runs", [{"run_id": plan_id, "stage": "understand_cluster_plan", "run_date": DAY,
                                       "status": "batch", "started_at": stamp, "counts": json.dumps(counts)}])
    duck.load(con, "core.clusters", [{"cluster_date": DAY, "cluster_id": cid, "market": "za", "item_id": "it_1"}])
    duck.load(con, "core.cluster_members", [{"cluster_id": cid, "post_id": i, "probability": 0.0} for i in ids])
    by_id = {r["id"]: r for r in pack["evidence"]}
    for i in ids[:-1]:
        duck.load(con, "core.posts", [{"post_id": i, "platform": "tiktok", "creator_id": "hidden" if i == "tt_3" else i,
                                      "post_date": DAY, "published_at": datetime(2026, 9, 28, tzinfo=timezone.utc),
                                      "text": by_id.get(i, {}).get("text", "Shaya step at holiday braais")}])
    client = duck.Client(con)
    snapshot = module.read_snapshot(client, ROW, DAY, "ZA", pack, core="core", agent="agent")
    assert snapshot["status"] == "complete", snapshot
    assert snapshot["producer_plan_id"] == "first-plan" and snapshot["N"] == 13
    assert "tt_3" in snapshot["member_ids"] and "tt_3" not in {r["post_id"] for r in snapshot["records"]}
    assert all("LIMIT" not in sql.upper() for sql in client.sql)


@pytest.mark.parametrize("supported", [6, 7])
def test_half_is_not_enough_for_the_full_thirteen_members(monkeypatch, supported):
    snapshot, pack = reader(monkeypatch)
    pack["title_snapshot"] = snapshot

    def bound(out):
        out["member_checks"] = out["member_checks"][:supported]
    result = run(MemberModel(bound), pack=pack, candidate=ROW)
    assert (result["title_written"] is not None) == (supported == 7)
    assert result["title_majority"]["N"] == 13
    assert result["title_majority"]["U"] == 13 - supported
