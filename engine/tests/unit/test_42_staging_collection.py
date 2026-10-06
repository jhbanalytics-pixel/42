"""Boundary, bootstrap profile and receipt tests for the isolated staging collector.

Nothing here opens a network connection, builds a warehouse client or reaches a
vendor. The collector is always an injected callable, and every refusal must fire
before it is reached.
"""

# The first block is the D01 packet text, with the one change the boundary now
# forces: a caller must name the authority it runs under. The two boundary
# regressions and the imports they came with are otherwise untouched.
# fmt: off
# isort: off
import pytest
from scripts.staging.collect_42_sources import collect_staging, unverified_authority

PACKET_AUTHORITY = unverified_authority("d01_packet_regression")

def test_production_dataset_refused_before_collection():
    called = []
    with pytest.raises(ValueError, match="source_dataset"):
        collect_staging(
            {"environment": "staging", "source_dataset": "trends_v2_dev"},
            authority=PACKET_AUTHORITY,
            collector=lambda **kw: called.append(kw),
        )
    assert called == []

def test_explicit_staging_dataset_and_collection_only(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", "intelligence_42_sources_staging")
    profile = {"environment": "staging",
               "source_dataset": "intelligence_42_sources_staging"}
    result = collect_staging(profile, authority=PACKET_AUTHORITY,
                             collector=lambda **kw: kw)
    assert result == {"stop_after_ingestion": True}
# isort: on
# fmt: on

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from scripts.staging.collect_42_sources import (
    EXECUTION_VARIABLE,
    INGEST_JOB,
    RECEIPT_CONTRACT_VERSION,
    build_bootstrap_profile,
    collection_receipt,
    entry_point_profile,
    main,
    manifest_authority,
)
from src.analysis.open_intelligence.execution_generations import (
    TrustedGeneration,
    active_generation,
)
from src.analysis.open_intelligence.execution_origins import OriginRefusal
from src.analysis.open_intelligence.source_policy import policy_digest
from src.utils import bigquery as dataset_resolver

ENGINE_ROOT = Path(__file__).resolve().parents[2]
STAGING_DATASET = "intelligence_42_sources_staging"
STAGING_PROFILE = {"environment": "staging", "source_dataset": STAGING_DATASET}
JOB_RESOURCE = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1"
    "/jobs/intelligence-42-ingest-staging"
)
DATASET_RESOURCE = (
    "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/intelligence_42_sources_staging"
)
EXECUTION = f"{INGEST_JOB}-9k2xv"
# The policy digest of the source estate this tree reconciles, written out here as
# fixed text. It was derived once by hand: the shipped connector registry was
# reconciled against the shipped source configuration, the policy fields of the
# reconciled rows were written as sorted compact json, and the sha256 of those
# bytes is the value below. Nothing in this module recomputes it, and that is the
# point of it. An expected value recomputed from the same estate the entry point
# reads moves with the production formula and can never disagree with it, so it
# would prove nothing about either. Work this literal out by hand again, the same
# way, when the connector registry or the shipped source configuration changes the
# estate; a run whose two readings have parted is what this value exists to catch.
# That happened once already: the source estate lane moved the estate and added a
# route's condition to the policy fields the digest is taken over, so the value
# below was worked out by hand a second time over the twenty one field preimage.
# It was 83fb540739a7ca162724167b964ea01ba68d86c1336e4c223c8bb4a691e34c8a before.
RECONCILED_POLICY_DIGEST = "ff9c86565ec0aab5423fada601767eeadec6d290e417ad339d2b10646e5e26e7"
# The four digests the job definition sets beside the execution the runtime stamps.
# The policy digest among them is the one this tree can answer for itself, so the
# value the job definition is given here is the reconciled one.
COLLECTION_DIGESTS = {
    "COLLECTION_SOURCE_SHA": "a" * 40,
    "COLLECTION_IMAGE_URI": "registry.example/engine@sha256:" + "b" * 64,
    "COLLECTION_POLICY_SHA256": RECONCILED_POLICY_DIGEST,
    "COLLECTION_PROFILE_SHA256": "d" * 64,
}
STAMPED_AUTHORITY = {
    "execution_id": EXECUTION,
    "source_sha": COLLECTION_DIGESTS["COLLECTION_SOURCE_SHA"],
    "image_uri": COLLECTION_DIGESTS["COLLECTION_IMAGE_URI"],
    "policy_sha256": COLLECTION_DIGESTS["COLLECTION_POLICY_SHA256"],
    "profile_sha256": COLLECTION_DIGESTS["COLLECTION_PROFILE_SHA256"],
}


def _returns(receipt):
    """A collector that takes the collection-only flag and returns this receipt."""

    def collector(**_kwargs):
        return receipt

    return collector


def _recorder():
    calls = []

    def collector(**kwargs):
        calls.append(kwargs)
        return kwargs

    return calls, collector


# Refusal ordering. Every case leaves at least two later checks failing as well, so
# the message proves which check fired first. The resolved dataset spy proves the
# runtime check runs before the dataset is ever resolved.
@pytest.mark.parametrize(
    ("profile", "runtime_env", "expected", "dataset_lookups"),
    [
        ({}, None, "environment", 0),
        ({"environment": "prod", "source_dataset": "trends_v2_dev"}, "dev", "environment", 0),
        ({"environment": "staging", "source_dataset": "trends_v2_dev"}, "dev", "source_dataset", 0),
        ({"environment": "staging", "source_dataset": "trends_v2"}, "prod", "source_dataset", 0),
        (STAGING_PROFILE, "dev", "runtime_environment", 0),
        (STAGING_PROFILE, None, "runtime_environment", 0),
        (STAGING_PROFILE, "staging", "resolved_source_dataset", 1),
    ],
)
def test_refusals_fire_in_order_before_the_collector(
    monkeypatch, profile, runtime_env, expected, dataset_lookups
):
    if runtime_env is None:
        monkeypatch.delenv("TRENDS_ENV", raising=False)
    else:
        monkeypatch.setenv("TRENDS_ENV", runtime_env)
    monkeypatch.setenv("BIGQUERY_DATASET", "trends_v2")
    lookups = []
    real_get_dataset = dataset_resolver.get_dataset
    monkeypatch.setattr(
        dataset_resolver, "get_dataset", lambda: lookups.append(1) or real_get_dataset()
    )
    calls, collector = _recorder()
    with pytest.raises(ValueError) as refusal:
        collect_staging(profile, authority=PACKET_AUTHORITY, collector=collector)
    assert str(refusal.value) == expected
    assert calls == []
    assert len(lookups) == dataset_lookups


