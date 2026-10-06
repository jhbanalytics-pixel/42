import hashlib
import json
from copy import deepcopy

import pytest

from ops.deploy import resource_guard
from ops.deploy.resource_guard import assert_allowed, load_resource_manifest

PROJECT = "ogilvy-trends-v2"
PROJECT_NUMBER = "590353929363"
REGION = "us-central1"
ORIGIN_REGISTRY_BYTES = b"synthetic test origin registry, never native authority"
ORIGIN_REGISTRY_SHA256 = hashlib.sha256(ORIGIN_REGISTRY_BYTES).hexdigest()
RUN_JOB = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
    "jobs/intelligence-42-daily-staging"
)
SCHEDULER_JOB = (
    "//cloudscheduler.googleapis.com/projects/ogilvy-trends-v2/"
    "locations/us-central1/jobs/intelligence-42-daily-staging"
)
DATASET = "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/staging_42"
PROJECT_RESOURCE = "//cloudresourcemanager.googleapis.com/projects/ogilvy-trends-v2"
SECRET = (
    "//secretmanager.googleapis.com/projects/590353929363/secrets/ui-passcode-staging"
)
SECRET_VERSION = SECRET + "/versions/7"
BUILD_PARENT = (
    "//cloudbuild.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1"
)
CONNECTION = BUILD_PARENT + "/connections/staging-source"
CANONICAL_RESOURCES = (
    (
        (
            "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
            "services/intelligence-42-staging"
        ),
        "read",
    ),
    (
        (
            "//cloudtasks.googleapis.com/projects/ogilvy-trends-v2/locations/"
            "us-central1/queues/intelligence-42-staging"
        ),
        "read",
    ),
    (SCHEDULER_JOB, "read"),
    (
        (
            "//artifactregistry.googleapis.com/projects/ogilvy-trends-v2/locations/"
            "us-central1/repositories/intelligence-42-staging"
        ),
        "read",
    ),
    (CONNECTION + "/repositories/staging-repository", "read"),
    (DATASET + "/routines/read_staging", "read"),
    (None, "read"),
    (
        ("//iam.googleapis.com/projects/ogilvy-trends-v2/roles/stagingResourceGuard"),
        "read",
    ),
    ("//storage.googleapis.com/projects/_/buckets/ogilvy-42-staging", "read"),
)

REQUIRED_IDENTITIES = (
    "app",
    "build",
    "deploy",
    "freshness",
    "ingestion",
    "orchestration",
    "price_policy",
    "qa",
    "scheduler",
)


def _identity(purpose):
    account = f"intelligence-42-{purpose.replace('_', '-')}"
    return (
        f"//iam.googleapis.com/projects/{PROJECT}/serviceAccounts/"
        f"{account}@{PROJECT}.iam.gserviceaccount.com"
    )


CANONICAL_RESOURCES = tuple(
    (_identity("app"), action) if resource is None else (resource, action)
    for resource, action in CANONICAL_RESOURCES
)


def synthetic_manifest():
    resources = [
        {"name": BUILD_PARENT, "actions": ["write"]},
        {"name": CONNECTION, "actions": ["read"]},
        {"name": DATASET, "actions": ["read"]},
        {"name": PROJECT_RESOURCE, "actions": ["read", "write"]},
        {"name": RUN_JOB, "actions": ["invoke"]},
        {"name": SECRET, "actions": ["read", "write"]},
        {"name": SECRET_VERSION, "actions": ["read"]},
    ]
    return {
        "contract_version": "42_resource_manifest_v1",
        "project": PROJECT,
        "project_number": PROJECT_NUMBER,
        "region": REGION,
        "bigquery_location": "US",
        "resources": sorted(resources, key=lambda row: row["name"]),
        "identities": {purpose: _identity(purpose) for purpose in REQUIRED_IDENTITIES},
        "origin_registry_sha256": ORIGIN_REGISTRY_SHA256,
        "review_auth": {
            "state": "prepared",
            "issuer": "https://accounts.google.com",
            "audience": None,
            "subjects": [],
            "enrollment_evidence_sha256": None,
        },
    }


def write_manifest(path, manifest=None):
    raw = json.dumps(
        manifest or synthetic_manifest(), sort_keys=True, separators=(",", ":")
    ).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def test_exact_resource_action_is_allowed_but_other_action_is_denied():
    manifest = synthetic_manifest()
    assert_allowed(RUN_JOB, "invoke", manifest)
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(RUN_JOB, "read", manifest)


