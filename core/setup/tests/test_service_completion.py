import copy

import pytest

from core.setup import service_completion as completion

NAME = "f42-agent"
OLD_TRAFFIC = [{"revision": completion.PREVIOUS_REVISION, "percent": 100}]
NEW_TRAFFIC = [{"revision": completion.PREPARED_REVISION, "percent": 100}]
TARGET = {
    "name": NAME,
    "image": completion.PREPARED_IMAGE,
    "f42_version": completion.PINNED_HEAD[:12],
    "model_provider": "gemini",
    "service_account": "f42-agent@ogilvy-trends-v2.iam.gserviceaccount.com",
    "old_revision": completion.PREVIOUS_REVISION,
    "old_traffic": OLD_TRAFFIC,
}
RECEIPT = {
    **TARGET,
    "new_revision": completion.PREPARED_REVISION,
    "specific_ready_revision": completion.PREPARED_REVISION,
    "latest_ready_alias": completion.PREVIOUS_REVISION,
}
PROMOTION_TARGET = {
    "name": NAME,
    "revision": completion.PREPARED_REVISION,
    "traffic": NEW_TRAFFIC,
}
MISSING = object()


class FakeLedger:
    def __init__(self, *actions):
        self.actions = {row["id"]: copy.deepcopy(row) for row in actions}
        self.writes = []

    def action(self, action_id):
        return self.actions.get(action_id)

    def begin(self, action_id, target, *, kind, name):
        self.writes.append(("begin", action_id))
        self.actions[action_id] = {
            "id": action_id,
            "kind": kind,
            "name": name,
            "state": "in_flight",
            "readback": False,
            "target": copy.deepcopy(target),
        }

    def complete(self, action_id, target, observed):
        self.writes.append(("complete", action_id))
        action = self.actions[action_id]
        action.update({
            "state": "complete",
            "readback": True,
            "target": copy.deepcopy(target),
            "observed": copy.deepcopy(observed),
        })


class FakeOperator:
    Refusal = completion.Refusal
    PROJECT = "ogilvy-trends-v2"
    REGION = "us-central1"

    def __init__(self, *, revision=completion.PREPARED_REVISION, promoted=False):
        self.revision = revision
        self.promoted = promoted
        self.mutations = []
        self.reads = []
        self.readbacks = []
        self.original_raw = self._document(
            revision=completion.PREVIOUS_REVISION,
            traffic=OLD_TRAFFIC,
            image="old-image",
            version="old-version",
            ready_revision=completion.PREVIOUS_REVISION,
        )

    def _spec(self, image=completion.PREPARED_IMAGE, version=completion.PINNED_HEAD[:12]):
        return {
            "serviceAccountName": TARGET["service_account"],
            "containers": [{
                "image": image,
                "env": [
                    {"name": "F42_VERSION", "value": version},
                    {"name": "MODEL_PROVIDER", "value": "gemini"},
                ],
            }],
        }

    def _document(self, *, revision, traffic, image=completion.PREPARED_IMAGE,
                  version=completion.PINNED_HEAD[:12], ready_revision=None):
        return {
            "metadata": {"name": NAME},
            "revision": revision,
            "ready_revision": ready_revision or completion.PREVIOUS_REVISION,
            "traffic": copy.deepcopy(traffic),
            "spec": self._spec(image, version),
            "unapproved_settings": {"ingress": "all"},
        }

    def safe_service_state(self, service_doc):
        spec = service_doc["spec"]
        env = {row["name"]: row.get("value") for row in spec["containers"][0]["env"]}
        return {
            "revision": service_doc["revision"],
            "ready_revision": service_doc["ready_revision"],
            "traffic": copy.deepcopy(service_doc["traffic"]),
            "image": spec["containers"][0]["image"],
            "f42_version": env.get("F42_VERSION"),
            "model_provider": env.get("MODEL_PROVIDER"),
            "service_account": spec["serviceAccountName"],
        }

    def _read_service(self, name):
        self.reads.append(name)
        traffic = NEW_TRAFFIC if self.promoted else OLD_TRAFFIC
        ready_revision = completion.PREPARED_REVISION if self.promoted else completion.PREVIOUS_REVISION
        return self._document(
            revision=self.revision,
            traffic=traffic,
            ready_revision=ready_revision,
        )

    def _project_except(self, service_doc, resource, name, *, reference):
        return service_doc["unapproved_settings"]

    def _gcloud_json(self, args):
        self.readbacks.append(list(args))
        active = self.promoted
        conditions = [
            {"type": "Ready", "state": "CONDITION_SUCCEEDED"},
            {"type": "ContainerReady", "state": "CONDITION_SUCCEEDED"},
            ({"type": "Active", "state": "CONDITION_SUCCEEDED"} if active else
             {"type": "Active", "state": "CONDITION_FAILED", "severity": "INFO"}),
        ]
        return {
            "metadata": {"name": self.revision},
            "status": {"imageDigest": completion.PREPARED_IMAGE, "conditions": conditions},
            "spec": self._spec(),
        }

    def _service_config(self, service_doc):
        return service_doc.get("metadata"), service_doc["spec"]

    def update_argv(self, resource, name, image, *, version):
        return ["update_service", name, image, version]

    def promote_argv(self, name, revision):
        return ["promote_service", name, revision]

    def _gcloud(self, args):
        command = list(args)
        self.mutations.append(command)
        if command[0] == "promote_service":
            self.promoted = True


