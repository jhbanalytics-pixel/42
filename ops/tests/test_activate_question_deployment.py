"""Guarded activation producer: the ordered chain, every refusal, the idempotent
rerun, the dry run, and acceptance by release.verify_activation and release.py plan.
"""

import hashlib
import json
from datetime import timedelta

import pytest

from ops.deploy import activate_question_deployment as activate
from ops.deploy import release
from ops.deploy import release_native_adapter as native
from ops.tests.test_foundation_release import (
    ADAPTER_PATH,
    BUNDLE,
    ENGINE_COMMIT,
    GATE_SHA,
    LEDGER_NAME,
    LP_COMMIT,
    MANIFEST_PATH,
    NEW_IMAGE,
    OLD_IMAGE,
    OLD_LP_COMMIT,
    PREFIX,
    QUEUE_API,
    SERVICE,
    VERIFIED_AT,
    authority,
    base_state,
    fake,
    make_activation,
    make_binding,
    make_ledger,
    make_policy,
    make_service,
    write_json,
)
from ops.tests.test_release_native_adapter import TOKEN, FakeCloud, live_window

REVISION = SERVICE + "-q-new"


class Prepared:
    """Old revision serving, queue running, new image built, nothing activated yet."""

    def __init__(self, tmp_path, *, queue_state="RUNNING", tasks=(), objects=None):
        self.dir = tmp_path
        self.policy = make_policy()
        self.old_policy = make_policy(VERIFIED_AT - timedelta(hours=1))
        self.old_binding = make_binding(
            self.old_policy,
            revision=SERVICE + "-q-old",
            image_digest=OLD_IMAGE,
            lp_commit=OLD_LP_COMMIT,
        )
        self.binding = make_binding(
            self.policy, revision=REVISION, image_digest=NEW_IMAGE
        )
        self.ledger = make_ledger(self.old_binding, self.old_policy)
        self.state = base_state(
            ledger=self.ledger,
            service=make_service(self.old_binding, "question-old0000"),
            policies=[self.policy, self.old_policy],
            bindings=[self.old_binding],
            tasks=tasks,
            queue_state=queue_state,
        )
        self.state["objects"].update(objects or {})
        self.state_path = write_json(tmp_path / "state.json", self.state)
        build = make_activation(self.binding, self.policy, self.ledger)["build"]
        self.build_path = write_json(tmp_path / "build.json", build)
        self.authority_path = write_json(
            tmp_path / "authority.json", authority("release")
        )
        self.output = tmp_path / "activation.json"

    def binding_name(self):
        return PREFIX + f"deployments/{self.binding['deployment_digest']}/binding.json"

    def activated(self):
        ledger = json.loads(json.dumps(self.ledger))
        ledger["bindings"][self.binding["deployment_digest"]] = self.policy[
            "policy_digest"
        ]
        ledger["active_deployment_digest"] = self.binding["deployment_digest"]
        return ledger

    def argv(self, *extra, output=None, adapter=None, resources=None):
        return [
            "--build",
            str(self.build_path),
            "--lp-commit",
            LP_COMMIT,
            "--engine-commit",
            ENGINE_COMMIT,
            "--engine-bundle-digest",
            BUNDLE,
            "--gate-sha256",
            GATE_SHA,
            "--policy-digest",
            self.policy["policy_digest"],
            "--revision-name",
            REVISION,
            "--sdk-version",
            "2.20.0",
            "--adapter",
            adapter or str(ADAPTER_PATH) + ":clients",
            "--adapter-state",
            str(self.state_path),
            *(resources or ["--resources", str(MANIFEST_PATH)]),
            "--output",
            str(output or self.output),
            *extra,
        ]

    def current(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def journal(self):
        return [
            (entry["call"], entry.get("name") or entry.get("queue"))
            for entry in self.current()["journal"]
        ]


def run(argv, capsys, **kwargs):
    code = activate.execute(argv, **kwargs)
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    return code, json.loads(lines[-1])


def test_chain_runs_in_order_and_release_accepts_the_receipt(tmp_path, capsys):
    prepared = Prepared(tmp_path)
    code, summary = run(prepared.argv(), capsys)
    assert (code, summary["state"]) == (0, release.ACTIVATION_STATE), summary
    receipt = json.loads(prepared.output.read_text(encoding="utf-8"))
    assert receipt["binding"] == prepared.binding
    assert release.verify_activation(receipt) == prepared.binding
    assert release.activation_tag(receipt) == "question-" + ENGINE_COMMIT[:7]
    assert receipt["queue_after"]["state"] == "PAUSED"
    assert receipt["control_after"] == prepared.activated()
    assert receipt["policy"] == prepared.policy
    assert prepared.journal() == [
        ("pause_queue", QUEUE_API),
        ("create_object", prepared.binding_name()),
        ("create_object", LEDGER_NAME),
    ]
    creates = [e for e in prepared.current()["journal"] if e["call"] == "create_object"]
    assert [entry["if_generation_match"] for entry in creates] == [0, 40]
    stored = prepared.current()["objects"]
    assert fake.decode_raw(stored[prepared.binding_name()]["raw_b64"]) == (
        release.canonical_bytes(prepared.binding)
    )
    assert stored[LEDGER_NAME]["generation"] == receipt["chain"]["ledger"]["generation"]
    assert receipt["chain"]["ledger"]["if_generation_match"] == 40
    assert receipt["chain"]["binding_object"]["action"] == "created"
    assert prepared.current()["queue"]["state"] == "PAUSED"

    plan_path = tmp_path / "release-plan.json"
    code = release.execute(
        [
            "plan",
            "--activation",
            str(prepared.output),
            "--authority",
            str(prepared.authority_path),
            "--output",
            str(plan_path),
            "--adapter",
            str(ADAPTER_PATH) + ":clients",
            "--adapter-state",
            str(prepared.state_path),
            "--resources",
            str(MANIFEST_PATH),
        ]
    )
    assert code == 0, capsys.readouterr().out
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["changes"][0]["operation"] == "deploy_revision"
    assert plan["changes"][0]["revision"] == REVISION
    assert plan["binding"] == prepared.binding


def test_chain_over_the_native_adapter_sends_the_guarded_requests_in_order(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(native, "_gcloud_token", lambda: TOKEN)
    prepared = live_window(Prepared(tmp_path))
    cloud = FakeCloud(prepared.state)
    clients = native.build("owner-gcloud", transport=cloud)
    code, summary = run(
        prepared.argv(adapter="native:factory"), capsys, clients=clients
    )
    assert (code, summary["state"]) == (0, release.ACTIVATION_STATE), summary
    receipt = json.loads(prepared.output.read_text(encoding="utf-8"))
    assert release.verify_activation(receipt) == prepared.binding
    binding_raw = release.canonical_bytes(prepared.binding)
    ledger_raw = release.canonical_bytes(prepared.activated())
    assert cloud.mutations() == [
        ("POST", native.TASKS_ENDPOINT + QUEUE_API + ":pause"),
        (
            "POST",
            native.UPLOAD_ENDPOINT
            + f"b/{release.BUCKET}/o?uploadType=media&name="
            + prepared.binding_name().replace("/", "%2F")
            + "&ifGenerationMatch=0",
        ),
        (
            "POST",
            native.UPLOAD_ENDPOINT
            + f"b/{release.BUCKET}/o?uploadType=media&name="
            + LEDGER_NAME.replace("/", "%2F")
            + "&ifGenerationMatch=40",
        ),
    ]
    uploads = [call for call in cloud.calls if call["method"] == "POST"][1:]
    assert [call["body"] for call in uploads] == [binding_raw, ledger_raw]
    assert cloud.objects[LEDGER_NAME]["raw"] == ledger_raw
    assert cloud.queue["state"] == "PAUSED"
    assert (
        receipt["chain"]["ledger"]["sha256"] == hashlib.sha256(ledger_raw).hexdigest()
    )
    assert receipt["native_record"] == clients["_native"].record
    assert TOKEN not in json.dumps(receipt)
    plan_path = tmp_path / "release-plan.json"
    code = release.execute(
        [
            "plan",
            "--activation",
            str(prepared.output),
            "--authority",
            str(prepared.authority_path),
            "--output",
            str(plan_path),
            "--adapter",
            "native:factory",
            "--resources",
            str(MANIFEST_PATH),
        ],
        clients=clients,
    )
    assert code == 0, capsys.readouterr().out


def test_rerun_is_idempotent_without_a_second_write(tmp_path, capsys):
    prepared = Prepared(tmp_path)
    code, _summary = run(prepared.argv(), capsys)
    assert code == 0
    first = json.loads(prepared.output.read_text(encoding="utf-8"))
    again = tmp_path / "activation-again.json"
    code, summary = run(prepared.argv(output=again), capsys)
    assert (code, summary["state"]) == (0, release.ACTIVATION_STATE), summary
    second = json.loads(again.read_text(encoding="utf-8"))
    for field in ("binding", "policy", "control_after", "build", "reviewed_inputs"):
        assert second[field] == first[field], field
    assert second["chain"]["binding_object"]["action"] == "reused"
    assert second["chain"]["ledger"]["action"] == "already_refreshed"
    assert (
        second["chain"]["ledger"]["generation"]
        == first["chain"]["ledger"]["generation"]
    )
    assert prepared.journal() == [
        ("pause_queue", QUEUE_API),
        ("create_object", prepared.binding_name()),
        ("create_object", LEDGER_NAME),
        ("pause_queue", QUEUE_API),
    ]
    assert release.verify_activation(second) == prepared.binding
    code, summary = run(prepared.argv(output=again), capsys)
    assert (code, summary["error"]) == (1, "output_exists")


def test_queue_with_tasks_refuses_before_any_mutation(tmp_path, capsys):
    task = {"name": QUEUE_API + "/tasks/t1", "httpRequest": {}}
    prepared = Prepared(tmp_path, tasks=[task])
    code, summary = run(prepared.argv(), capsys)
    assert (code, summary["error"]) == (1, "queue_not_empty"), summary
    assert prepared.journal() == []
    assert not prepared.output.exists()


def test_task_arriving_after_the_pause_refuses_queue_not_empty(tmp_path, capsys):
    prepared = Prepared(tmp_path)
    clients = fake.clients(prepared.state_path)
    original = clients["tasks"].pause

    def pause_then_enqueue(name):
        queue = original(name)
        clients["_state"].data["tasks"].append(
            {"name": QUEUE_API + "/tasks/late", "httpRequest": {}}
        )
        clients["_state"].save()
        return queue

    clients["tasks"].pause = pause_then_enqueue
    code, summary = run(prepared.argv(), capsys, clients=clients)
    assert (code, summary["error"]) == (1, "queue_not_empty"), summary
    assert prepared.journal() == [("pause_queue", QUEUE_API)]
    assert summary["progress"] == ["queue_paused"]


def test_pause_that_does_not_take_refuses_queue_not_paused(tmp_path, capsys):
    prepared = Prepared(tmp_path)
    state = prepared.current()
    state["failures"]["pause_queue"] = "ignored"
    write_json(prepared.state_path, state)
    code, summary = run(prepared.argv(), capsys)
    assert (code, summary["error"]) == (1, "queue_not_paused"), summary
    assert prepared.journal() == [("pause_queue", QUEUE_API)]


def test_different_binding_object_refuses_binding_conflict(tmp_path, capsys):
    prepared = Prepared(tmp_path)
    name = prepared.binding_name()
    state = prepared.current()
    state["objects"][name] = {
        "generation": "77",
        "raw_b64": fake.encode_raw(b'{"contract_version":"other"}'),
    }
    write_json(prepared.state_path, state)
    code, summary = run(prepared.argv(), capsys)
    assert (code, summary["error"]) == (1, "binding_conflict"), summary
    assert prepared.journal() == []
    assert prepared.current()["queue"]["state"] == "RUNNING"


def test_binding_create_race_refuses_binding_conflict(tmp_path, capsys):
    prepared = Prepared(tmp_path)
    clients = fake.clients(prepared.state_path)
    original = clients["objects"].create

    def race(name, raw, *, if_generation_match):
        if name == prepared.binding_name():
            raise fake.PreconditionFailed("generation_mismatch")
        return original(name, raw, if_generation_match=if_generation_match)

    clients["objects"].create = race
    code, summary = run(prepared.argv(), capsys, clients=clients)
    assert (code, summary["error"]) == (1, "binding_conflict"), summary
    assert summary["detail"] == "PreconditionFailed: generation_mismatch"
    assert summary["progress"] == ["queue_paused"]


def test_moved_ledger_refuses_ledger_moved_without_retry(tmp_path, capsys):
    prepared = Prepared(tmp_path)
    state = prepared.current()
    state["failures"]["create_object"] = "concurrent_write"
    write_json(prepared.state_path, state)
    code, summary = run(prepared.argv(), capsys)
    assert (code, summary["error"]) == (1, "ledger_moved"), summary
    assert summary["progress"] == ["queue_paused", "binding_object_created"]
    journal = prepared.journal()
    assert journal == [
        ("pause_queue", QUEUE_API),
        ("create_object", prepared.binding_name()),
        ("concurrent_write", LEDGER_NAME),
        ("create_object", LEDGER_NAME),
    ]
    ledger, generation = (
        json.loads(
            fake.decode_raw(prepared.current()["objects"][LEDGER_NAME]["raw_b64"])
        ),
        prepared.current()["objects"][LEDGER_NAME]["generation"],
    )
    assert ledger == prepared.ledger and generation == "101"
    assert not prepared.output.exists()


def test_ledger_binding_bound_to_another_policy_refuses(tmp_path, capsys):
    prepared = Prepared(tmp_path)
    state = prepared.current()
    ledger = prepared.ledger
    ledger["bindings"][prepared.binding["deployment_digest"]] = "e" * 64
    state["objects"][LEDGER_NAME]["raw_b64"] = fake.encode_raw(
        release.canonical_bytes(ledger)
    )
    write_json(prepared.state_path, state)
    code, summary = run(prepared.argv(), capsys)
    assert (code, summary["error"]) == (1, "ledger_binding_conflict"), summary
    assert prepared.journal() == []


def test_guard_refuses_a_manifest_without_queue_write_before_any_mutation(
    tmp_path, capsys
):
    prepared = Prepared(tmp_path)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for row in manifest["resources"]:
        if row["name"] == release.QUEUE_RESOURCE:
            row["actions"] = [a for a in row["actions"] if a != "write"]
    path = tmp_path / "manifest.json"
    raw = json.dumps(manifest, indent=2).encode("utf-8")
    path.write_bytes(raw)
    code, summary = run(
        prepared.argv(
            resources=[
                "--resources",
                str(path),
                "--resources-sha256",
                hashlib.sha256(raw).hexdigest(),
            ]
        ),
        capsys,
    )
    assert (code, summary["error"]) == (1, "resource_action_forbidden"), summary
    assert prepared.journal() == []


@pytest.mark.parametrize(
    "mutate, error",
    [
        (
            lambda argv: argv.__setitem__(argv.index(REVISION), "other-q-x"),
            "binding_invalid",
        ),
        (
            lambda argv: argv.__setitem__(argv.index(LP_COMMIT), "f" * 40),
            "build_image_name_mismatch",
        ),
        (
            lambda argv: argv.__setitem__(argv.index(GATE_SHA), "f" * 64),
            "activation_invalid",
        ),
        (
            lambda argv: argv.__setitem__(argv.index(ENGINE_COMMIT), "short"),
            "reviewed_inputs_invalid",
        ),
        (lambda argv: argv.__setitem__(argv.index("2.20.0"), "x"), "binding_invalid"),
    ],
)
def test_invalid_inputs_refuse_before_any_mutation(tmp_path, capsys, mutate, error):
    prepared = Prepared(tmp_path)
    argv = prepared.argv()
    mutate(argv)
    code, summary = run(argv, capsys)
    assert (code, summary["error"]) == (1, error), summary
    assert prepared.journal() == []
    assert not prepared.output.exists()


def test_missing_policy_object_refuses_policy_unavailable(tmp_path, capsys):
    prepared = Prepared(tmp_path)
    state = prepared.current()
    del state["objects"][
        PREFIX + f"policies/{prepared.policy['policy_digest']}/policy.json"
    ]
    write_json(prepared.state_path, state)
    code, summary = run(prepared.argv(), capsys)
    assert (code, summary["error"]) == (1, "policy_unavailable"), summary
    assert prepared.journal() == []


def test_dry_run_renders_every_request_and_writes_nothing(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(native, "_gcloud_token", lambda: TOKEN)
    prepared = Prepared(tmp_path)
    cloud = FakeCloud(prepared.state)
    clients = native.build("owner-gcloud", transport=cloud)
    code, summary = run(
        prepared.argv("--dry-run", adapter="native:factory"), capsys, clients=clients
    )
    assert (code, summary["state"]) == (0, "dry_run"), summary
    assert not prepared.output.exists()
    assert cloud.mutations() == []
    assert cloud.queue["state"] == "RUNNING"
    requests = summary["requests"]
    assert [item["call"] for item in requests] == [
        "pause_queue",
        "create_object",
        "create_object",
    ]
    assert requests[0]["native"] == {
        "call": "pause_queue",
        "method": "POST",
        "url": native.TASKS_ENDPOINT + QUEUE_API + ":pause",
        "body": {},
    }
    binding_raw = release.canonical_bytes(prepared.binding)
    assert requests[1]["object"] == prepared.binding_name()
    assert requests[1]["if_generation_match"] == 0
    assert requests[1]["body"] == prepared.binding
    assert requests[1]["native"]["url"].endswith("&ifGenerationMatch=0")
    assert requests[1]["native"]["body"] == {
        "sha256": hashlib.sha256(binding_raw).hexdigest(),
        "bytes": len(binding_raw),
    }
    ledger_raw = release.canonical_bytes(prepared.activated())
    assert requests[2]["object"] == LEDGER_NAME
    assert requests[2]["if_generation_match"] == 40
    assert requests[2]["sha256"] == hashlib.sha256(ledger_raw).hexdigest()
    assert requests[2]["native"]["url"].endswith("&ifGenerationMatch=40")
    assert summary["binding"] == prepared.binding
    assert summary["tag"] == "question-" + ENGINE_COMMIT[:7]
    code, summary = run(prepared.argv("--dry-run"), capsys)
    assert (code, summary["state"]) == (0, "dry_run")
    assert "native" not in summary["requests"][0]
    assert prepared.journal() == []
