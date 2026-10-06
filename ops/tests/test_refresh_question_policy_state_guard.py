import copy
import json
from datetime import datetime, timedelta

import pytest

from ops.deploy import refresh_question_policy, release
from ops.tests.test_foundation_release import (
    LEDGER_NAME,
    MANIFEST_SHA256,
    NOW,
    PRICING_SOURCE,
    fake,
    make_revision,
    observation,
    renewal_grant,
    write_json,
)
from ops.tests.test_refresh_question_policy_cli import (
    REALISTIC_PAGE,
    Lane,
    recording_clients,
    run_main,
)


def _clients(lane):
    clients = recording_clients(lane.state_path)
    clients["manifest"] = {
        "value": lane.renewal.manifest,
        "sha256": MANIFEST_SHA256,
    }
    return clients


def _renew(lane, clients, observed, *, expected_generation=None):
    options = {}
    if expected_generation is not None:
        options["expected_ledger_generation"] = expected_generation
    return refresh_question_policy.refresh_observed_policy(
        release_index=lane.renewal.index,
        observation=observed,
        grant=renewal_grant(),
        clients=clients,
        **options,
    )


def _install_active_rates(lane, clients, input_rate, output_rate):
    ledger = release._read_ledger(clients)
    active = release._active_binding(clients, ledger["value"])
    active_policy = refresh_question_policy._read_active_policy(
        clients, ledger["value"], active
    )
    policy = refresh_question_policy.build_question_policy(
        pricing_verified_at=datetime.fromisoformat(
            active_policy["pricing"]["verified_at"].replace("Z", "+00:00")
        ),
        input_usd_per_million=input_rate,
        output_usd_per_million=output_rate,
        approval_contract_digest=active_policy["approval_contract_digest"],
        expires_at=active_policy["expires_at"],
    )
    binding = copy.deepcopy(active)
    binding["policy_digest"] = policy["policy_digest"]
    binding["revision_name"] = (
        f"{release.SERVICE_NAME}-p-{policy['policy_digest'][:10]}"
    )
    del binding["deployment_digest"]
    binding["deployment_digest"] = release.canonical_digest(binding)
    clients["objects"].create(
        release.OBJECT_PREFIX + f"policies/{policy['policy_digest']}/policy.json",
        release.canonical_bytes(policy),
        if_generation_match=0,
    )
    clients["objects"].create(
        release.OBJECT_PREFIX
        + f"deployments/{binding['deployment_digest']}/binding.json",
        release.canonical_bytes(binding),
        if_generation_match=0,
    )
    updated = copy.deepcopy(ledger["value"])
    updated["bindings"][binding["deployment_digest"]] = policy["policy_digest"]
    updated["active_deployment_digest"] = binding["deployment_digest"]
    release._validate_ledger(updated)
    written = clients["objects"].create(
        LEDGER_NAME,
        release.canonical_bytes(updated),
        if_generation_match=int(ledger["generation"]),
    )
    state = clients["_state"]
    service = state.data["service"]
    tag, _ = release._tag_from_environment(
        service["template"]["containers"][0]["env"]
    )
    service["template"] = release.expected_template(binding, tag)
    targets = release.traffic_targets(binding, tag)
    service["traffic"] = copy.deepcopy(targets)
    service["trafficStatuses"] = copy.deepcopy(targets)
    full_revision = service["name"] + "/revisions/" + binding["revision_name"]
    service["latestCreatedRevision"] = full_revision
    service["latestReadyRevision"] = full_revision
    service["generation"] = str(int(service["generation"]) + 1)
    service["observedGeneration"] = service["generation"]
    service["reconciling"] = False
    state.data["revisions"][full_revision] = make_revision(binding, tag)
    state.save()
    return int(written["generation"])


def _use_realistic_page(lane):
    state = lane.state()
    state["bytes"]["source:" + PRICING_SOURCE] = fake.encode_raw(REALISTIC_PAGE)
    write_json(lane.state_path, state)
    return observation(
        observed_at=NOW - timedelta(hours=1),
        input_rate="0.375000",
        output_rate="3.750000",
        page=REALISTIC_PAGE,
    )


def test_active_policy_price_drift_refuses_before_controller_mutation(tmp_path):
    lane = Lane(tmp_path)
    clients = _clients(lane)
    generation = _install_active_rates(lane, clients, "1.000000", "5.000000")
    calls_before = lane.calls()
    observed = observation(observed_at=NOW - timedelta(hours=1))

    receipt = _renew(lane, clients, observed, expected_generation=generation)

    assert (receipt["state"], receipt["error"]) == (
        "refused",
        "active_policy_rate_mismatch",
    )
    assert receipt["rate_changed"] is False
    assert receipt["mutations"] == []
    assert receipt["operations"] == {}
    assert lane.calls() == calls_before