def test_resolved_dataset_must_equal_the_profile_dataset(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", "trends_v2")
    assert dataset_resolver.get_dataset() == "trends_v2_staging"
    calls, collector = _recorder()
    with pytest.raises(ValueError, match=r"^resolved_source_dataset$"):
        collect_staging(dict(STAGING_PROFILE), authority=PACKET_AUTHORITY, collector=collector)
    assert calls == []


def test_collector_receives_only_the_collection_only_flag(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", STAGING_DATASET)
    calls, collector = _recorder()
    result = collect_staging(dict(STAGING_PROFILE), authority=PACKET_AUTHORITY, collector=collector)
    assert calls == [{"stop_after_ingestion": True}]
    assert result == {"stop_after_ingestion": True}


def test_importing_the_module_does_not_import_the_producer():
    probe = "\n".join(
        [
            "import json, sys",
            "import scripts.staging.collect_42_sources as module",
            "loaded = lambda: sorted(n for n in sys.modules if n.endswith('run_rss_now'))",
            "before = loaded()",
            "collector = module.producer_collector()",
            "print(json.dumps({'before': before, 'after': loaded(), 'name': collector.__name__}))",
        ]
    )
    env = dict(os.environ, TRENDS_ENV="dev", BIGQUERY_DATASET="trends_v2")
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ENGINE_ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    assert outcome["before"] == []
    assert outcome["after"] == ["scripts.run_rss_now"]
    assert outcome["name"] == "_run_impl"


# Bootstrap profile builder.
def _executable_configuration():
    return {
        "markets": ["za", "ng", "ke"],
        "routes": {
            "rss": {"caps": {"max_items_per_market": 200}},
            "social_search": {"caps": {"budget_units_per_run": 500, "max_posts_per_market": 100}},
            "trending_search": {"caps": {}},
        },
    }


def _new_target_authority():
    return {
        "targets": {
            "rss": {"caps": {"max_items_per_market": 300, "max_age_days": 7}},
            "social_search": {"caps": {"budget_units_per_run": 400}},
            "app_charts": {"caps": {"max_items_per_market": 50}},
        }
    }


def _digest_of(profile):
    body = {key: value for key, value in profile.items() if key != "profile_sha256"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def test_bootstrap_profile_carries_only_routes_approved_by_both_inputs(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network attempted"))
    profile = build_bootstrap_profile(_executable_configuration(), _new_target_authority())
    assert profile == {
        "contract_version": "collection_profile_bootstrap_v1",
        "environment": "staging",
        "source_dataset": STAGING_DATASET,
        "markets": ["za", "ng", "ke"],
        "routes": {
            "rss": {"caps": {"max_items_per_market": 200}},
            "social_search": {"caps": {"budget_units_per_run": 400}},
        },
        "excluded_routes": [
            {"route": "app_charts", "reason": "not_executable"},
            {"route": "trending_search", "reason": "not_in_new_target_authority"},
        ],
        "excluded_caps": [
            {"route": "rss", "cap": "max_age_days", "reason": "not_in_both_inputs"},
            {
                "route": "social_search",
                "cap": "max_posts_per_market",
                "reason": "not_in_both_inputs",
            },
        ],
        "profile_sha256": _digest_of(profile),
    }


def test_bootstrap_profile_digest_is_deterministic_and_cap_sensitive():
    first = build_bootstrap_profile(_executable_configuration(), _new_target_authority())
    second = build_bootstrap_profile(_executable_configuration(), _new_target_authority())
    assert first == second
    authority = _new_target_authority()
    authority["targets"]["rss"]["caps"]["max_items_per_market"] = 150
    tightened = build_bootstrap_profile(_executable_configuration(), authority)
    assert tightened["routes"]["rss"]["caps"] == {"max_items_per_market": 150}
    assert tightened["profile_sha256"] != first["profile_sha256"]
    assert tightened["profile_sha256"] == _digest_of(tightened)


def test_bootstrap_profile_with_no_overlap_approves_nothing():
    configuration = _executable_configuration()
    configuration["routes"] = {"rss": {"caps": {}}}
    authority = {"targets": {"app_charts": {}}}
    profile = build_bootstrap_profile(configuration, authority)
    assert profile["routes"] == {}
    assert profile["excluded_routes"] == [
        {"route": "app_charts", "reason": "not_executable"},
        {"route": "rss", "reason": "not_in_new_target_authority"},
    ]
    assert profile["excluded_caps"] == []


def test_bootstrap_profile_caps_are_the_minimum_over_names_both_inputs_share():
    configuration = _executable_configuration()
    configuration["routes"] = {
        "rss": {"caps": {"shared_low": 10, "shared_high": 900, "executable_only": 5}}
    }
    authority = {
        "targets": {"rss": {"caps": {"shared_low": 40, "shared_high": 300, "authority_only": 1}}}
    }
    profile = build_bootstrap_profile(configuration, authority)
    assert profile["routes"] == {"rss": {"caps": {"shared_high": 300, "shared_low": 10}}}
    assert profile["excluded_caps"] == [
        {"route": "rss", "cap": "authority_only", "reason": "not_in_both_inputs"},
        {"route": "rss", "cap": "executable_only", "reason": "not_in_both_inputs"},
    ]
    assert profile["excluded_routes"] == []
    assert profile["profile_sha256"] == _digest_of(profile)


@pytest.mark.parametrize(
    ("markets", "field"),
    [
        (["za", "ng"], "markets"),
        (["za", "ng", "ke", "gh"], "markets"),
        (["za", "ng", "ng"], "markets"),
        (None, "markets"),
    ],
)
def test_bootstrap_profile_requires_exactly_the_three_markets(markets, field):
    configuration = _executable_configuration()
    configuration["markets"] = markets
    with pytest.raises(ValueError, match=field):
        build_bootstrap_profile(configuration, _new_target_authority())


@pytest.mark.parametrize(
    ("configuration", "authority", "field"),
    [
        ({"markets": ["za", "ng", "ke"]}, {"targets": {}}, "routes"),
        ({"markets": ["za", "ng", "ke"], "routes": {}}, {}, "targets"),
        (
            {"markets": ["za", "ng", "ke"], "routes": {"rss": {"caps": {"limit": "10"}}}},
            {"targets": {"rss": {}}},
            "caps",
        ),
        (
            {"markets": ["za", "ng", "ke"], "routes": {"rss": {"caps": {"limit": -1}}}},
            {"targets": {"rss": {}}},
            "caps",
        ),
        (
            {"markets": ["za", "ng", "ke"], "routes": {"rss": {}}},
            {"targets": {"rss": {"caps": {"limit": True}}}},
            "caps",
        ),
    ],
)
def test_bootstrap_profile_refuses_malformed_inputs(configuration, authority, field):
    with pytest.raises(ValueError, match=field):
        build_bootstrap_profile(configuration, authority)


# Collection receipt.
def _receipt(**overrides):
    fields = {
        "run_id": "20260913T003000Z",
        "execution_id": "exec-0001",
        "source_sha": "a" * 40,
        "image_uri": "registry.example/engine@sha256:" + "b" * 64,
        "policy_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "cutoff": "2026-09-13T00:30:00+00:00",
        "market_states": {"za": "collected", "ng": "collected", "ke": "collected"},
        "raw_count": 1200,
        "enriched_count": 1150,
    }
    fields.update(overrides)
    return collection_receipt(**fields)


def test_receipt_names_its_counts_by_unit_and_is_complete_when_all_collected():
    receipt = _receipt()
    assert receipt == {
        "contract_version": "collection_receipt_v1",
        "run_id": "20260913T003000Z",
        "execution_id": "exec-0001",
        "source_sha": "a" * 40,
        "image_uri": "registry.example/engine@sha256:" + "b" * 64,
        "policy_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "cutoff": "2026-09-13T00:30:00+00:00",
        "market_states": {"za": "collected", "ng": "collected", "ke": "collected"},
        "raw_rows_persisted": 1200,
        "enriched_rows_persisted": 1150,
        "funded_close": None,
        "complete": True,
    }
    assert "raw_count" not in receipt
    assert "enriched_count" not in receipt


@pytest.mark.parametrize(
    "states",
    [
        {"za": "collected", "ng": "partial", "ke": "collected"},
        {"za": "failed", "ng": "collected", "ke": "collected"},
        {"za": "collected", "ng": "collected", "ke": "skipped"},
        {"za": "skipped", "ng": "skipped", "ke": "skipped"},
        {"za": "collected", "ng": "empty", "ke": "collected"},
    ],
)
def test_receipt_is_not_complete_unless_every_market_collected(states):
    receipt = _receipt(market_states=states)
    assert receipt["complete"] is False
    assert receipt["market_states"] == states


def test_receipt_keeps_zero_counts_for_an_idempotent_rerun():
    states = {"za": "skipped", "ng": "skipped", "ke": "skipped"}
    receipt = _receipt(market_states=states, raw_count=0, enriched_count=0)
    assert receipt["raw_rows_persisted"] == 0
    assert receipt["enriched_rows_persisted"] == 0
    assert receipt["complete"] is False


@pytest.mark.parametrize(
    ("states", "field"),
    [
        (
            {"za": "collected", "ng": "collected", "ke": "collected", "gh": "collected"},
            "market_states",
        ),
        ({"za": "collected", "ng": "collected"}, "market_states"),
        ({"za": "collected", "ng": "done", "ke": "collected"}, "market_states"),
        ({"ZA": "collected", "ng": "collected", "ke": "collected"}, "market_states"),
        ([("za", "collected")], "market_states"),
    ],
)
def test_receipt_refuses_unknown_missing_or_non_terminal_markets(states, field):
    with pytest.raises(ValueError, match=field):
        _receipt(market_states=states)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"raw_count": "1200"}, "raw_rows_persisted"),
        ({"raw_count": 12.0}, "raw_rows_persisted"),
        ({"raw_count": True}, "raw_rows_persisted"),
        ({"raw_count": -1}, "raw_rows_persisted"),
        ({"enriched_count": None}, "enriched_rows_persisted"),
        ({"enriched_count": 3.5}, "enriched_rows_persisted"),
        ({"run_id": ""}, "run_id"),
        ({"cutoff": None}, "cutoff"),
        ({"image_uri": 7}, "image_uri"),
    ],
)
def test_receipt_refuses_non_integer_counts_and_empty_identity(overrides, field):
    with pytest.raises(ValueError, match=field):
        _receipt(**overrides)


