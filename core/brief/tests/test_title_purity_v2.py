import copy
import hashlib
import json
from datetime import date

from core.brief import job, title_purity
from core.brief.tests import test_title_purity as cases
from core.brief.tests.test_brief_explain import make_pack, run
from core.brief.tests import test_brief_job as job_cases
from core.collect import chain


def test_absorption_cannot_reuse_support_for_an_older_source_version(monkeypatch):
    cases.reader(monkeypatch, n=3)
    pack = make_pack()
    incoming = copy.deepcopy(pack["evidence"].pop(1))
    snapshot = title_purity.read_snapshot(object(), cases.ROW, cases.DAY, "ZA", pack, core="core", agent="agent")
    assert snapshot["status"] == "complete"
    pack["title_snapshot"] = snapshot
    incoming["text"] += " Changed after the producer-content read."
    incoming["quote_text"] = incoming["text"]
    into = {"row": cases.ROW, "pack": pack, "also": [], "posts": {"tt_1", "tt_3"}}
    other = {"row": dict(cases.ROW, item_id="other"), "pack": {"evidence": [incoming]}, "posts": {"ig_2"}}
    job._absorb(into, other)
    result = run(cases.MemberModel(), pack=into["pack"], candidate=cases.ROW)
    assert result["reason"] is None and result["title_written"] is None
    assert result["title_majority"]["decision"] == "unknown"


def test_success_receipt_does_not_keep_the_pre_call_reason(monkeypatch):
    snapshot, pack = cases.reader(monkeypatch, n=13)
    pack["title_snapshot"] = snapshot
    result = run(cases.MemberModel(), pack=pack, candidate=cases.ROW)
    audit = result["title_majority"]
    assert audit["decision"] == "pass" and audit["response_hash"]
    assert audit.get("reason") != "title support check not run"


class CaptureClient:
    def __init__(self):
        self.wire_bytes = []

    def insert_rows_json(self, table, rows):
        size = len(json.dumps({"rows": [{"json": row} for row in rows]}, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
        self.wire_bytes.append(size)
        if size > 10_000_000:
            return [{"index": 0, "errors": [{"reason": "payloadTooLarge"}]}]
        return []


def test_permitted_unicode_workload_fits_the_actual_run_store_encoder():
    receipts = []
    text = "漢" * title_purity.SOURCE_CHARS
    for market in ("za", "ng", "ke"):
        for topic in range(10):
            ids = ["obs1_" + hashlib.md5(f"{market}:{topic}:{i}".encode()).hexdigest() for i in range(2000)]
            reference = {"cluster_id": f"20260928-{market}-{topic:03d}", "cluster_date": "2026-09-28",
                         "market": market, "item_id": hashlib.sha256(f"{market}:{topic}".encode()).hexdigest()}
            records = []
            for post_id in ids[:title_purity.SOURCE_LIMIT]:
                record = {"post_id": post_id, "text": text, "source_fields": {
                    "platform": "tiktok", "creator_id": post_id, "url": "https://example.test/" + post_id,
                    "published_at": "2026-09-28T00:00:00+02:00", "geo_market": market.upper(),
                    "geo_confidence": 0.9, "geo_source": "ext_region"}}
                records.append({**record, "content_hash": title_purity.digest(record)})
            snapshot = {"status": "complete", "N": len(ids), "member_ids": ids, "records": records,
                        "producer_plan_id": f"plan-{market}", "cluster_ref": reference,
                        "detect_run_id": "detect-fixture", "membership_hash": title_purity.digest({
                            "cluster": reference, "members": ids}), "content_read_at": ["2026-09-28T03:00:00Z"]}
            audit = title_purity.assess(snapshot, "Fixture title", {
                "verdict": "unsupported", "reason": "fixture", "member_checks": []},
                request_hash="fixture-request", model_id="fixture-model")
            receipts.append({"market": market.upper(), "item_id": reference["item_id"], **audit})
    counts = {"markets": 3, "cards": 30, "held": 0, "credits": 0, "model_usd": 0,
              "title_majority_receipts": receipts}
    client = CaptureClient()
    store = chain.BigQueryRunsStore(client, project="fixture", dataset="fixture")
    producer = chain.Run("fixture-brief", "brief", date(2026, 9, 28), "2026-09-28T03:00:00Z", runs=store)
    chain.finish(producer, "ok", counts)
    assert client.wire_bytes[0] < 1_000_000
    print("V2_ENCODED_RUN_BYTES", client.wire_bytes[0])
    assert all("records" not in receipt and "member_ids" not in receipt for receipt in receipts)


def test_a_receipt_byte_bound_drops_only_the_optional_title(monkeypatch):
    snapshot, pack = cases.reader(monkeypatch, n=13)
    pack["title_snapshot"] = snapshot
    monkeypatch.setattr(title_purity, "RECEIPT_WIRE_LIMIT", 1024, raising=False)
    result = run(cases.MemberModel(), pack=pack, candidate=cases.ROW)
    assert result["reason"] is None and result["title_written"] is None
    assert result["title_majority"]["decision"] == "unknown"


def test_span_receipts_recover_the_basis_only_when_source_hashes_match(monkeypatch):
    snapshot, pack = cases.reader(monkeypatch, n=3)
    pack["title_snapshot"] = snapshot
    audit = run(cases.MemberModel(), pack=pack, candidate=cases.ROW)["title_majority"]
    sources = {r["post_id"]: r for r in snapshot["records"]}
    for receipt in audit["member_receipts"]:
        source = sources[receipt["post_id"]]
        start, end = receipt["quote_span"]
        assert title_purity.digest(source["text"][start:end]) == receipt["quote_hash"]
        assert source["content_hash"] == receipt["content_hash"]
        assert "quote" not in receipt
    assert "records" not in audit and "member_ids" not in audit and "checker_response" not in audit


def test_run_receipt_bound_vetoes_title_before_public_projection(monkeypatch):
    candidate = {"market": "ZA", "row": cases.ROW}
    result = {"title_written": "Fixture title", "reason": None, "claims": ["unchanged"], "checks": [],
              "title_majority": {"decision": "pass", "N": 13, "S": 7, "R": 0, "U": 6}}
    monkeypatch.setattr(title_purity, "RUN_RECEIPT_WIRE_LIMIT", 64)
    receipts, omitted = title_purity.project_receipts([candidate], {id(candidate): result})
    assert receipts == [] and omitted == 1
    assert result["title_written"] is None and result["reason"] is None and result["claims"] == ["unchanged"]
    assert result["checks"][-1]["rule"] == "title"


def test_final_counts_bound_cannot_publish_an_unauditable_topic_title(monkeypatch):
    original = job._explain_one

    def audited(candidate, **kwargs):
        result = original(candidate, **kwargs)
        result.update(title_written="Fixture title", title_majority={"decision": "pass", "N": 3, "S": 3, "R": 0, "U": 0})
        return result
    monkeypatch.setattr(job, "_explain_one", audited)
    monkeypatch.setattr(title_purity, "COUNTS_WIRE_LIMIT", 1)
    con = job_cases.world(n=1, markets=("ZA",))
    con.execute("UPDATE core.item_state SET kind = 'topic'")
    con.execute("UPDATE core.cultural_map SET kind = 'topic'")
    result = job_cases.brief(con)
    cards = job_cases.all_cards(job_cases.payload(result, "ZA"))
    assert cards and all(card["title_written"] is None and card["explained"] for card in cards)
    assert "title_majority_receipts" not in result.counts