def test_dry_plan_generation_mismatch_refuses_before_send_mutations(tmp_path, capsys):
    lane = Lane(tmp_path)
    dry_path = tmp_path / "dry-plan.json"
    code, dry = run_main(lane.renew_argv("--dry-run", output=dry_path), capsys)
    assert (code, dry["state"]) == (0, "dry_run"), dry
    expected_generation = int(dry["ledger_generation"])
    state = lane.state()
    state["objects"][LEDGER_NAME]["generation"] = str(expected_generation + 1)
    write_json(lane.state_path, state)
    calls_before = lane.calls()
    send_path = tmp_path / "send.json"

    code, summary = run_main(
        lane.renew_argv(
            "--expected-ledger-generation",
            str(expected_generation),
            output=send_path,
        ),
        capsys,
    )

    assert (code, summary["state"], summary["error"]) == (
        1,
        "refused",
        "stale_ledger_generation",
    ), summary
    receipt = json.loads(send_path.read_text(encoding="utf-8"))
    assert receipt["mutations"] == []
    assert receipt["operations"] == {}
    assert lane.calls() == calls_before


def test_exact_active_rates_allow_guarded_renewal_and_keep_rate_changed(tmp_path):
    lane = Lane(tmp_path)
    observed = _use_realistic_page(lane)
    clients = _clients(lane)
    generation = _install_active_rates(lane, clients, "0.375000", "3.750000")

    receipt = _renew(lane, clients, observed, expected_generation=generation)

    assert receipt["state"] == "activated", receipt
    assert receipt["rate_changed"] is True


@pytest.mark.parametrize("generation", [0, -1, True])
def test_invalid_controller_generation_refuses_without_mutations(tmp_path, generation):
    lane = Lane(tmp_path)
    clients = _clients(lane)
    calls_before = lane.calls()
    observed = observation(observed_at=NOW - timedelta(hours=1))

    receipt = _renew(lane, clients, observed, expected_generation=generation)

    assert (receipt["state"], receipt["error"]) == (
        "refused",
        "expected_ledger_generation_invalid",
    )
    assert receipt["mutations"] == []
    assert receipt["operations"] == {}
    assert lane.calls() == calls_before


def test_legacy_renewal_without_generation_keeps_profile_change_behavior(tmp_path):
    lane = Lane(tmp_path)
    observed = _use_realistic_page(lane)
    clients = _clients(lane)

    receipt = _renew(lane, clients, observed)

    assert receipt["state"] == "activated", receipt
    assert receipt["rate_changed"] is True


@pytest.mark.parametrize(
    ("drift", "error"),
    [
        ("service_account", "service_mismatch"),
        ("timeout", "service_mismatch"),
        ("scaling", "service_mismatch"),
        ("cpu", "service_mismatch"),
        ("memory", "service_mismatch"),
        ("secret_ref", "service_mismatch"),
        ("old_pointer", "service_mismatch"),
        ("concurrency", "serving_template_mismatch"),
        ("ports", "serving_template_mismatch"),
        ("startup_probe", "serving_template_mismatch"),
        ("volumes", "serving_template_mismatch"),
        ("new_env", "serving_template_mismatch"),
        ("unknown_field", "serving_template_mismatch"),
        ("traffic", "service_not_serving_active_revision"),
    ],
)
def test_pricing_renewal_refuses_service_template_drift_from_serving_revision(
    tmp_path, drift, error
):
    lane = Lane(tmp_path)
    observed = _use_realistic_page(lane)
    state = lane.state()
    template = state["service"]["template"]
    container = template["containers"][0]
    if drift == "service_account":
        template["serviceAccount"] = "different@example.invalid"
    elif drift == "timeout":
        template["timeout"] = "500s"
    elif drift == "scaling":
        template["scaling"]["maxInstanceCount"] = 2
    elif drift == "cpu":
        container["resources"]["limits"]["cpu"] = "4"
    elif drift == "memory":
        container["resources"]["limits"]["memory"] = "8Gi"
    elif drift == "secret_ref":
        secret = next(
            entry for entry in container["env"] if entry["name"] == "UI_PASSCODE"
        )
        secret["valueSource"]["secretKeyRef"]["version"] = "changed"
    elif drift == "old_pointer":
        next(
            entry
            for entry in container["env"]
            if entry["name"] == "GENERAL_QUESTION_DEPLOYMENT_DIGEST"
        )["value"] = "changed-old-pointer"
    elif drift == "concurrency":
        template["maxInstanceRequestConcurrency"] = 160
    elif drift == "ports":
        container["ports"] = [{"containerPort": 8181}]
    elif drift == "startup_probe":
        container["startupProbe"] = {"tcpSocket": {"port": 8181}}
    elif drift == "volumes":
        template["volumes"] = [{"name": "unreviewed"}]
    elif drift == "unknown_field":
        template["unreviewedField"] = "changed"
    elif drift == "traffic":
        state["service"]["traffic"][0]["revision"] = "unreviewed-revision"
    else:
        container["env"].append({"name": "UNREVIEWED", "value": "changed"})
    write_json(lane.state_path, state)
    clients = _clients(lane)
    before = lane.calls()

    receipt = _renew(lane, clients, observed)

    assert (receipt["state"], receipt["error"]) == ("refused", error), receipt
    assert not any(
        item["call"] == "deploy_revision" for item in lane.calls()[len(before) :]
    )


