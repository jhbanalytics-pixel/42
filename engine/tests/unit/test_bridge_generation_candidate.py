"""The bridge generation is the active pair, derived from the amendment e generation."""

import json
import shutil
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from src.analysis.open_intelligence import execution_generations
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.execution_generations import (
    ACTIVE_GENERATION_PAIR,
    _parse_resource_manifest,
    active_generation,
    load_trusted_generation,
    require_active_generation,
)
from src.analysis.open_intelligence.execution_origins import OriginRefusal, load_origin_registry

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/open_intelligence"
POLICY = CONFIG / "candidate_contracts/source-bridge-capture-v3.json"
REGISTRY = CONFIG / "execution_origins_bridge_v3.json"
RESOURCES = CONFIG / "resource_manifest_bridge_v3.json"
OLD_POLICY = CONFIG / "origin_contracts/successor-source-snapshot-capture.json"
V3_ROUTINE = "sp_consume_open_intelligence_source_snapshot_v3"
V3_RESOURCE = (
    "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/trends_v2_staging_approvals"
    f"/routines/{V3_ROUTINE}"
)
# The v3 plan reads collection evidence and history and writes snapshots in the product dataset.
BRIDGE_DATASETS = ["trends_v2_staging"]
# The route A capture stores its inputs and captures in this bucket
# (production_snapshot_storage.BUCKET_NAME); amendment g grants on it, so the bridge
# manifest names it, read and write, as ops/deploy/iam_delta.py pins it for g.
ARTIFACT_BUCKET_ROW = {
    "actions": ["read", "write"],
    "name": "//storage.googleapis.com/projects/_/buckets/ogilvy-trends-v2-oi-source-artifacts-staging",
}
V2_RETAINED_SHA256 = "4432d55a8f53ad160139ed84244581c3e9d39dc06585092fe6d81670778fbc4d"
# Amendment e is the generation the bridge generation is derived from. It moved the
# amendment d manifest bytes to a retained copy, and its own manifest already carries the
# v3 routine row. It stays trusted for reading records written under it, but it is no
# longer the active pair.
AMENDMENT_E_PAIR = (
    "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179",
    "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09",
)
AMENDMENT_D_RESOURCES = CONFIG / "resource_manifest_amendment_d.json"
AMENDMENT_E_RESOURCES = CONFIG / "resource_manifest_v1.json"
AMENDMENT_E_DELTA_ROWS_SHA256 = "ab5f698aef59f2d0bb4830438c6c5bec954eed82fd0ca22d5c80e61817038942"
# Amendment f's rows, each amendment with its approval block taken out.
AMENDMENT_F_ROWS_SHA256 = "d1e0babe8dfd6bc900d9a27259307ffb143c8a50a0c2aac4b7c7eafb80beb182"
# Amendment f moves one deploy file beside the delta: ops/deploy/iam_delta.py, whose
# validator admits the amendments list and amendment f's four pinned rows. The listing
# was 1d0006718d1a5374c6875cf750a43ed536546f3c227f2ccc2a5de14b2487921a under amendment e.
AMENDMENT_F_DEPLOY_LISTING_SHA256 = (
    "d0d5180f74a5a34faf2573e63043628d21eb91c2a083147662eb967b8c1c5463"
)
# The bridge reads switch moved ops/deploy/release.py after amendment f (b06ad4b, turn
# bridge reads on for every staging question revision); the listing became this.
BRIDGE_READS_DEPLOY_LISTING_SHA256 = (
    "5f4d2652341abe7c271ed0c2769cb2473a1e2817fe915b66ed73f50a0338703c"
)
# The question policy expiry then moves one more deploy file,
# ops/deploy/refresh_question_policy.py, which gains extend-expiry and keeps a renewal on
# the active policy's own expiry. The listing was BRIDGE_READS_DEPLOY_LISTING_SHA256
# before it.
POLICY_EXPIRY_DEPLOY_LISTING_SHA256 = (
    "b21e02bb93415c2954fd7494985a40c95717716efec473565ef6577261a360db"
)
# The unattended renewal then moves four deploy files and adds one: refresh_question_policy.py
# stages the index and grant and reads them at their current generation,
# release_native_adapter.py returns an object's creation time, runtime_native_adapter.py and
# runtime_schedulers.py gain the price policy scheduler resume, and runtime_scheduler_resume.py
# is its command. The listing was POLICY_EXPIRY_DEPLOY_LISTING_SHA256 over 18 files before it.
UNATTENDED_RENEWAL_DEPLOY_LISTING_SHA256 = (
    "31e073cb19bde8dab1e24c3bafb2b906ec6ccb9996b61526b941f5ab2314820e"
)
# The replace path for an existing managed job then moves ops/deploy/runtime_jobs.py. The
# listing was UNATTENDED_RENEWAL_DEPLOY_LISTING_SHA256 before it.
JOB_REPLACE_DEPLOY_LISTING_SHA256 = (
    "8e59bd11848411badbf3760bf20abd2ed0f9f3d6eba36f0934cbb45b31903f5d"
)
# Staging an unattended grant ahead of its granted_at then moves
# ops/deploy/refresh_question_policy.py. The listing was JOB_REPLACE_DEPLOY_LISTING_SHA256
# before it.
GRANT_AHEAD_DEPLOY_LISTING_SHA256 = (
    "c5a7c082b8da69d4c5c7d501bdcf0e12018f69108e16ca23ac330681c0fee85c"
)
# The ingest job then adds ops/deploy/ingest_runtime.py and changes
# ops/deploy/runtime_jobs.py, so the listing carries 20 files. The listing was
# GRANT_AHEAD_DEPLOY_LISTING_SHA256 before it.
INGEST_DEPLOY_LISTING_SHA256 = "137c7ea895585fc429f0db685efce88b052422e0e38830005aafbc8ada6843b6"
# Amendment g then moves ops/deploy/iam_delta.py once more: the validator pins g's six
# rows and the artifact bucket row g alone may name. The listing was
# POLICY_EXPIRY_DEPLOY_LISTING_SHA256 before it. The daily contract revision then added
# its three operations' identities to ops/deploy/resource_guard.py (the listing was
# b1e44212 before it). G's seventh row, the daily account's Vertex role, then moved
# ops/deploy/iam_delta.py and iam_delta_v1.json (the listing was 80ceed91 before it).
AMENDMENT_G_DEPLOY_LISTING_SHA256 = (
    "004484cf526edf69627c8fd58b6d0fb0f85299bed2d6b89dad45aba17fdb0ce4"
)
# Amendments f and g's rows, each with its approval block taken out. Amendment f's rows
# alone still hash to AMENDMENT_F_ROWS_SHA256. G's seventh row, the daily account's Vertex
# role, moved it from ab73b762.
AMENDMENT_G_ROWS_SHA256 = "521060c582b7c6906f4266ee7a2bf7a39ed275d8527e4486f54d44f154055f45"
# On the core integration amendment g lands over the ingest job, so the listing carries
# the ingest job's 20 files with g's ops/deploy/iam_delta.py and ops/deploy/resource_guard.py.
# The listing was INGEST_DEPLOY_LISTING_SHA256 before it.
AMENDMENT_G_OVER_INGEST_DEPLOY_LISTING_SHA256 = (
    "70f17dfa6e2d7c71a565ff7382466e048c52b2c63d04472cafef196db0d96827"
)
# The scheduler readback reconciliation then moves ops/deploy/runtime_schedulers.py, whose
# readback and resume take Cloud Scheduler's documented retry defaults and its User-Agent
# header as the service's own. The listing still carries 20 files. The listing was
# AMENDMENT_G_OVER_INGEST_DEPLOY_LISTING_SHA256 before it.
SCHEDULER_READBACK_DEPLOY_LISTING_SHA256 = (
    "27b30711b4c7640e3e680a0219bd9c008f5fe94fd8ef13495cbfe3b520a1e0d1"
)
# Guarded renewal binds its dry plan to the current ledger and active prices.
PRICING_STATE_GUARD_DEPLOY_LISTING_SHA256 = (
    "5746e4ffc28f4232fa272a837c5e97a627a08b156d2db930db7dfec080469e12"
)
# Pricing renewal validates complete markup after the closed document.
PRICING_PAGE_COMPLETENESS_DEPLOY_LISTING_SHA256 = (
    "e5778655ca18db96938b0e41751a24eb1ec9301086d3abe4a4bb3dfbd5c2db14"
)
# Complete pricing trailers require nonempty script and footer payloads.
PRICING_PAGE_PAYLOAD_DEPLOY_LISTING_SHA256 = (
    "f69ae6b2132d6edc28a3cf1a02e7fd3249d32001b778438c3a4b4aec6185ab08"
)

