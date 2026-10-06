import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from ops.certification.bigquery_metadata import (
    capture_datasets,
    compare_captures,
    main,
)

PROJECT = "ogilvy-trends-v2"
API = "https://bigquery.googleapis.com/bigquery/v2"
FINGERPRINT = "sha256:" + hashlib.sha256(b"metadata-session").hexdigest()


def canonical_digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def full_table(table_id: str = "answers") -> dict:
    return {
        "kind": "bigquery#table",
        "id": f"{PROJECT}:staging.{table_id}",
        "etag": "etag-1",
        "tableReference": {"projectId": PROJECT, "datasetId": "staging", "tableId": table_id},
        "type": "TABLE",
        "location": "US",
        "creationTime": "1757000000000",
        "lastModifiedTime": "1757000000001",
        "numRows": "10",
        "numBytes": "100",
        "schema": {
            "fields": [
                {"name": "request_id", "type": "STRING", "mode": "REQUIRED"},
                {"name": "answered_at", "type": "TIMESTAMP"},
                {
                    "name": "claims",
                    "type": "RECORD",
                    "mode": "REPEATED",
                    "fields": [{"name": "text", "type": "STRING"}],
                },
            ]
        },
        "timePartitioning": {"type": "DAY", "field": "answered_at", "expirationMs": "7776000000"},
        "requirePartitionFilter": True,
        "clustering": {"fields": ["request_id"]},
        "labels": {"owner": "intelligence-42"},
    }


def dataset(dataset_id: str = "staging") -> dict:
    return {
        "kind": "bigquery#dataset",
        "etag": "dataset-etag",
        "datasetReference": {"projectId": PROJECT, "datasetId": dataset_id},
        "location": "US",
        "creationTime": "1750000000000",
        "lastModifiedTime": "1750000000001",
        "defaultTableExpirationMs": "5184000000",
        "access": [
            {"role": "OWNER", "userByEmail": "owner@example.invalid"},
            {"role": "READER", "specialGroup": "projectReaders"},
        ],
    }


def routine(body: str = "SELECT 1") -> dict:
    return {
        "etag": "routine-etag",
        "routineReference": {"projectId": PROJECT, "datasetId": "staging", "routineId": "sp_read_v1"},
        "routineType": "PROCEDURE",
        "language": "SQL",
        "definitionBody": body,
        "creationTime": "1756000000000",
        "lastModifiedTime": "1756000000001",
    }


def table_policy(members: list[str] | None = None) -> dict:
    return {
        "version": 3,
        "etag": "policy-etag",
        "bindings": [
            {"role": "roles/bigquery.dataViewer", "members": members or ["serviceAccount:reader@example.invalid"]}
        ],
    }


class FakeClient:
    def __init__(self, *, tables=None, routines_payload=None, policy=None, dataset_payload=None, missing_dataset=False):
        self.tables = tables if tables is not None else {"answers": full_table()}
        self.routines_payload = routines_payload
        self.policy = policy or table_policy()
        self.dataset_payload = dataset_payload or dataset()
        self.missing_dataset = missing_dataset
        self.calls: list[tuple[str, str]] = []

    def authenticated_principal_evidence(self) -> dict:
        material = {
            "principal": "reader@example.invalid",
            "credential_fingerprint": FINGERPRINT,
            "acquisition": "trusted_test_fixture",
            "no_fallback": True,
        }
        return {**material, "evidence_digest": canonical_digest(material)}

    def session_fingerprint(self) -> str:
        return FINGERPRINT

    def request_json(self, method, url, *, params=None, json_body=None):
        self.calls.append((method, url))
        base = f"{API}/projects/{PROJECT}/datasets/staging"
        if method != "GET" and not url.endswith(":getIamPolicy"):
            raise AssertionError(f"write attempted: {method} {url}")
        if url == base:
            if self.missing_dataset:
                error = RuntimeError("not found")
                error.response = type("Response", (), {"status_code": 404})()
                raise error
            return deepcopy(self.dataset_payload)
        if url == f"{base}/tables":
            if not self.tables:
                return {"kind": "bigquery#tableList", "etag": "e", "totalItems": 0}
            return {
                "kind": "bigquery#tableList",
                "tables": [
                    {"tableReference": {"projectId": PROJECT, "datasetId": "staging", "tableId": table_id}}
                    for table_id in self.tables
                ],
                "totalItems": len(self.tables),
            }
        if url.startswith(f"{base}/tables/") and url.endswith(":getIamPolicy"):
            assert method == "POST"
            return deepcopy(self.policy)
        if url.startswith(f"{base}/tables/"):
            return deepcopy(self.tables[url.rsplit("/", 1)[-1]])
        if url == f"{base}/routines":
            if self.routines_payload is not None:
                return deepcopy(self.routines_payload)
            return {"routines": [{"routineReference": routine()["routineReference"]}]}
        if url == f"{base}/routines/sp_read_v1":
            return routine()
        raise AssertionError(f"unexpected read: {method} {url}")