def test_pricing_renewal_carries_native_revision_fields_without_changing_them(
    tmp_path
):
    lane = Lane(tmp_path)
    observed = _use_realistic_page(lane)
    state = lane.state()
    service = state["service"]
    revision_name = service["name"] + "/revisions/" + service["template"]["revision"]
    revision = state["revisions"][revision_name]
    for current in (service["template"], revision):
        current["maxInstanceRequestConcurrency"] = 160
        current["containers"][0]["ports"] = [{"containerPort": 8080}]
        current["containers"][0]["startupProbe"] = {
            "tcpSocket": {"port": 8080},
            "timeoutSeconds": 240,
        }
    revision["containers"][0]["name"] = "generated-container-name"
    write_json(lane.state_path, state)
    clients = _clients(lane)

    receipt = _renew(lane, clients, observed)

    assert receipt["state"] == "activated", receipt
    after = lane.state()["service"]["template"]
    assert after["maxInstanceRequestConcurrency"] == 160
    assert after["containers"][0]["ports"] == [{"containerPort": 8080}]
    assert after["containers"][0]["startupProbe"] == {
        "tcpSocket": {"port": 8080},
        "timeoutSeconds": 240,
    }


@pytest.mark.parametrize(
    "drift",
    [
        "reconciling",
        "unobserved_generation",
        "missing_generation",
        "different_created_revision",
        "different_ready_revision",
        "missing_ready_revision",
    ],
)
def test_pricing_renewal_refuses_unready_service_before_deploy(tmp_path, drift):
    lane = Lane(tmp_path)
    observed = _use_realistic_page(lane)
    state = lane.state()
    service = state["service"]
    if drift == "reconciling":
        service["reconciling"] = True
    elif drift == "unobserved_generation":
        service["observedGeneration"] = "0"
    elif drift == "missing_generation":
        service.pop("generation")
    elif drift == "different_created_revision":
        service["latestCreatedRevision"] = service["name"] + "/revisions/other"
    elif drift == "different_ready_revision":
        service["latestReadyRevision"] = service["name"] + "/revisions/other"
    else:
        service.pop("latestReadyRevision")
    write_json(lane.state_path, state)
    clients = _clients(lane)
    before = lane.calls()

    receipt = _renew(lane, clients, observed)

    assert (receipt["state"], receipt["error"]) == (
        "refused",
        "service_not_ready",
    ), receipt
    assert not any(
        item["call"] == "deploy_revision" for item in lane.calls()[len(before) :]
    )


def test_omitted_false_reconciling_keeps_settled_pricing_renewal_valid(tmp_path):
    lane = Lane(tmp_path)
    observed = _use_realistic_page(lane)
    state = lane.state()
    state["service"].pop("reconciling")
    write_json(lane.state_path, state)

    receipt = _renew(lane, _clients(lane), observed)

    assert receipt["state"] == "activated", receipt


def test_cli_exact_dry_generation_allows_one_guarded_renewal(tmp_path, capsys):
    lane = Lane(tmp_path)
    code, dry = run_main(
        lane.renew_argv("--dry-run", output=tmp_path / "dry.json"), capsys
    )
    assert (code, dry["state"], dry["rate_changed"]) == (0, "dry_run", False)
    expected = int(dry["ledger_generation"])
    output = tmp_path / "guarded-send.json"
    code, summary = run_main(
        lane.renew_argv("--expected-ledger-generation", str(expected), output=output),
        capsys,
    )
    assert (code, summary["state"], summary["rate_changed"]) == (0, "activated", False)
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["activation"]["if_generation_match"] == expected
    assert len(receipt["mutations"]) == 2
    assert set(receipt["operations"]) == {"deploy_revision", "route_traffic"}


