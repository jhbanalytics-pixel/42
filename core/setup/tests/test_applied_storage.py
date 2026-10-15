"""Synthetic proof boundaries for composed jobs and services release storage."""

import datetime as dt
from pathlib import Path

import pytest

from core.setup.release import durable_prior

ROOT = Path(__file__).resolve().parents[3]


def test_standard_services_manifest_still_refuses_changed_storage():
    with pytest.raises(durable_prior.Refused, match="no storage change"):
        durable_prior.build_manifest(
            ROOT,
            "e93d32f8e72e6e55eb45a4c53dfb66f50f73f7f9",
            "c31d1c6e964da3c4bd9b56a58ccbf416284b33b1",
            "Synthetic fixture owner",
        )


def test_fixed_receipt_shape_refuses_forged_hash_and_future_or_expired_capture(
    tmp_path,
):
    from core.setup.release import applied_storage as storage

    now = dt.datetime(2026, 10, 14, 7, tzinfo=dt.timezone.utc)
    body = {
        "schema_version": 1,
        "kind": storage.KIND,
        "source": {"commit": "1" * 40, "tree": "2" * 40},
        "jobs": {
            "release_id": "rel-1111111-01",
            "image_digest": "sha256:" + "3" * 64,
            "packet_dir": str(tmp_path),
        },
        "files": {key: "4" * 64 for key in storage.FILE_ROLES},
        "installer_receipts": {"runs/synthetic/warehouse-install-0001.json": "5" * 64},
        "storage_effects_sha256": "6" * 64,
        "captured_utc": now.isoformat(),
        "deferred_effect_ids": ["E-JOB-VIEWS"],
    }
    body["receipt_sha256"] = storage.receipt_hash(body)
    storage.validate_receipt(body, "1" * 40, "2" * 40, now=now, max_age_minutes=360)
    wrong = {**body, "receipt_sha256": "0" * 64}
    with pytest.raises(storage.Refused):
        storage.validate_receipt(
            wrong, "1" * 40, "2" * 40, now=now, max_age_minutes=360
        )
    with pytest.raises(storage.Refused):
        storage.validate_receipt(
            body,
            "1" * 40,
            "2" * 40,
            now=now - dt.timedelta(seconds=1),
            max_age_minutes=360,
        )
    with pytest.raises(storage.Refused):
        storage.validate_receipt(
            body,
            "1" * 40,
            "2" * 40,
            now=now + dt.timedelta(minutes=361),
            max_age_minutes=360,
        )


def test_dependency_closure_includes_required_uninstalled_deferred_member():
    from core.setup.release import applied_storage as storage

    definitions = {
        "intelligence_42_core.outer": {
            "kind": "view",
            "target": "intelligence_42_core.outer",
            "body": "SELECT * FROM intelligence_42_core.v_item_evidence",
        },
        "intelligence_42_core.v_item_evidence": {
            "kind": "view",
            "target": "intelligence_42_core.v_item_evidence",
            "body": "SELECT 1 x",
        },
    }
    assert storage.object_closure({"intelligence_42_core.outer"}, definitions) == set(
        definitions
    )
    with pytest.raises(storage.Refused):
        storage.object_closure(
            {"intelligence_42_core.outer"},
            {"intelligence_42_core.outer": definitions["intelligence_42_core.outer"]},
        )


def test_receipt_effect_has_only_existing_read_only_service_contract():
    from core.setup.release import applied_storage as storage

    effect = storage.receipt_effect(
        "rel-1111111-01", "a" * 64, {"lane": 3, "person": "Synthetic fixture owner"}, 7
    )
    assert (effect["kind"], effect["apply_kind"], effect["change"]) == (
        "receipt",
        "code",
        "write_shape",
    )
    assert effect["native_readback"]["kind"] == "helper_phase"
    assert effect["evidence"] == {
        "applied_jobs_storage": {
            "receipt_file": storage.RECEIPT_FILE,
            "receipt_sha256": "a" * 64,
        }
    }
    assert effect["apply_kind"] not in ("schema", "warehouse_install", "job_image")