@pytest.mark.parametrize(
    "resource",
    [
        RUN_JOB.replace("staging", "production"),
        DATASET.replace("staging_42", "production_42"),
        DATASET.replace("staging_42", "staging_420"),
        SCHEDULER_JOB,
        SECRET + "/versions/8",
    ],
)
def test_unlisted_and_lookalike_resources_are_denied(resource):
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(resource, "read", synthetic_manifest())


def test_send_is_always_denied():
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(RUN_JOB, "send", synthetic_manifest())


def test_project_secret_and_build_parent_actions_are_independent():
    manifest = synthetic_manifest()
    assert_allowed(PROJECT_RESOURCE, "write", manifest)
    assert_allowed(SECRET, "write", manifest)
    assert_allowed(BUILD_PARENT, "write", manifest)
    assert_allowed(SECRET_VERSION, "read", manifest)
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(SECRET_VERSION, "write", manifest)
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(SECRET + "/versions/9", "read", manifest)
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(CONNECTION, "write", manifest)


@pytest.mark.parametrize(("resource", "action"), CANONICAL_RESOURCES)
def test_each_canonical_resource_family_is_admitted(resource, action):
    manifest = synthetic_manifest()
    manifest["resources"].append({"name": resource, "actions": [action]})
    manifest["resources"].sort(key=lambda row: row["name"])
    assert_allowed(resource, action, manifest)


def _with_resource(resource, action="read"):
    manifest = synthetic_manifest()
    manifest["resources"].append({"name": resource, "actions": [action]})
    manifest["resources"].sort(key=lambda row: row["name"])
    return manifest


@pytest.mark.parametrize(
    "identifier",
    ["Foo", "foo_bar", "foo.bar", "a" + "b" * 49],
)
def test_cloud_run_service_rejects_invalid_characters_and_length(identifier):
    resource = (
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
        f"services/{identifier}"
    )
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(resource, "read", _with_resource(resource))


def test_cloud_run_service_accepts_documented_restricted_length_boundary():
    identifier = "a" + "b" * 48
    resource = (
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/"
        f"services/{identifier}"
    )
    assert_allowed(resource, "read", _with_resource(resource))


@pytest.mark.parametrize(
    ("authority", "collection", "actual_identifier"),
    [
        ("cloudtasks.googleapis.com", "queues", "intelligence-42-staging"),
        ("cloudscheduler.googleapis.com", "jobs", "intelligence-42-staging"),
        (
            "artifactregistry.googleapis.com",
            "repositories",
            "intelligence-42-staging",
        ),
    ],
)
def test_regional_resource_families_use_restricted_lowercase_ids(
    authority, collection, actual_identifier
):
    prefix = (
        f"//{authority}/projects/ogilvy-trends-v2/locations/us-central1/{collection}/"
    )
    assert_allowed(
        prefix + actual_identifier, "read", _with_resource(prefix + actual_identifier)
    )
    assert_allowed(
        prefix + "a" + "b" * 62, "read", _with_resource(prefix + "a" + "b" * 62)
    )
    for identifier in ("Upper", "under_score", "with.dot", "a" + "b" * 63):
        resource = prefix + identifier
        with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
            assert_allowed(resource, "read", _with_resource(resource))


def test_artifact_repository_rejects_identifier_below_restricted_minimum():
    resource = (
        "//artifactregistry.googleapis.com/projects/ogilvy-trends-v2/locations/"
        "us-central1/repositories/abc"
    )
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(resource, "read", _with_resource(resource))


@pytest.mark.parametrize("collection", ["connections", "repositories"])
def test_cloud_build_child_ids_use_restricted_lowercase_ids(collection):
    if collection == "connections":
        prefix = BUILD_PARENT + "/connections/"
    else:
        prefix = BUILD_PARENT + "/connections/staging-source/repositories/"
    assert_allowed(
        prefix + "a" + "b" * 62, "read", _with_resource(prefix + "a" + "b" * 62)
    )
    for identifier in ("Upper", "under_score", "with.dot", "a" + "b" * 63):
        resource = prefix + identifier
        with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
            assert_allowed(resource, "read", _with_resource(resource))