def revision_action(state="in_flight", *, observed=MISSING, target=TARGET):
    row = {
        "id": f"service-revision:{NAME}",
        "kind": "service_revision",
        "name": NAME,
        "state": state,
        "readback": state == "complete",
        "target": copy.deepcopy(target),
    }
    if observed is not MISSING:
        row["observed"] = copy.deepcopy(observed)
    return row


def promotion_action():
    return {
        "id": f"service-promotion:{NAME}",
        "kind": "service_promotion",
        "name": NAME,
        "state": "complete",
        "readback": True,
        "target": copy.deepcopy(PROMOTION_TARGET),
        "observed": copy.deepcopy(PROMOTION_TARGET),
    }


def apply(operator, ledger):
    return completion._apply_service(
        operator,
        NAME,
        completion.PREPARED_IMAGE,
        ledger,
        operator.original_raw,
        completion.PINNED_HEAD[:12],
        proof=True,
    )


def test_pending_revision_without_observed_receipt_resumes_without_service_update():
    operator = FakeOperator()
    ledger = FakeLedger(revision_action())

    apply(operator, ledger)

    assert ledger.action(f"service-revision:{NAME}")["observed"]["new_revision"] == completion.PREPARED_REVISION
    assert not [call for call in operator.mutations if call[0] == "update_service"]
    assert [call[0] for call in operator.mutations] == ["promote_service"]


def test_completed_revision_uses_its_valid_receipt():
    operator = FakeOperator()
    ledger = FakeLedger(revision_action("complete", observed=RECEIPT))

    apply(operator, ledger)

    assert operator.mutations == [["promote_service", NAME, completion.PREPARED_REVISION]]
    assert ledger.action(f"service-revision:{NAME}")["observed"] == RECEIPT


def test_completed_revision_and_promotion_replay_without_writes():
    operator = FakeOperator(promoted=True)
    ledger = FakeLedger(revision_action("complete", observed=RECEIPT), promotion_action())

    apply(operator, ledger)

    assert ledger.writes == []
    assert operator.mutations == []


def test_pending_revision_verification_refusal_leaves_ledger_and_mutations_untouched():
    operator = FakeOperator(revision="unexpected-revision")
    ledger = FakeLedger(revision_action())

    with pytest.raises(completion.Refusal):
        apply(operator, ledger)

    assert ledger.writes == []
    assert operator.mutations == []


def test_unknown_revision_state_fails_closed_without_writes_or_mutations():
    operator = FakeOperator()
    ledger = FakeLedger(revision_action("unknown"))

    with pytest.raises(completion.Refusal):
        apply(operator, ledger)

    assert ledger.writes == []
    assert operator.mutations == []


def test_completed_revision_without_a_valid_receipt_fails_closed():
    operator = FakeOperator()
    ledger = FakeLedger(revision_action("complete", observed={"target": TARGET}))

    with pytest.raises(completion.Refusal):
        apply(operator, ledger)

    assert ledger.writes == []
    assert operator.mutations == []
