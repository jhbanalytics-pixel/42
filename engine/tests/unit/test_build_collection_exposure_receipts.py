"""The R3 exposure receipt builder derives every claim from records, never from typed values."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from scripts.staging import build_collection_exposure_receipts as builder
from scripts.staging import issue_collection_exposure_receipts as issuer

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "configs" / "open_intelligence" / "collection_exposure_policy_r3.json"
SOURCE_SHA = "b" * 40
IMAGE_DIGEST = "sha256:" + "c" * 64
ISSUED_AT = datetime(2026, 9, 2, 15, 0, tzinfo=UTC)
WINDOW = tuple(date(2026, 8, 21) + timedelta(days=offset) for offset in range(14))


def _pipeline_rows(
    *,
    missing: tuple[tuple[date, str], ...] = (),
    no_youtube: tuple[tuple[date, str], ...] = (),
    no_socialcrawl: tuple[tuple[date, str], ...] = (),
):
    rows = []
    for day in WINDOW:
        for market in ("za", "ng", "ke"):
            status = "failed" if (day, market) in missing else "success"
            youtube_rows = 0 if (day, market) in no_youtube else 300
            rows.append(
                {
                    "run_day": day,
                    "market": market,
                    "status": status,
                    "youtube_rows": youtube_rows,
                    "socialcrawl_rows": 0 if (day, market) in no_socialcrawl else 900,
                    "socialcrawl_credits": 0 if (day, market) in no_socialcrawl else 210,
                }
            )
    return rows


def _copy_rows():
    return [
        {
            "copy_run_id": "dynamic_replay_source_copy_20260821_20260903_v1",
            "source_table": table,
            "source_set_digest": digest * 64,
        }
        for table, digest in (
            ("enriched_content", "1"),
            ("event_ledger", "2"),
            ("seed_candidates", "3"),
            ("seed_graph", "4"),
        )
    ]


def _runner(pipeline_rows, copy_rows):
    calls = []

    def run(sql: str):
        calls.append(sql)
        if "pipeline_runs" in sql:
            return pipeline_rows
        if "open_intelligence_source_copy_receipts_v1" in sql:
            return copy_rows
        raise AssertionError(sql)

    run.calls = calls
    return run


def _build(**overrides):
    kwargs = {
        "policy": json.loads(POLICY.read_text(encoding="utf-8")),
        "query_runner": _runner(_pipeline_rows(), _copy_rows()),
        "source_sha": SOURCE_SHA,
        "image_digest": IMAGE_DIGEST,
        "issuer_identity": builder.ISSUER_IDENTITY,
        "issued_at": ISSUED_AT,
    }
    kwargs.update(overrides)
    return builder.build_receipts(**kwargs)


def test_policy_file_names_every_family_with_a_stated_authority():
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    assert policy["run_id"] == issuer.R3_RUN_ID
    assert policy["window_start"] == issuer.R3_WINDOW_START.isoformat()
    assert policy["window_end"] == issuer.R3_WINDOW_END.isoformat()
    assert set(policy["families"]) == {
        "news",
        "search",
        "music",
        "youtube",
        "reddit",
        "short_video",
    }
    for name, family in policy["families"].items():
        quota = family["quota"]
        if name in {"reddit", "short_video"}:
            # Opened 4 Sep 2026 on Albert's word: both ride the SocialCrawl
            # phases the pipeline_runs record counts in socialcrawl_rows, against
            # the per-run credit breaker in configs/sources.yaml.
            assert quota["applicability"] == "metered"
            assert quota["unit"] == "socialcrawl_credits"
            assert quota["limit"] == 1860
            assert quota["record"]["column"] == "socialcrawl_rows"
            assert quota["record"]["usage_column"] == "socialcrawl_credits"
            assert "units_per_market_day" not in quota["record"]
            assert quota["record"]["authority"]
            assert family["collection_sources"] == ["socialcrawl"]
        elif name == "youtube":
            # Approved 3 Sep 2026: youtube rides the pipeline_runs record, which
            # holds every market-day of the window, against the connector's own
            # documented unit cost and the API's daily budget.
            assert quota["applicability"] == "metered"
            assert quota["unit"] == "youtube_data_api_units"
            assert quota["limit"] == 10000
            assert quota["record"]["column"] == "youtube_rows"
            assert quota["record"]["units_per_market_day"] == 406
            assert quota["record"]["authority"]
        else:
            assert quota["applicability"] == "unmetered"
            assert quota["authority"]
    assert {"ensemble", "brand24"} <= set(policy["excluded_families"])
    assert not {"youtube", "reddit", "short_video"} & set(policy["excluded_families"])


def test_builder_emits_fourteen_receipts_per_family_from_the_pipeline_record():
    receipts = _build()
    assert len(receipts) == 84
    keys = [(item["source_family"], item["exposure_date"]) for item in receipts]
    assert keys == sorted(keys)
    assert {family for family, _ in keys} == {
        "news",
        "search",
        "music",
        "youtube",
        "reddit",
        "short_video",
    }
    assert {day for _, day in keys} == set(WINDOW)
    for item in receipts:
        assert item["capture_complete"] is True
        assert item["quota_exhausted"] is False
        if item["source_family"] == "youtube":
            assert item["quota_applicability"] == "metered"
            assert item["quota_unit"] == "youtube_data_api_units"
            assert item["quota_limit"] == 10000
            assert item["quota_used"] == 406 * 3
        elif item["source_family"] in {"reddit", "short_video"}:
            assert item["quota_applicability"] == "metered"
            assert item["quota_unit"] == "socialcrawl_credits"
            assert item["quota_limit"] == 1860
            assert item["quota_used"] == 210 * 3
        else:
            assert item["quota_applicability"] == "unmetered"
            assert item["quota_unit"] is None
            assert item["quota_limit"] is None
            assert item["quota_used"] is None
        assert item["source_sha"] == SOURCE_SHA
        assert item["image_digest"] == IMAGE_DIGEST
        assert item["issuer_identity"] == builder.ISSUER_IDENTITY
        assert item["issued_at"] == ISSUED_AT
        assert len(item["source_copy_receipt_refs"]) == 4
        assert item["receipt_digest"] == issuer.exposure_receipt_digest(item)
    issuer.validate_exposure_receipt_set(receipts)


def test_a_market_day_without_a_success_row_is_not_capture_complete():
    missing = ((date(2026, 8, 27), "ng"),)
    receipts = _build(query_runner=_runner(_pipeline_rows(missing=missing), _copy_rows()))
    by_key = {(item["source_family"], item["exposure_date"]): item for item in receipts}
    assert by_key[("news", date(2026, 8, 27))]["capture_complete"] is False
    assert by_key[("news", date(2026, 8, 22))]["capture_complete"] is True
    assert by_key[("search", date(2026, 8, 27))]["capture_complete"] is False
    youtube = by_key[("youtube", date(2026, 8, 27))]
    assert youtube["capture_complete"] is False
    assert youtube["quota_used"] == 406 * 2
    assert by_key[("reddit", date(2026, 8, 27))]["capture_complete"] is False
    assert by_key[("short_video", date(2026, 8, 27))]["quota_used"] == 210 * 2


def test_a_success_day_with_no_socialcrawl_rows_is_not_collected_for_its_families():
    quiet = ((date(2026, 8, 29), "ng"),)
    receipts = _build(query_runner=_runner(_pipeline_rows(no_socialcrawl=quiet), _copy_rows()))
    by_key = {(item["source_family"], item["exposure_date"]): item for item in receipts}
    assert by_key[("youtube", date(2026, 8, 29))]["capture_complete"] is True
    for family in ("reddit", "short_video"):
        item = by_key[(family, date(2026, 8, 29))]
        assert item["capture_complete"] is False
        assert item["quota_used"] == 210 * 2
        assert item["quota_exhausted"] is False


def test_a_success_day_with_no_youtube_rows_is_not_collected_for_youtube():
    quiet = ((date(2026, 8, 30), "ke"),)
    receipts = _build(query_runner=_runner(_pipeline_rows(no_youtube=quiet), _copy_rows()))
    by_key = {(item["source_family"], item["exposure_date"]): item for item in receipts}
    assert by_key[("news", date(2026, 8, 30))]["capture_complete"] is True
    youtube = by_key[("youtube", date(2026, 8, 30))]
    assert youtube["capture_complete"] is False
    assert youtube["quota_used"] == 406 * 2
    assert youtube["quota_exhausted"] is False


def test_pipeline_record_sql_selects_the_metered_record_column():
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    sql = builder.pipeline_record_sql(policy)
    assert "status, socialcrawl_credits, socialcrawl_rows, youtube_rows FROM" in sql


def test_builder_refuses_a_metered_family_without_a_record():
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    del policy["families"]["youtube"]["quota"]["record"]
    with pytest.raises(builder.ReceiptBuildRefusal, match="no quota record"):
        _build(policy=policy)


def test_policy_digests_are_constant_per_family_and_differ_between_families():
    receipts = _build()
    by_family = {}
    for item in receipts:
        by_family.setdefault(item["source_family"], set()).add(
            (item["collection_policy_digest"], item["quota_authority_id"], item["config_digest"])
        )
    assert all(len(values) == 1 for values in by_family.values())
    assert len({next(iter(values))[0] for values in by_family.values()}) == 6
    assert len({next(iter(values))[1] for values in by_family.values()}) == 6
    assert len({next(iter(values))[2] for values in by_family.values()}) == 1


def test_builder_refuses_an_incomplete_copy_receipt_set_or_bad_identity():
    with pytest.raises(builder.ReceiptBuildRefusal, match="source-copy"):
        _build(query_runner=_runner(_pipeline_rows(), _copy_rows()[:3]))
    with pytest.raises(builder.ReceiptBuildRefusal, match="source_sha"):
        _build(source_sha="zz")
    with pytest.raises(builder.ReceiptBuildRefusal, match="image_digest"):
        _build(image_digest="sha256:short")


def test_rendered_artifact_round_trips_through_the_issuer_loader(tmp_path):
    receipts = _build()
    path = tmp_path / "receipts.json"
    path.write_bytes(builder.render_receipts(receipts))
    loaded = issuer._load_receipt_artifact(path)
    assert len(loaded) == 84
    assert loaded[0]["receipt_digest"] == receipts[0]["receipt_digest"]
    artifacts = issuer._build_execution_artifacts(loaded)
    assert set(artifacts) == {
        "issuer_contract",
        "source_copy_receipt_set",
        "vendor_quota_receipt",
        "config",
    }
