"""Test-only operator files for the route A bridge capture of cutoff 2026-09-22.

Every record here is synthetic and unissued. The generation is the packaged bridge
generation, which is the active pair. The temporal rule list is a fixture list, never a
reviewed rule list.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest

ENGINE_ROOT = Path(__file__).resolve().parents[2]
CUTOFF = "2026-09-22"
WINDOW_END = datetime(2026, 9, 23, tzinfo=UTC)
RUN_STARTED = WINDOW_END + timedelta(minutes=1)
RUN_COMPLETED = WINDOW_END + timedelta(minutes=15, seconds=30, microseconds=250000)
COLLECTION_AS_OF = WINDOW_END + timedelta(minutes=15, seconds=31)
PRICE_OBSERVED = WINDOW_END + timedelta(minutes=40)
PRODUCED_AT = WINDOW_END + timedelta(hours=1)
GRANT_ID = "bridge_capture_grant_20260922"
# Amendment g added the route A capture's artifact bucket to the bridge manifest, so its
# digest moved from 1643e4ce to ab7802f4; the registry stays 573deeea.
# The daily contract revision then registered three daily operations
# and bounded exposure, so the pair moved again: registry 573deeea to e6b95e35, manifest
# ab7802f4 to b7553041.
# Tightening the date regex to ASCII digits then moved them to f23001ed and 592ed45f.
BRIDGE_PAIR = (
    "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2",
    "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe",
)
CONTRACT = ENGINE_ROOT / "tests/fixtures/open_intelligence/protected_source_snapshot_contract.md"
SOURCE_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
BUILD_ID = "11111111-2222-3333-4444-555555555555"
BUILD_RESOURCE = f"projects/ogilvy-trends-v2/locations/us-central1/builds/{BUILD_ID}"
FIXTURE_RULES = {
    "contract_version": "source_observation_semantics_v1",
    "rules": [
        {
            "source_family": "news",
            "endpoint": "articles",
            "content_type": "article",
            "time_basis": "native_event_instant",
            "event_field": "published_at",
            "missing_time_action": "withhold_event_time_claim",
            "future_time_action": "withhold_event_time_claim",
        }
    ],
}


def stamp(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def synthetic_digest(label):
    return canonical_digest({"unissued_route_a_fixture": label})


def activate_bridge(monkeypatch):
    """Hold the bridge generation as the active pair for the test; it is the packaged
    active pair."""
    from src.analysis.open_intelligence import execution_generations

    assert execution_generations.ACTIVE_GENERATION_PAIR == BRIDGE_PAIR
    monkeypatch.setattr(execution_generations, "ACTIVE_GENERATION_PAIR", BRIDGE_PAIR)


# Amendment e: trusted, but its capture policy takes the v2 plan, not the bridge plan.
AMENDMENT_E_PAIR = (
    "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179",
    "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09",
)


def activate_amendment_e(monkeypatch):
    """Make the retained amendment e generation the active pair, as it was before the
    bridge generation, so a test can hold what every non bridge generation does."""
    from src.analysis.open_intelligence import execution_generations

    monkeypatch.setattr(execution_generations, "ACTIVE_GENERATION_PAIR", AMENDMENT_E_PAIR)


def fixture_rules(monkeypatch):
    from scripts.staging import produce_bridge_capture_inputs as producer

    monkeypatch.setattr(producer, "REVIEWED_TEMPORAL_RULES", FIXTURE_RULES)


def bridge_origin():
    from src.analysis.open_intelligence import execution_approval
    from src.analysis.open_intelligence.execution_generations import load_trusted_generation

    registry = load_trusted_generation(*BRIDGE_PAIR).registry
    return registry, execution_approval._v2_origin_for_operation(
        "source_snapshot_capture", mode="new_approval", registry=registry
    )


def result_ref(label):
    return {
        "operation": "daily_source_collection",
        "consumption_id": "exc_" + synthetic_digest("consumption:" + label),
        "manifest_sha256": synthetic_digest("manifest:" + label),
        "result_id": "exr_" + synthetic_digest("result:" + label),
        "result_digest": synthetic_digest("result:" + label),
        "origin_registry_sha256": BRIDGE_PAIR[0],
        "resource_manifest_sha256": BRIDGE_PAIR[1],
    }


# The funded Wave 1 pilot execution that collected the day, synthetic and unissued: it
# starts after the observation window closes, consumes its approval, records its funded
# close as its execution result and completes before the collection clones snapshot.
FUNDED_EXECUTION_ID = "intelligence-42-funded-pilot-staging-r7a2c"
FUNDED_APPROVED = WINDOW_END + timedelta(seconds=10)
FUNDED_STARTED = WINDOW_END + timedelta(seconds=45)
FUNDED_RECORDED = WINDOW_END + timedelta(minutes=14, seconds=30)
FUNDED_RUN = "0c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"


def funded_chain(*, payload=None, **changes):
    """The funded pilot's approval, consumption and result, typed like the ledger's."""
    from tests.unit.bridge_ledger_fixture import ledger_capture
    from tests.unit.test_execution_manifest_origins import load_registry, manifest

    value = manifest("wave1_pilot")
    value["expires_at"] = stamp(WINDOW_END + timedelta(hours=12))
    close = {
        "attribution_state": "complete",
        "calls": 3,
        "budget_debit_credits": "3",
        "source_values_state": "complete",
        "source_values": {},
        "gdelt": {"persistence": {"complete": True, "run_id": FUNDED_RUN}},
    }
    arguments = {
        "approved_at": FUNDED_APPROVED,
        "consumed_at": RUN_STARTED,
        "completed_at": FUNDED_RECORDED,
        "operation": "wave1_pilot",
        "execution_id": FUNDED_EXECUTION_ID,
        "reference_suffix": "#funded-run-close",
        "expires_at": WINDOW_END + timedelta(hours=12),
        **changes,
    }
    return ledger_capture(
        load_registry(), value, close if payload is None else payload, **arguments
    )


def funded_execution(
    chain, state="succeeded", *, started=FUNDED_STARTED, completed=None, **changes
):
    """The funded execution as one Cloud Run v2 executions.get returns it, synthetic.

    ``started`` moves only the execution's start; the chain and the collection keep theirs.
    """
    from tests.unit.test_bridge_ledger_route import funded_execution as execution

    return execution(
        state,
        execution_id=FUNDED_EXECUTION_ID,
        started=stamp(started),
        completed=completed or stamp(RUN_COMPLETED),
        image=chain["consumption"].image_uri,
        createTime=stamp(WINDOW_END + timedelta(seconds=40)),
        **changes,
    )


def funded_receipt(chain):
    """The collection receipt the producer derives from the funded chain."""
    result = chain["result"]
    return {
        "run_id": FUNDED_EXECUTION_ID,
        "result_ref": {
            name: getattr(result, name)
            for name in (
                "operation",
                "consumption_id",
                "manifest_sha256",
                "result_id",
                "result_digest",
                "origin_registry_sha256",
                "resource_manifest_sha256",
            )
        },
        "collection_receipt_digest": result.result_digest,
        "collection_started_at": stamp(RUN_STARTED),
        "collection_completed_at": stamp(FUNDED_RECORDED),
    }


def funded_readers(chain, execution=None):
    """The capture evidence readers over the funded chain and its execution, recorded."""
    from types import SimpleNamespace

    from src.analysis.open_intelligence.daily_child_execution import (
        native_funded_execution_view,
    )

    served = funded_execution(chain) if execution is None else execution
    reads = []

    def read_chain(consumption_id):
        reads.append(("chain", consumption_id))
        return list(chain["rows"]) if consumption_id == chain["consumption"].consumption_id else []

    def read_funded_execution(execution_id):
        reads.append(("execution", execution_id))
        return native_funded_execution_view(json.loads(json.dumps(served)))

    return SimpleNamespace(
        read_chain=read_chain,
        read_funded_execution=read_funded_execution,
        generation_loader=chain["catalogue"],
        reads=reads,
    )


def completion_fact(lane, market, day, state):
    return {
        "lane": lane,
        "market": market,
        "product_date": day,
        "state": state,
        "result_ref": dict(result_ref("products " + day), operation="daily_composition_apply"),
        "product_receipt_digest": synthetic_digest("completion " + day),
        "output_digest": synthetic_digest(f"rows {lane} {market} {day}"),
        "row_count": 3 if state == "completed" else 0,
        "completed_at": f"{_next(day)}T02:00:00.000000Z",
        "available_at": f"{_next(day)}T02:00:01.000000Z",
        "reason_code": None,
    }


def _next(day):
    return (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()


def history_facts():
    entries = [
        completion_fact("trend_analysis", market, "2026-09-20", state)
        for market, state in (("ke", "empty"), ("ng", "completed"), ("za", "completed"))
    ]
    entries.append(completion_fact("trend_analysis", "za", "2026-09-21", "completed"))
    entries.sort(key=lambda row: (row["lane"], row["market"], row["product_date"]))
    return {
        "contract_version": "42_bridge_history_completion_facts_v1",
        "first_product_date": "2026-09-20",
        "entries": entries,
    }


def capture_contract():
    return CONTRACT.read_bytes()


def grant(**changes):
    value = {
        "grant_id": GRANT_ID,
        "environment": "staging",
        "source_estate_digest": synthetic_digest("connector inventory"),
        "contract_sha256": hashlib.sha256(capture_contract()).hexdigest(),
        "valid_from": "2026-09-21T00:00:00.000000Z",
        "valid_until": "2026-09-26T00:00:00.000000Z",
        "allowed_cutoffs": [CUTOFF],
        "reserved_micro_usd_per_capture": 750000,
        "cumulative_ceiling_micro_usd": 750000,
        "revocation_state": "active",
    }
    value.update(changes)
    return value


def price_observation(**changes):
    value = {
        "observed_at": stamp(PRICE_OBSERVED),
        "pricing_sources": [
            "https://cloud.google.com/bigquery/pricing",
            "https://cloud.google.com/storage/pricing",
        ],
        "storage_micro_usd_per_gib_month": 20000,
        "query_micro_usd_per_tib": 6250000,
        "operations_micro_usd_per_10k": 50000,
        "source_logical_bytes": 7 * 4096,
        "max_bytes_billed_per_query": 4096,
        "queries_per_cycle": 7,
        "jobs_per_cycle": 7,
        "retention_days": 90,
        "operations_per_cycle": 40,
        "permitted_retries": 1,
        "cadence_cycles_per_day": 1,
    }
    value.update(changes)
    return value


PARTITIONS = {
    "enriched_content": ("collected_at", "TIMESTAMP"),
    "raw_content": ("collected_at", "TIMESTAMP"),
    "seed_candidates": ("proposed_date", "DATE"),
}


def source_table(lane):
    # Collection and history lanes alike are read from the product dataset, where the funded
    # Wave 1 pilot writes raw_content and enriched_content.
    return f"ogilvy-trends-v2.trends_v2_staging.{lane}"


def table_schema(lane):
    field, kind = PARTITIONS.get(lane, ("trend_date", "DATE"))
    return {
        "fields": [
            {"name": field, "type": kind, "mode": "REQUIRED"},
            {"name": "market", "type": "STRING", "mode": "NULLABLE"},
        ]
    }


def source_metadata():
    from src.analysis.open_intelligence.source_estate_bridge import LANES

    tables = {}
    for lane in LANES:
        project, dataset, table = source_table(lane).split(".")
        tables[source_table(lane)] = {
            "status": 200,
            "type": "TABLE",
            "etag": "unissued-route-a-etag",
            "tableReference": {"projectId": project, "datasetId": dataset, "tableId": table},
            "timePartitioning": {"type": "DAY", "field": PARTITIONS.get(lane, ("trend_date",))[0]},
            "schema": table_schema(lane),
        }
    return {"tables": tables}


def build_describe():
    _, origin = bridge_origin()
    return {
        "name": BUILD_RESOURCE,
        "id": BUILD_ID,
        "projectId": "ogilvy-trends-v2",
        "status": "SUCCESS",
        "results": {
            "images": [{"name": f"{origin.image_repository}:{SOURCE_SHA}", "digest": IMAGE_DIGEST}]
        },
        "finishTime": "2026-09-22T20:00:00.000000Z",
        "source": {
            "connectedRepository": {"repository": origin.connected_repo, "revision": SOURCE_SHA}
        },
    }


def operator_files(root, **overrides):
    """Write the operator supplied files; an override replaces one file's value."""
    root.mkdir(parents=True, exist_ok=True)
    chain = overrides.pop("chain", None) or funded_chain()
    values = {
        "funded_chain": chain["rows"],
        "funded_execution": funded_execution(chain),
        "history_completions": history_facts(),
        "grant": grant(),
        "price_observation": price_observation(),
        "source_metadata": source_metadata(),
        "build": build_describe(),
    }
    values.update({key: value for key, value in overrides.items() if key != "capture_contract"})
    paths = {}
    for name, value in values.items():
        paths[name] = root / f"{name}.json"
        paths[name].write_bytes(json.dumps(value, sort_keys=True).encode())
    paths["capture_contract"] = root / "capture_contract.md"
    paths["capture_contract"].write_bytes(overrides.get("capture_contract", capture_contract()))
    paths["catalogue"] = chain["catalogue"]
    return paths