def test_bigquery_routine_uses_its_own_character_and_length_rules():
    prefix = DATASET + "/routines/"
    assert_allowed(prefix + "A_name_1", "read", _with_resource(prefix + "A_name_1"))
    assert_allowed(prefix + "a" * 256, "read", _with_resource(prefix + "a" * 256))
    for identifier in ("with-hyphen", "with.dot", "a" * 257):
        resource = prefix + identifier
        with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
            assert_allowed(resource, "read", _with_resource(resource))


def test_secret_id_uses_its_own_character_and_length_rules():
    prefix = "//secretmanager.googleapis.com/projects/590353929363/secrets/"
    assert_allowed(
        prefix + "Upper_case-1", "read", _with_resource(prefix + "Upper_case-1")
    )
    assert_allowed(prefix + "a" * 255, "read", _with_resource(prefix + "a" * 255))
    for identifier in ("with.dot", "a" * 256):
        resource = prefix + identifier
        with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
            assert_allowed(resource, "read", _with_resource(resource))


def test_custom_role_id_uses_its_own_character_and_length_rules():
    prefix = "//iam.googleapis.com/projects/ogilvy-trends-v2/roles/"
    assert_allowed(
        prefix + "Role_name.1", "read", _with_resource(prefix + "Role_name.1")
    )
    assert_allowed(prefix + "a" * 64, "read", _with_resource(prefix + "a" * 64))
    for identifier in ("ab", "with-hyphen", "a" * 65):
        resource = prefix + identifier
        with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
            assert_allowed(resource, "read", _with_resource(resource))


def test_connection_membership_does_not_substitute_for_cloud_build_parent():
    manifest = synthetic_manifest()
    manifest["resources"] = [
        row for row in manifest["resources"] if row["name"] != BUILD_PARENT
    ]
    connection = next(row for row in manifest["resources"] if row["name"] == CONNECTION)
    connection["actions"] = ["write"]
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(BUILD_PARENT, "write", manifest)


def test_refusal_precedes_test_harness_mutation_callback():
    calls = []

    def guarded_mutation(resource, action, callback):
        assert_allowed(resource, action, synthetic_manifest())
        callback()

    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        guarded_mutation(SCHEDULER_JOB, "invoke", lambda: calls.append("mutated"))
    assert calls == []


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.update(contract_version="wrong"),
        lambda m: m.update(project="other-project"),
        lambda m: m.update(project_number=590353929363),
        lambda m: m.update(region="europe-west1"),
        lambda m: m.update(bigquery_location="us"),
        lambda m: m.pop("origin_registry_sha256"),
        lambda m: m.update(origin_registry_sha256="0" * 63),
        lambda m: m.update(extra=True),
        lambda m: m.update(resources=[]),
    ],
)
def test_closed_manifest_schema_rejects_invalid_root_fields(change):
    manifest = synthetic_manifest()
    change(manifest)
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)


@pytest.mark.parametrize("purpose", REQUIRED_IDENTITIES)
def test_every_required_identity_purpose_is_mandatory(purpose):
    manifest = synthetic_manifest()
    manifest["identities"].pop(purpose)
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)


def test_identity_alias_unknown_purpose_and_cross_project_email_are_rejected():
    manifest = synthetic_manifest()
    manifest["identities"]["qa"] = manifest["identities"]["app"]
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)

    manifest = synthetic_manifest()
    manifest["identities"]["qa"] = []
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)

    manifest = synthetic_manifest()
    manifest["identities"]["unknown"] = _identity("unknown")
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)

    manifest = synthetic_manifest()
    manifest["identities"]["qa"] = manifest["identities"]["qa"].replace(
        "@ogilvy-trends-v2.iam", "@other-project.iam"
    )
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)


def test_optional_identity_is_closed_and_must_remain_unique():
    manifest = synthetic_manifest()
    manifest["identities"]["execution_brain_read"] = _identity("brain")
    assert_allowed(RUN_JOB, "invoke", manifest)
    manifest["identities"]["execution_r3_apply"] = manifest["identities"]["app"]
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)


# The daily contract revision added collection, the daily compose and the legacy chain
# replay to the daily job's operations under the orchestration account.
DAILY_OPERATION_IDENTITIES = (
    "execution_collection_exposure_issue",
    "execution_source_snapshot_capture",
    "execution_r3_apply",
    "execution_r3_proof_issue",
    "execution_r3_release",
    "execution_source_collection",
    "execution_daily_composition_apply",
    "execution_legacy_chain_replay",
)


