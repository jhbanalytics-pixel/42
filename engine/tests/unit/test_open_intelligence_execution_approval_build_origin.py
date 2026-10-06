"""Build origin admission for the real engine publish build.

The engine image is built from a connected repository revision with the build's dir set
to engine (engine/cloudbuild.publish.yaml), so its describe response carries ``dir`` next to
``repository`` and ``revision`` under ``source.connectedRepository``. The app build has
no ``dir``. Both ``source`` blocks below are copied verbatim from real describe output.

The expected directory is pinned in code per image repository and never read from the
payload: the engine image repository expects ``engine``; every other row expects none.
"""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from src.analysis.open_intelligence import execution_approval, execution_origins

ENGINE_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ENGINE_ROOT / "configs/open_intelligence/execution_origins_v1.json"
REGISTRY_SHA256 = "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179"
MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
NEW_CONSUME = "new_consume"
HISTORICAL_READ = "historical_read"
REFUSAL = "execution_approval_build_provenance_invalid"
CONNECTED_REPOSITORY = (
    "projects/ogilvy-trends-v2/locations/us-central1/connections/tev2-gh/"
    "repositories/jhbanalytics-pixel-42-ogilvy-intelligence"
)
ENGINE_IMAGE_REPOSITORY = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine"
V1_IMAGE_REPOSITORY = "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine"
V1_CONNECTED_REPOSITORY = (
    "projects/ogilvy-trends-v2/locations/us-central1/connections/tev2-gh/"
    "repositories/jhbanalytics-pixel-trends-engine-v2"
)
# The daily contract: since amendment d it carries the r3 flow beside the capture.
DAILY_CONTRACT = "66e1a11f30c6dd8a7ca9db2110ef0af91d9a3aeda6b48d0b6323797839a06546"
BUILD_ID = "889fd1ff-0f4e-4ea1-8c11-9fb1f5992308"
BUILD_RESOURCE = f"projects/590353929363/locations/us-central1/builds/{BUILD_ID}"
CANONICAL_BUILD_RESOURCE = f"projects/ogilvy-trends-v2/locations/us-central1/builds/{BUILD_ID}"
IMAGE_DIGEST = "sha256:" + "b" * 64

# Real engine publish build describe output, source block verbatim.
ENGINE_SOURCE = {
    "connectedRepository": {
        "dir": "engine",
        "repository": CONNECTED_REPOSITORY,
        "revision": "f4b2e04b1ee0d179535a02a02b24edb7a9afa495",
    }
}
# Real app build describe output, source block verbatim: no dir.
APP_SOURCE = {
    "connectedRepository": {
        "repository": CONNECTED_REPOSITORY,
        "revision": "decd5e327b3f26a6bcdd47311be4e566c76e4922",
    }
}

REGISTRY = execution_origins.load_origin_registry(
    REGISTRY_PATH, expected_sha256=REGISTRY_SHA256, contract_root=ENGINE_ROOT
)
V2_CONTRACTS = sorted(contract for version, contract in REGISTRY if version == MANIFEST_V2)
V1_CONTRACTS = sorted(contract for version, contract in REGISTRY if version == MANIFEST_V1)


def engine_origin(contract=DAILY_CONTRACT):
    return execution_origins.select_origin(
        manifest_version=MANIFEST_V2, contract_sha256=contract, mode=NEW_CONSUME, registry=REGISTRY
    )


def build_response(source, *, image_repository=ENGINE_IMAGE_REPOSITORY):
    revision = source["connectedRepository"]["revision"]
    return {
        "name": BUILD_RESOURCE,
        "id": BUILD_ID,
        "projectId": "ogilvy-trends-v2",
        "status": "SUCCESS",
        "results": {"images": [{"name": f"{image_repository}:{revision}", "digest": IMAGE_DIGEST}]},
        "finishTime": "2026-09-19T08:00:00.000000Z",
        "source": deepcopy(source),
        "sourceProvenance": {},
    }


def refusal():
    return pytest.raises(execution_approval.ApprovalRefusal, match=f"^{REFUSAL}$")


def test_engine_source_fixture_is_the_real_describe_shape():
    assert sorted(ENGINE_SOURCE["connectedRepository"]) == ["dir", "repository", "revision"]
    assert sorted(APP_SOURCE["connectedRepository"]) == ["repository", "revision"]
    assert ENGINE_SOURCE["connectedRepository"]["dir"] == "engine"


def test_real_engine_build_source_is_admitted_on_the_engine_origin():
    receipt = execution_approval.build_provenance_from_response(
        build_response(ENGINE_SOURCE),
        DAILY_CONTRACT,
        manifest_version=MANIFEST_V2,
        mode=NEW_CONSUME,
        registry=REGISTRY,
    )
    assert receipt.resolved_source_sha == ENGINE_SOURCE["connectedRepository"]["revision"]
    assert receipt.image_uri == f"{ENGINE_IMAGE_REPOSITORY}@{IMAGE_DIGEST}"
    assert receipt.build_resource == CANONICAL_BUILD_RESOURCE
    assert receipt.status == "SUCCESS"