def argv(paths, output, *, cutoff=CUTOFF):
    """The producer's command line: each switch is the producer's own, in its order."""
    from scripts.staging.produce_bridge_capture_inputs import _CLI_FLAGS

    values = {"cutoff_date": cutoff, "output_dir": str(output)}
    values.update({name: str(path) for name, path in paths.items() if name != "catalogue"})
    result = []
    for flag in _CLI_FLAGS:
        result += [flag, values[flag[2:].replace("-", "_")]]
    return result


def produced(tmp_path, monkeypatch, **overrides):
    """Run the producer over fresh operator files; return its receipt and output directory."""
    from scripts.staging import produce_bridge_capture_inputs as producer

    activate_bridge(monkeypatch)
    fixture_rules(monkeypatch)
    paths = operator_files(tmp_path / "operator", **overrides)
    output = tmp_path / "produced"
    receipt = producer.produce(
        argv(paths, output), clock=lambda: PRODUCED_AT, generation_loader=paths["catalogue"]
    )
    return receipt, output


def read_outputs(output):
    from scripts.staging.produce_bridge_capture_inputs import ARTIFACT_NAMES

    inputs = {name: (output / "inputs" / f"{name}.json").read_bytes() for name in ARTIFACT_NAMES}
    return inputs, (output / "manifest.json").read_bytes()