def test_absent_dependency_or_wrong_argument_blocks_even_when_all_installer_objects_match():
    from core.setup.release import applied_storage as storage

    wanted = {
        "kind": "routine",
        "target": "intelligence_42_agent.tvf_search_posts_scoped",
        "routineType": "TABLE_VALUED_FUNCTION",
        "language": "SQL",
        "arguments": [{"name": "q", "dataType": {"typeKind": "STRING"}}],
        "body": "SELECT 1",
    }
    with pytest.raises(storage.Refused):
        storage.compare_native({"x": wanted}, {"x": {**wanted, "arguments": []}})
    with pytest.raises(storage.Refused):
        storage.compare_native({"x": wanted}, {})


def test_actual_source_dependency_inventory_includes_deferred_view():
    from core.setup.release import applied_storage as storage

    definitions = storage.dependency_inventory(
        ROOT, "e93d32f8e72e6e55eb45a4c53dfb66f50f73f7f9"
    )
    assert "intelligence_42_agent.v_item_evidence" in definitions
    assert definitions["intelligence_42_agent.v_item_evidence"]["kind"] == "view"


def test_real_fake_jobs_install_and_update_produce_read_only_storage_proof(
    tmp_path, monkeypatch
):
    import shutil
    from core.schema import release_install
    from core.setup.release import applied_storage as storage
    from core.setup.tests.jobs_e2e_world import EndToEnd
    from core.setup.tests.e2e_shim import WarehouseBq
    from core.setup.tests import jobs_packet_world as jpw

    make_jobs_repo = jpw.make_jobs_repo

    def complete_source_repo(path):
        path = make_jobs_repo(path)
        shutil.copytree(
            ROOT / "core",
            path / "core",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        jpw.pw.git(path, "add", "-A")
        jpw.pw.git(path, "commit", "-q", "-m", "complete synthetic service source")
        return path

    monkeypatch.setattr(jpw, "make_jobs_repo", complete_source_repo)

    original_archive = storage.de.git_tree_files
    immutable_baseline = original_archive(
        "a80be1dee7f4ee2aa9775d0f353bab930809f057", cwd=ROOT
    )

    def fixture_archive(rev, prefix="core", cwd=ROOT):
        if rev == "a80be1dee7f4ee2aa9775d0f353bab930809f057":
            return immutable_baseline
        return original_archive(rev, prefix, cwd)

    monkeypatch.setattr(storage.de, "git_tree_files", fixture_archive)
    run = EndToEnd(tmp_path / "synthetic", monkeypatch).make_packet()
    import os

    fake_path = str(run.bin) + os.pathsep + os.environ["PATH"]
    for name in ("py", "gcloud"):
        expected = run.bin / (name + ".cmd" if os.name == "nt" else name)
        assert Path(shutil.which(name, path=fake_path)).resolve() == expected.resolve()
    for action in ("JobsCandidate", "JobsUpdate"):
        result = run.paste(action)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert result.commands and all(argv[0] in ("py", "gcloud") for argv in result.commands)
        assert all(argv[1:3] == ["config", "list"] for argv in result.commands if argv[0] == "gcloud")
    packet = run.bench.packet
    conventional = packet.parent / "locks/jobs" / run.bench.lock_file().name
    conventional.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(run.bench.lock_file(), conventional)
    state = run.state()
    before = {k: v.copy() for k, v in state["warehouse_objects"].items()}

    complete = storage.dependency_inventory(run.bench.repo, run.bench.target)
    from google.cloud.bigquery import Model, Routine, Table
    from google.api_core.exceptions import NotFound
    import re

    class MetadataClient:
        def get_table(self, full):
            wanted = complete[full.removeprefix(storage.so.PROJECT + ".")]
            if wanted["kind"] == "absent":
                raise NotFound("synthetic source-proved absence")
            return Table.from_api_repr(
                {
                    "tableReference": {
                        "projectId": storage.so.PROJECT,
                        "datasetId": wanted["target"].split(".")[0],
                        "tableId": wanted["target"].split(".")[1],
                    },
                    "type": "VIEW",
                    "view": {"query": wanted["body"]},
                }
            )

        def get_routine(self, full):
            wanted = complete[full.removeprefix(storage.so.PROJECT + ".")]
            return Routine.from_api_repr(
                {
                    "routineReference": {
                        "projectId": storage.so.PROJECT,
                        "datasetId": wanted["target"].split(".")[0],
                        "routineId": wanted["target"].split(".")[1],
                    },
                    "routineType": wanted["routineType"],
                    "language": wanted["language"],
                    "arguments": wanted["arguments"],
                    "definitionBody": wanted["body"],
                }
            )

        def get_model(self, full):
            assert full == "ogilvy-trends-v2.intelligence_42_core.embed_gemini"
            return Model.from_api_repr(
                {
                    "modelReference": {
                        "projectId": "ogilvy-trends-v2",
                        "datasetId": "intelligence_42_core",
                        "modelId": "embed_gemini",
                    },
                    "remoteModelInfo": {
                        "endpoint": "projects/ogilvy-trends-v2/locations/us-central1/publishers/google/models/gemini-embedding-001",
                        "connection": "ogilvy-trends-v2.us.f42-vertex",
                    },
                }
            )

    class MetadataBq:
        _client = MetadataClient()

        def dry_run(self, sql):
            return 0

        def query(self, sql, params, cap):
            dataset = re.search(r"\.([a-z_0-9]+)\.INFORMATION_SCHEMA", sql)[1]
            name = re.search(r"table_name = '([^']+)'", sql)[1]
            return complete[dataset + "." + name]["columns"]

        def definition(self, typed):
            return release_install.definition(WarehouseBq(state), typed)[0]

    class ReadOnlyNative(storage.Native):
        def __init__(self, bound):
            self.bound = bound
            self.bq = MetadataBq()

        def verify_jobs(self, bound, digest):
            assert set(run.job_images().values()) == {digest}

    output = tmp_path / "applied-jobs-storage-receipt.json"
    body = storage.produce(
        run.bench.repo,
        run.bench.target,
        run.bench.tree,
        packet,
        output,
        now=run.bench.fixture.now,
        native_factory=ReadOnlyNative,
    )
    assert output.is_file() and body["deferred_effect_ids"] == ["E-JOB-VIEWS"]
    assert state["warehouse_objects"] == before
    services = tmp_path / "services"
    services.mkdir()
    shutil.copyfile(output, services / storage.RECEIPT_FILE)
    manifest = {
        "schema_version": 1,
        "source": {"commit": run.bench.target, "tree": run.bench.tree},
        "effects": [
            storage.receipt_effect(
                body["jobs"]["release_id"],
                storage.so.sha_bytes(output.read_bytes()),
                {"lane": 3, "person": "Synthetic fixture owner"},
                1,
            )
        ],
    }
    bound = {
        "mode": "services-only",
        "target": run.bench.target,
        "tree": run.bench.tree,
        "maxSmokeAgeMinutes": 360,
    }
    assert storage.verify(
        manifest,
        bound,
        services,
        repo=run.bench.repo,
        now=run.bench.fixture.now,
        native_factory=ReadOnlyNative,
    ) == storage.so.sha_bytes(output.read_bytes())
    target = "intelligence_42_agent.v_item_evidence"
    native_view = state["warehouse_objects"][storage.so.PROJECT + "." + target]["view"]
    saved_body = native_view["query"]
    native_view["query"] = "SELECT 0 AS wrong"
    assert ReadOnlyNative(bound).definition(complete[target])["body"] == "SELECT 0 AS wrong"
    with pytest.raises(storage.Refused, match="drift"):
        storage.verify(
            manifest,
            bound,
            services,
            repo=run.bench.repo,
            now=run.bench.fixture.now,
            native_factory=ReadOnlyNative,
        )
    native_view["query"] = saved_body
    with pytest.raises(storage.Refused, match="expired"):
        storage.verify(
            manifest,
            bound,
            services,
            repo=run.bench.repo,
            now=run.bench.fixture.now + dt.timedelta(minutes=361),
            native_factory=ReadOnlyNative,
        )
    import json

    latest = storage._latest(packet, "runs/*/jobs-update-*.json")
    original = latest.read_bytes()
    changed = json.loads(original)
    changed["complete"] = False
    latest.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(storage.Refused, match="incomplete"):
        storage.verify(
            manifest,
            bound,
            services,
            repo=run.bench.repo,
            now=run.bench.fixture.now,
            native_factory=ReadOnlyNative,
        )
    latest.write_bytes(original)
    from core.setup.tests import release_world as rw

    monkeypatch.setattr(rw, "COMMIT", run.bench.target)
    monkeypatch.setattr(rw, "TREE", run.bench.tree)
    monkeypatch.setattr(rw, "SHORT12", run.bench.target[:12])
    service_world = rw.World(jobs_digest=body["jobs"]["image_digest"])
    original_deploy = service_world.deploy_candidate
    service_world.deploy_candidate = lambda name, **kw: original_deploy(
        name, version=run.bench.target[:12], **kw
    )
    healthy = (
        200,
        {"ok": True, "checks": {"agent": "ok"}, "version": run.bench.target[:12]},
    )
    service_world.health[rw.CANON["f42-api"]] = [healthy]
    service_world.health[
        storage.so.tag_url(body["jobs"]["release_id"], rw.CANON["f42-api"])
    ] = [healthy]
    scenario = rw.Scenario(
        tmp_path / "service-flow",
        world=service_world,
        release_id=body["jobs"]["release_id"],
    )
    scenario.now = run.bench.fixture.now
    write_smoke = scenario.write_smoke_receipt
    scenario.write_smoke_receipt = lambda: write_smoke(
        started_utc=scenario.now.isoformat(), ended_utc=scenario.now.isoformat()
    )
    scenario_path = scenario.release_dir / storage.RECEIPT_FILE
    shutil.copyfile(output, scenario_path)
    manifest_path = scenario.release_dir / "durable-effects-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    scenario.bound.update(
        durableManifestPath=str(manifest_path),
        durableManifestSha256=storage.so.sha_bytes(manifest_path.read_bytes()),
    )
    real_verify = storage.verify

    def fixture_verify(value, bindings, packet_dir, **kwargs):
        return real_verify(
            value,
            bindings,
            packet_dir,
            repo=run.bench.repo,
            native_factory=ReadOnlyNative,
            **kwargs,
        )

    monkeypatch.setattr(storage, "verify", fixture_verify)
    assert scenario.to("AfterPromotion")["phase"] == "AfterPromotion"
    assert state["warehouse_objects"] == before
    scenario.now += dt.timedelta(minutes=361)
    assert scenario.stop("BeforeSmoke").code == "APPLIED_STORAGE"
    assert scenario.run("BeforeRollback")["phase"] == "BeforeRollback"
    with pytest.raises(storage.Refused, match="never renewed"):
        storage.produce(
            run.bench.repo,
            run.bench.target,
            run.bench.tree,
            packet,
            output,
            now=run.bench.fixture.now,
            native_factory=ReadOnlyNative,
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT intelligence_42_agent.missing_fn(1)",
        "SELECT * FROM other_dataset.missing",
        "SELECT * FROM `foreign.intelligence_42_core.missing`",
        "SELECT * FROM missing",
    ],
)
def test_parser_refuses_unclosed_scalar_foreign_and_unqualified_dependencies(sql):
    from core.setup.release import applied_storage as storage

    with pytest.raises(storage.Refused):
        storage.object_closure({"outer"}, {"outer": {"body": sql}})