def test_daily_operation_identities_all_name_the_orchestration_account():
    manifest = synthetic_manifest()
    for key in DAILY_OPERATION_IDENTITIES:
        manifest["identities"][key] = manifest["identities"]["orchestration"]
    assert_allowed(RUN_JOB, "invoke", manifest)
    assert DAILY_OPERATION_IDENTITIES == resource_guard.DAILY_OPERATION_IDENTITIES


@pytest.mark.parametrize("key", DAILY_OPERATION_IDENTITIES)
def test_daily_operation_identity_at_any_other_account_is_refused(key):
    manifest = synthetic_manifest()
    manifest["identities"][key] = manifest["identities"]["app"]
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)
    manifest["identities"][key] = _identity("retired")
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)


def test_uniqueness_still_holds_over_every_key_outside_the_daily_allow_list():
    manifest = synthetic_manifest()
    for key in DAILY_OPERATION_IDENTITIES:
        manifest["identities"][key] = manifest["identities"]["orchestration"]
    manifest["identities"]["execution_brain_read"] = manifest["identities"]["qa"]
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)
    manifest["identities"]["execution_brain_read"] = manifest["identities"][
        "orchestration"
    ]
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)


@pytest.mark.parametrize(
    "resource",
    [
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/a b",
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/a%20b",
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/../job",
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/*",
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/job/",
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations//jobs/job",
        "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/jób",
        "//unknown.googleapis.com/projects/ogilvy-trends-v2/jobs/job",
        SECRET + "/versions/latest",
        SECRET + "/versions/0",
    ],
)
def test_malformed_resource_names_in_manifest_are_rejected(resource):
    manifest = synthetic_manifest()
    manifest["resources"][0] = {"name": resource, "actions": ["read"]}
    manifest["resources"].sort(key=lambda row: row["name"])
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)


def test_resource_and_action_order_duplicates_and_closed_rows_are_enforced():
    manifest = synthetic_manifest()
    manifest["resources"] = list(reversed(manifest["resources"]))
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)

    manifest = synthetic_manifest()
    manifest["resources"].append(deepcopy(manifest["resources"][0]))
    manifest["resources"].sort(key=lambda row: row["name"])
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)

    manifest = synthetic_manifest()
    run_row = next(row for row in manifest["resources"] if row["name"] == RUN_JOB)
    run_row["actions"] = ["invoke", "invoke"]
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)

    manifest = synthetic_manifest()
    run_row = next(row for row in manifest["resources"] if row["name"] == RUN_JOB)
    run_row["extra"] = True
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)


def test_secret_version_manifest_row_can_only_allow_read():
    manifest = synthetic_manifest()
    row = next(row for row in manifest["resources"] if row["name"] == SECRET_VERSION)
    row["actions"] = ["write"]
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(SECRET_VERSION, "write", manifest)


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(state="active"),
        lambda r: r.update(issuer="https://example.test"),
        lambda r: r.update(subjects=["synthetic@example.test"]),
        lambda r: r.update(enrollment_evidence_sha256="0" * 64),
        lambda r: r.update(extra=True),
        lambda r: r.update(audience=1),
    ],
)
def test_review_auth_remains_prepared_and_unenrolled(change):
    manifest = synthetic_manifest()
    change(manifest["review_auth"])
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(RUN_JOB, "invoke", manifest)


def test_loader_accepts_exact_bytes_and_digest(tmp_path):
    path = tmp_path / "synthetic-resource-manifest.json"
    digest = write_manifest(path)
    loaded = load_resource_manifest(path, expected_sha256=digest)
    assert loaded == synthetic_manifest()
    assert_allowed(RUN_JOB, "invoke", loaded)


def test_loader_rejects_changed_digest_before_parsing(tmp_path):
    path = tmp_path / "synthetic-resource-manifest.json"
    digest = write_manifest(path)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="^resource_manifest_digest_mismatch$"):
        load_resource_manifest(path, expected_sha256=digest)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"contract_version":"a","contract_version":"b"}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
    ],
)
def test_loader_rejects_duplicate_json_keys_and_nonfinite_values(tmp_path, raw):
    path = tmp_path / "synthetic-resource-manifest.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        load_resource_manifest(path, expected_sha256=digest)


def test_loader_rejects_nonfile_link_and_oversized_input(tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        load_resource_manifest(directory, expected_sha256="0" * 64)

    target = tmp_path / "target.json"
    digest = write_manifest(target)
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        load_resource_manifest(link, expected_sha256=digest)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        load_resource_manifest(
            oversized,
            expected_sha256=hashlib.sha256(oversized.read_bytes()).hexdigest(),
        )


def test_loader_rejects_invalid_expected_digest(tmp_path):
    path = tmp_path / "synthetic-resource-manifest.json"
    write_manifest(path)
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        load_resource_manifest(path, expected_sha256="A" * 64)


@pytest.mark.parametrize("path", [None, b"not-a-path"])
def test_loader_maps_nonpath_inputs_to_manifest_invalid(path):
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        load_resource_manifest(path, expected_sha256="0" * 64)


BUILDS = BUILD_PARENT + "/builds"


def test_the_builds_collection_is_its_own_read_only_kind():
    # Amendment e: release verify reads a build record under the builds
    # collection, so the collection is a kind of its own, read only, and a read
    # there is never a read of the parent or the other way round.
    manifest = _with_resource(BUILDS)
    assert_allowed(BUILDS, "read", manifest)
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(BUILD_PARENT, "read", manifest)
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(BUILDS, "write", manifest)
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(BUILDS, "read", synthetic_manifest())


@pytest.mark.parametrize(
    "actions", (["write"], ["read", "write"], ["deploy", "read"], ["invoke"])
)
def test_the_builds_collection_admits_read_alone(actions):
    manifest = synthetic_manifest()
    manifest["resources"].append({"name": BUILDS, "actions": actions})
    manifest["resources"].sort(key=lambda row: row["name"])
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(BUILDS, actions[0], manifest)


@pytest.mark.parametrize(
    "resource",
    (
        BUILDS + "/abc123",
        BUILDS + "2",
        BUILD_PARENT + "/Builds",
        "//cloudbuild.googleapis.com/projects/other/locations/us-central1/builds",
        (
            "//cloudbuild.googleapis.com/projects/ogilvy-trends-v2/locations/"
            "europe-west1/builds"
        ),
    ),
)
def test_single_builds_and_lookalike_collections_stay_unclassified(resource):
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(resource, "read", _with_resource(resource))


LEDGER_DATASET = "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/trends_v2_staging_approvals"
LEDGER_TABLES = tuple(
    LEDGER_DATASET + "/tables/open_intelligence_execution_" + name + "_v2"
    for name in ("approvals", "consumptions", "results")
)


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_each_v2_ledger_table_is_a_read_only_table_row(table):
    # Amendment e: the serving identity reads the three v2 ledger tables, so each
    # is a manifest row of its own, read only, and a read there is never a read
    # of the dataset or the other way round.
    manifest = _with_resource(table)
    assert_allowed(table, "read", manifest)
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(LEDGER_DATASET, "read", manifest)
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(table, "write", manifest)
    with pytest.raises(ValueError, match="^resource_action_forbidden$"):
        assert_allowed(table, "read", synthetic_manifest())


@pytest.mark.parametrize(
    "actions", (["write"], ["read", "write"], ["deploy", "read"], ["invoke"])
)
def test_a_ledger_table_row_admits_read_alone(actions):
    manifest = synthetic_manifest()
    manifest["resources"].append({"name": LEDGER_TABLES[0], "actions": actions})
    manifest["resources"].sort(key=lambda row: row["name"])
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(LEDGER_TABLES[0], actions[0], manifest)


@pytest.mark.parametrize(
    "resource",
    (
        LEDGER_DATASET + "/tables/open_intelligence_execution_approvals_v1",
        LEDGER_DATASET + "/tables/open_intelligence_execution_results_v3",
        LEDGER_DATASET + "/tables/open_intelligence_recurring_grants_v2",
        LEDGER_DATASET + "/tables/Open_intelligence_execution_approvals_v2",
        LEDGER_TABLES[0] + "_copy",
        LEDGER_TABLES[0] + "/rows",
        (
            "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/"
            "trends_v2_staging/tables/open_intelligence_execution_approvals_v2"
        ),
        (
            "//bigquery.googleapis.com/projects/590353929363/datasets/"
            "trends_v2_staging_approvals/tables/open_intelligence_execution_approvals_v2"
        ),
        (
            "//bigquery.googleapis.com/projects/other/datasets/"
            "trends_v2_staging_approvals/tables/open_intelligence_execution_approvals_v2"
        ),
    ),
)
def test_other_tables_stay_unclassified(resource):
    with pytest.raises(ValueError, match="^resource_manifest_invalid$"):
        assert_allowed(resource, "read", _with_resource(resource))