@pytest.mark.parametrize("contract", V2_CONTRACTS)
def test_every_v2_engine_row_admits_the_real_engine_source(contract):
    receipt = execution_approval.build_provenance_from_response(
        build_response(ENGINE_SOURCE),
        contract,
        manifest_version=MANIFEST_V2,
        mode=NEW_CONSUME,
        registry=REGISTRY,
    )
    assert receipt.resolved_source_sha == ENGINE_SOURCE["connectedRepository"]["revision"]


def test_normalizer_keeps_the_admitted_dir_verbatim():
    canonical = execution_approval.normalize_cloud_build_response(
        build_response(ENGINE_SOURCE), origin=engine_origin()
    )
    assert canonical["source"] == ENGINE_SOURCE
    assert canonical["sourceProvenance"] == {}


def test_app_shape_without_dir_still_passes_on_the_engine_origin():
    receipt = execution_approval.build_provenance_from_response(
        build_response(APP_SOURCE),
        DAILY_CONTRACT,
        manifest_version=MANIFEST_V2,
        mode=NEW_CONSUME,
        registry=REGISTRY,
    )
    assert receipt.resolved_source_sha == APP_SOURCE["connectedRepository"]["revision"]


def _dir_app(connected):
    connected["dir"] = "app"


def _dir_empty(connected):
    connected["dir"] = ""


def _dir_trailing_slash(connected):
    connected["dir"] = "engine/"


def _dir_dot_prefixed(connected):
    connected["dir"] = "./engine"


def _dir_uppercase(connected):
    connected["dir"] = "Engine"


def _dir_nested(connected):
    connected["dir"] = "engine/src"


def _dir_list(connected):
    connected["dir"] = ["engine"]


def _dir_none(connected):
    connected["dir"] = None


def _extra_key_beside_dir(connected):
    connected["extra"] = "engine"


def _extra_key_instead_of_dir(connected):
    del connected["dir"]
    connected["subdir"] = "engine"


def _dir_only(connected):
    del connected["repository"]


@pytest.mark.parametrize(
    "mutate",
    [
        _dir_app,
        _dir_empty,
        _dir_trailing_slash,
        _dir_dot_prefixed,
        _dir_uppercase,
        _dir_nested,
        _dir_list,
        _dir_none,
        _extra_key_beside_dir,
        _extra_key_instead_of_dir,
        _dir_only,
    ],
    ids=lambda fn: fn.__name__.lstrip("_"),
)
def test_engine_origin_refuses_every_other_directory_and_key(mutate):
    payload = build_response(ENGINE_SOURCE)
    mutate(payload["source"]["connectedRepository"])
    assert payload["source"] != ENGINE_SOURCE
    with refusal():
        execution_approval.build_provenance_from_response(
            payload,
            DAILY_CONTRACT,
            manifest_version=MANIFEST_V2,
            mode=NEW_CONSUME,
            registry=REGISTRY,
        )


@pytest.mark.parametrize("contract", V1_CONTRACTS)
def test_a_dir_on_an_origin_that_expects_none_refuses(contract):
    """The retained v1 rows build the pipeline image from the repository root, so even the
    exact engine directory refuses there; only the engine image row expects a subtree."""
    source = {
        "connectedRepository": {
            "dir": "engine",
            "repository": V1_CONNECTED_REPOSITORY,
            "revision": "d" * 40,
        }
    }
    payload = build_response(source, image_repository=V1_IMAGE_REPOSITORY)
    with refusal():
        execution_approval.build_provenance_from_response(
            payload,
            contract,
            manifest_version=MANIFEST_V1,
            mode=HISTORICAL_READ,
            registry=REGISTRY,
        )
    del payload["source"]["connectedRepository"]["dir"]
    receipt = execution_approval.build_provenance_from_response(
        payload, contract, manifest_version=MANIFEST_V1, mode=HISTORICAL_READ, registry=REGISTRY
    )
    assert receipt.resolved_source_sha == "d" * 40


def test_expected_directory_is_pinned_per_image_repository_not_read_from_the_payload():
    """A row whose image repository is not the engine image expects no directory, even when
    the payload names one; the payload cannot nominate its own expected directory."""
    assert execution_approval._CONNECTED_REPOSITORY_DIR == {ENGINE_IMAGE_REPOSITORY: "engine"}
    other_row = replace(engine_origin(), image_repository=f"{ENGINE_IMAGE_REPOSITORY}-other")
    with refusal():
        execution_approval._connected_repository_sha(deepcopy(ENGINE_SOURCE), origin=other_row)
    assert (
        execution_approval._connected_repository_sha(deepcopy(APP_SOURCE), origin=other_row)
        == APP_SOURCE["connectedRepository"]["revision"]
    )
    assert (
        execution_approval._connected_repository_sha(
            deepcopy(ENGINE_SOURCE), origin=engine_origin()
        )
        == ENGINE_SOURCE["connectedRepository"]["revision"]
    )


def test_registry_rows_split_exactly_by_pinned_directory():
    """Every v2 row is the engine image and expects engine; every v1 row expects none."""
    for (version, _contract), row in REGISTRY.items():
        expected = execution_approval._CONNECTED_REPOSITORY_DIR.get(row.image_repository)
        if version == MANIFEST_V2:
            assert expected == "engine"
        else:
            assert expected is None