# Finding 6b. The receipt's digests are checked for what they are, not only that
# they are text, and a profile digest is checked against the profile it names.
@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"policy_sha256": "nope"}, "policy_sha256"),
        ({"policy_sha256": "c" * 63}, "policy_sha256"),
        ({"policy_sha256": "C" * 64}, "policy_sha256"),
        ({"policy_sha256": "g" * 64}, "policy_sha256"),
        ({"policy_sha256": "c" * 64 + " "}, "policy_sha256"),
        ({"profile_sha256": "nope"}, "profile_sha256"),
        ({"profile_sha256": "d" * 65}, "profile_sha256"),
        ({"profile_sha256": "D" * 64}, "profile_sha256"),
    ],
)
def test_receipt_refuses_a_digest_that_is_not_one(overrides, field):
    with pytest.raises(ValueError, match=field):
        _receipt(**overrides)


def test_receipt_checks_the_profile_digest_against_the_profile_it_names():
    profile = build_bootstrap_profile(_executable_configuration(), _new_target_authority())
    receipt = _receipt(profile_sha256=profile["profile_sha256"], profile=profile)
    assert receipt["profile_sha256"] == profile["profile_sha256"]
    with pytest.raises(ValueError, match="profile_sha256"):
        _receipt(profile_sha256="d" * 64, profile=profile)
    tightened = dict(profile)
    tightened["markets"] = ["za", "ng", "ke"]
    tightened["routes"] = {}
    with pytest.raises(ValueError, match="profile_sha256"):
        _receipt(profile_sha256=profile["profile_sha256"], profile=tightened)
    with pytest.raises(ValueError, match="profile"):
        _receipt(profile_sha256=profile["profile_sha256"], profile="not-a-profile")


# Funded terminal close carried by the receipt.
def _funded_close(**overrides):
    block = {
        "attribution_state": "gap_detected",
        "balance_read_status": "unavailable",
        "calls": 4,
        "balance_delta": None,
        "run_balance_delta": None,
        "terminal_id": "terminal-0001",
    }
    block.update(overrides)
    return block


def test_receipt_keeps_an_unread_funded_balance_unknown():
    receipt = _receipt(funded_close=_funded_close())
    assert receipt["funded_close"] == _funded_close()
    assert receipt["funded_close"]["balance_delta"] is None
    assert receipt["funded_close"]["run_balance_delta"] is None


def test_receipt_carries_a_measured_funded_close_as_decimal_text():
    measured = _funded_close(
        attribution_state="complete",
        balance_read_status="measured",
        balance_delta="12",
        run_balance_delta="12",
    )
    assert _receipt(funded_close=measured)["funded_close"] == measured


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"balance_delta": "0"}, "balance_delta must stay unknown"),
        ({"run_balance_delta": "0"}, "run_balance_delta must stay unknown"),
        ({"balance_read_status": "measured"}, "balance_delta"),
        ({"balance_read_status": "guessed"}, "balance_read_status"),
        ({"attribution_state": ""}, "attribution_state"),
        ({"calls": -1}, "calls"),
        ({"calls": True}, "calls"),
        ({"terminal_id": ""}, "terminal_id"),
    ],
)
def test_receipt_refuses_a_funded_close_that_invents_a_balance(overrides, field):
    with pytest.raises(ValueError, match=field):
        _receipt(funded_close=_funded_close(**overrides))


@pytest.mark.parametrize("value", [{}, {"balance_read_status": "unavailable"}, "unavailable", 0])
def test_receipt_refuses_a_malformed_funded_close(value):
    with pytest.raises(ValueError, match="funded_close"):
        _receipt(funded_close=value)


# The job entry point. The R03 manifest reader and the exact execution identity
# both run before the producer module is imported, so an unauthorized resource
# or an execution that is not this job's refuses without loading the collector.
class _Factory:
    """Stands in for the producer binding, recording how it was bound and called."""

    def __init__(self, receipt=None):
        self.bindings = []
        self.placements = []
        self.calls = []
        self._receipt = receipt or _receipt(execution_id=EXECUTION)

    def __call__(self, *, receipt_ledger=None, collection_authority=None, **placement):
        self.bindings.append(
            {"receipt_ledger": receipt_ledger, "collection_authority": collection_authority}
        )
        # The profile and the authority kind the run is placed under are recorded
        # apart from the binding above, so the binding stays the one assertion it
        # always was and the placement is asserted on its own.
        self.placements.append(placement)

        def collector(**kwargs):
            self.calls.append(kwargs)
            return self._receipt

        return collector