def capture(client: FakeClient | None = None, key: bytes = b"metadata-key") -> dict:
    return capture_datasets(
        PROJECT,
        ["staging"],
        client or FakeClient(),
        identity="reader@example.invalid",
        private_hash_key=key,
        observed_at="2026-09-25T08:00:00+00:00",
    )


def as_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def mutate(base: dict, table_mutator=None, dataset_mutator=None, policy_mutator=None, routine_mutator=None) -> dict:
    after = deepcopy(base)
    resources = after["resources"]
    for row in resources["tables"]:
        if table_mutator:
            table_mutator(row["observed"])
    for row in resources["datasets"]:
        if dataset_mutator:
            dataset_mutator(row["observed"])
    for row in resources["iam"]:
        if policy_mutator:
            policy_mutator(row["observed"])
    for row in resources["routines"]:
        if routine_mutator:
            routine_mutator(row["observed"])
    after.pop("content_digest")
    after["content_digest"] = canonical_digest(after)
    return after


def compare(before: dict, after: dict) -> dict:
    return compare_captures(as_bytes(before), as_bytes(after))


def diffs_by_category(result: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in result["resources"]:
        for diff in row["diffs"]:
            grouped.setdefault(diff["category"], []).append(diff)
    return grouped


def test_capture_reads_full_metadata_for_datasets_tables_routines_and_policies() -> None:
    client = FakeClient()
    result = capture(client)

    assert result["complete"] is True
    assert result["coverage"] == {"datasets": 1, "tables": 1, "routines": 1, "iam": 1}
    assert result["content_digest"] == canonical_digest(
        {key: value for key, value in result.items() if key != "content_digest"}
    )
    assert all(method == "GET" or url.endswith(":getIamPolicy") for method, url in client.calls)
    assert (
        "GET",
        f"{API}/projects/{PROJECT}/datasets/staging/tables/answers",
    ) in client.calls


def test_identical_captures_report_no_change_and_ignore_statistics() -> None:
    before = capture()
    after = mutate(
        before,
        table_mutator=lambda table: table.update(
            numRows="99", numBytes="999", lastModifiedTime="1", etag="etag-2"
        ),
        dataset_mutator=lambda value: value.update(lastModifiedTime="2", etag="x"),
    )

    result = compare(before, after)

    assert result["verdict"] == "no_change"
    assert result["counts"]["by_status"]["unchanged"] == 3
    assert all(row["diffs"] == [] for row in result["resources"])


@pytest.mark.parametrize(
    ("category", "mutator", "field"),
    [
        ("columns", lambda t: t["schema"]["fields"][1].update(type="DATETIME"), "definitions.answered_at.type"),
        ("columns", lambda t: t["schema"]["fields"].append({"name": "added", "type": "STRING"}), "order"),
        ("columns", lambda t: t["schema"]["fields"].reverse(), "order"),
        ("columns", lambda t: t["schema"]["fields"][2]["fields"][0].update(type="JSON"), "definitions.claims.text.type"),
        ("modes", lambda t: t["schema"]["fields"][1].update(mode="REQUIRED"), "answered_at"),
        ("modes", lambda t: t["schema"]["fields"][0].update(mode="NULLABLE"), "request_id"),
        ("partitioning", lambda t: t["timePartitioning"].update(field="request_id"), "timePartitioning.field"),
        ("partitioning", lambda t: t.update(requirePartitionFilter=False), "requirePartitionFilter"),
        ("clustering", lambda t: t["clustering"].update(fields=["answered_at"]), "clustering"),
        ("location", lambda t: t.update(location="EU"), "location"),
        ("expiry", lambda t: t.update(expirationTime="1760000000000"), "expirationTime"),
        ("expiry", lambda t: t["timePartitioning"].update(expirationMs="1"), "partitionExpirationMs"),
        ("options", lambda t: t["labels"].update(owner="someone-else"), "labels.owner"),
        ("lifecycle", lambda t: t.update(creationTime="1758000000000"), "creationTime"),
    ],
)
def test_each_table_metadata_change_is_reported_in_its_category(category, mutator, field) -> None:
    before = capture()
    after = mutate(before, table_mutator=mutator)

    result = compare(before, after)

    assert result["verdict"] == "differences_or_limits"
    grouped = diffs_by_category(result)
    assert field in [diff["field"] for diff in grouped[category]]


def test_table_permission_change_is_reported() -> None:
    before = capture()
    after = mutate(
        before,
        policy_mutator=lambda policy: policy["bindings"][0]["members"].append("user:new@example.invalid"),
    )

    grouped = diffs_by_category(compare(before, after))

    assert [diff["field"] for diff in grouped["permissions"]] == ["permissions"]


def test_dataset_access_location_and_default_expiry_changes_are_reported() -> None:
    before = capture()
    after = mutate(
        before,
        dataset_mutator=lambda value: (
            value["access"].append({"role": "WRITER", "userByEmail": "writer@example.invalid"}),
            value.update(defaultTableExpirationMs="1", location="EU"),
        ),
    )

    grouped = diffs_by_category(compare(before, after))

    assert {"permissions", "expiry", "location"} <= set(grouped)
    assert "defaultTableExpirationMs" in [diff["field"] for diff in grouped["expiry"]]


def test_access_and_binding_order_alone_is_not_a_change() -> None:
    before = capture(FakeClient(policy=table_policy(["user:a@example.invalid", "user:b@example.invalid"])))
    after = mutate(
        before,
        dataset_mutator=lambda value: value["access"].reverse(),
        policy_mutator=lambda policy: policy["bindings"][0]["members"].reverse(),
    )

    assert compare(before, after)["verdict"] == "no_change"


def test_routine_body_change_is_reported() -> None:
    before = capture()
    after = mutate(before, routine_mutator=lambda value: value.update(definitionBody="SELECT 2"))

    grouped = diffs_by_category(compare(before, after))

    assert [diff["field"] for diff in grouped["routine_definition"]] == [
        "definitionBodySha256"
    ]


def test_added_and_removed_tables_are_presence_changes() -> None:
    before = capture()
    after = capture(FakeClient(tables={"other": full_table("other")}))

    result = compare(before, after)

    statuses = {row["name"].rsplit("/", 1)[-1]: row["status"] for row in result["resources"]}
    assert statuses["answers"] == "removed"
    assert statuses["other"] == "added"
    assert result["counts"]["by_category"]["presence"] == 2


def test_schema_only_record_cannot_prove_table_options() -> None:
    before = capture()

    def schema_only(table: dict) -> None:
        for key in ("type", "location", "creationTime", "timePartitioning", "clustering"):
            table.pop(key, None)

    after = mutate(before, table_mutator=schema_only)
    result = compare(before, after)
    row = next(row for row in result["resources"] if row["kind"] == "tables")

    assert row["status"] == "options_unproven"
    assert "location" in row["missing_full_fields"]["after"]
    assert result["verdict"] == "differences_or_limits"

    unchanged = mutate(after)
    same = compare(after, unchanged)
    assert same["verdict"] == "differences_or_limits"
    assert same["counts"]["by_status"]["options_unproven"] == 1


def test_capture_failures_are_coverage_limits_that_block_no_change() -> None:
    incomplete = capture(FakeClient(missing_dataset=True))

    assert incomplete["complete"] is False
    assert incomplete["failures"][0]["http_status"] == 404
    result = compare(incomplete, mutate(incomplete))
    assert result["verdict"] == "differences_or_limits"
    assert len(result["coverage_limits"]) == 2


def test_missing_full_table_field_marks_capture_incomplete() -> None:
    table = full_table()
    table.pop("location")

    result = capture(FakeClient(tables={"answers": table}))

    assert result["complete"] is False
    assert {"operation": "tables", "field": "location"}.items() <= result["failures"][0].items()


def test_empty_dataset_and_empty_routine_list_are_recorded_normalizations() -> None:
    result = capture(FakeClient(tables={}, routines_payload={}))

    assert result["complete"] is True
    assert [item["normalization"] for item in result["transport_normalizations"]] == [
        "omitted_empty_bigquery_table_list",
        "empty_routine_list_response",
    ]


def test_unanchored_routine_list_is_a_failure() -> None:
    result = capture(FakeClient(routines_payload={"nextPageToken": "p"}))

    assert result["complete"] is False
    assert result["failures"][0]["operation"] == "list_routines"


def test_tampered_capture_is_refused() -> None:
    before = capture()
    tampered = deepcopy(before)
    tampered["resources"]["tables"][0]["observed"]["location"] = "EU"

    with pytest.raises(ValueError, match="after_content_digest_mismatch"):
        compare(before, tampered)


def test_private_values_are_flagged_and_key_mismatch_is_reported() -> None:
    before = capture(key=b"key-one")
    after = capture(key=b"key-two")

    result = compare(before, after)

    assert result["private_values_comparable"] is False
    assert compare(before, mutate(before))["private_values_comparable"] is True


def test_production_baseline_capture_format_is_accepted() -> None:
    table = full_table()
    capture_value = {
        "schema_version": 1,
        "project": PROJECT,
        "complete": True,
        "resources": {
            "datasets": [{"name": f"projects/{PROJECT}/datasets/staging", "observed": dataset()}],
            "tables": [{"name": f"projects/{PROJECT}/datasets/staging/tables/answers", "observed": table}],
            "iam": [
                {"name": f"projects/{PROJECT}", "observed": {"bindings": []}},
                {"name": f"projects/{PROJECT}/datasets/staging/tables/answers", "observed": table_policy()},
            ],
        },
        "failures": [],
    }
    capture_value["content_digest"] = canonical_digest(capture_value)

    result = compare_captures(as_bytes(capture_value), as_bytes(capture_value), datasets={"staging"})

    assert result["verdict"] == "no_change"
    assert result["counts"]["by_status"]["unchanged"] == 2


def test_cli_capture_and_compare(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("BIGQUERY_METADATA_HASH_KEY", "shared-key")
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    for path in (before_path, after_path):
        code = main(
            ["capture", "--project", PROJECT, "--dataset", "staging", "--identity", "reader@example.invalid", "--output", str(path)],
            client_factory=lambda identity: FakeClient(),
        )
        assert code == 0
    with pytest.raises(FileExistsError):
        main(
            ["capture", "--project", PROJECT, "--dataset", "staging", "--identity", "reader@example.invalid", "--output", str(before_path)],
            client_factory=lambda identity: FakeClient(),
        )
    capsys.readouterr()
    code = main(["compare", "--before", str(before_path), "--after", str(after_path), "--output", str(tmp_path / "cmp.json")])
    summary = json.loads(capsys.readouterr().out)

    assert code == 0
    assert summary["verdict"] == "no_change"
    written = json.loads((tmp_path / "cmp.json").read_text(encoding="utf-8"))
    assert written["private_values_comparable"] is True

    changed = json.loads(after_path.read_text(encoding="utf-8"))
    changed["resources"]["tables"][0]["observed"]["clustering"]["fields"] = ["answered_at"]
    changed.pop("content_digest")
    changed["content_digest"] = canonical_digest(changed)
    changed_path = tmp_path / "changed.json"
    changed_path.write_text(json.dumps(changed), encoding="utf-8")
    code = main(["compare", "--before", str(before_path), "--after", str(changed_path), "--output", str(tmp_path / "cmp2.json")])

    assert code == 3


@pytest.mark.parametrize(
    "mutator",
    [
        lambda t: t["schema"]["fields"][0].update(dataPolicies=[{"name": "mask"}]),
        lambda t: t["schema"]["fields"][1].update(timestampPrecision="12"),
        lambda t: t["schema"]["fields"][2]["fields"][0].update(categories={"names": ["x"]}),
        lambda t: t.update(partitionDefinition={"partitionedColumn": [{"field": "day"}]}),
        lambda t: t["schema"].update(foreignTypeInfo={"typeSystem": "HIVE"}),
    ],
)
def test_unlisted_column_and_table_configuration_is_never_dropped(mutator) -> None:
    before = capture()
    after = mutate(before, table_mutator=mutator)

    result = compare(before, after)

    assert result["verdict"] == "differences_or_limits"
    assert result["counts"]["by_status"]["changed"] == 1


def test_dataset_filter_naming_an_uncaptured_dataset_is_refused() -> None:
    before = capture()
    after = mutate(before, table_mutator=lambda t: t.update(location="EU"))

    with pytest.raises(ValueError, match="dataset_filter_not_captured:typo"):
        compare_captures(as_bytes(before), as_bytes(after), datasets={"typo"})
    with pytest.raises(ValueError, match="dataset_filter_not_captured"):
        compare_captures(as_bytes(before), as_bytes(after), datasets=set())


def test_an_empty_comparison_scope_is_never_no_change() -> None:
    empty = capture()
    for rows in empty["resources"].values():
        rows.clear()
    empty = mutate(empty)

    result = compare(empty, mutate(empty))

    assert result["resources"] == []
    assert result["verdict"] == "differences_or_limits"


def test_routine_list_that_is_not_an_object_is_a_failure() -> None:
    class ListClient(FakeClient):
        def request_json(self, method, url, *, params=None, json_body=None):
            if url.endswith("/routines"):
                return ["not", "an", "object"]
            return super().request_json(method, url, params=params, json_body=json_body)

    result = capture(ListClient())

    assert result["complete"] is False
    assert result["failures"][0]["operation"] == "list_routines"


def test_paged_routines_read_each_page_once() -> None:
    reference = {"routineReference": routine()["routineReference"]}

    class PagedClient(FakeClient):
        def request_json(self, method, url, *, params=None, json_body=None):
            if url.endswith("/routines"):
                self.calls.append((method, url))
                if not (params or {}).get("pageToken"):
                    return {"routines": [], "nextPageToken": "page-2"}
                return {"routines": [reference]}
            return super().request_json(method, url, params=params, json_body=json_body)

    client = PagedClient()
    result = capture(client)

    assert result["complete"] is True
    assert result["coverage"]["routines"] == 1
    assert sum(url.endswith("/routines") for _, url in client.calls) == 2


def test_dataset_read_requests_conditional_access_entries() -> None:
    class RecordingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.params = {}

        def request_json(self, method, url, *, params=None, json_body=None):
            self.params[url] = params
            return super().request_json(method, url, params=params, json_body=json_body)

    client = RecordingClient()
    capture(client)

    assert client.params[f"{API}/projects/{PROJECT}/datasets/staging"] == {
        "datasetView": "FULL",
        "accessPolicyVersion": "3",
    }


def test_table_policy_condition_change_is_reported() -> None:
    conditional = table_policy()
    conditional["bindings"][0]["condition"] = {"expression": "request.time < timestamp('2027-01-01T00:00:00Z')"}
    before = capture(FakeClient(policy=conditional))
    after = mutate(
        before,
        policy_mutator=lambda policy: policy["bindings"][0]["condition"].update(expression="true"),
    )

    assert "permissions" in diffs_by_category(compare(before, after))