PRICING_CORE_DEPLOY_LISTING_SHA256 = (
    "82ad55ec6266fb4ad938361f391c0ef6c72998a862f1ff49a42264f687353d90"
)

# Finish activation adds ops/deploy/finish_pricing_activation.py and
# ops/deploy/pricing_project_viewer.py; existing deploy files retain their bytes.
FINISH_DEPLOY_LISTING_SHA256 = (
    "5526f22cfffee51ff386a1020dd57b3dbcd3c053d9599e1cc8bf980f2c4cc8b5"
)

# The active pair. The registry is unchanged since the candidate policy move; the manifest
# is the amendment e manifest under the bridge registry digest, so it carries the five rows
# amendment e added as well as the v3 routine row. Amendment g adds one row to it, the
# route A capture's artifact bucket, so the manifest moved from 1643e4ce to ab7802f4
# before any record was written under it; the registry does not move.
# The daily contract revision then registered three daily operations
# and bounded exposure, so the pair moved again: registry 573deeea to e6b95e35, manifest
# ab7802f4 to b7553041.
# Tightening the date regex to ASCII digits then moved them to f23001ed and 592ed45f.
BRIDGE_PAIR = (
    "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2",
    "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe",
)


def read(path):
    raw = path.read_bytes()
    value = json.loads(raw)
    assert raw == canonical_bytes(value)
    return value, sha256(raw).hexdigest()