def _staging_environment(monkeypatch, execution=EXECUTION):
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", STAGING_DATASET)
    for variable, value in COLLECTION_DIGESTS.items():
        monkeypatch.setenv(variable, value)
    if execution is None:
        monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
    else:
        monkeypatch.setenv("CLOUD_RUN_EXECUTION", execution)


def _generation_without(resource_name):
    real = active_generation()
    manifest = dict(real.resource_manifest)
    manifest["resources"] = tuple(
        row for row in real.resource_manifest["resources"] if row["name"] != resource_name
    )
    return TrustedGeneration(
        origin_registry_sha256=real.origin_registry_sha256,
        resource_manifest_sha256=real.resource_manifest_sha256,
        registry=real.registry,
        resource_manifest=manifest,
        registry_path=real.registry_path,
        resource_manifest_path=real.resource_manifest_path,
    )


def _generation_without_action(resource_name, action):
    """Keep the resource row and drop only the action it must carry.

    ``_generation_without`` removes the whole row, so it proves only that the
    name is in the manifest. This one leaves the name and every other action
    in place, so the refusal can only come from the action the guard names.
    """
    real = active_generation()
    manifest = dict(real.resource_manifest)
    manifest["resources"] = tuple(
        {"name": row["name"], "actions": tuple(a for a in row["actions"] if a != action)}
        if row["name"] == resource_name
        else row
        for row in real.resource_manifest["resources"]
    )
    return TrustedGeneration(
        origin_registry_sha256=real.origin_registry_sha256,
        resource_manifest_sha256=real.resource_manifest_sha256,
        registry=real.registry,
        resource_manifest=manifest,
        registry_path=real.registry_path,
        resource_manifest_path=real.resource_manifest_path,
    )


def _generation_naming(project, region):
    """A generation that names another target while carrying the authentic rows."""
    real = active_generation()
    manifest = dict(real.resource_manifest)
    manifest["project"] = project
    manifest["region"] = region
    return TrustedGeneration(
        origin_registry_sha256=real.origin_registry_sha256,
        resource_manifest_sha256=real.resource_manifest_sha256,
        registry=real.registry,
        resource_manifest=manifest,
        registry_path=real.registry_path,
        resource_manifest_path=real.resource_manifest_path,
    )


def _generation_with_extra_row():
    """Every approved row kept, one more added, and the authentic digest claimed."""
    real = active_generation()
    manifest = dict(real.resource_manifest)
    manifest["resources"] = (
        *real.resource_manifest["resources"],
        {"name": "//run.googleapis.com/projects/x/locations/y/jobs/z", "actions": ("invoke",)},
    )
    return TrustedGeneration(
        origin_registry_sha256=real.origin_registry_sha256,
        resource_manifest_sha256=real.resource_manifest_sha256,
        registry=real.registry,
        resource_manifest=manifest,
        registry_path=real.registry_path,
        resource_manifest_path=real.resource_manifest_path,
    )


@pytest.mark.parametrize(
    ("resource", "action"),
    [(JOB_RESOURCE, "invoke"), (DATASET_RESOURCE, "write")],
)
def test_entry_point_refuses_a_resource_row_that_drops_the_action(monkeypatch, resource, action):
    """The action is load bearing, not just the resource name.

    The job row keeps deploy and read, the dataset row keeps read, so a guard
    that asked for any other action than the one it needs would pass here.
    """
    _staging_environment(monkeypatch)
    factory = _Factory()
    with pytest.raises(OriginRefusal, match="resource_action_forbidden"):
        main(generation=_generation_without_action(resource, action), collector_factory=factory)
    assert factory.bindings == []
    assert factory.calls == []


# Finding 6a. The manifest may not be asked whether it approves of itself: the
# target is pinned here, and the digest the generation carries is checked against
# the manifest the trusted catalogue reads for it.
def test_entry_point_refuses_a_manifest_that_names_another_target(monkeypatch):
    _staging_environment(monkeypatch)
    factory = _Factory()
    with pytest.raises(ValueError, match=r"^resource_manifest_target$"):
        main(
            generation=_generation_naming("attacker-project", "europe-west9"),
            collector_factory=factory,
        )
    assert factory.bindings == []
    assert factory.calls == []


def test_entry_point_refuses_a_manifest_that_is_not_the_one_its_digest_names(monkeypatch):
    _staging_environment(monkeypatch)
    factory = _Factory()
    with pytest.raises(ValueError, match=r"^resource_manifest_digest$"):
        main(generation=_generation_with_extra_row(), collector_factory=factory)
    assert factory.bindings == []
    assert factory.calls == []


def test_manifest_authority_names_the_approved_job_dataset_and_execution(monkeypatch):
    _staging_environment(monkeypatch)
    authority = manifest_authority()
    assert authority == {
        "job_resource": JOB_RESOURCE,
        "source_dataset_resource": DATASET_RESOURCE,
        "execution_id": EXECUTION,
        "resource_manifest_sha256": active_generation().resource_manifest_sha256,
    }


@pytest.mark.parametrize("resource", [JOB_RESOURCE, DATASET_RESOURCE])
def test_entry_point_refuses_a_resource_the_manifest_does_not_authorize(monkeypatch, resource):
    _staging_environment(monkeypatch)
    factory = _Factory()
    with pytest.raises(OriginRefusal, match="resource_action_forbidden"):
        main(generation=_generation_without(resource), collector_factory=factory)
    assert factory.bindings == []
    assert factory.calls == []


@pytest.mark.parametrize(
    ("execution", "expected"),
    [
        (None, "execution_identity"),
        ("", "execution_identity"),
        ("   ", "execution_identity"),
        # Already refused before this slice, kept as regressions.
        ("intelligence-42-daily-staging-9k2xv", "execution_job"),
        ("intelligence-42-ingest-staging", "execution_job"),
        ("prod-ingest-9k2xv", "execution_job"),
        ("INTELLIGENCE-42-INGEST-STAGING-9k2xv", "execution_job"),
        ("Intelligence-42-Ingest-Staging-9k2xv", "execution_job"),
        ("intelligence-42-ingest-stag\u0456ng-9k2xv", "execution_job"),
        ("intelligence-42-ingest-stag\u200bing-9k2xv", "execution_job"),
        ("intelligence-42-ingest-stagin-9k2xv", "execution_job"),
        # A sibling job whose name starts with this one's, which the prefix test took.
        ("intelligence-42-ingest-staging-v2-9k2xv", "execution_job"),
        ("intelligence-42-ingest-staging-prod-abc", "execution_job"),
        # An empty, punctuated, invisible, traversing or line breaking suffix.
        ("intelligence-42-ingest-staging-", "execution_job"),
        ("intelligence-42-ingest-staging-9k2xv.", "execution_job"),
        ("intelligence-42-ingest-staging-\u200b9k2xv", "execution_job"),
        ("intelligence-42-ingest-staging-../../etc/passwd", "execution_job"),
        ("intelligence-42-ingest-staging-9k2xv\nExecution:      forged", "execution_job"),
        ("intelligence-42-ingest-staging-9K2XV", "execution_job"),
        ("intelligence-42-ingest-staging-9k2x", "execution_job"),
        ("intelligence-42-ingest-staging-9k2xvv", "execution_job"),
    ],
)
def test_entry_point_refuses_an_execution_that_is_not_this_job(monkeypatch, execution, expected):
    _staging_environment(monkeypatch, execution=execution)
    factory = _Factory()
    with pytest.raises(ValueError, match=rf"^{expected}$"):
        main(collector_factory=factory)
    assert factory.bindings == []
    assert factory.calls == []


