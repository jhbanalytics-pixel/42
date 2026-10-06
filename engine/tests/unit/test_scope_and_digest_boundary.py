"""E03: the question worker process holds scope isolation and digest integrity.

Scope A is the configured default, ogilvy_default, which owns an admitted
question. Scope B is another client scope. Every read goes through the worker
process entry point the app launches, with the real store and the real
observation reader, so the refusal is the one the app receives. The execute
operation is then given a task whose digests are altered, malformed or stale.
"""

from __future__ import annotations

import io
import json
from uuid import UUID

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest

from tests.unit import test_general_question_admission as fixture
from tests.unit import test_general_question_store as store_fixture
from tests.unit.test_general_question_worker_cli import cli

FOREIGN_SCOPE = {**store_fixture.scope(), "client_scope_id": "bsa_pulse", "brand_config_id": "bsa"}
ABSENT_ID = str(UUID(int=4242))


def admitted(monkeypatch):
    store, bucket, invocation, identity, _ = fixture.deployment_fixture.fixture()
    monkeypatch.setattr(
        store_fixture.Blob, "updated", property(lambda self: store_fixture.NOW), raising=False
    )
    request = json.loads(
        bucket.objects[store_fixture.PREFIX + f"requests/{invocation['request_id']}/request.json"][
            1
        ]
    )

    def forbidden(*args, **kwargs):
        pytest.fail("a cross scope read attempted a write")

    monkeypatch.setattr(bucket, "blob", forbidden)
    uploads = list(bucket.uploads)
    host = {"store": store, "runtime_identity": identity, "credentials": object()}
    return host, bucket, invocation["request_id"], request, uploads


def run(host, operation, payload):
    output, errors = io.BytesIO(), io.BytesIO()
    status = cli().main(
        ["--operation", operation],
        stdin=io.BytesIO(json.dumps(payload).encode()),
        stdout=output,
        stderr=errors,
        host_factory=lambda **_: host,
        now=lambda: store_fixture.NOW,
    )
    return status, output.getvalue(), errors.getvalue()


def status_payload(request_id, scope):
    return {
        "contract_version": "general_question_status_request_v1",
        "request_id": request_id,
        "scope": scope,
    }


def observe_payload(request_id, scope):
    return {
        "contract_version": "general_question_observe_request_v1",
        "request_id": request_id,
        "scope": scope,
    }


def private_values(request):
    """Values only scope A may see; none may appear in any scope B reply."""
    return [request["question"], request["request_digest"], request["request_id"]]


def assert_nothing_leaked(raw, request):
    for value in private_values(request):
        assert value.encode() not in raw


def test_the_status_of_a_request_owned_by_another_scope_is_refused_without_content(
    monkeypatch,
):
    host, bucket, request_id, request, uploads = admitted(monkeypatch)
    own = run(host, "status", status_payload(request_id, store_fixture.scope()))
    assert own[0] == 0, own
    assert request["request_digest"].encode() in own[1]
    status, output, errors = run(host, "status", status_payload(request_id, FOREIGN_SCOPE))
    assert (status, output) == (1, b"")
    assert json.loads(errors) == {"error": "scope_invalid"}
    assert_nothing_leaked(errors, request)
    assert bucket.uploads == uploads


def test_a_status_read_under_another_scope_distinguishes_absent_from_foreign(
    monkeypatch,
):
    """Recorded behaviour: the worker reply names request_unknown for an absent id
    and scope_invalid for a foreign one. The app maps every status failure to one
    503, so the distinction stops at the process boundary; the app side test in
    test_cross_scope_integrity.py holds that mapping."""
    host, _bucket, request_id, _request, _uploads = admitted(monkeypatch)
    absent = run(host, "status", status_payload(ABSENT_ID, FOREIGN_SCOPE))
    foreign = run(host, "status", status_payload(request_id, FOREIGN_SCOPE))
    assert absent[:2] == foreign[:2] == (1, b"")
    assert json.loads(absent[2]) == {"error": "request_unknown"}
    assert json.loads(foreign[2]) == {"error": "scope_invalid"}


def test_detail_of_a_foreign_request_is_byte_identical_to_an_absent_one(monkeypatch):
    host, bucket, request_id, request, uploads = admitted(monkeypatch)
    own = run(host, "observe", observe_payload(request_id, store_fixture.scope()))
    assert own[0] == 0, own
    foreign = run(host, "observe", observe_payload(request_id, FOREIGN_SCOPE))
    absent = run(host, "observe", observe_payload(ABSENT_ID, FOREIGN_SCOPE))
    assert foreign == absent
    assert foreign[:2] == (1, b"")
    assert json.loads(foreign[2]) == {"error": "observation_request_unavailable"}
    assert_nothing_leaked(foreign[2], request)
    assert bucket.uploads == uploads