DAILY_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
ORCHESTRATION = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
# ASCII digits only: Python's \d also admits other scripts' decimal digits.
DATE = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
REVISED_OPERATIONS = ("daily_composition_apply", "legacy_chain_replay", "source_collection")


def _range(maximum):
    return {"maximum": maximum, "minimum": 0}


def _daily_contract_revision(policy):
    """The daily contract revision over the bridge policy: exposure bounded, and three
    operations on the daily job under the orchestration account. r3_apply is untouched;
    the daily compose and the legacy chain replay take its template with their own
    arguments and bounds."""
    rules = policy["operation_validation"]
    exposure = rules["collection_exposure_issue"]["limits"]
    exposure["max_bytes_billed"] = _range(5_000_000_000)
    exposure["max_rows_written"] = _range(1_000_000)
    template = rules["r3_apply"]
    rules["daily_composition_apply"] = {
        **deepcopy(template),
        "arguments": {
            "kind": "prefix_pattern_v1",
            "pattern": [{"regex": DATE}],
            "prefix": ["scripts/staging/compose_daily_open_intelligence.py", "--cutoff-date"],
        },
        "limits": {
            "max_bytes_billed": _range(10_000_000_000),
            "max_credits": {"exact": 0},
            "max_model_calls": _range(100),
            "max_rows_written": _range(1_000_000),
        },
    }
    rules["legacy_chain_replay"] = {
        **deepcopy(template),
        "arguments": {
            "kind": "prefix_pattern_v1",
            "pattern": [
                {"regex": DATE},
                {"literal": "--run-id"},
                {"regex": "^[a-z0-9_][a-z0-9_-]{0,127}$"},
                {"literal": "--source-sha"},
                {"regex": "^[0-9a-f]{40}$"},
            ],
            "prefix": [
                "scripts/staging/replay_open_intelligence.py",
                "--apply-run",
                "--trend-date",
            ],
        },
        "limits": {
            "max_bytes_billed": _range(10_000_000_000),
            "max_credits": {"exact": 0},
            "max_model_calls": {"exact": 0},
            "max_rows_written": _range(1_000_000),
        },
    }
    rules["source_collection"] = {
        "arguments": {"kind": "exact", "value": ["scripts/staging/collect_42_sources.py"]},
        "command": ["python"],
        "datasets": ["intelligence_42_sources_staging"],
        "environment": [
            {"name": "BIGQUERY_DATASET", "value": "intelligence_42_sources_staging"},
            {"name": "GCP_PROJECT", "value": "ogilvy-trends-v2"},
            {"name": "TRENDS_ENV", "value": "staging"},
        ],
        "input_artifact_digest_regex": "^[0-9a-f]{64}$",
        "input_artifact_names": ["build_provenance", "cost_policy", "daily_profile"],
        "job_resource": DAILY_JOB,
        "limits": {
            "max_bytes_billed": _range(1_000_000_000),
            "max_credits": _range(620),
            "max_model_calls": {"exact": 0},
            "max_rows_written": {"exact": 0},
        },
        "secrets": [],
        "service_identity": ORCHESTRATION,
    }
    policy["successor"]["operations"] = sorted(
        [*policy["successor"]["operations"], *REVISED_OPERATIONS]
    )


