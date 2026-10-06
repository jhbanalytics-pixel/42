"""An anchored empty BigQuery table list is complete, and nothing else is.

BigQuery answers tables.list for an empty dataset without the tables key and
with a zero totalItems count. The certification captures keep that answer as
a recorded transport normalization. Any other answer missing the collection
stays a collection_missing failure. The P1 production capture runs through
ops.certification.production_capture and the P3 staging capture through
ops.certification.bigquery_metadata; both are covered here.
"""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from ops.certification.production_capture import capture_production
from ops.certification.production_capture import main as capture_main
from ops.deploy.baseline_scope import _verify_content_digest
from ops.deploy.production_baseline import capture_baseline
from ops.tests.test_certification_bigquery import FakeClient
from ops.tests.test_certification_bigquery import capture as metadata_capture
from ops.tests.test_foundation_inventory import FakeNativeClient, inventory

PROD_TABLES = (
    "https://bigquery.googleapis.com/bigquery/v2/projects/"
    "ogilvy-trends-v2/datasets/prod/tables"
)
STAGING_TABLES = (
    "https://bigquery.googleapis.com/bigquery/v2/projects/"
    "ogilvy-trends-v2/datasets/staging/tables"
)
UNANCHORED_OR_NONZERO = [
    {},
    {"kind": "bigquery#tableList", "etag": "e"},
    {"kind": "bigquery#tableList", "etag": "e", "totalItems": 3},
    {"kind": "bigquery#tableList", "etag": "e", "totalItems": "0"},
    {"kind": "bigquery#tableList", "etag": "e", "totalItems": 0, "nextPageToken": "p"},
    {"kind": "bigquery#datasetList", "etag": "e", "totalItems": 0},
    {"kind": "bigquery#tableList", "etag": "e", "totalItems": 0, "unexpected": 1},
]


def payload_digest(payload: dict) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


class EmptyTableListClient(FakeNativeClient):
    """Serves the prod dataset as empty the way BigQuery does: the tables key
    is omitted and totalItems is zero."""

    def __init__(self, table_list_payload: dict):
        super().__init__()
        self.table_list_payload = table_list_payload

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        if url.endswith("/datasets/prod/tables"):
            self.calls.append((method, url, params or {}, json_body))
            return deepcopy(self.table_list_payload)
        return super().request_json(method, url, params=params, json_body=json_body)


def capture(client: FakeNativeClient | None = None) -> dict:
    return capture_production(
        inventory(),
        client or FakeNativeClient(),
        identity="reader@example.invalid",
        private_hash_key=b"private-test-key",
        observed_at="2026-09-12T10:00:00+00:00",
    )


def test_empty_dataset_table_list_is_complete_and_records_normalization() -> None:
    payload = {"kind": "bigquery#tableList", "etag": "empty-etag", "totalItems": 0}

    result = capture(EmptyTableListClient(payload))

    assert result["failures"] == []
    assert result["complete"] is True
    assert result["resources"]["tables"] == []
    assert result["transport_normalizations"] == [
        {
            "operation": "list_tables",
            "resource": PROD_TABLES,
            "collection": "tables",
            "normalization": "omitted_empty_bigquery_table_list",
            "original_response_digest": payload_digest(payload),
        }
    ]
    assert _verify_content_digest(result, "capture") == result["content_digest"]


@pytest.mark.parametrize("payload", UNANCHORED_OR_NONZERO)
def test_unanchored_or_nonzero_table_list_without_tables_remains_failure(
    payload: dict,
) -> None:
    result = capture(EmptyTableListClient(payload))

    assert result["complete"] is False
    assert [failure["operation"] for failure in result["failures"]] == ["list_tables"]
    assert result["failures"][0]["code"] == "collection_missing"
    assert result["failures"][0]["resource"] == PROD_TABLES
    assert "transport_normalizations" not in result
    assert _verify_content_digest(result, "capture") == result["content_digest"]


def test_a_capture_without_an_empty_table_list_is_the_baseline_capture() -> None:
    wrapped = capture(FakeNativeClient())
    direct = capture_baseline(
        inventory(),
        FakeNativeClient(),
        identity="reader@example.invalid",
        private_hash_key=b"private-test-key",
        observed_at="2026-09-12T10:00:00+00:00",
    )

    assert wrapped == direct


def test_production_capture_cli_records_the_normalization(
    tmp_path: Path, monkeypatch
) -> None:
    payload = {"kind": "bigquery#tableList", "etag": "empty-etag", "totalItems": 0}
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(inventory()), encoding="utf-8")
    output = tmp_path / "capture.json"
    monkeypatch.setenv("R00_BASELINE_HASH_KEY", "private-test-key")

    exit_code = capture_main(
        [
            "capture",
            "--inventory",
            str(inventory_path),
            "--output",
            str(output),
            "--identity",
            "reader@example.invalid",
        ],
        client_factory=lambda identity: EmptyTableListClient(payload),
    )

    written = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert written["complete"] is True
    assert [item["normalization"] for item in written["transport_normalizations"]] == [
        "omitted_empty_bigquery_table_list"
    ]
    assert _verify_content_digest(written, "capture") == written["content_digest"]