def test_entry_point_collects_only_and_binds_the_ledger_it_was_given(monkeypatch, capsys):
    _staging_environment(monkeypatch)
    ledger = object()
    factory = _Factory()
    assert main(receipt_ledger=ledger, collector_factory=factory) == 0
    assert factory.bindings == [
        {"receipt_ledger": ledger, "collection_authority": STAMPED_AUTHORITY}
    ]
    assert factory.calls == [{"stop_after_ingestion": True}]
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == factory._receipt


@pytest.mark.parametrize(
    ("states", "status"),
    [
        ({"za": "collected", "ng": "collected", "ke": "collected"}, 0),
        ({"za": "collected", "ng": "empty", "ke": "skipped"}, 0),
        ({"za": "skipped", "ng": "skipped", "ke": "skipped"}, 0),
        ({"za": "partial", "ng": "collected", "ke": "collected"}, 1),
        ({"za": "collected", "ng": "failed", "ke": "collected"}, 1),
    ],
)
def test_entry_point_fails_the_run_when_a_market_lost_data(monkeypatch, states, status):
    _staging_environment(monkeypatch)
    factory = _Factory(receipt=_receipt(market_states=states, execution_id=EXECUTION))
    assert main(collector_factory=factory) == status
    assert factory.calls == [{"stop_after_ingestion": True}]


def test_entry_point_refuses_a_runtime_that_is_not_the_staging_target(monkeypatch):
    _staging_environment(monkeypatch)
    monkeypatch.setenv("BIGQUERY_DATASET", "trends_v2")
    factory = _Factory()
    with pytest.raises(ValueError, match=r"^resolved_source_dataset$"):
        main(collector_factory=factory)
    assert factory.bindings == []
    assert factory.calls == []


def test_entry_point_refusal_never_imports_the_producer():
    probe = "\n".join(
        [
            "import json, sys",
            "import scripts.staging.collect_42_sources as module",
            "try:",
            "    module.main()",
            "except ValueError as refusal:",
            "    outcome = str(refusal)",
            "loaded = sorted(n for n in sys.modules if n.endswith('run_rss_now'))",
            "print(json.dumps({'refusal': outcome, 'loaded': loaded}))",
        ]
    )
    env = dict(os.environ, TRENDS_ENV="staging", BIGQUERY_DATASET=STAGING_DATASET)
    env.pop("CLOUD_RUN_EXECUTION", None)
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ENGINE_ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    assert outcome == {"refusal": "execution_identity", "loaded": []}


