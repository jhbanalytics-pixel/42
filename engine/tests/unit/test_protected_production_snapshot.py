import copy
import importlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.production_snapshot import _serialize_plan
from src.analysis.open_intelligence.production_snapshot_tables import (
    build_snapshot_plan,
    derive_protected_creation_statements,
)

from tests.unit.test_production_snapshot_tables import source_metadata_bytes

NOW = datetime(2026, 9, 8, 1, tzinfo=UTC)
CUTOFF = date(2026, 9, 7)


def subject():
    return importlib.import_module("scripts.staging.capture_protected_production_snapshot")


def artifacts():
    metadata = source_metadata_bytes()
    plan = build_snapshot_plan(CUTOFF, now=NOW, source_metadata=metadata)
    return {
        "capture_contract": (
            Path(__file__).parents[1]
            / "fixtures/open_intelligence/protected_source_snapshot_contract.md"
        ).read_bytes(),
        "capture_plan": canonical_bytes(
            {
                "contract_version": "open_intelligence_protected_capture_plan_v1",
                "cutoff_date": CUTOFF.isoformat(),
                "client_scope_id": "ogilvy_default",
                "market_scope": ["ke", "ng", "za"],
                "snapshot_plan": _serialize_plan(plan),
                "creation_statements": derive_protected_creation_statements(plan),
            }
        ),
        "source_metadata": metadata,
        "recovery_context": b"null",
        "storage_policy": canonical_bytes(
            {
                "contract_version": "open_intelligence_source_capture_storage_v1",
                "allowance_id": "source_capture_20260907_20260908_v1",
                "bucket": "ogilvy-trends-v2-oi-source-artifacts-staging",
                "input_prefix": "inputs/",
                "output_prefix": "captures/",
                "snapshot_retention_days": 90,
                "object_retention_days": 90,
                "max_source_logical_bytes": 5368709120,
                "max_artifact_bytes": 536870912,
                "reserved_micro_usd_per_cutoff": 500000,
                "price_review": {
                    "reviewed_at": (NOW - timedelta(minutes=1)).isoformat(),
                    "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                    "pricing_sources": ["https://cloud.google.com/bigquery/pricing"],
                    "assumptions": ["Synthetic complete cycle pricing fixture."],
                    "maximum_cycle_cost_micro_usd": 400000,
                },
            }
        ),
    }


def test_exact_approved_inputs_preserve_structural_plan_and_bound_expiring_ddl():
    values = artifacts()
    result = subject()._validate_inputs(values, CUTOFF, "initial", NOW)
    assert result["plan"].cutoff_date == CUTOFF
    assert result["recovery"] is None
    assert canonical_bytes(result["envelope"]) == values["capture_plan"]
    assert result["policy"]["reserved_micro_usd_per_cutoff"] == 500000


@pytest.mark.parametrize("mutation", ["contract", "ddl", "cutoff", "markets", "money", "expiry"])
def test_changed_bound_inputs_refuse_before_runtime_access(mutation):
    import json

    values = artifacts()
    if mutation == "contract":
        values["capture_contract"] += b"\n"
    elif mutation in {"ddl", "cutoff", "markets"}:
        plan = json.loads(values["capture_plan"])
        if mutation == "ddl":
            plan["creation_statements"][0]["sql"] += " OPTIONS(description='unapproved')"
        elif mutation == "cutoff":
            plan["cutoff_date"] = "2026-09-08"
        else:
            plan["market_scope"] = ["za", "ke"]
        values["capture_plan"] = canonical_bytes(plan)
    else:
        policy = json.loads(values["storage_policy"])
        if mutation == "money":
            policy["reserved_micro_usd_per_cutoff"] = 500001
        else:
            policy["price_review"]["expires_at"] = NOW.isoformat()
        values["storage_policy"] = canonical_bytes(policy)
    with pytest.raises(ValueError):
        subject()._validate_inputs(values, CUTOFF, "initial", NOW)


def test_initial_invocation_cannot_smuggle_recovery_context():
    values = copy.deepcopy(artifacts())
    values["recovery_context"] = b"{}"
    with pytest.raises(ValueError):
        subject()._validate_inputs(values, CUTOFF, "initial", NOW)


def metadata_client(inputs, *, historical_bytes, metadata_updated_after_cutoff=False):
    from urllib.parse import unquote, urlsplit

    import requests
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery
    from src.analysis.open_intelligence.production_snapshot_tables import _reviewed_source_metadata

    schemas, _ = _reviewed_source_metadata(artifacts()["source_metadata"])
    millis = int(inputs["plan"].source_as_of.timestamp() * 1000)
    paths = []

    class HTTP:
        is_mtls = False

        def request(self, method, url, **kwargs):
            assert method == "GET"
            path = unquote(urlsplit(url).path)
            paths.append(path)
            table_id = path.rsplit("/", 1)[1]
            lane = table_id.split("@", 1)[0]
            native = {
                "tableReference": {
                    "projectId": "ogilvy-trends-v2",
                    "datasetId": "trends_v2_dev",
                    "tableId": table_id,
                },
                "type": "TABLE",
                "location": "US",
                "schema": schemas[lane]["schema"],
                "numBytes": historical_bytes if "@" in table_id else "1",
                "lastModifiedTime": str(
                    millis + 1 if metadata_updated_after_cutoff else millis - 1
                ),
            }
            response = requests.Response()
            response.status_code = 200
            response._content = canonical_bytes(native)
            return response

    return bigquery.Client(
        project="ogilvy-trends-v2", credentials=AnonymousCredentials(), _http=HTTP()
    ), paths


@pytest.mark.parametrize("historical_bytes", [None, "5368709121"])
def test_asof_size_unknown_or_excess_refuses_even_when_current_bases_are_small(historical_bytes):
    inputs = subject()._validate_inputs(artifacts(), CUTOFF, "initial", NOW)
    client, _paths = metadata_client(inputs, historical_bytes=historical_bytes)
    with pytest.raises(ValueError):
        subject()._source_preflight(inputs, client)


def test_source_size_preflight_reads_exact_asof_metadata_for_every_lane():
    inputs = subject()._validate_inputs(artifacts(), CUTOFF, "initial", NOW)
    client, paths = metadata_client(inputs, historical_bytes="100")
    subject()._source_preflight(inputs, client)
    stamp = str(int(inputs["plan"].source_as_of.timestamp() * 1000))
    assert len(paths) == 5
    assert all(path.endswith("@" + stamp) for path in paths)


def test_asof_metadata_remains_eligible_after_table_iam_updates():
    inputs = subject()._validate_inputs(artifacts(), CUTOFF, "initial", NOW)
    client, paths = metadata_client(
        inputs, historical_bytes="100", metadata_updated_after_cutoff=True
    )
    subject()._source_preflight(inputs, client)
    stamp = str(int(inputs["plan"].source_as_of.timestamp() * 1000))
    assert len(paths) == 5
    assert all(path.endswith("@" + stamp) for path in paths)