def test_the_history_inventory_of_another_scope_lists_nothing_from_scope_a(monkeypatch):
    host, bucket, request_id, request, uploads = admitted(monkeypatch)
    own = run(host, "observe", observe_payload(None, store_fixture.scope()))
    assert own[0] == 0, own
    assert json.loads(own[1])["coverage"]["total_count"] == 1
    status, output, errors = run(host, "observe", observe_payload(None, FOREIGN_SCOPE))
    assert (status, errors) == (0, b"")
    inventory = json.loads(output)
    assert inventory["rows"] == []
    assert inventory["coverage"]["known_count"] == 0
    assert inventory["coverage"]["total_count"] == 0
    for value in private_values(request):
        assert value.encode() not in output
    assert request_id.replace("-", "").encode() not in output
    assert bucket.uploads == uploads


@pytest.mark.parametrize(
    "field, value",
    [
        ("client_scope_id", "bsa_pulse"),
        ("brand_config_id", "bsa"),
        ("theme_id", "another_theme"),
        ("audience_lens_ids", ["lens_other"]),
    ],
)
def test_every_scope_value_that_differs_hides_the_request(monkeypatch, field, value):
    host, _bucket, request_id, _request, _uploads = admitted(monkeypatch)
    scope = {**store_fixture.scope(), field: value}
    foreign = run(host, "observe", observe_payload(request_id, scope))
    absent = run(host, "observe", observe_payload(ABSENT_ID, scope))
    assert foreign == absent
    assert json.loads(foreign[2]) == {"error": "observation_request_unavailable"}
    listing = run(host, "observe", observe_payload(None, scope))
    assert listing[0] == 0, listing
    assert json.loads(listing[1])["rows"] == []


# Altered digest or stale generation on the execute boundary


def flip_one(value):
    return value[:-1] + ("0" if value[-1] != "0" else "1")


DIGEST_VARIANTS = {
    "one_hex_changed": flip_one,
    "short": lambda value: value[:-1],
    "long": lambda value: value + "0",
    "uppercase": str.upper,
    "algorithm_prefix": lambda value: "sha256:" + value,
    "trailing_newline": lambda value: value + "\n",
}
TASK_DIGESTS = ("request_digest", "intake_digest", "policy_digest", "deployment_digest")


def published():
    from tests.unit import test_general_question_result as result_fixture

    store, bucket, invocation, identity, binding = fixture.deployment_fixture.fixture()
    expected = result_fixture.publish(store, invocation["request_id"])
    host = {"store": store, "runtime_identity": identity, "credentials": object()}
    return host, bucket, invocation, expected, binding


def test_the_exact_invocation_reaches_the_stored_result():
    """The refusals below are the digest checks, not a harness that never serves."""
    host, _bucket, invocation, expected, _binding = published()
    status, output, _errors = run(host, "execute", invocation)
    assert status == 0
    assert json.loads(output)["result_digest"] == expected["result_digest"]


@pytest.mark.parametrize("variant", sorted(DIGEST_VARIANTS))
@pytest.mark.parametrize("field", TASK_DIGESTS)
def test_an_altered_task_digest_is_refused_with_a_named_code(field, variant):
    host, bucket, invocation, expected, _binding = published()
    uploads = list(bucket.uploads)
    altered = {**invocation, field: DIGEST_VARIANTS[variant](invocation[field])}
    assert altered[field] != invocation[field]
    status, output, errors = run(host, "execute", altered)
    assert (status, output) == (1, b"")
    reply = json.loads(errors.splitlines()[-1])
    # Malformed text never writes to storage (it is refused after the ledger read); a well formed but different digest
    # is compared with the bound record and refused as an unapproved binding.
    expected_code = "approval_required" if variant == "one_hex_changed" else "request_invalid"
    assert reply == {"error": expected_code}
    assert expected["result_digest"].encode() not in errors
    assert bucket.uploads == uploads


def test_a_deployment_generation_on_record_but_not_the_admitted_one_is_refused():
    """A binding the ledger still knows is not the one this request was admitted under."""
    host, bucket, invocation, expected, binding = published()
    control = json.loads(bucket.objects[store_fixture.PREFIX + store_fixture.LEDGER][1])
    stale = {**binding, "revision_name": "listening-post-staging-synthetic-000"}
    stale_digest = canonical_digest(
        {key: value for key, value in stale.items() if key != "deployment_digest"}
    )
    stale["deployment_digest"] = stale_digest
    control["bindings"][stale_digest] = invocation["policy_digest"]
    bucket.seed(store_fixture.LEDGER, control)
    bucket.seed(f"deployments/{stale_digest}/binding.json", stale)
    status, output, errors = run(host, "execute", {**invocation, "deployment_digest": stale_digest})
    assert (status, output) == (1, b"")
    assert json.loads(errors.splitlines()[-1]) == {"error": "approval_required"}
    assert expected["result_digest"].encode() not in errors
