"""Profile construction is deterministic comparison work, not native admission."""

import importlib
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from uuid import UUID

import pytest
from pandas import Timestamp
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.historical_connector_contract import validate_requested_window


def inputs():
    return {
        "policy": {
            "contract_version": "historical_connector_policy_v1",
            "project": "ogilvy-trends-v2",
            "source_dataset": "trends_v2_staging",
            "snapshot_dataset": "intelligence_42_sources_staging",
            "relations": ["enriched_content", "raw_content"],
            "market_scope": ["ke", "ng", "za"],
            "collection_window": {
                "start": "2026-08-21T00:00:00.000000Z",
                "end_exclusive": "2026-09-09T00:00:00.000000Z",
            },
            "temporal_rules_digest": "a" * 64,
            "identity_policy_digest": "b" * 64,
            "allowed_purposes": ["historical_question_context"],
        },
        "client_scope_id": "42",
        "snapshot_as_of": datetime(2026, 9, 21, tzinfo=UTC),
        "observed_at": datetime(2026, 9, 21, 1, tzinfo=UTC),
        "source_schema_digests": {"enriched_content": "c" * 64, "raw_content": "d" * 64},
        "projection_version": "native_id_bound_v1",
    }


def module():
    return importlib.import_module("src.analysis.open_intelligence.historical_connector_profile")


def test_new_identity_precedes_dependent_names(monkeypatch):
    api = module()
    seen = []

    def identity():
        seen.append("id")
        return UUID("12345678-1234-4234-8234-123456789abc")

    original = api.validate_profile

    def check(profile, **kwargs):
        assert seen == ["id"]
        seen.append("profile")
        return original(profile, **kwargs)

    monkeypatch.setattr(api, "uuid4", identity)
    monkeypatch.setattr(api, "validate_profile", check)
    args = inputs()
    profile = api.build_historical_profile(**args)
    capture = "hc_12345678123442348234123456789abc"
    assert seen == ["id", "profile"]
    assert profile["capture_id"] == capture
    assert profile["profile_id"] == f"historical_connector_{capture}_v1"
    assert profile["policy_digest"] == canonical_digest(args["policy"])
    assert set(profile) == {
        "contract_version",
        "profile_id",
        "capture_id",
        "policy_digest",
        "client_scope_id",
        "market_scope",
        "snapshot_as_of",
        "relation_bindings",
        "projection_version",
        "temporal_rules_digest",
        "identity_policy_digest",
    }
    for binding in profile["relation_bindings"]:
        lane = binding["lane"]
        assert binding["source_table"] == f"ogilvy-trends-v2.trends_v2_staging.{lane}"
        assert (
            binding["destination_table"]
            == f"ogilvy-trends-v2.intelligence_42_sources_staging.historical_connector_{capture}_{lane}"
        )
        assert binding["snapshot_as_of"] == "2026-09-21T00:00:00.000000Z"


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-09-21T02:00:00+02:00",
        "2026-09-20T19:00:00-05:00",
        "2026-09-21T00:00:00Z",
        datetime(2026, 9, 21, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    ],
)
def test_equivalent_offsets_hash_identically(monkeypatch, stamp):
    api = module()
    monkeypatch.setattr(api, "uuid4", lambda: UUID(int=1))
    args = inputs()
    expected = api.build_historical_profile(**args)
    args["snapshot_as_of"] = stamp
    args["observed_at"] = "2026-09-21T03:00:00+02:00"
    actual = api.build_historical_profile(**args)
    assert actual == expected
    assert canonical_digest(actual) == canonical_digest(expected)


@pytest.mark.parametrize("field", ["snapshot_as_of", "observed_at"])
def test_nanosecond_objects_refuse_before_formatting_or_identity(monkeypatch, field):
    api = module()
    args = inputs()
    args[field] = Timestamp("2026-09-21T00:00:00.123456789Z")
    formatted, identities = [], []
    formatter, identity = api._format_timestamp, api.uuid4

    def format_value(value, code):
        formatted.append(value)
        return formatter(value, code)

    def new_identity():
        identities.append(True)
        return identity()

    monkeypatch.setattr(api, "_format_timestamp", format_value)
    monkeypatch.setattr(api, "uuid4", new_identity)
    with pytest.raises(ValueError, match="historical_timestamp_invalid"):
        api.build_historical_profile(**args)
    assert all(getattr(value, "nanosecond", 0) == 0 for value in formatted)
    assert identities == []


def test_exact_microsecond_objects_match_ordinary_datetime(monkeypatch):
    api = module()
    monkeypatch.setattr(api, "uuid4", lambda: UUID("12345678-1234-4234-8234-123456789abc"))
    args = inputs()
    args["snapshot_as_of"] = datetime(2026, 9, 21, microsecond=123456, tzinfo=UTC)
    expected = api.build_historical_profile(**args)
    args["snapshot_as_of"] = Timestamp("2026-09-21T02:00:00.123456000+02:00")
    args["observed_at"] = Timestamp("2026-09-21T01:00:00.000000000Z")
    actual = api.build_historical_profile(**args)
    assert actual == expected
    assert actual["snapshot_as_of"] == "2026-09-21T00:00:00.123456Z"
    assert canonical_digest(actual) == canonical_digest(expected)