# Finding 4. The public pair reaches the producer with no manifest read at all, so
# a caller must state the authority it runs under. The daily stage runs under its
# own approval chain and substitutes its own attempt id; that is not this slice's
# to re-gate, but it must be declared rather than left implicit.
def test_collect_staging_refuses_a_caller_that_names_no_authority(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", STAGING_DATASET)
    calls, collector = _recorder()
    with pytest.raises(TypeError, match="authority"):
        collect_staging(dict(STAGING_PROFILE), collector=collector)  # type: ignore[call-arg]
    assert calls == []


@pytest.mark.parametrize(
    "authority",
    [
        None,
        {},
        "manifest",
        {"authority_kind": "unverified"},
        {"authority_kind": "unverified", "declared_by": ""},
        {"authority_kind": "unverified", "declared_by": "   "},
        {"authority_kind": "unverified", "declared_by": 7},
        {"authority_kind": "verified_manifest", "execution_id": EXECUTION},
        {
            "job_resource": JOB_RESOURCE,
            "source_dataset_resource": DATASET_RESOURCE,
            "execution_id": EXECUTION,
            "resource_manifest_sha256": "not-a-digest",
        },
        {
            "job_resource": JOB_RESOURCE,
            "source_dataset_resource": DATASET_RESOURCE,
            "execution_id": "intelligence-42-daily-staging-9k2xv",
            "resource_manifest_sha256": "f" * 64,
        },
        {
            "job_resource": "//run.googleapis.com/projects/x/locations/y/jobs/z",
            "source_dataset_resource": DATASET_RESOURCE,
            "execution_id": EXECUTION,
            "resource_manifest_sha256": "f" * 64,
        },
        # A verified authority naming another warehouse dataset is not this run's.
        {
            "job_resource": JOB_RESOURCE,
            "source_dataset_resource": (
                "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/trends_v2"
            ),
            "execution_id": EXECUTION,
            "resource_manifest_sha256": "f" * 64,
        },
        # The four fields exactly: one more field leaves a mapping that straddles
        # both branches, so the set is compared whole rather than for containment.
        {
            "job_resource": JOB_RESOURCE,
            "source_dataset_resource": DATASET_RESOURCE,
            "execution_id": EXECUTION,
            "resource_manifest_sha256": "f" * 64,
            "authority_kind": "verified_manifest",
        },
    ],
)
def test_collect_staging_refuses_an_authority_it_cannot_place(monkeypatch, authority):
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", STAGING_DATASET)
    calls, collector = _recorder()
    with pytest.raises(ValueError, match=r"^collection_authority$"):
        collect_staging(dict(STAGING_PROFILE), authority=authority, collector=collector)
    assert calls == []


def test_collect_staging_names_an_unverified_authority_in_the_run_it_makes(monkeypatch):
    """A named unverified authority is carried, and nothing about it is verified.

    The receipt of such a run is the producer's own, stamped under whatever the
    caller keyed it by, and this boundary binds nothing to it. A manifest
    verified authority is the other case, and the cross check below is what
    tells the two apart in the record.
    """
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", STAGING_DATASET)
    declared = unverified_authority("daily_collect_stage")
    assert declared == {"authority_kind": "unverified", "declared_by": "daily_collect_stage"}
    receipt = _receipt(execution_id="attempt:collect:7")
    assert (
        collect_staging(dict(STAGING_PROFILE), authority=declared, collector=_returns(receipt))
        is receipt
    )


def test_collect_staging_binds_a_verified_authority_to_the_receipt(monkeypatch):
    _staging_environment(monkeypatch)
    authority = manifest_authority()
    stamped = _receipt(execution_id=EXECUTION)
    assert (
        collect_staging(dict(STAGING_PROFILE), authority=authority, collector=_returns(stamped))
        is stamped
    )
    for foreign in (
        _receipt(execution_id="attempt:collect:7"),
        _receipt(execution_id=f"{INGEST_JOB}-aaaaa"),
        {"stop_after_ingestion": True},
        # The right execution carried by something that is no collection receipt.
        {"execution_id": EXECUTION},
    ):
        with pytest.raises(ValueError, match=r"^collection_execution_identity$"):
            collect_staging(dict(STAGING_PROFILE), authority=authority, collector=_returns(foreign))


def test_the_daily_stage_declares_the_unverified_authority_it_runs_under():
    from src.analysis.open_intelligence import daily_stages

    assert daily_stages.COLLECTION_AUTHORITY == {
        "authority_kind": "unverified",
        "declared_by": "daily_collect_stage",
    }


# Finding 3. The authority the entry point verified is the one the producer stamps.
def test_entry_point_refuses_before_binding_when_a_stamped_digest_is_missing(monkeypatch):
    for variable in COLLECTION_DIGESTS:
        _staging_environment(monkeypatch)
        monkeypatch.delenv(variable)
        factory = _Factory()
        with pytest.raises(ValueError, match=rf"^collection_authority: {variable}$"):
            main(collector_factory=factory)
        assert factory.bindings == []
        assert factory.calls == []


def test_entry_point_reads_the_stamped_authority_from_the_environment_it_was_given(monkeypatch):
    _staging_environment(monkeypatch, execution=None)
    for variable in COLLECTION_DIGESTS:
        monkeypatch.delenv(variable)
    environ = {EXECUTION_VARIABLE: EXECUTION, **COLLECTION_DIGESTS}
    factory = _Factory()
    assert main(environ=environ, collector_factory=factory) == 0
    assert factory.bindings == [{"receipt_ledger": None, "collection_authority": STAMPED_AUTHORITY}]


# A frozen estate, written out in full here rather than read back from the tree, and
# the policy digest of it. Both are fixed text. The digest was derived once by hand,
# the same way as the one above: the policy fields of the rows below were written as
# sorted compact json and the sha256 of those bytes is the literal. Nothing in this
# module recomputes it, so a change to what the digest is taken over moves the
# computed value away from this literal instead of moving the two together. If a row
# below is edited, or the set of policy fields a route carries changes, this literal
# is wrong and must be worked out by hand again. The estate is a real one in every
# respect the digest cares about, and it is not the estate this tree reconciles,
# which is what makes its digest usable as the value the entry point must refuse: a
# check on the shape of a digest, or on its being the policy digest of some estate,
# admits it and only a recomputation of this estate turns it away.
# The set of policy fields did change once: the source estate lane gave every route
# a condition, the reason behind its state, and put it in the digest preimage. The
# row below carries the condition a normal executable route in this state carries,
# and the literal was worked out by hand again over the twenty one field preimage.
# It was 778a7129fc5d8be2a1aceba127bae7910dde55125b10311c115418e3f7f18efc before.
FROZEN_ESTATE_ROUTE = {
    "route": "frozen_source/listing",
    "state": "approved_unproven",
    "condition": "switch_on_unproven",
    "live": False,
    "vendor": "frozen_vendor",
    "account": "frozen_account",
    "platform": "frozen_platform",
    "market": "za",
    "feature": "listing",
    "required": True,
    "capability_proof": None,
    "cadence": "daily",
    "freshness": "same_day",
    "native_identity": "frozen_native_identity",
    "date_rule": "frozen_date_rule",
    "author_rule": "frozen_author_rule",
    "url_rule": "frozen_url_rule",
    "geography_method": "frozen_geography_method",
    "budget_stage": None,
    "qualified_contribution": None,
    "next_owner_action": "frozen_next_owner_action",
    # Carried by the row and excluded from the digest, so a reading that folded it
    # in would move the computed value off the literal below.
    "evidence": {"switch": "frozen.enabled", "value": True},
}
FROZEN_ESTATE_ROWS = (
    {
        "source": "frozen_source",
        "market": "za",
        "state": "approved_unproven",
        "routes": ({**FROZEN_ESTATE_ROUTE},),
    },
    {
        "source": "frozen_source",
        "market": "ng",
        "state": "excluded",
        "routes": ({**FROZEN_ESTATE_ROUTE, "state": "excluded", "market": "ng"},),
    },
)
FROZEN_ESTATE_POLICY_DIGEST = "ca23c3f7ffdbb97e2bcd4d87fcde2cfbce17754f1f8c566c0c6e2e6808726fb0"


def test_entry_point_stamps_the_policy_digest_of_the_reconciled_estate(monkeypatch):
    """The stamped policy digest is held to the estate this image reconciles.

    Two digests are pinned here as literals and neither is read back from the run
    under test. The first is the digest of the frozen estate this module writes
    out, so the reading the rest of this test leans on is fixed rather than
    restated. The second is the digest of the estate this tree reconciles, which
    the entry point must admit. It must then refuse the frozen estate's digest,
    which is a real policy digest of a real estate and not this one, so nothing
    that merely recognises a digest, or a digest of something, passes this.
    """
    assert policy_digest(FROZEN_ESTATE_ROWS) == FROZEN_ESTATE_POLICY_DIGEST
    assert FROZEN_ESTATE_POLICY_DIGEST != RECONCILED_POLICY_DIGEST
    _staging_environment(monkeypatch)
    admitted = _Factory()
    assert main(collector_factory=admitted) == 0
    assert admitted.bindings == [
        {"receipt_ledger": None, "collection_authority": STAMPED_AUTHORITY}
    ]
    assert STAMPED_AUTHORITY["policy_sha256"] == RECONCILED_POLICY_DIGEST
    monkeypatch.setenv("COLLECTION_POLICY_SHA256", FROZEN_ESTATE_POLICY_DIGEST)
    refused = _Factory()
    with pytest.raises(ValueError, match=r"^collection_policy_unreconciled$"):
        main(collector_factory=refused)
    assert refused.bindings == []
    assert refused.calls == []


def test_entry_point_refuses_a_stamped_policy_digest_from_the_environment_it_was_given(monkeypatch):
    """The refusal reads the environment the caller handed in, not the process one.

    A process environment carrying the reconciled digest must not stand in for a
    handed in environment that carries another, or the entry point would be taking
    its answer from somewhere other than the block it is checking.
    """
    _staging_environment(monkeypatch)
    environ = {
        **COLLECTION_DIGESTS,
        EXECUTION_VARIABLE: EXECUTION,
        "COLLECTION_POLICY_SHA256": FROZEN_ESTATE_POLICY_DIGEST,
    }
    factory = _Factory()
    with pytest.raises(ValueError, match=r"^collection_policy_unreconciled$"):
        main(environ=environ, collector_factory=factory)
    assert factory.bindings == []
    assert factory.calls == []


# Finding 7. A receipt that names no markets is not a clean run.
@pytest.mark.parametrize(
    "states",
    [
        None,
        {},
        [("za", "collected")],
        "collected",
        {"za": "collected", "ng": "collected"},
        {"za": "collected", "ng": "collected", "ke": "collected", "gh": "collected"},
    ],
)
def test_entry_point_refuses_a_receipt_that_does_not_name_every_market(monkeypatch, states):
    _staging_environment(monkeypatch)
    receipt = {
        "contract_version": RECEIPT_CONTRACT_VERSION,
        "execution_id": EXECUTION,
        "market_states": states,
    }
    if states is None:
        receipt.pop("market_states")
    factory = _Factory(receipt=receipt)
    with pytest.raises(ValueError, match="market_states"):
        main(collector_factory=factory)
    assert factory.calls == [{"stop_after_ingestion": True}]


# Finding 5. The entry point runs the way this repository's deploy manifests name it.
def test_the_entry_point_runs_as_a_path_not_only_as_a_module():
    env = dict(os.environ, TRENDS_ENV="staging", BIGQUERY_DATASET=STAGING_DATASET)
    env.pop("CLOUD_RUN_EXECUTION", None)
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "scripts/staging/collect_42_sources.py"],
        cwd=ENGINE_ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert "ModuleNotFoundError" not in completed.stderr
    assert completed.returncode == 1
    assert "ValueError: execution_identity" in completed.stderr


