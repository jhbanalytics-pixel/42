import json
from dataclasses import replace
from datetime import UTC, date, datetime
from types import MappingProxyType

import pytest
from scripts.staging.replay_open_intelligence import _digest
from src.analysis.open_intelligence.candidates import CandidateInputs, Observation
from src.analysis.open_intelligence.pipeline import (
    EVIDENCE_COLUMNS_BY_TABLE,
    EvidenceProjection,
    SourceWindow,
    _projected_evidence_row,
)
from src.analysis.open_intelligence.source_provenance import (
    bind_source_provenance,
    encode_source_provenance,
    provenance_member_id,
    validate_source_provenance,
)

MEMBER = "za|keyword|rhythm"
OBSERVATIONS = (Observation("za", "rhythm", "keyword", ("seed1",), platforms=("news",)),)


def source_row(row_id, source="Example publisher", platform="web", content_type="article"):
    row = dict.fromkeys(EVIDENCE_COLUMNS_BY_TABLE["enriched_content"])
    row.update(
        id=row_id,
        market="za",
        source=source,
        platform=platform,
        content_type=content_type,
        collected_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
        published_at=datetime(2026, 9, 5, 12, tzinfo=UTC),
        url=f"https://example.invalid/{row_id}",
    )
    return row


def inputs(rows, *, requested=None, projected=None):
    rows = tuple(rows)
    snapshot = {
        "snapshot_id": "synthetic_source_v3",
        "captured_at": "2026-09-06T13:00:00Z",
        "rows_by_table": {"enriched_content": rows, "raw_content": ()},
    }
    snapshot["source_digest"] = _digest(snapshot)
    records = (
        tuple(_projected_evidence_row(row, "enriched_content") for row in rows)
        if projected is None
        else projected
    )
    projection = EvidenceProjection(
        CandidateInputs(),
        MappingProxyType({}),
        MappingProxyType({MEMBER: records}),
        MappingProxyType({MEMBER: ("seed1",)}),
        (),
        (),
        SourceWindow(
            date(2026, 8, 31),
            date(2026, 9, 6),
            ("za",),
            ("ogilvy-trends-v2.trends_v2_staging.enriched_content",),
            False,
        ),
    )
    return {
        "observations": OBSERVATIONS,
        "member_samples": {
            MEMBER: tuple(row["id"] for row in rows) if requested is None else requested
        },
        "projection": projection,
        "source_snapshot": snapshot,
    }


def test_mixed_actual_source_pairs_keep_their_row_bindings():
    rows = (
        source_row("rss1"),
        source_row("search1", "google_trends_rss", "google_search", "trend"),
        source_row("music1", "apple_music", "music", "chart_track"),
        source_row("google1", "youtube", "youtube", "video"),
        source_row("social1", "socialcrawl", "youtube", "video"),
    )
    kwargs = inputs(rows)
    result = bind_source_provenance(**kwargs)
    envelope = result[MEMBER]
    pairs = {(p["vendor_family"], p["channel_family"]): p for p in envelope["pairs"]}
    assert set(pairs) == {
        ("rss", "news"),
        ("google_trends", "search"),
        ("apple_music", "music"),
        ("google_youtube", "youtube"),
        ("socialcrawl", "youtube"),
    }
    assert pairs[("rss", "news")]["sample_refs"] == (
        {"row_id": "rss1", "row_digest": _digest(rows[0])},
    )
    assert envelope["coverage_basis"] == "sampled_record_support"
    assert envelope["resolution_state"] == "resolved"
    assert envelope["source_snapshot_digest"] == kwargs["source_snapshot"]["source_digest"]
    assert envelope["unresolved_sample_refs"] == ()
    validate_source_provenance(result, **kwargs)
    assert (
        json.loads(encode_source_provenance(envelope))["contract_version"]
        == "candidate_source_provenance_v1"
    )
    with pytest.raises(TypeError):
        pairs[("rss", "news")]["sample_refs"][0]["row_id"] = "changed"


def test_missing_and_ambiguous_samples_do_not_gain_source_support():
    row = source_row("duplicate")
    kwargs = inputs((row, dict(row)), requested=("duplicate", "missing"), projected=())
    envelope = bind_source_provenance(**kwargs)[MEMBER]
    assert envelope["resolution_state"] == "unavailable"
    assert envelope["pairs"] == ()
    assert envelope["unresolved_sample_refs"] == (
        {"row_id": "duplicate", "reason": "ambiguous"},
        {"row_id": "missing", "reason": "missing"},
    )