@pytest.mark.parametrize("value", ["0", "-1", "not-a-generation"])
def test_cli_invalid_generation_refuses_before_client_loading(
    tmp_path, value, monkeypatch
):
    lane = Lane(tmp_path)
    calls_before = lane.calls()

    def forbidden(*args, **kwargs):
        raise AssertionError("adapter loaded before generation validation")

    monkeypatch.setattr(release, "_load_adapter", forbidden)
    with pytest.raises(SystemExit) as error:
        refresh_question_policy.main(
            [
                str(item)
                for item in lane.renew_argv(
                    "--expected-ledger-generation",
                    value,
                    output=tmp_path / "never.json",
                )
            ]
        )
    assert error.value.code == 2
    assert not (tmp_path / "never.json").exists()
    assert lane.calls() == calls_before


@pytest.mark.parametrize(
    "input_rate,output_rate", [("1.000000", "3.750000"), ("0.750000", "5.000000")]
)
def test_each_active_price_must_match_before_guarded_writes(
    tmp_path, input_rate, output_rate
):
    lane = Lane(tmp_path)
    clients = _clients(lane)
    generation = _install_active_rates(lane, clients, input_rate, output_rate)
    before = lane.calls()
    receipt = _renew(
        lane,
        clients,
        observation(observed_at=NOW - timedelta(hours=1)),
        expected_generation=generation,
    )
    assert (receipt["state"], receipt["error"]) == (
        "refused",
        "active_policy_rate_mismatch",
    )
    assert receipt["mutations"] == [] and receipt["operations"] == {}
    assert lane.calls() == before


def test_saved_ready_candidate_refuses_ordinary_renewal_before_mutation(tmp_path):
    lane = Lane(tmp_path)
    observed_at = datetime.fromisoformat("2026-09-28T06:46:46.721138+00:00")
    page = b"Input tokens cost $0.75 per million. Output tokens cost $3.75 per million."
    observed = observation(
        observed_at=observed_at,
        input_rate="0.750000",
        output_rate="3.750000",
        page=page,
    )
    state = lane.state()
    state["clock"]["now"] = "2026-09-28T06:49:18.634863+00:00"
    state["bytes"]["source:" + PRICING_SOURCE] = fake.encode_raw(page)
    write_json(lane.state_path, state)
    clients = _clients(lane)
    ledger = release._read_ledger(clients)
    active = release._active_binding(clients, ledger["value"])
    active_policy = refresh_question_policy._read_active_policy(
        clients, ledger["value"], active
    )
    policy = refresh_question_policy.build_question_policy(
        pricing_verified_at=observed_at,
        input_usd_per_million=observed["input_usd_per_million"],
        output_usd_per_million=observed["output_usd_per_million"],
        approval_contract_digest=active_policy["approval_contract_digest"],
        expires_at=active_policy["expires_at"],
    )
    binding = copy.deepcopy(active)
    binding["policy_digest"] = policy["policy_digest"]
    binding["revision_name"] = f"{release.SERVICE_NAME}-p-{policy['policy_digest'][:10]}"
    del binding["deployment_digest"]
    binding["deployment_digest"] = release.canonical_digest(binding)
    tag, _ = release._tag_from_environment(
        clients["_state"].data["service"]["template"]["containers"][0]["env"]
    )
    service = clients["_state"].data["service"]
    old_revision = release.SERVICE_API_NAME + "/revisions/" + active["revision_name"]
    candidate_revision = (
        release.SERVICE_API_NAME + "/revisions/" + binding["revision_name"]
    )
    service["template"] = release.expected_template(binding, tag)
    service["generation"] = "263"
    service["observedGeneration"] = "263"
    service["latestCreatedRevision"] = candidate_revision
    service["latestReadyRevision"] = old_revision
    service["reconciling"] = False
    service["etag"] = "saved-candidate-service-etag"
    candidate = make_revision(binding, tag)
    candidate["conditions"].append(
        {
            "type": "Active",
            "state": "CONDITION_FAILED",
            "revisionReason": "RETIRED",
        }
    )
    clients["_state"].data["revisions"][candidate_revision] = candidate
    clients["_state"].save()
    clients = _clients(lane)
    writes_before = lane.calls()

    receipt = _renew(lane, clients, observed)

    assert (receipt["state"], receipt["error"]) == ("refused", "service_mismatch"), receipt
    assert receipt["mutations"] == []
    assert receipt["operations"] == {}
    assert lane.calls() == writes_before