# The closing pass over the slice. Each block below names the gap it closes and
# the mutation that survived without it.
def test_an_unverified_authority_must_be_named_where_it_is_declared():
    """The name guard refuses at the declaration, not two calls later.

    ``_declared_authority`` would catch an unnamed declaration at the boundary,
    so the refusal would still happen; it would happen in the wrong place and
    under the wrong caller's name. This pins the guard where it is written.
    """
    for value in ("", "   ", "\t\n", 7, None, b"daily", ["daily"]):
        with pytest.raises(ValueError, match=r"^collection_authority$"):
            unverified_authority(value)  # type: ignore[arg-type]
    assert unverified_authority("  daily_collect_stage  ") == {
        "authority_kind": "unverified",
        "declared_by": "daily_collect_stage",
    }


@pytest.mark.parametrize(
    ("project", "region"),
    [
        ("ogilvy-trends-v2", "europe-west9"),
        ("attacker-project", "us-central1"),
        ("attacker-project", "europe-west9"),
    ],
)
def test_entry_point_pins_both_halves_of_its_target(monkeypatch, project, region):
    """Project and region are each checked, not only the pair as a whole.

    A single case that moves both fields at once passes a guard that reads only
    one of them, so each half is moved on its own here.
    """
    _staging_environment(monkeypatch)
    factory = _Factory()
    with pytest.raises(ValueError, match=r"^resource_manifest_target$"):
        main(generation=_generation_naming(project, region), collector_factory=factory)
    assert factory.bindings == []
    assert factory.calls == []


@pytest.mark.parametrize("digest", [None, 7, True, b"f" * 64, ["f" * 64], {"sha256": "f" * 64}])
def test_a_verified_authority_refuses_a_manifest_digest_that_is_no_text(monkeypatch, digest):
    """The digest is text before it is a sha256.

    The grammar check is asked of it next, and asking a regular expression about
    something that is not text is a type error, which is no refusal this
    boundary names. The refusal must be this boundary's own.
    """
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", STAGING_DATASET)
    calls, collector = _recorder()
    authority = {
        "job_resource": JOB_RESOURCE,
        "source_dataset_resource": DATASET_RESOURCE,
        "execution_id": EXECUTION,
        "resource_manifest_sha256": digest,
    }
    with pytest.raises(ValueError, match=r"^collection_authority$"):
        collect_staging(dict(STAGING_PROFILE), authority=authority, collector=collector)
    assert calls == []


def test_authority_refusal_fires_before_the_profile_the_caller_handed_in(monkeypatch):
    """The declared authority is the first refusal, ahead of every profile check.

    Each case leaves the profile environment, the profile dataset and the
    runtime environment failing too, so the message proves the authority was
    read first and a caller with a bad authority is not told it has a bad
    dataset instead.
    """
    monkeypatch.delenv("TRENDS_ENV", raising=False)
    monkeypatch.setenv("BIGQUERY_DATASET", "trends_v2")
    for authority in (None, {}, {"authority_kind": "unverified", "declared_by": ""}):
        calls, collector = _recorder()
        with pytest.raises(ValueError) as refusal:
            collect_staging(
                {"environment": "prod", "source_dataset": "trends_v2_dev"},
                authority=authority,
                collector=collector,
            )
        assert str(refusal.value) == "collection_authority"
        assert calls == []


def test_manifest_authority_carries_the_stripped_execution_it_verified(monkeypatch):
    """The value stamped is the verified one, not the raw environment string.

    A padded execution passes the grammar only after stripping, and the padded
    form must not reach the operator block, the operation key or the receipt.
    """
    _staging_environment(monkeypatch, execution=f"  {EXECUTION}  ")
    assert manifest_authority()["execution_id"] == EXECUTION
    factory = _Factory()
    assert main(collector_factory=factory) == 0
    assert factory.bindings == [{"receipt_ledger": None, "collection_authority": STAMPED_AUTHORITY}]


def test_entry_point_binds_the_receipt_to_the_execution_it_verified(monkeypatch):
    """The binding runs on the path the job takes, not only when collect_staging is called.

    ``collect_staging`` is never called directly by production code. If ``main``
    declared itself unverified, or handed the boundary anything but the authority
    it verified, the binding would stop running and a producer receipt stamped
    under a foreign execution would be printed as this run's record.
    """
    _staging_environment(monkeypatch)
    for foreign in (
        _receipt(execution_id="attempt:collect:7"),
        _receipt(execution_id=f"{INGEST_JOB}-aaaaa"),
        {"contract_version": RECEIPT_CONTRACT_VERSION},
    ):
        factory = _Factory(receipt=foreign)
        with pytest.raises(ValueError, match=r"^collection_execution_identity$"):
            main(collector_factory=factory)
        assert factory.calls == [{"stop_after_ingestion": True}]


# Each value is truthy, so the factory carries it through rather than falling
# back to its own receipt.
@pytest.mark.parametrize("returned", [7, "receipt", ["contract_version"], object(), True])
def test_entry_point_refuses_a_producer_result_that_is_no_receipt_at_all(monkeypatch, returned):
    """The mapping check is the guard, not the two field reads behind it.

    A mapping carrying the wrong contract or the wrong execution is refused by
    either. Something that is no mapping reaches the field reads as an attribute
    error, which is no refusal this boundary names.
    """
    _staging_environment(monkeypatch)
    factory = _Factory(receipt=returned)
    with pytest.raises(ValueError, match=r"^collection_execution_identity$"):
        main(collector_factory=factory)
    assert factory.calls == [{"stop_after_ingestion": True}]


def test_entry_point_reads_the_manifest_under_the_active_generation_only(monkeypatch):
    """A retired generation is no authority this entry point runs under.

    The catalogue retains prior generations by design, so selecting by digest
    alone would admit a retired manifest the day a second entry is appended.
    """
    from src.analysis.open_intelligence import execution_generations

    _staging_environment(monkeypatch)
    retired = active_generation()
    monkeypatch.setattr(execution_generations, "ACTIVE_GENERATION_PAIR", ("0" * 64, "1" * 64))
    # The catalogue still reads it: selecting by digest alone admits it.
    assert (
        execution_generations.load_trusted_generation(
            retired.origin_registry_sha256, retired.resource_manifest_sha256
        ).resource_manifest_sha256
        == retired.resource_manifest_sha256
    )
    factory = _Factory()
    with pytest.raises(OriginRefusal, match="execution_generation_inactive"):
        main(generation=retired, collector_factory=factory)
    assert factory.bindings == []
    assert factory.calls == []


