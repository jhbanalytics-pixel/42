"""The one replacement that moves the active question policy to the extended expiry.

Every test runs against the local double in fixtures/release_fake_adapter.py
through the renewal scenario in test_foundation_release.py. Nothing touches a
network.
"""

import copy
import json
from datetime import timedelta

import pytest

from ops.deploy import refresh_question_policy, release
from ops.tests.test_foundation_release import (
    IMAGE_NAME,
    LEDGER_NAME,
    MANIFEST_SHA256,
    NOW,
    Renewal,
    observation,
    write_json,
)
from ops.tests.test_refresh_question_policy_cli import Lane, run_main
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_policy import (
    EXTENDED_EXPIRES_AT,
)

FIXED_EXPIRES_AT = "2026-09-30T23:59:59Z"
WRITES = {"create_object", "deploy_revision", "update_traffic"}


def expiry_grant(renewal, **changes):
    grant = {
        "contract_version": "42_policy_expiry_grant_v1",
        "purpose": "policy_expiry_replacement",
        "resource_manifest_sha256": MANIFEST_SHA256,
        "granted_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(hours=4)).isoformat(),
        "grantor": "operator",
        "from_policy_digest": renewal.scenario.policy["policy_digest"],
        "to_expires_at": EXTENDED_EXPIRES_AT,
        "release_commit": renewal.index["commit"],
    }
    grant.update(changes)
    return grant


def replace(renewal, grant=None, *, execute=True, index=None):
    return refresh_question_policy.replace_policy_expiry(
        release_index=index or renewal.index,
        grant=grant or expiry_grant(renewal),
        clients=renewal.clients(),
        execute=execute,
    )


def expiry_argv(lane, grant_path, *extra, output=None):
    return [
        "extend-expiry",
        "--release-index",
        lane.index_path,
        "--grant",
        grant_path,
        "--output",
        output or lane.output,
        *lane.adapter(),
        *extra,
    ]


def test_the_replacement_changes_the_expiry_and_its_digest_and_nothing_else(tmp_path):
    renewal = Renewal(tmp_path)
    before = renewal.scenario.policy
    assert before["expires_at"] == FIXED_EXPIRES_AT
    result = replace(renewal)
    assert result["state"] == "activated", result
    after = result["policy"]
    assert after["expires_at"] == EXTENDED_EXPIRES_AT
    assert {key for key in after if after[key] != before[key]} == {
        "expires_at",
        "policy_digest",
    }
    assert after["policy_digest"] == canonical_digest(
        {key: value for key, value in after.items() if key != "policy_digest"}
    )
    assert result["replaced_policy_digest"] == before["policy_digest"]
    ledger, _generation = renewal.scenario.ledger_now()
    binding = result["binding"]
    assert ledger["active_deployment_digest"] == binding["deployment_digest"]
    assert ledger["bindings"][binding["deployment_digest"]] == after["policy_digest"]
    # The old binding stays in the ledger, exactly as a renewal leaves it.
    assert (
        ledger["bindings"][renewal.scenario.binding["deployment_digest"]]
        == before["policy_digest"]
    )
    old = renewal.scenario.binding
    assert {key for key in binding if binding[key] != old[key]} == {
        "policy_digest",
        "revision_name",
        "deployment_digest",
    }
    assert binding["revision_name"] == (
        f"{release.SERVICE_NAME}-p-{after['policy_digest'][:10]}"
    )
    service = renewal.scenario.current()["service"]
    assert service["trafficStatuses"][0]["revision"] == binding["revision_name"]
    assert renewal.calls()[-4:] == [
        "create_object",
        "deploy_revision",
        "create_object",
        "update_traffic",
    ]


def test_a_rerun_after_the_replacement_is_already_activated_and_writes_nothing(
    tmp_path,
):
    renewal = Renewal(tmp_path)
    grant = expiry_grant(renewal)
    assert replace(renewal, grant)["state"] == "activated"
    calls = renewal.calls()
    again = replace(renewal, grant)
    assert again["state"] == "already_activated", again
    assert again["active"] is True
    assert not WRITES & set(renewal.calls()[len(calls) :])