def test_native_model_matches_retained_metadata_and_refuses_changed_endpoint_connection():
    import copy
    from google.cloud.bigquery import Model
    from core.setup.release import applied_storage as storage

    api = {
        "modelReference": {
            "projectId": "ogilvy-trends-v2",
            "datasetId": "intelligence_42_core",
            "modelId": "embed_gemini",
        },
        "remoteModelInfo": {
            "endpoint": "projects/ogilvy-trends-v2/locations/us-central1/publishers/google/models/gemini-embedding-001",
            "connection": "ogilvy-trends-v2.us.f42-vertex",
        },
    }

    class Client:
        def get_model(self, target):
            assert target == "ogilvy-trends-v2.intelligence_42_core.embed_gemini"
            return Model.from_api_repr(api)

    bq = type("Bq", (), {"_client": Client()})()
    native = storage.Native({}, bq=bq, reader=object())
    wanted = {
        "kind": "model",
        "target": "intelligence_42_core.embed_gemini",
        "body": "",
        "endpoint": "gemini-embedding-001",
        "connection": "ogilvy-trends-v2.us.f42-vertex",
    }
    storage.compare_native({"model": wanted}, {"model": native.definition(wanted)})
    for field in ("endpoint", "connection"):
        saved = copy.deepcopy(api)
        api["remoteModelInfo"][field] += "-foreign"
        with pytest.raises(storage.Refused):
            storage.compare_native(
                {"model": wanted}, {"model": native.definition(wanted)}
            )
        api.clear()
        api.update(saved)