@pytest.mark.parametrize("variable", ["COLLECTION_POLICY_SHA256", "COLLECTION_PROFILE_SHA256"])
@pytest.mark.parametrize("value", ["x", "z" * 64, "C" * 64, "c" * 63, "c" * 65, " " + "c" * 63])
def test_entry_point_refuses_a_stamped_digest_that_is_not_a_sha256(monkeypatch, variable, value):
    """The stamped digests are held to the shape the daily boundary requires.

    Nothing verified these four values: they are read from the environment here.
    A value that is merely non-empty is what made this entry point weaker than
    the daily one it was meant to harden past.
    """
    _staging_environment(monkeypatch)
    monkeypatch.setenv(variable, value)
    factory = _Factory()
    with pytest.raises(ValueError, match=rf"^collection_authority: {variable}$"):
        main(collector_factory=factory)
    assert factory.bindings == []
    assert factory.calls == []


def test_entry_point_places_the_run_under_the_profile_and_the_authority_kind(monkeypatch):
    """The producer is handed the profile and the kind, so the receipt can carry both.

    Without the profile the receipt's profile digest stays the job definition's
    own word about itself, which is the defect the cross check exists to close.
    Without the kind no consumer of the record can tell a manifest verified run
    from a self declared one.
    """
    _staging_environment(monkeypatch)
    factory = _Factory()
    assert main(collector_factory=factory) == 0
    assert factory.placements == [
        {"collection_profile": entry_point_profile(), "authority_kind": "verified_manifest"}
    ]


def test_the_entry_point_profile_carries_the_digest_of_itself():
    profile = entry_point_profile()
    assert profile["environment"] == "staging"
    assert profile["source_dataset"] == STAGING_DATASET
    assert profile["markets"] == ["za", "ng", "ke"]
    body = {key: value for key, value in profile.items() if key != "profile_sha256"}
    assert (
        profile["profile_sha256"]
        == hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    # The receipt admits it as the digest of the profile it names.
    assert (
        _receipt(profile_sha256=profile["profile_sha256"], profile=profile)["profile_sha256"]
        == profile["profile_sha256"]
    )


@pytest.mark.parametrize("kind", ["verified_manifest", "unverified"])
def test_receipt_names_the_authority_kind_that_produced_it(kind):
    assert _receipt(authority_kind=kind)["authority_kind"] == kind
    assert "authority_kind" not in _receipt()


@pytest.mark.parametrize("kind", ["", "verified", "VERIFIED_MANIFEST", 7, True])
def test_receipt_refuses_an_authority_kind_this_boundary_never_places(kind):
    with pytest.raises(ValueError, match="authority_kind"):
        _receipt(authority_kind=kind)


def test_entry_point_refuses_an_incomplete_receipt_before_printing_it(monkeypatch, capsys):
    """A receipt that is no record of this run never reaches the operator block."""
    _staging_environment(monkeypatch)
    receipt = {
        "contract_version": RECEIPT_CONTRACT_VERSION,
        "execution_id": EXECUTION,
        "market_states": {"za": "collected", "ng": "collected"},
    }
    factory = _Factory(receipt=receipt)
    with pytest.raises(ValueError, match="market_states"):
        main(collector_factory=factory)
    printed = capsys.readouterr().out
    assert "market_states" not in printed
    assert EXECUTION in printed


def test_importing_the_module_as_a_library_leaves_the_path_alone():
    """The bootstrap is guarded, so a library import has no side effect on sys.path.

    The module is imported as a library by the daily stage, the native clients,
    the producer and the ops suite. An unguarded insert prepends the engine root
    at position 0 on each of those imports, putting src, scripts, tests, docs,
    infra and configs ahead of anything installed for the rest of the process.
    """
    probe = "\n".join(
        [
            "import json, sys",
            "before = list(sys.path)",
            "import scripts.staging.collect_42_sources as module",
            "print(json.dumps({'unchanged': sys.path == before,"
            " 'root_once': sys.path.count(str(module._ROOT))}))",
        ]
    )
    env = dict(os.environ, PYTHONPATH=str(ENGINE_ROOT))
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ENGINE_ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "unchanged": True,
        "root_once": 1,
    }


@pytest.mark.parametrize("value", [None, 7, ["a" * 40], object()])
def test_entry_point_refuses_a_stamped_digest_that_is_no_text(monkeypatch, value):
    """A digest the environment mapping carries as something other than text refuses.

    The entry point may be handed an environment mapping directly, and a value
    that is not a string is not a digest. It must refuse by this boundary's own
    name rather than reaching the grammar check as a type error.
    """
    _staging_environment(monkeypatch, execution=None)
    for variable in COLLECTION_DIGESTS:
        monkeypatch.delenv(variable)
    for variable in COLLECTION_DIGESTS:
        environ = {EXECUTION_VARIABLE: EXECUTION, **COLLECTION_DIGESTS, variable: value}
        factory = _Factory()
        with pytest.raises(ValueError, match=rf"^collection_authority: {variable}$"):
            main(environ=environ, collector_factory=factory)
        assert factory.bindings == []


def test_entry_point_stamps_the_digests_trimmed_of_the_padding_they_arrived_with(monkeypatch):
    """A padded digest reaches the producer trimmed, not as it arrived.

    The operation key, the printed block and the receipt all carry these values,
    so padding that survived the read would travel into the record.
    """
    _staging_environment(monkeypatch, execution=None)
    for variable in COLLECTION_DIGESTS:
        monkeypatch.delenv(variable)
    padded = {variable: f"  {value}  " for variable, value in COLLECTION_DIGESTS.items()}
    environ = {EXECUTION_VARIABLE: f" {EXECUTION} ", **padded}
    factory = _Factory()
    assert main(environ=environ, collector_factory=factory) == 0
    assert factory.bindings == [{"receipt_ledger": None, "collection_authority": STAMPED_AUTHORITY}]


def test_entry_point_tells_the_operator_which_markets_lost_data(monkeypatch, capsys):
    """The failing exit status is not the whole signal; the markets are named."""
    _staging_environment(monkeypatch)
    states = {"za": "partial", "ng": "collected", "ke": "failed"}
    factory = _Factory(receipt=_receipt(market_states=states, execution_id=EXECUTION))
    assert main(collector_factory=factory) == 1
    assert "Collection lost data in ke, za" in capsys.readouterr().out


def test_the_module_names_its_public_surface():
    """``__all__`` is the boundary's declared surface, so it must match what it defines."""
    import scripts.staging.collect_42_sources as module

    assert module.__all__ == sorted(module.__all__)
    assert len(set(module.__all__)) == len(module.__all__)
    for name in module.__all__:
        assert hasattr(module, name), name
    for name in (
        "collect_staging",
        "collection_receipt",
        "main",
        "producer_collector",
        "reconciled_policy_digest",
    ):
        assert name in module.__all__