def test_candidate_policy_changes_only_reviewed_capture_rule():
    old, _ = read(OLD_POLICY)
    new, _ = read(POLICY)
    expected = deepcopy(old)
    rule = expected["operation_validation"]["source_snapshot_capture"]
    rule["input_artifact_names"] = [
        "bridge_policy",
        "capture_contract",
        "capture_plan",
        "collection_receipt_set",
        "history_completion_set",
        "recovery_context",
        "source_metadata",
        "storage_policy",
        "temporal_rules",
    ]
    rule["arguments"]["plan_contract_version"] = "open_intelligence_protected_capture_plan_v3"
    rule["arguments"]["consume_routine"] = V3_ROUTINE
    rule["datasets"] = BRIDGE_DATASETS
    rule["arguments"]["consume_routine_sha256"] = sha256(
        (ROOT / f"infra/bigquery_routines/{V3_ROUTINE}.sql").read_bytes()
    ).hexdigest()
    _daily_contract_revision(expected)
    assert new == expected
    # The retained policy still names the deployed v2 routine by its retained bytes.
    retained = old["operation_validation"]["source_snapshot_capture"]["arguments"]
    assert retained["consume_routine"] == "sp_consume_open_intelligence_source_snapshot_v2"
    assert retained["consume_routine_sha256"] == V2_RETAINED_SHA256
    assert (
        sha256(
            (
                ROOT / "infra/bigquery_routines/sp_consume_open_intelligence_source_snapshot_v2.sql"
            ).read_bytes()
        ).hexdigest()
        == V2_RETAINED_SHA256
    )


def test_candidate_policy_lives_outside_the_registered_policy_directory():
    """The bridge registry names the policy at its candidate_contracts path, so the policy
    stays there and the registry digest does not move; origin_contracts keeps exactly the
    amendment e generation's policies."""
    assert POLICY.is_file()
    assert not (CONFIG / "origin_contracts" / POLICY.name).exists()


def test_candidate_registry_preserves_other_rows_and_links_new_policy():
    old, _ = read(CONFIG / "execution_origins_v1.json")
    new, digest = read(REGISTRY)
    _, policy_digest = read(POLICY)
    expected = deepcopy(old)
    matches = [
        row
        for row in expected["rows"]
        if row["manifest_version"] == "open_intelligence_execution_manifest_v2"
        and "source_snapshot_capture" in row["exact_operation_bindings"]
    ]
    assert len(matches) == 1
    matches[0]["contract_file"] = (
        "configs/open_intelligence/candidate_contracts/source-bridge-capture-v3.json"
    )
    matches[0]["contract_sha256"] = policy_digest
    matches[0]["contract_digests"] = [policy_digest]
    matches[0]["exact_operation_bindings"]["source_snapshot_capture"]["datasets"] = BRIDGE_DATASETS
    matches[0]["datasets"] = BRIDGE_DATASETS
    # The daily contract revision binds its three operations on the daily row.
    bindings = matches[0]["exact_operation_bindings"]
    for name in ("daily_composition_apply", "legacy_chain_replay"):
        bindings[name] = {
            "datasets": ["trends_v2_staging"],
            "job_resource": DAILY_JOB,
            "service_identity": ORCHESTRATION,
        }
    bindings["source_collection"] = {
        "datasets": ["intelligence_42_sources_staging"],
        "job_resource": DAILY_JOB,
        "service_identity": ORCHESTRATION,
    }
    matches[0]["datasets"] = sorted({*BRIDGE_DATASETS, "intelligence_42_sources_staging"})
    assert new == expected
    loaded = load_origin_registry(REGISTRY, expected_sha256=digest, contract_root=ROOT)
    assert loaded.sha256 == digest