def test_the_replacement_refuses_when_the_active_policy_is_not_the_granted_one(
    tmp_path,
):
    renewal = Renewal(tmp_path)
    grant = expiry_grant(renewal)
    renewed = renewal.refresh(observation(observed_at=NOW - timedelta(hours=1)))
    assert renewed["state"] == "activated", renewed
    before = renewal.calls()
    ledger_before = renewal.scenario.ledger_now()
    result = replace(renewal, grant)
    assert (result["state"], result["error"]) == ("refused", "active_policy_moved")
    assert result["mutations"] == [] and result["ledger_moved"] is False
    assert not WRITES & set(renewal.calls()[len(before) :])
    assert renewal.scenario.ledger_now() == ledger_before


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"contract_version": "42_renewal_grant_v1"}, "grant_invalid"),
        ({"purpose": "pricing_renewal"}, "grant_invalid"),
        ({"resource_manifest_sha256": "0" * 64}, "grant_manifest_mismatch"),
        ({"granted_at": (NOW + timedelta(minutes=1)).isoformat()}, "grant_not_yet_valid"),
        ({"expires_at": NOW.isoformat()}, "grant_expired"),
        (
            {
                "granted_at": (NOW - timedelta(hours=5)).isoformat(),
                "expires_at": (NOW + timedelta(hours=3, seconds=1)).isoformat(),
            },
            "grant_window_too_long",
        ),
        ({"to_expires_at": FIXED_EXPIRES_AT}, "grant_expiry_invalid"),
        ({"to_expires_at": "2027-01-01T00:00:00Z"}, "grant_expiry_invalid"),
        ({"from_policy_digest": "A" * 64}, "grant_invalid"),
        ({"from_policy_digest": None}, "grant_invalid"),
        ({"release_commit": "A" * 40}, "grant_invalid"),
        ({"release_commit": "a" * 39}, "grant_invalid"),
        ({"release_commit": None}, "grant_invalid"),
    ],
)
def test_the_grant_is_checked_before_anything_is_read_or_written(
    tmp_path, changes, error
):
    renewal = Renewal(tmp_path)
    before = renewal.calls()
    result = replace(renewal, expiry_grant(renewal, **changes))
    assert (result["state"], result["error"]) == ("refused", error), result
    assert renewal.calls() == before


def test_a_grant_with_an_extra_or_missing_field_is_refused(tmp_path):
    renewal = Renewal(tmp_path)
    extra = dict(expiry_grant(renewal), note="x")
    missing = expiry_grant(renewal)
    del missing["grantor"]
    for grant in (extra, missing, [], "grant"):
        result = refresh_question_policy.replace_policy_expiry(
            release_index=renewal.index, grant=grant, clients=renewal.clients()
        )
        assert (result["state"], result["error"]) == ("refused", "grant_invalid")


def test_the_replacement_refuses_stale_pricing_rather_than_extending_it(tmp_path):
    renewal = Renewal(tmp_path)
    policy = renewal.scenario.policy
    renewal.set_now(NOW + timedelta(hours=17))
    grant = expiry_grant(
        renewal,
        granted_at=(NOW + timedelta(hours=16)).isoformat(),
        expires_at=(NOW + timedelta(hours=18)).isoformat(),
    )
    before = renewal.calls()
    result = replace(renewal, grant)
    assert policy["pricing"]["verified_at"].startswith("2026-09-13T00:00")
    assert (result["state"], result["error"]) == ("refused", "observation_stale")
    assert not WRITES & set(renewal.calls()[len(before) :])


def test_the_release_index_must_name_the_active_image(tmp_path):
    renewal = Renewal(tmp_path)
    index = copy.deepcopy(renewal.index)
    index["app_image"] = IMAGE_NAME + "@sha256:" + "e" * 64
    before = renewal.calls()
    result = replace(renewal, index=index)
    assert (result["state"], result["error"]) == (
        "refused",
        "release_index_image_mismatch",
    ), result
    assert not WRITES & set(renewal.calls()[len(before) :])


def test_the_grant_must_name_the_released_commit_that_carries_the_code(tmp_path):
    # The hand written grant attests which release carries the extended expiry;
    # a run against any other release index refuses before the ledger is read.
    renewal = Renewal(tmp_path)
    before = renewal.calls()
    result = replace(renewal, expiry_grant(renewal, release_commit="f" * 40))
    assert (result["state"], result["error"]) == (
        "refused",
        "expiry_release_commit_mismatch",
    ), result
    assert renewal.calls() == before


def test_a_policy_already_at_the_extended_expiry_is_not_replaced_again(tmp_path):
    renewal = Renewal(tmp_path)
    first = replace(renewal)
    assert first["state"] == "activated"
    grant = expiry_grant(renewal, from_policy_digest=first["policy"]["policy_digest"])
    before = renewal.calls()
    result = replace(renewal, grant)
    assert (result["state"], result["error"]) == ("refused", "policy_already_extended")
    assert not WRITES & set(renewal.calls()[len(before) :])