def test_optional_absence_requires_every_caller_guard_and_fresh_native_absence():
    import ast
    from google.api_core.exceptions import NotFound
    from core.setup.release import applied_storage as storage

    source = """class Store:
    def exists(self):
        return f"{CORE}.optional" in self._catalog()["objects"]
    def read(self):
        return [self._t(f"{CORE}.optional")] if self.exists() else []
"""
    target = "intelligence_42_core.optional"
    assert storage._guarded_absence(ast.parse(source), target)
    assert not storage._guarded_absence(
        ast.parse(
            source
            + '    def required(self):\n        return self._t(f"{CORE}.optional")\n'
        ),
        target,
    )

    class Client:
        present = False

        def get_table(self, target):
            if not self.present:
                raise NotFound("synthetic absent")
            return object()

    client = Client()
    native = storage.Native(
        {}, bq=type("Bq", (), {"_client": client})(), reader=object()
    )
    wanted = {"kind": "absent", "target": target, "body": ""}
    assert native.definition(wanted) == wanted
    client.present = True
    with pytest.raises(storage.Refused, match="became present"):
        native.definition(wanted)


@pytest.mark.parametrize(
    "insert",
    [
        "import core.not_in_tree\n",
        "from core.not_in_tree import missing\n",
        'from importlib import import_module as load\nload("core.not_in_tree")\n',
        '__import__("core.not_in_tree")\n',
        'import importlib\nif unknown_condition:\n    unknown_module = "core.api.store"\nimportlib.import_module(unknown_module)\n',
    ],
)
def test_source_closure_refuses_missing_or_unresolved_first_party_imports(
    monkeypatch, insert
):
    from core.setup.release import applied_storage as storage

    files = dict(
        storage.de.git_tree_files("e93d32f8e72e6e55eb45a4c53dfb66f50f73f7f9", cwd=ROOT)
    )
    files["core/api/app.py"] = insert + files["core/api/app.py"]
    monkeypatch.setattr(
        storage.de, "git_tree_files", lambda *args, **kw: list(files.items())
    )
    with pytest.raises(storage.Refused, match="import"):
        storage.dependency_inventory(ROOT, "e93d32f8e72e6e55eb45a4c53dfb66f50f73f7f9")