def test_bridge_resources_are_the_amendment_e_manifest_under_the_bridge_registry():
    old, old_digest = read(AMENDMENT_E_RESOURCES)
    new, _ = read(RESOURCES)
    _, registry_digest = read(REGISTRY)
    assert old_digest == AMENDMENT_E_PAIR[1]
    added = {"actions": ["deploy", "invoke", "read"], "name": V3_RESOURCE}
    assert added in old["resources"]
    # Amendment g adds exactly one row over the amendment e manifest: the route A
    # capture's artifact bucket, which the amendment e manifest does not carry.
    assert ARTIFACT_BUCKET_ROW not in old["resources"]
    # The daily contract revision names its three operations' identity, the
    # orchestration account, as the execution approval path reads it.
    identities = dict(old["identities"])
    for name in REVISED_OPERATIONS:
        identities[f"execution_{name}"] = identities["orchestration"]
    expected = {
        **old,
        "identities": identities,
        "origin_registry_sha256": registry_digest,
        "resources": sorted([*old["resources"], ARTIFACT_BUCKET_ROW], key=lambda row: row["name"]),
    }
    assert new == expected
    assert RESOURCES.read_bytes() == canonical_bytes(expected)
    # Every amendment d row is kept, and the rows amendment e added (the v3 routine row
    # among them) are all present.
    amendment_d, _ = read(AMENDMENT_D_RESOURCES)
    assert all(row in new["resources"] for row in amendment_d["resources"])
    assert added not in amendment_d["resources"]
    assert len(new["resources"]) == len(amendment_d["resources"]) + 7
    parsed = _parse_resource_manifest(
        RESOURCES.read_bytes(), origin_registry_sha256=registry_digest
    )
    assert parsed["origin_registry_sha256"] == registry_digest


def test_bridge_generation_is_the_active_pair():
    _, registry_digest = read(REGISTRY)
    _, resource_digest = read(RESOURCES)
    assert (registry_digest, resource_digest) == BRIDGE_PAIR
    assert ACTIVE_GENERATION_PAIR == BRIDGE_PAIR
    active = active_generation()
    assert active.registry_path == REGISTRY
    assert active.resource_manifest_path == RESOURCES
    assert require_active_generation(*BRIDGE_PAIR) == active
    # The amendment e files keep their bytes and stay trusted for reading only.
    assert read(CONFIG / "execution_origins_v1.json")[1] == AMENDMENT_E_PAIR[0]
    assert read(AMENDMENT_E_RESOURCES)[1] == AMENDMENT_E_PAIR[1]
    retained = load_trusted_generation(*AMENDMENT_E_PAIR)
    assert retained.resource_manifest_path == AMENDMENT_E_RESOURCES
    with pytest.raises(OriginRefusal, match="execution_generation_inactive"):
        require_active_generation(*AMENDMENT_E_PAIR)


@pytest.mark.parametrize(
    "name", ["execution_origins_bridge_v3.json", "resource_manifest_bridge_v3.json"]
)
def test_reviewed_file_changes_are_rejected_by_independent_pins(tmp_path, monkeypatch, name):
    copied = tmp_path / "configs/open_intelligence"
    shutil.copytree(CONFIG, copied)
    path = copied / name
    path.write_bytes(path.read_bytes() + b" ")
    monkeypatch.setattr(execution_generations, "_PACKAGE_ROOT", tmp_path)
    with pytest.raises(OriginRefusal, match="digest_mismatch"):
        load_trusted_generation(
            BRIDGE_PAIR[0],
            BRIDGE_PAIR[1],
        )


def test_candidate_policy_directory_is_one_of_two_fixed_directories(tmp_path):
    copied = tmp_path / "configs/open_intelligence"
    shutil.copytree(CONFIG, copied)
    other = copied / "other_contracts"
    other.mkdir()
    shutil.copy(POLICY, other / POLICY.name)
    registry = json.loads(REGISTRY.read_bytes())
    for row in registry["rows"]:
        if row["contract_file"] == f"configs/open_intelligence/candidate_contracts/{POLICY.name}":
            row["contract_file"] = f"configs/open_intelligence/other_contracts/{POLICY.name}"
    raw = canonical_bytes(registry)
    path = copied / REGISTRY.name
    path.write_bytes(raw)
    with pytest.raises(OriginRefusal, match="execution_origin_registry_invalid"):
        load_origin_registry(path, expected_sha256=sha256(raw).hexdigest(), contract_root=tmp_path)