def test_renewal_keeps_whatever_expiry_the_active_policy_carries(tmp_path):
    (tmp_path / "fixed").mkdir()
    (tmp_path / "extended").mkdir()
    fixed = Renewal(tmp_path / "fixed")
    renewed = fixed.refresh(observation(observed_at=NOW - timedelta(hours=1)))
    assert renewed["state"] == "activated", renewed
    assert renewed["policy"]["expires_at"] == FIXED_EXPIRES_AT

    extended = Renewal(tmp_path / "extended")
    assert replace(extended)["state"] == "activated"
    renewed = extended.refresh(observation(observed_at=NOW - timedelta(hours=1)))
    assert renewed["state"] == "activated", renewed
    assert renewed["policy"]["expires_at"] == EXTENDED_EXPIRES_AT


def test_dry_run_renders_the_requests_the_run_then_sends(tmp_path, capsys):
    lane = Lane(tmp_path)
    grant_path = write_json(tmp_path / "expiry-grant.json", expiry_grant(lane.renewal))
    before = lane.calls()
    ledger_before, generation_before = lane.renewal.scenario.ledger_now()
    dry = tmp_path / "dry.json"
    code, summary = run_main(expiry_argv(lane, grant_path, "--dry-run", output=dry), capsys)
    assert (code, summary["state"]) == (0, "dry_run"), summary
    written = json.loads(dry.read_text(encoding="utf-8"))
    assert written["command"] == "extend-expiry --dry-run"
    assert set(written["inputs"]) == {"release_index", "grant"}
    assert lane.calls() == before
    assert lane.renewal.scenario.ledger_now() == (ledger_before, generation_before)
    requests = summary["requests"]
    assert [item["call"] for item in requests] == [
        "create_object",
        "create_object",
        "deploy_revision",
        "create_object",
        "update_traffic",
    ]
    assert requests[3]["object"] == LEDGER_NAME
    assert requests[3]["if_generation_match"] == int(generation_before)
    assert summary["policy"]["expires_at"] == EXTENDED_EXPIRES_AT

    code, real = run_main(expiry_argv(lane, grant_path), capsys)
    assert (code, real["state"]) == (0, "activated"), real
    receipt = json.loads(lane.output.read_text(encoding="utf-8"))
    assert receipt["command"] == "extend-expiry"
    assert receipt["policy"] == summary["policy"]
    assert receipt["binding"] == summary["binding"]
    assert real["replaced_policy_digest"] == lane.renewal.scenario.policy["policy_digest"]
    sent = [
        entry
        for entry in lane.state()["journal"][len(before) :]
        if entry["call"] != "guard"
    ]
    assert [entry["call"] for entry in sent] == [item["call"] for item in requests]
    assert sent[2]["idempotency_key"] == requests[2]["idempotency_key"]
    assert sent[3]["sha256"] == requests[3]["sha256"]


def test_the_command_refuses_an_existing_output_before_any_native_call(
    tmp_path, capsys
):
    lane = Lane(tmp_path)
    grant_path = write_json(tmp_path / "expiry-grant.json", expiry_grant(lane.renewal))
    lane.output.write_bytes(b"existing")
    before = lane.calls()
    code, summary = run_main(expiry_argv(lane, grant_path), capsys)
    assert (code, summary["error"]) == (1, "output_exists"), summary
    assert lane.output.read_bytes() == b"existing"
    assert lane.calls() == before


def test_the_unattended_renewal_keeps_the_extended_expiry(tmp_path, capsys):
    from ops.tests.test_refresh_question_policy_unattended import DUE, Unattended

    job = Unattended(tmp_path)
    renewal = job.lane.renewal
    grant = expiry_grant(
        renewal,
        granted_at=(DUE - timedelta(hours=1)).isoformat(),
        expires_at=(DUE + timedelta(hours=1)).isoformat(),
    )
    replaced = replace(renewal, grant)
    assert replaced["state"] == "activated", replaced
    assert replaced["policy"]["expires_at"] == EXTENDED_EXPIRES_AT

    code, summary = job.run(capsys)
    assert (code, summary["state"]) == (0, "activated"), summary
    policy = summary["receipt"]["policy"]
    assert policy["expires_at"] == EXTENDED_EXPIRES_AT
    assert policy["policy_digest"] != replaced["policy"]["policy_digest"]
    changed = {
        key for key in policy if policy[key] != replaced["policy"][key]
    }
    assert changed == {"pricing", "policy_digest"}
