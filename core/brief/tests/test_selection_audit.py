"""Same-read selection omissions, with fake warehouse rows and no evidence or model calls."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from core.brief import job


DAY = date(2026, 9, 20)


def selected_rows(total=150, *, eligible=True, corrupt=False):
    items = [{"item_id": f"topic-{n:03}", "label": f"Topic {n}", "canonical_key": f"topic:{n}",
              "map_kind": "topic", "sql_rank": n, "market_scope": "global" if n == 149 else "market"}
             for n in range(1, total + 1)]
    snapshot = {"detect_run_id": "detect-fixture", "detect_run_count": 1, "total_count": total,
                "eligible_count": (999 if corrupt else total if eligible else 0),
                "items": items if eligible else []}
    return [{"item_id": item["item_id"], "label": item["label"], "canonical_key": item["canonical_key"],
             "map_kind": "topic", "kind": "topic", "eligible": eligible, "run_id": "detect-fixture",
             "_selection_sql_rank": item["sql_rank"], "_selection_market_scope": item["market_scope"],
             "_selection_snapshot": json.dumps(snapshot) if n == 0 else None}
            for n, item in enumerate(items[:job.POOL])]


def candidates(monkeypatch, rows, *, g1=(), deadline=False):
    prepared, calls, audits = [], [], {}

    class Client:
        def query(self, sql, job_config=None):
            market = next(p.value for p in job_config.query_parameters if p.name == "market")
            calls.append((sql, market))
            result = rows.get(market, []) if isinstance(rows, dict) else rows if market == "NG" else []
            return SimpleNamespace(job_id=f"selection-{market}", result=lambda: result)

    def prepare(client, day, market, row, **kwargs):
        prepared.append(dict(row))
        return {"market": market, "row": row}

    monkeypatch.setattr(job, "_prepare", prepare)
    monkeypatch.setattr(job, "_gate", lambda cand, result: SimpleNamespace(
        rule="G1" if cand["row"]["item_id"] in g1 or (cand["market"], cand["row"]["item_id"]) in g1 else None))
    monkeypatch.setattr(job, "PACK_WORKERS", 1)
    for name in ("FORCE_RERUN", "RUN_DATE", "BRIEF_RECOVERY_UNTIL"):
        monkeypatch.delenv(name, raising=False)
    now = datetime(2026, 9, 20, 7, tzinfo=job.SAST)
    by_market = job._candidates(Client(), DAY, build_ctx=None, campaign_hashtags=[],
        political_terms={m: [] for m in job.MARKETS}, core="core", agent="agent",
        hidden=(set(), set(), set()), selection_audits=audits,
        chain=SimpleNamespace(past_deadline=lambda *args: deadline), clock=lambda: now)
    return by_market, prepared, calls, audits


def test_rank_149_and_rank_18_have_separate_actual_omission_reasons(monkeypatch):
    by_market, prepared, calls, audits = candidates(monkeypatch, selected_rows())
    block = audits["NG"]["not_assessed"]
    items = {item["item_id"]: item for item in block["items"]}
    assert len(prepared) == 10 and len(by_market["NG"]) == 10 and len(calls) == 3
    assert not any(key.startswith("_selection") for row in prepared for key in row)
    assert block["detect_run_id"] == "detect-fixture" and block["count"] == 140
    assert items["topic-149"]["reason"] == "outside_candidate_pool"
    assert items["topic-149"]["sql_rank"] == 149 and items["topic-149"]["pool_rank"] is None
    assert items["topic-149"]["market_scope"] == "global"
    assert items["topic-018"]["reason"] == "judged_limit_reached" and items["topic-018"]["pool_rank"] == 18
    receipt = audits["NG"]["selection_receipt"]
    assert receipt["job_id"] == "selection-NG" and len(receipt["result_sha256"]) == 64
    assert receipt["parameters"] == {"d": DAY.isoformat(), "market": "NG"}
    assert receipt["preparation"]["taken"] == receipt["preparation"]["judged"] == 10


def test_g1_backfill_preserves_ten_non_g1_preparations(monkeypatch):
    by_market, prepared, _, audits = candidates(monkeypatch, selected_rows(),
        g1={f"topic-{n:03}" for n in range(1, 6)})
    assert len(prepared) == 15 and sum(c["decision"].rule != "G1" for c in by_market["NG"]) == 10
    assert audits["NG"]["not_assessed"]["count"] == 135
    assert not {row["item_id"] for row in prepared} & {
        item["item_id"] for item in audits["NG"]["not_assessed"]["items"]}


def test_actual_preparation_deadline_is_recorded_without_labelling_prepared_items(monkeypatch):
    _, prepared, _, audits = candidates(monkeypatch, selected_rows(),
        g1={f"topic-{n:03}" for n in range(1, 6)}, deadline=True)
    assert len(prepared) == 10
    item = next(item for item in audits["NG"]["not_assessed"]["items"] if item["item_id"] == "topic-018")
    assert item["reason"] == "deadline_reached"
    stop = audits["NG"]["selection_receipt"]["preparation"]
    assert stop["cutoff_basis"] == "deadline" and stop["judged"] == 5 and stop["observed_at"]


@pytest.mark.parametrize("rows", [[], selected_rows(corrupt=True)])
def test_missing_or_incomplete_snapshot_stays_unknown(monkeypatch, rows):
    _, prepared, _, audits = candidates(monkeypatch, rows)
    assert audits["NG"]["not_assessed"] is None
    assert len(prepared) == min(10, len(rows))


def test_successful_complete_snapshot_with_no_eligible_omissions_is_empty_not_unknown(monkeypatch):
    _, prepared, _, audits = candidates(monkeypatch, selected_rows(total=1, eligible=False))
    assert len(prepared) == 1
    assert audits["NG"]["not_assessed"]["count"] == 0
    assert audits["NG"]["not_assessed"]["items"] == []


def test_shared_deadline_does_not_become_the_cutoff_basis_of_a_market_already_at_its_limit(monkeypatch):
    _, _, _, audits = candidates(monkeypatch, {"ZA": selected_rows(total=20), "NG": selected_rows(total=20)},
        g1={("NG", f"topic-{n:03}") for n in range(1, 6)}, deadline=True)
    za = audits["ZA"]["selection_receipt"]["preparation"]
    ng = audits["NG"]["selection_receipt"]["preparation"]
    assert za["exit_reason"] == "judged_limit_reached" and za["judged"] == 10
    assert za["cutoff_basis"] is None
    assert ng["exit_reason"] == "deadline_reached" and ng["judged"] == 5
    assert ng["cutoff_basis"] == "deadline"


@pytest.mark.parametrize("field", ["label", "canonical_key", "map_kind"])
def test_malformed_outside_pool_names_make_audit_unknown_without_changing_preparation(monkeypatch, field):
    rows = selected_rows()
    snapshot = json.loads(rows[0]["_selection_snapshot"])
    snapshot["items"][-1][field] = {"unexpected": "object"}
    rows[0]["_selection_snapshot"] = json.dumps(snapshot)
    by_market, prepared, _, audits = candidates(monkeypatch, rows)
    assert len(prepared) == len(by_market["NG"]) == 10
    assert audits["NG"]["not_assessed"] is None


@pytest.mark.parametrize("total", [0, 2])
def test_shared_deadline_does_not_relabel_an_empty_or_exhausted_market(monkeypatch, total):
    _, _, _, audits = candidates(monkeypatch, {"ZA": selected_rows(total=total), "NG": selected_rows(total=20)},
        g1={("NG", f"topic-{n:03}") for n in range(1, 6)}, deadline=True)
    za = audits["ZA"]["selection_receipt"]["preparation"]
    assert za["exit_reason"] == "pool_exhausted"
    assert za["cutoff_basis"] is None
    assert audits["NG"]["selection_receipt"]["preparation"]["cutoff_basis"] == "deadline"