def test_production_capture_cli_exits_two_on_an_unanchored_table_list(
    tmp_path: Path, monkeypatch
) -> None:
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(inventory()), encoding="utf-8")
    output = tmp_path / "capture.json"
    monkeypatch.setenv("R00_BASELINE_HASH_KEY", "private-test-key")

    exit_code = capture_main(
        [
            "capture",
            "--inventory",
            str(inventory_path),
            "--output",
            str(output),
            "--identity",
            "reader@example.invalid",
        ],
        client_factory=lambda identity: EmptyTableListClient({}),
    )

    assert exit_code == 2
    assert json.loads(output.read_text(encoding="utf-8"))["complete"] is False


class StagingTableListClient(FakeClient):
    def __init__(self, table_list_payload: dict):
        super().__init__(tables={})
        self.table_list_payload = table_list_payload

    def request_json(self, method, url, *, params=None, json_body=None):
        if url == STAGING_TABLES:
            self.calls.append((method, url))
            return deepcopy(self.table_list_payload)
        return super().request_json(method, url, params=params, json_body=json_body)


def test_staging_empty_table_list_is_complete_and_records_normalization() -> None:
    payload = {"kind": "bigquery#tableList", "etag": "empty-etag", "totalItems": 0}

    result = metadata_capture(StagingTableListClient(payload))

    assert result["failures"] == []
    assert result["complete"] is True
    assert result["resources"]["tables"] == []
    assert result["transport_normalizations"][0] == {
        "operation": "list_tables",
        "resource": STAGING_TABLES,
        "collection": "tables",
        "normalization": "omitted_empty_bigquery_table_list",
        "original_response_digest": payload_digest(payload),
    }
    assert [item["normalization"] for item in result["transport_normalizations"]] == [
        "omitted_empty_bigquery_table_list"
    ]


@pytest.mark.parametrize("payload", UNANCHORED_OR_NONZERO)
def test_staging_unanchored_or_nonzero_table_list_remains_failure(payload: dict) -> None:
    result = metadata_capture(StagingTableListClient(payload))

    assert result["complete"] is False
    assert [failure["operation"] for failure in result["failures"]] == ["list_tables"]
    assert result["failures"][0]["code"] == "collection_missing"
    assert result["failures"][0]["resource"] == STAGING_TABLES
    assert "transport_normalizations" not in result


class PagedEmptyTableListClient(FakeNativeClient):
    def request_json(self, method, url, *, params=None, json_body=None):
        if url.endswith("/datasets/prod/tables"):
            self.calls.append((method, url, params or {}, json_body))
            if not (params or {}).get("pageToken"):
                return {"kind": "bigquery#tableList", "tables": [{"tableReference": {"projectId": "ogilvy-trends-v2", "datasetId": "prod", "tableId": "events"}}], "nextPageToken": "p2"}
            return {"kind": "bigquery#tableList", "etag": "e", "totalItems": 0}
        return super().request_json(method, url, params=params, json_body=json_body)


def test_empty_table_list_answer_on_a_later_page_remains_failure() -> None:
    result = capture(PagedEmptyTableListClient())

    assert result["complete"] is False
    assert [failure["operation"] for failure in result["failures"]] == ["list_tables"]
    assert result["failures"][0]["code"] == "collection_missing"
    assert "transport_normalizations" not in result


class StagingPagedEmptyTableListClient(FakeClient):
    def request_json(self, method, url, *, params=None, json_body=None):
        if url == STAGING_TABLES:
            self.calls.append((method, url))
            if not (params or {}).get("pageToken"):
                return {
                    "kind": "bigquery#tableList",
                    "tables": [
                        {"tableReference": {"projectId": "ogilvy-trends-v2", "datasetId": "staging", "tableId": "answers"}}
                    ],
                    "nextPageToken": "p2",
                }
            return {"kind": "bigquery#tableList", "etag": "e", "totalItems": 0}
        return super().request_json(method, url, params=params, json_body=json_body)


def test_staging_empty_table_list_answer_on_a_later_page_remains_failure() -> None:
    result = metadata_capture(StagingPagedEmptyTableListClient())

    assert result["complete"] is False
    assert [failure["operation"] for failure in result["failures"]] == ["list_tables"]
    assert result["failures"][0]["code"] == "collection_missing"
    assert "transport_normalizations" not in result