@pytest.mark.parametrize(
    "action,count", [("Candidate", 5), ("Promote", 2), ("Rollback", 0), ("Retire", 0)]
)
def test_actual_paste_has_fresh_fixed_forward_gates_and_preserves_recovery(
    tmp_path, action, count
):
    from core.setup.release import applied_storage as storage
    from core.setup.tests.paste_world import PasteWorld, RID

    durable = {
        "schema_version": 1,
        "effects": [
            {"apply_kind": "tag_add"},
            {"apply_kind": "traffic_pin"},
            storage.receipt_effect(
                RID, "a" * 64, {"lane": 3, "person": "Synthetic fixture owner"}, 3
            ),
        ],
    }
    world = PasteWorld(tmp_path, action, durable=durable)
    if action != "Candidate":
        world.after_smoke()
    result = world.run()
    assert result.returncode == 0, result.stderr
    proofs = [
        c
        for c in result.calls
        if c.get("kind") == "run" and c.get("name") == "applied-storage-readbacks"
    ]
    assert len(proofs) == count
    assert all(not c["passcode_sha"] for c in proofs)
    if action == "Candidate":
        runs = [c["name"] for c in result.calls if c.get("kind") == "run"]
        assert runs[runs.index("smoke") - 1] == "applied-storage-readbacks"