def test_resolved_and_missing_samples_remain_partial():
    kwargs = inputs((source_row("rss1"),), requested=("rss1", "missing"))
    envelope = bind_source_provenance(**kwargs)[MEMBER]
    assert envelope["resolution_state"] == "partial"
    assert len(envelope["pairs"]) == 1


@pytest.mark.parametrize(
    "mutation",
    ["market", "pair", "unrequested", "snapshot_digest", "row_bytes", "duplicate_projected"],
)
def test_mismatched_source_or_projection_is_refused(mutation):
    kwargs = inputs((source_row("rss1"),))
    projection = kwargs["projection"]
    row = projection.receipts_by_member[MEMBER][0]
    if mutation in {"market", "pair", "unrequested", "duplicate_projected"}:
        replacement = {
            "market": replace(row, market="ng"),
            "pair": replace(row, vendor_family="gdelt"),
            "unrequested": replace(row, row_id="unrequested"),
            "duplicate_projected": row,
        }[mutation]
        records = (row, row) if mutation == "duplicate_projected" else (replacement,)
        kwargs["projection"] = replace(projection, receipts_by_member={MEMBER: records})
    elif mutation == "snapshot_digest":
        kwargs["source_snapshot"]["source_digest"] = "0" * 64
    else:
        kwargs["source_snapshot"]["rows_by_table"]["enriched_content"][0]["url"] = (
            "https://example.invalid/changed"
        )
    with pytest.raises(ValueError, match="source_provenance"):
        bind_source_provenance(**kwargs)


def test_reordered_projection_has_identical_envelope_and_changed_binding_fails():
    kwargs = inputs((source_row("rss1"), source_row("rss2")))
    result = bind_source_provenance(**kwargs)
    projection = kwargs["projection"]
    kwargs["projection"] = replace(
        projection,
        receipts_by_member={MEMBER: tuple(reversed(projection.receipts_by_member[MEMBER]))},
    )
    assert bind_source_provenance(**kwargs) == result
    changed = json.loads(encode_source_provenance(result[MEMBER]))
    changed["pairs"][0]["sample_refs"][0]["row_digest"] = "0" * 64
    with pytest.raises(ValueError, match="source_provenance"):
        validate_source_provenance({MEMBER: changed}, **kwargs)


def test_unknown_source_pair_remains_unavailable():
    row = source_row("unknown")
    row.update(vendor_family="unapproved_vendor", channel_family="news")
    kwargs = inputs((row,), projected=())
    envelope = bind_source_provenance(**kwargs)[MEMBER]
    assert envelope["resolution_state"] == "unavailable"
    assert envelope["unresolved_sample_refs"] == (
        {"row_id": "unknown", "reason": "unsupported_identity"},
    )


def test_rehashed_snapshot_cannot_hide_projection_content_drift():
    kwargs = inputs((source_row("rss1"),))
    snapshot = kwargs["source_snapshot"]
    snapshot["rows_by_table"]["enriched_content"][0]["url"] = "https://example.invalid/changed"
    snapshot["source_digest"] = _digest(
        {key: value for key, value in snapshot.items() if key != "source_digest"}
    )
    with pytest.raises(ValueError, match="source_provenance"):
        bind_source_provenance(**kwargs)


def test_member_market_must_belong_to_projection_window():
    kwargs = inputs((source_row("rss1"),))
    kwargs["projection"] = replace(
        kwargs["projection"],
        source_window=replace(kwargs["projection"].source_window, markets=("ng",)),
    )
    with pytest.raises(ValueError, match="source_provenance"):
        bind_source_provenance(**kwargs)


def test_encoder_rejects_noncanonical_pair_spelling():
    envelope = json.loads(
        encode_source_provenance(bind_source_provenance(**inputs((source_row("rss1"),)))[MEMBER])
    )
    envelope["pairs"][0]["vendor_family"] = "RSS"
    with pytest.raises(ValueError, match="source_provenance"):
        encode_source_provenance(envelope)