def canonical(value):
    return canonical_bytes(value)


# The approval, the runner's native provider and the ledger, all synthetic.

APPROVED_AT = WINDOW_END + timedelta(hours=1, minutes=10)
CONSUMED_AT = WINDOW_END + timedelta(hours=1, minutes=20)
FIRST_JOB = CONSUMED_AT + timedelta(seconds=30)
START_AT = CONSUMED_AT - timedelta(seconds=20)
CAPTURED_AT = CONSUMED_AT + timedelta(minutes=5)
STORED_AT = CAPTURED_AT + timedelta(seconds=10)
RESULT_COMPLETED_AT = CAPTURED_AT + timedelta(minutes=1)
REQUEST_AT = WINDOW_END + timedelta(hours=2)
IDENTITY = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
EXECUTION_NAME = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
    "/executions/route-a-1"
)


def millis(instant):
    return str(int(instant.timestamp() * 1000))


def bridge_generation():
    from src.analysis.open_intelligence.execution_generations import load_trusted_generation

    return load_trusted_generation(*BRIDGE_PAIR)


def approval(manifest_raw, *, approved_at=APPROVED_AT):
    """The approval record the v3 approval routine would write, typed by the real class."""
    from src.analysis.open_intelligence import execution_approval

    from tests.unit.test_execution_records_v2 import APPROVED_BY

    registry = bridge_generation().registry
    manifest = json.loads(manifest_raw)
    digest = hashlib.sha256(manifest_raw).hexdigest()
    expires = datetime.strptime(manifest["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    return execution_approval.ExecutionApprovalV2(
        approval_contract_version="open_intelligence_execution_approval_v2",
        approval_id=execution_approval.approval_id_v2(
            digest,
            APPROVED_BY,
            approved_at,
            origin_registry_sha256=BRIDGE_PAIR[0],
            resource_manifest_sha256=BRIDGE_PAIR[1],
        ),
        manifest_version=manifest["manifest_version"],
        operation="source_snapshot_capture",
        contract_sha256=manifest["contract_sha256"],
        manifest_sha256=digest,
        canonical_manifest_json=manifest_raw.decode(),
        approved_by=APPROVED_BY,
        approved_at=approved_at,
        expires_at=expires,
        approval_phrase_sha256=execution_approval._approval_phrase_sha256_v2(
            "source_snapshot_capture", digest, BRIDGE_PAIR[0], BRIDGE_PAIR[1]
        ),
        origin_registry_sha256=BRIDGE_PAIR[0],
        resource_manifest_sha256=BRIDGE_PAIR[1],
        mode="new_consume",
        registry=registry,
        expected_resource_manifest_sha256=BRIDGE_PAIR[1],
    )


def runtime_pair(manifest_raw, *, execution_name=EXECUTION_NAME):
    """The Cloud Run execution and job the approved manifest names, as the runner reads them."""
    manifest = json.loads(manifest_raw)
    task = {
        "serviceAccount": manifest["service_identity"],
        "maxRetries": 0,
        "timeout": f"{manifest['timeout_seconds']}s",
        "containers": [
            {
                "image": manifest["image_uri"],
                "command": manifest["command"],
                "args": manifest["arguments"],
                "env": manifest["environment"],
                "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
            }
        ],
    }
    annotations = {
        "42.ogilvy/execution-approval-sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "42.ogilvy/source-sha": manifest["source_sha"],
        "42.ogilvy/origin-registry-sha256": BRIDGE_PAIR[0],
        "42.ogilvy/resource-manifest-sha256": BRIDGE_PAIR[1],
    }
    return {
        "execution": {
            "name": execution_name,
            "job": manifest["job_resource"],
            "annotations": dict(annotations),
            "template": json.loads(json.dumps(task)),
        },
        "job": {
            "name": manifest["job_resource"],
            "template": {
                "annotations": dict(annotations),
                "template": json.loads(json.dumps(task)),
            },
        },
    }


class Clock:
    """The runner's clock; the provider moves it as native work completes."""

    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class BridgeBigQueryHTTP:
    """The BigQuery REST surface of the seven clone creations, synthetic."""

    is_mtls = False

    def __init__(self, plan, clock, *, identity=IDENTITY, failed_lane=None, shift=None, billed="0"):
        self.billed = billed
        self.plan = plan
        self.clock = clock
        self.identity = identity
        self.failed_lane = failed_lane
        self.shift = shift or timedelta(0)
        self.calls = []
        self.jobs = {}
        self.tables = {}

    def _relation(self, lane):
        return next(
            row for row in self.plan["snapshot_plan"]["relation_bindings"] if row["lane"] == lane
        )

    def _response(self, method, url, status, value):
        import requests

        response = requests.Response()
        response.status_code = status
        response.request = requests.Request(method, url).prepare()
        response._content = json.dumps(value).encode()
        response.headers["Content-Type"] = "application/json"
        return response

    def request(self, method, url, **kwargs):
        from urllib.parse import urlparse

        parsed = urlparse(url)
        assert parsed.netloc == "bigquery.googleapis.com"
        assert kwargs.get("allow_redirects") is False
        payload = json.loads(kwargs["data"]) if kwargs.get("data") else None
        self.calls.append((method, parsed.path, payload))
        if method == "POST":
            job_id = payload["jobReference"]["jobId"]
            lane = job_id.split("_", 4)[-1]
            index = len(self.jobs)
            relation = self._relation(lane)
            project, dataset, table = relation["destination_table"].split(".")
            created = FIRST_JOB + self.shift + timedelta(seconds=20 * index)
            body = json.loads(json.dumps(payload))
            body.update(
                user_email=self.identity,
                status={"state": "DONE"},
                statistics={
                    "creationTime": millis(created),
                    "startTime": millis(created + timedelta(seconds=1)),
                    "endTime": millis(created + timedelta(seconds=5)),
                    "query": {
                        "statementType": "CREATE_SNAPSHOT_TABLE",
                        "ddlOperationPerformed": "CREATE",
                        "ddlTargetTable": {
                            "projectId": project,
                            "datasetId": dataset,
                            "tableId": table,
                        },
                        "totalBytesBilled": self.billed,
                    },
                },
            )
            if lane == self.failed_lane:
                body["status"] = {"state": "DONE", "errorResult": {"reason": "invalidQuery"}}
            self.jobs[job_id] = body
            self.tables[relation["destination_table"]] = self.clone(relation, created)
            self.clock.value = created + timedelta(seconds=6)
            return self._response(method, url, 200, body)
        if "/jobs/" in parsed.path:
            return self._response(method, url, 200, self.jobs[parsed.path.rsplit("/", 1)[1]])
        if "/tables/" in parsed.path:
            parts = parsed.path.split("/")
            name = f"{parts[4]}.{parts[6]}.{parts[8]}"
            if len(self.tables) == len(self.plan["creation_statements"]) and name == next(
                reversed(self.tables)
            ):
                self.clock.value = CAPTURED_AT
            return self._response(method, url, 200, self.tables[name])
        raise AssertionError(f"unexpected {method} {parsed.path}")

    def clone(self, relation, created):
        project, dataset, table = relation["destination_table"].split(".")
        base_project, base_dataset, base_table = relation["source_table"].split(".")
        stamp_at = datetime.strptime(relation["snapshot_as_of"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=UTC
        )
        return {
            "kind": "bigquery#table",
            "etag": f"route-a-{relation['lane']}",
            "location": "US",
            "tableReference": {"projectId": project, "datasetId": dataset, "tableId": table},
            "type": "SNAPSHOT",
            "snapshotDefinition": {
                "baseTableReference": {
                    "projectId": base_project,
                    "datasetId": base_dataset,
                    "tableId": base_table,
                },
                "snapshotTime": stamp_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            },
            "creationTime": millis(created + timedelta(seconds=3)),
            "lastModifiedTime": millis(created + timedelta(seconds=3)),
            "numRows": "12",
            "numBytes": "4096",
            "schema": table_schema(relation["lane"]),
        }


class CreationCredentials:
    """Anonymous credentials that name the runner's service identity."""

    def __new__(cls, identity=IDENTITY):
        from google.auth.credentials import AnonymousCredentials

        credentials = AnonymousCredentials()
        credentials.service_account_email = identity
        return credentials


def storage(inputs):
    """The private capture bucket with the approved inputs seeded, synthetic."""
    from tests.unit import test_production_snapshot_storage as storage_fixture
    from tests.unit.test_protected_snapshot_integration import JoinedStorageHTTP

    class HTTP(JoinedStorageHTTP):
        def seed(self, name, raw, *, generation=None, created=None):
            return storage_fixture.HTTP.seed(
                self, name, raw, generation=generation, created=stamp(STORED_AT)
            )

    http = HTTP()
    for name, raw in inputs.items():
        http.seed(f"inputs/{hashlib.sha256(raw).hexdigest()}/{name}.json", raw)
    objects, _ = storage_fixture.objects(http)
    return objects, http


class Ledger:
    """The approval ledger's consumption and result procedures, synthetic."""

    def __init__(self, approval_record):
        self.approval = approval_record
        self.consume_calls = []
        self.results = []
        self.consumed = set()

    def consume(self, routine, parameters):
        from src.analysis.open_intelligence import execution_approval

        values = {item.name: item.value for item in parameters}
        self.consume_calls.append((routine, values))
        if self.approval.approval_id in self.consumed:
            raise RuntimeError("source_snapshot_slot_consumed")
        self.consumed.add(self.approval.approval_id)
        return {
            "consumption_contract_version": "open_intelligence_execution_consumption_v2",
            "consumption_id": execution_approval.consumption_id_v2(
                self.approval.approval_id,
                values["execution_name"],
                CONSUMED_AT,
                origin_registry_sha256=values["origin_registry_sha256"],
                resource_manifest_sha256=values["resource_manifest_sha256"],
            ),
            "approval_id": self.approval.approval_id,
            "manifest_sha256": values["manifest_sha256"],
            "operation": "source_snapshot_capture",
            "execution_name": values["execution_name"],
            "consumed_at": CONSUMED_AT,
            "origin_registry_sha256": values["origin_registry_sha256"],
            "resource_manifest_sha256": values["resource_manifest_sha256"],
        }

    def write_result(self, request):
        from src.analysis.open_intelligence import execution_approval

        row = {
            **request,
            "result_contract_version": "open_intelligence_execution_result_v2",
            "result_id": execution_approval.result_id_v2(
                request["consumption_id"],
                request["result_reference"],
                request["result_digest"],
                request["status"],
                RESULT_COMPLETED_AT,
                origin_registry_sha256=request["origin_registry_sha256"],
                resource_manifest_sha256=request["resource_manifest_sha256"],
            ),
            "completed_at": RESULT_COMPLETED_AT,
        }
        self.results.append(row)
        return row

    def read_result(self, consumption_id):
        return [row for row in self.results if row["consumption_id"] == consumption_id]


class EvidenceResolver:
    """Stands in for the capture evidence resolver the bridge fix lane still owes."""

    def __init__(self):
        self.calls = []

    def __call__(self, artifacts, **kwargs):
        self.calls.append((sorted(artifacts), sorted(kwargs)))


def evidence_readers():
    from types import SimpleNamespace

    return SimpleNamespace(read_chain=None, read_funded_execution=None)


def freeze_preparation(monkeypatch, instant):
    """The consumer prepares the plan at its own clock; pin that clock for the test."""
    from src.analysis.open_intelligence import execution_approval

    original = execution_approval.prepare_source_snapshot_capture_v3

    def prepared(expected, *, artifact_reader, rule, now):
        return original(expected, artifact_reader=artifact_reader, rule=rule, now=instant)

    monkeypatch.setattr(execution_approval, "prepare_source_snapshot_capture_v3", prepared)