def test_actual_paste_refuses_at_final_smoke_gate_before_secret_or_paid_call(tmp_path):
    from core.setup.release import applied_storage as storage
    from core.setup.tests.paste_world import PasteWorld, RID

    durable = {
        "schema_version": 1,
        "effects": [
            {"apply_kind": "tag_add"},
            {"apply_kind": "traffic_pin"},
            storage.receipt_effect(
                RID, "a" * 64, {"lane": 3, "person": "Synthetic fixture owner"}, 3
            ),
        ],
    }
    good = PasteWorld(tmp_path / "good", durable=durable).run()
    assert good.returncode == 0, good.stderr
    runs = [c for c in good.calls if c.get("kind") == "run"]
    smoke = next(i for i, c in enumerate(runs) if c["name"] == "smoke")
    proof_number = smoke
    injection = "$Script:Cfg.exits | Add-Member -NotePropertyName 'applied-storage-readbacks' -NotePropertyValue 1 -Force"
    bad = PasteWorld(tmp_path / "bad", durable=durable).run(
        extra={"inject": injection, "inject_at_run": proof_number}
    )
    assert bad.returncode != 0
    assert not any(c.get("name") == "smoke" for c in bad.calls)
    assert all(not c["passcode_sha"] for c in bad.calls if c.get("kind") == "run")


@pytest.mark.parametrize(
    "addition",
    [
        '\ndef unsafe(bq, dataset, table):\n    return bq.query(f"SELECT * FROM `{dataset}.{table}`")\n',
        '\ndef unsafe(bq, table):\n    return bq.query(f"SELECT * FROM `intelligence_42_core.{table}`")\n',
        '\nREVIEW_TABLE = "review_missing_table"\ndef unsafe(bq, CORE):\n    return bq.query(f"SELECT * FROM `{CORE}.{REVIEW_TABLE}`")\n',
        '\nREVIEW_TABLES = {"known": "posts", "bad": "review_missing_table"}\ndef unsafe(bq, kind):\n    return bq.query(f"SELECT * FROM `intelligence_42_core.{REVIEW_TABLES[kind]}`")\n',
        '\nclass Shadow:\n    def read(self, bq):\n        self.project = "foreign"\n        return bq.query(f"SELECT * FROM `{self.project}.intelligence_42_core.posts`")\n',
    ],
)
def test_source_refuses_unresolved_dynamic_sql_namespace(monkeypatch, addition):
    from core.setup.release import applied_storage as storage

    files = dict(
        storage.de.git_tree_files("e93d32f8e72e6e55eb45a4c53dfb66f50f73f7f9", cwd=ROOT)
    )
    files["core/api/app.py"] += addition
    monkeypatch.setattr(
        storage.de, "git_tree_files", lambda *args, **kw: list(files.items())
    )
    with pytest.raises(
        storage.Refused, match="dynamic SQL object|dependency is unknown"
    ):
        storage.dependency_inventory(ROOT, "e93d32f8e72e6e55eb45a4c53dfb66f50f73f7f9")
