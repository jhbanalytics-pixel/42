"""Cold read: the engine builds the bytes, this process stores them, a fresh process reads them.

Three processes, no shared memory. The engine interpreter builds the dossier
body from the existing frame and accepted-question fixtures. This process
creates the investigation record and the dossier through the real stores into
a directory-backed, generation-aware bucket double. A fresh app interpreter
then resolves the workspace scope through the real resolver, follows the
pointer through the real dossier store, projects the record and applies the
existing Client Read projection, holding no producer object at any point.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed

from tests.unit.object_creator_bucket import listed_names

APP_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "open-intelligence/v2/staging/"
MARKETS = ["za", "ng", "ke"]


# A directory-backed, generation-aware bucket double
#
# Every object is a file beside a metadata file that carries its generation.
# A fresh process rebuilds the same view from the directory alone, which is
# the point: nothing the producer held in memory survives into the reader.


class DirectoryBlob:
    def __init__(self, bucket, name, generation=None):
        self.bucket = bucket
        self.name = name
        self.generation = generation

    def upload_from_string(self, data, *, content_type, if_generation_match):
        assert content_type == "application/json"
        current = self.bucket.meta(self.name)
        current_generation = current["generation"] if current else 0
        if if_generation_match != current_generation:
            raise PreconditionFailed("precondition")
        generation = self.bucket.next_generation()
        self.bucket.data_path(self.name).write_bytes(data)
        self.bucket.meta_path(self.name).write_text(
            json.dumps({"name": self.name, "generation": generation}), encoding="utf-8"
        )
        self.generation = generation

    def download_as_bytes(self, *, if_generation_match=None, **kwargs):
        current = self.bucket.meta(self.name)
        if current is None:
            raise NotFound("gone")
        if (
            if_generation_match is not None
            and if_generation_match != current["generation"]
        ):
            raise PreconditionFailed("generation moved")
        return self.bucket.data_path(self.name).read_bytes()


class DirectoryBucket:
    name = "listening-post-staging-cache"

    def __init__(self, root):
        self.root = Path(root)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)

    def _key(self, name):
        return hashlib.sha256(name.encode("utf-8")).hexdigest()

    def data_path(self, name):
        return self.root / "objects" / (self._key(name) + ".bin")

    def meta_path(self, name):
        return self.root / "objects" / (self._key(name) + ".meta.json")

    def meta(self, name):
        path = self.meta_path(name)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def next_generation(self):
        counter = self.root / "generation"
        value = int(counter.read_text(encoding="utf-8")) if counter.is_file() else 1000
        value += 1
        counter.write_text(str(value), encoding="utf-8")
        return value

    def blob(self, name):
        return DirectoryBlob(self, name)

    def get_blob(self, name):
        current = self.meta(name)
        if current is None:
            return None
        return DirectoryBlob(self, name, current["generation"])

    def list_blobs(self, *, prefix, max_results=None, delimiter=None):
        generations = {}
        for path in (self.root / "objects").glob("*.meta.json"):
            current = json.loads(path.read_text(encoding="utf-8"))
            generations[current["name"]] = current["generation"]
        return [
            DirectoryBlob(self, name, generations[name])
            for name in listed_names(generations, prefix, max_results, delimiter)
        ]


# The three steps

ENGINE_SCRIPT = """
import base64, json
from tests.unit import test_investigation_dossier as fixture
from src.analysis.open_intelligence.investigation_dossier import build_dossier_body, dossier_version
frame = fixture.frame()
frame["market_scope"] = %(markets)s
scope = fixture.scope()
scope["market_scope"] = %(markets)s
values = fixture.inputs(frame=frame, scope=scope, investigation_id=fixture.frame_validator(frame))
body = build_dossier_body(**values)
print(json.dumps({
    "body": base64.b64encode(body).decode("ascii"),
    "version": dossier_version(body),
    "frame": frame,
    "request_id": values["request_reference"]["request_id"],
}))
"""

COLD_READ_SCRIPT = """
import hashlib, importlib.util, json, sys
root, investigation_id, module_path = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("cold_read_double", module_path)
double = importlib.util.module_from_spec(spec)
spec.loader.exec_module(double)
bucket = double.DirectoryBucket(root)
from src.api import dossier_resolver, dossier_store, intelligence_dossier, workspace_scope
workspace_scope._workspace_bucket = lambda: bucket
scope = workspace_scope.resolve_workspace_scope(investigation_id)
resolution = dossier_resolver.resolve_dossier(scope, bucket=bucket)
working = dossier_resolver.working_response(resolution, [])
exact = dossier_store.read_dossier_version(
    bucket=bucket,
    prefix=dossier_store.STAGING_PREFIX,
    investigation_id=investigation_id,
    dossier_version=resolution["dossier_version"],
    scope_digest=scope.scope_digest,
)
client_read = intelligence_dossier.read_dossier(investigation_id, working["projection"])
import os
os.environ["LP_ALLOW_OPEN_GATE"] = "true"
os.environ.pop("UI_PASSCODE", None)
from fastapi.testclient import TestClient
from src.api import main
main._investigation_bucket = lambda: bucket
route = TestClient(main.app).get(
    f"/api/internal/v2/investigations/{investigation_id}/dossier/read"
    "?contract_version=intelligence_dossier_v1"
)
print(json.dumps({
    "scope_digest": scope.scope_digest,
    "resolution": resolution,
    "working": working,
    "client_read": client_read,
    "route_status": route.status_code,
    "route_payload": route.json(),
    "stored_sha256": hashlib.sha256(exact["body"]).hexdigest(),
    "stored_generation": exact["generation"],
    "stored_object_name": exact["object_name"],
    "in_memory_producer_modules": [
        name for name in sys.modules if "investigation_dossier" in name
    ],
}))
"""


def _engine_runtime():
    configured_engine = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_ROOT")
    configured_python = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_PYTHON")
    if bool(configured_engine) != bool(configured_python):
        pytest.fail("Both engine integration test paths must be configured together")
    engine = (
        Path(configured_engine) if configured_engine else APP_ROOT.parent / "engine"
    )
    interpreter = Path(configured_python) if configured_python else Path(sys.executable)
    producer = engine / "src/analysis/open_intelligence/investigation_dossier.py"
    if configured_engine:
        assert producer.is_file()
        assert interpreter.is_file()
    elif not producer.is_file():
        pytest.skip("Engine checkout required for the cold-read regression")
    return engine, interpreter


def _run(interpreter, script, cwd, *args):
    completed = subprocess.run(
        [str(interpreter), "-c", script, *args],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _produce_and_store(tmp_path):
    """Engine builds the bytes; this process stores them through the real stores."""
    from src.api import dossier_store, investigation_store, investigations

    engine, engine_python = _engine_runtime()
    produced = _run(
        engine_python, ENGINE_SCRIPT % {"markets": json.dumps(MARKETS)}, engine
    )
    body = base64.b64decode(produced["body"])
    expected_version = hashlib.sha256(body).hexdigest()
    assert produced["version"] == expected_version

    # The app's own identity rule agrees with the engine's injected validator.
    frame = investigations.investigation_frame_from_payload(produced["frame"])
    investigation_id = investigations.investigation_id_for_frame(frame)
    assert json.loads(body)["investigation_id"] == investigation_id
    assert produced["request_id"] != investigation_id
    assert not produced["request_id"].startswith("inv_")

    bucket = DirectoryBucket(tmp_path / "bucket")
    response = investigations.build_plan_ready(
        frame=frame,
        plan=investigations.default_plan_for_frame(frame),
        investigation_id=investigation_id,
        created_at=datetime(2026, 9, 12, 0, tzinfo=UTC),
        active_role_versions={},
    )
    investigation_store.create_investigation(
        bucket=bucket,
        prefix=PREFIX,
        record=investigations.build_investigation_storage_record(frame, response),
    )
    created = dossier_store.create_dossier(
        bucket=bucket,
        prefix=PREFIX,
        body=body,
        publication={
            "source_binding_digest": json.loads(body)["bindings"][
                "source_binding_digest"
            ],
            "publisher_ref": "engine:dossier-producer",
            "publisher_build_digest": json.loads(body)["bindings"][
                "publisher_build_digest"
            ],
            "created_at": "2026-09-12T00:00:01Z",
            "authority_ref": "authority:cold-read-fixture",
        },
        pointer_writer=lambda pointer: dossier_store.publish_pointer(
            pointer, bucket=bucket, prefix=PREFIX
        ),
    )
    object_name = created["publication"]["object_name"]
    stored_bytes = bucket.data_path(object_name).read_bytes()
    assert stored_bytes == body
    return {
        "investigation_id": investigation_id,
        "version": expected_version,
        "generation": created["publication"]["generation"],
        "object_name": object_name,
        "stored_bytes": stored_bytes,
    }


def _cold_read(tmp_path, investigation_id):
    return _run(
        sys.executable,
        COLD_READ_SCRIPT,
        APP_ROOT,
        str(tmp_path / "bucket"),
        investigation_id,
        str(Path(__file__).resolve()),
    )


def test_dossier_created_through_the_store_cold_reads_through_the_resolver(tmp_path):
    from src.api import dossier_store

    stored = _produce_and_store(tmp_path)
    investigation_id = stored["investigation_id"]
    expected_version = stored["version"]
    published_generation = stored["generation"]
    stored_bytes = stored["stored_bytes"]
    del stored

    cold = _cold_read(tmp_path, investigation_id)

    assert cold["in_memory_producer_modules"] == []
    resolution = cold["resolution"]
    assert resolution["state"] == "ready"
    assert resolution["dossier_version"] == expected_version
    assert resolution["generation"] == published_generation
    assert cold["stored_sha256"] == expected_version
    assert cold["stored_generation"] == published_generation
    assert cold["stored_object_name"] == (
        f"{PREFIX}dossiers/{investigation_id}/{expected_version}.json"
    )
    dossier = resolution["dossier"]
    assert dossier == json.loads(stored_bytes)
    assert dossier["investigation_id"] == investigation_id
    assert dossier["scope"]["market_scope"] == MARKETS
    assert dossier["request_reference"]["request_id"] != investigation_id
    assert dossier["frame"]["decision_question"].startswith("Which emerging behaviour")
    assert dossier["admitted_answer"]["state"] == "complete"
    assert dossier["review_state"]["unresolved_questions"][0]["blocking"] is True
    assert resolution["publication"]["publisher_ref"] == "engine:dossier-producer"
    assert dossier_store.scope_digest_for_record(dossier) == cold["scope_digest"]

    # Citations resolve against storage: every cited receipt is in the
    # retained snapshot, and every projected evidence id is a receipt id.
    receipts = {item["receipt_id"] for item in dossier["evidence_snapshot"]["receipts"]}
    projection = cold["working"]["projection"]
    assert {item["evidence_id"] for item in projection["evidence"]} == receipts
    for item in projection["claims"]:
        assert item["citations"]
        assert set(item["citations"]) <= receipts
    assert projection["cutoff"] == dossier["temporal_binding"]["source_cutoff"]
    assert all(item["evidence_state"] == "unchecked" for item in projection["claims"])
    assert all(item["status"] == "pending" for item in projection["claims"])

    client_read = cold["client_read"]
    assert tuple(client_read) == (
        "contract_version",
        "investigation_id",
        "concise_answer",
        "claims",
        "evidence",
        "excluded_count",
        "excluded_reasons",
        "artifact_readiness",
    )
    assert client_read["claims"] == []
    assert client_read["artifact_readiness"]["state"] == "blocked"
    assert cold["working"]["dossier"] == dossier
    # The console route, in the same fresh process, resolves through the
    # store and hands back exactly the client read, with no producer loaded.
    assert cold["route_status"] == 200
    assert cold["route_payload"] == client_read


RESOLVE_LINE = "resolution = dossier_resolver.resolve_dossier(scope, bucket=bucket)"
REPORT_LINE = "print(json.dumps(resolution)); raise SystemExit(0)"
COLD_ROUTE_SCRIPT = """
import importlib.util, json, os, sys
root, investigation_id, module_path = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("cold_read_double", module_path)
double = importlib.util.module_from_spec(spec)
spec.loader.exec_module(double)
bucket = double.DirectoryBucket(root)
os.environ["LP_ALLOW_OPEN_GATE"] = "true"
os.environ.pop("UI_PASSCODE", None)
from fastapi.testclient import TestClient
from src.api import main, workspace_scope
workspace_scope._workspace_bucket = lambda: bucket
main._investigation_bucket = lambda: bucket
route = TestClient(main.app).get(
    f"/api/internal/v2/investigations/{investigation_id}/dossier/read"
    "?contract_version=intelligence_dossier_v1"
)
print(json.dumps({
    "status": route.status_code,
    "payload": route.json(),
    "in_memory_producer_modules": [
        name for name in sys.modules if "investigation_dossier" in name
    ],
}))
"""


@pytest.mark.parametrize("tamper", ["generation_moved", "bytes_padded"])
def test_tampered_storage_cold_reads_as_a_named_unavailable_state(tmp_path, tamper):
    stored = _produce_and_store(tmp_path)
    bucket = DirectoryBucket(tmp_path / "bucket")
    if tamper == "generation_moved":
        meta = bucket.meta(stored["object_name"])
        meta["generation"] = bucket.next_generation()
        bucket.meta_path(stored["object_name"]).write_text(
            json.dumps(meta), encoding="utf-8"
        )
        reason = "dossier_generation_mismatch"
    else:
        padded = json.dumps(
            json.loads(stored["stored_bytes"]), sort_keys=True, indent=1
        )
        bucket.data_path(stored["object_name"]).write_bytes(padded.encode("utf-8"))
        reason = "dossier_version_mismatch"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            COLD_READ_SCRIPT.replace(RESOLVE_LINE, RESOLVE_LINE + "; " + REPORT_LINE),
            str(tmp_path / "bucket"),
            stored["investigation_id"],
            str(Path(__file__).resolve()),
        ],
        cwd=APP_ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    resolution = json.loads(completed.stdout.strip().splitlines()[-1])
    assert resolution["state"] == "unavailable"
    assert resolution["reason"] == reason
    assert resolution["dossier"] is None


def test_a_cold_console_read_of_an_unpublished_dossier_is_named_not_empty(tmp_path):
    """Break caught: the old read of a field the scope never had, rendering as ready."""
    stored = _produce_and_store(tmp_path)
    bucket = DirectoryBucket(tmp_path / "bucket")
    # The first entry of the append-only current pointer log.
    pointer = f"{PREFIX}dossiers/{stored['investigation_id']}/current/{1:020d}.json"
    bucket.meta_path(pointer).unlink()
    bucket.data_path(pointer).unlink()
    cold = _run(
        sys.executable,
        COLD_ROUTE_SCRIPT,
        APP_ROOT,
        str(tmp_path / "bucket"),
        stored["investigation_id"],
        str(Path(__file__).resolve()),
    )
    assert cold["in_memory_producer_modules"] == []
    assert cold["status"] == 404
    assert cold["payload"]["detail"]["code"] == "dossier_unavailable"
    assert cold["payload"]["detail"]["reason"] == "dossier_unpublished"
    assert "claims" not in json.dumps(cold["payload"])