def test_submicrosecond_future_cannot_collapse_to_observation():
    args = inputs()
    args["snapshot_as_of"] = Timestamp("2026-09-21T00:00:00.123456789Z")
    args["observed_at"] = Timestamp("2026-09-21T00:00:00.123456100Z")
    assert args["snapshot_as_of"] > args["observed_at"]
    with pytest.raises(ValueError, match="historical_timestamp_invalid"):
        module().build_historical_profile(**args)


def test_nanosecond_retry_cannot_collapse_to_original_identity():
    args = inputs()
    args["snapshot_as_of"] = datetime(2026, 9, 21, microsecond=123456, tzinfo=UTC)
    args["retry_profile"] = module().build_historical_profile(**args)
    args["snapshot_as_of"] = Timestamp("2026-09-21T00:00:00.123456001Z")
    with pytest.raises(ValueError, match="historical_timestamp_invalid"):
        module().build_historical_profile(**args)


def test_exact_microsecond_future_still_refuses():
    args = inputs()
    args["snapshot_as_of"] = Timestamp("2026-09-21T00:00:00.123457000Z")
    args["observed_at"] = Timestamp("2026-09-21T00:00:00.123456000Z")
    assert args["snapshot_as_of"] > args["observed_at"]
    with pytest.raises(ValueError):
        module().build_historical_profile(**args)


class FoldOffset(tzinfo):
    def utcoffset(self, dt):
        return timedelta(hours=-5 if dt.fold else -4)

    def dst(self, dt):
        return timedelta(hours=0 if dt.fold else 1)


def test_dst_fold_is_preserved_without_host_timezone():
    api = module()
    args = inputs()
    args["observed_at"] = "2026-11-02T00:00:00Z"
    stamps = []
    for fold in (0, 1):
        args["snapshot_as_of"] = datetime(2026, 11, 1, 1, 30, tzinfo=FoldOffset(), fold=fold)
        stamps.append(api.build_historical_profile(**args)["snapshot_as_of"])
    assert stamps == ["2026-11-01T05:30:00.000000Z", "2026-11-01T06:30:00.000000Z"]


class UnknownOffset(tzinfo):
    def utcoffset(self, dt):
        return None


@pytest.mark.parametrize(
    "stamp",
    [
        datetime(2026, 9, 21),
        "2026-09-21T00:00:00",
        datetime(2026, 9, 21, tzinfo=UnknownOffset()),
        "2026-09-21T00:00:00.1234567Z",
        "2026-09-21T00:00:00,1234567Z",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
        True,
        None,
    ],
)
def test_bad_timestamps_refused(stamp):
    api = module()
    args = inputs()
    args["snapshot_as_of"] = stamp
    with pytest.raises(ValueError):
        api.build_historical_profile(**args)


@pytest.mark.parametrize(
    "mutation",
    [
        "future",
        "extra_schema",
        "missing_schema",
        "invalid_hash",
        "bad_policy",
        "projection",
        "scope",
    ],
)
def test_bad_inputs_refused(mutation):
    api = module()
    args = inputs()
    if mutation == "future":
        args["snapshot_as_of"] += timedelta(days=1)
    elif mutation == "extra_schema":
        args["source_schema_digests"]["trend_analysis"] = "c" * 64
    elif mutation == "missing_schema":
        del args["source_schema_digests"]["raw_content"]
    elif mutation == "invalid_hash":
        args["source_schema_digests"]["raw_content"] = "A" * 64
    elif mutation == "bad_policy":
        args["policy"]["project"] = "other"
    elif mutation == "scope":
        args["client_scope_id"] = " 42"
    else:
        args["projection_version"] = "unknown"
    with pytest.raises(ValueError):
        api.build_historical_profile(**args)


def test_exact_retry_does_not_generate_identity(monkeypatch):
    api = module()
    args = inputs()
    original = api.build_historical_profile(**args)

    def forbidden():
        raise AssertionError("retry must retain its original identity")

    monkeypatch.setattr(api, "uuid4", forbidden)
    args["retry_profile"] = original
    args["snapshot_as_of"] = "2026-09-21T02:00:00+02:00"
    retried = api.build_historical_profile(**args)
    assert retried == original
    retried["relation_bindings"].clear()
    assert len(original["relation_bindings"]) == 2


@pytest.mark.parametrize("mutation", ["instant", "scope", "schema", "projection", "policy"])
def test_retry_conflicts_refused(mutation):
    api = module()
    args = inputs()
    args["retry_profile"] = api.build_historical_profile(**args)
    if mutation == "instant":
        args["snapshot_as_of"] += timedelta(minutes=1)
    elif mutation == "scope":
        args["client_scope_id"] = "other"
    elif mutation == "schema":
        args["source_schema_digests"]["raw_content"] = "e" * 64
    elif mutation == "projection":
        args["projection_version"] = "v3_absent_nullable_v1"
    else:
        args["policy"]["identity_policy_digest"] = "e" * 64
    with pytest.raises(ValueError, match="historical_capture_identity_conflict"):
        api.build_historical_profile(**args)


def test_year_end_overflow_reaches_translation_guard():
    args = inputs()
    with pytest.raises(ValueError, match="historical_window_invalid") as exc:
        validate_requested_window(
            {"start": "9999-12-31", "end": "9999-12-31"},
            policy=args["policy"],
            request_as_of="9999-12-31T23:59:59.999999Z",
            closed=False,
        )
    assert isinstance(exc.value.__cause__, OverflowError)