@pytest.mark.parametrize("allow_raw", [False, True])
def test_raw_fallback_requires_its_declared_source_window(allow_raw):
    row = source_row("raw1")
    raw_row = {field: row[field] for field in EVIDENCE_COLUMNS_BY_TABLE["raw_content"]}
    projected = _projected_evidence_row(raw_row, "raw_content")
    kwargs = inputs((), requested=("raw1",), projected=(projected,))
    snapshot = kwargs["source_snapshot"]
    snapshot["rows_by_table"]["raw_content"] = (raw_row,)
    snapshot["source_digest"] = _digest(
        {key: value for key, value in snapshot.items() if key != "source_digest"}
    )
    if not allow_raw:
        with pytest.raises(ValueError, match="source_provenance"):
            bind_source_provenance(**kwargs)
        return
    projection = kwargs["projection"]
    kwargs["projection"] = replace(
        projection,
        source_window=replace(
            projection.source_window,
            source_tables=(
                "ogilvy-trends-v2.trends_v2_staging.enriched_content",
                "ogilvy-trends-v2.trends_v2_staging.raw_content",
            ),
        ),
    )
    envelope = bind_source_provenance(**kwargs)[MEMBER]
    assert envelope["pairs"][0]["source_table"] == "raw_content"
    assert envelope["pairs"][0]["sample_refs"][0]["row_digest"] == _digest(raw_row)


def test_record_collected_after_snapshot_cannot_supply_family_support():
    row = source_row("future")
    row["collected_at"] = datetime(2026, 9, 6, 14, tzinfo=UTC)
    kwargs = inputs((row,), projected=())
    envelope = bind_source_provenance(**kwargs)[MEMBER]
    assert envelope["resolution_state"] == "unavailable"
    assert envelope["pairs"] == ()


def test_old_same_id_record_does_not_make_current_window_ambiguous():
    current = source_row("rss1")
    old = {**current, "collected_at": datetime(2026, 8, 1, tzinfo=UTC)}
    kwargs = inputs(
        (old, current),
        requested=("rss1",),
        projected=(_projected_evidence_row(current, "enriched_content"),),
    )
    envelope = bind_source_provenance(**kwargs)[MEMBER]
    assert envelope["resolution_state"] == "resolved"
    assert envelope["pairs"][0]["sample_refs"] == (
        {"row_id": "rss1", "row_digest": _digest(current)},
    )


def test_unknown_channel_is_unavailable_but_incomplete_pair_is_conflict():
    unknown = source_row("unknown")
    unknown.update(vendor_family="rss", channel_family="not_a_channel")
    envelope = bind_source_provenance(**inputs((unknown,), projected=()))[MEMBER]
    assert envelope["unresolved_sample_refs"] == (
        {"row_id": "unknown", "reason": "unsupported_identity"},
    )
    unknown["vendor_family"] = None
    with pytest.raises(ValueError, match="source_provenance_conflict"):
        bind_source_provenance(**inputs((unknown,), projected=()))


def test_member_identifier_binds_exact_provenance_without_changing_legacy_value():
    envelope = bind_source_provenance(**inputs((source_row("rss1"),)))[MEMBER]
    legacy = "mem_" + "1" * 64
    identifier = provenance_member_id(legacy, envelope)
    assert identifier.startswith("mem_")
    assert len(identifier) == 68
    assert identifier != legacy
    assert provenance_member_id(legacy, envelope) == identifier
    changed = json.loads(encode_source_provenance(envelope))
    changed["source_snapshot_digest"] = "2" * 64
    assert provenance_member_id(legacy, changed) != identifier


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_as_of", "2026-09-07T12:00:00+00:00"),
        ("cutoff_date", "2026-09-05"),
        ("captured_at", "2026-09-06T23:00:00+00:00"),
        ("snapshot_contract_version", "unknown_production_version"),
    ],
)
def test_production_provenance_refuses_resealed_time_or_version_drift(field, value):
    kwargs = inputs((source_row("rss1"),))
    snapshot = kwargs["source_snapshot"]
    snapshot.update(
        snapshot_contract_version="open_intelligence_source_snapshot_v3_production_v1",
        cutoff_date="2026-09-06",
        source_as_of="2026-09-07T00:00:00+00:00",
        captured_at="2026-09-07T13:00:00+00:00",
    )
    snapshot[field] = value
    snapshot.pop("source_digest")
    snapshot["source_digest"] = _digest(snapshot)
    with pytest.raises(ValueError):
        bind_source_provenance(**kwargs)


def test_production_provenance_accepts_later_capture_with_cutoff_close_visibility():
    kwargs = inputs((source_row("rss1"),))
    snapshot = kwargs["source_snapshot"]
    snapshot.update(
        snapshot_contract_version="open_intelligence_source_snapshot_v3_production_v1",
        cutoff_date="2026-09-06",
        source_as_of="2026-09-07T00:00:00+00:00",
        captured_at="2026-09-07T13:00:00+00:00",
    )
    snapshot.pop("source_digest")
    snapshot["source_digest"] = _digest(snapshot)
    assert bind_source_provenance(**kwargs)[MEMBER]["resolution_state"] == "resolved"