def test_candidate_leaves_the_amendment_e_engine_generation_byte_identical():
    """Activating the bridge generation moves only its own files; the amendment e registry
    and manifest and the origin_contracts policies keep their reviewed bytes."""
    assert ACTIVE_GENERATION_PAIR == BRIDGE_PAIR
    pinned = {
        "configs/open_intelligence/execution_origins_v1.json": AMENDMENT_E_PAIR[0],
        "configs/open_intelligence/resource_manifest_v1.json": AMENDMENT_E_PAIR[1],
    }
    for name, expected in pinned.items():
        assert sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    # No amendment e or amendment d policy or registry names the v3 routine; only the
    # bridge policy does. The amendment e manifest names it in the one row it adds for it.
    retained = [
        CONFIG / "execution_origins_v1.json",
        AMENDMENT_D_RESOURCES,
        *(CONFIG / "origin_contracts").glob("*.json"),
    ]
    assert len(retained) == 6
    for path in retained:
        assert V3_ROUTINE.encode() not in path.read_bytes(), path
    active, _ = read(CONFIG / "resource_manifest_v1.json")
    assert [row for row in active["resources"] if V3_ROUTINE in row["name"]] == [
        {"actions": ["deploy", "invoke", "read"], "name": V3_RESOURCE}
    ]


def _deploy_tree(repo):
    """The approved deploy tree of the checkout at ``repo``. Only an engine only tree, one
    with no ops tree at all, skips; a checkout with ops but without its deploy tree fails."""
    if not (repo / "ops").is_dir():
        pytest.skip("monorepo sibling tree is not present")
    deploy = repo / "ops/deploy"
    assert deploy.is_dir(), "ops/deploy is missing from a checkout that carries ops"
    return deploy


def test_candidate_leaves_the_approved_deploy_files_byte_identical():
    """Every approved deploy file keeps its reviewed bytes. The deploy tree is a monorepo
    sibling that an engine only tree does not carry."""
    repo = ROOT.parent
    deploy = _deploy_tree(repo)
    # Pinned to the amendment e manifest by literal, never to the active pair: activating
    # the bridge generation leaves the approved deploy manifest as it was.
    assert (
        sha256((repo / "ops/deploy/resource_manifest.json").read_bytes()).hexdigest()
        == AMENDMENT_E_PAIR[1]
    )
    # The amendment e delta is pinned by its rows, not its approval block, so recording
    # Albert's approval changes nothing here. Amendment f stands beside it in its own
    # amendments list, pinned by its rows the same way, so stamping f moves nothing.
    delta = json.loads((repo / "ops/deploy/iam_delta_v1.json").read_bytes())
    delta.pop("approval")
    amendments = [
        {key: value for key, value in entry.items() if key != "approval"}
        for entry in delta.pop("amendments")
    ]
    assert sha256(canonical_bytes(amendments)).hexdigest() == AMENDMENT_G_ROWS_SHA256
    assert [entry["amendment"] for entry in amendments] == ["f", "g"]
    assert sha256(canonical_bytes(amendments[:1])).hexdigest() == AMENDMENT_F_ROWS_SHA256
    assert delta["resource_manifest_sha256"] == AMENDMENT_E_PAIR[1]
    assert sha256(canonical_bytes(delta)).hexdigest() == AMENDMENT_E_DELTA_ROWS_SHA256
    files = sorted(
        path
        for path in deploy.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.name != "iam_delta_v1.json"
    )
    listing = "".join(
        f"{path.relative_to(repo).as_posix()} {sha256(path.read_bytes()).hexdigest()}\n"
        for path in files
    )
    # Computed from a clean tree of the amendment f branch, whose ops/deploy carries
    # amendment e and the validator for amendment f; the delta is held above by its rows.
    assert (len(files), sha256(listing.encode()).hexdigest()) == (
        22,
        FINISH_DEPLOY_LISTING_SHA256,
    )
    # Only the amendment e manifest row and the delta rows granting on it name the v3
    # routine in the approved deploy tree.
    assert [
        path.relative_to(repo).as_posix()
        for path in files
        if V3_ROUTINE.encode() in path.read_bytes()
    ] == ["ops/deploy/resource_manifest.json"]
    assert V3_ROUTINE in json.dumps(delta)


def test_only_an_engine_only_tree_skips_the_deploy_pin(tmp_path):
    # No ops tree at all: this is an engine only tree, so the deploy pin skips.
    with pytest.raises(pytest.skip.Exception):
        _deploy_tree(tmp_path)
    # An ops tree without its deploy tree is a broken checkout and fails, never skips.
    (tmp_path / "ops").mkdir()
    with pytest.raises(AssertionError, match="ops/deploy"):
        _deploy_tree(tmp_path)
    (tmp_path / "ops/deploy").mkdir()
    assert _deploy_tree(tmp_path) == tmp_path / "ops/deploy"
