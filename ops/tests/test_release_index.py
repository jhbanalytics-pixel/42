"""The release index producer and the native byte reader behind
``release.py verify --release-index``: every symbolic reference resolves to a
native source through the adapter, the v2 index shape, and the build and publish
commands that produce an index for a real release without a network call.
"""

import base64
import copy
import json
import re
import subprocess
import sys
import urllib.parse
import uuid

import pytest
from ops.deploy import release, release_index
from ops.deploy import release_native_adapter as native
from ops.tests.test_foundation_release import (
    ADAPTER_PATH,
    APP_MANIFEST,
    AUDIENCE,
    ENGINE_COMMIT,
    IMAGE_NAME,
    LP_COMMIT,
    MANIFEST_PATH,
    MANIFEST_SHA256,
    NEW_IMAGE,
    PREFIX,
    RID,
    ROOT,
    Scenario,
    canonical,
    fake,
    make_activation,
    make_binding,
    make_ledger,
    make_policy,
    sha,
    tag_for,
    write_json,
)

TOKEN = "fake-token"
BUCKET = release.BUCKET
BUILD_ENDPOINT = "https://cloudbuild.googleapis.com/v1/"
REGISTRY = "https://us-central1-docker.pkg.dev/v2/ogilvy-trends-v2/intelligence-42/"
APP_BUILD = (
    "projects/590353929363/locations/us-central1/builds/"
    "5f6df121-b82c-4d3c-8935-8a78d1f9b499"
)
PRICING_URL = (
    "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"
)
MANIFEST_ACCEPT = (
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
RELEASE_PREFIX = "open-intelligence/v2/staging/releases/"
ENGINE_IMAGE_NAME = IMAGE_NAME.replace("listening-post-question", "engine")
ENGINE_BUILD = (
    "projects/590353929363/locations/us-central1/builds/"
    "889fd1ff-0f4e-4ea1-8c11-9fb1f5992308"
)
ENGINE_MANIFEST = b'{"schemaVersion": 2, "engine": true}'
CONTRACT = b'{"contract": "question_timeout"}'
AUTHORITY_OBJECT = b'{"binding": "object bytes"}'
LEDGER_AFTER = b'{"ledger": "after activation"}'
LEDGER_BEFORE = b'{"ledger": "before activation"}'
ASSET = b"console.log('served asset');\n"


def _json(status, value):
    return {
        "status": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(value).encode("utf-8"),
    }


def evidence_uri(commit, reference):
    return f"gs://{BUCKET}/{RELEASE_PREFIX}{commit}/{reference.replace(':', '/', 1)}"


def make_index(scenario, *, commit=LP_COMMIT):
    """A v3 index for the scenario plus the bytes each reference must resolve to
    and the native sources a transport must serve to produce those bytes.

    Every payload the validator reads lives under the release prefix, including
    the two raw authority objects and the source binding, whose bucket locators
    stay in the index as provenance the validator never fetches.
    """
    payloads = {
        "engine_tree": b"11" * 32 + b"  engine/src/a.py\n",
        "app_tree": b"22" * 32 + b"  app/src/b.py\n",
        "ops_tree": b"33" * 32 + b"  ops/deploy/c.py\n",
        "resource_manifest": MANIFEST_PATH.read_bytes(),
        "contract:question_timeout": CONTRACT,
        "authority:deployment_binding": AUTHORITY_OBJECT,
        "authority:activation_ledger": LEDGER_AFTER,
        "source_binding": LEDGER_BEFORE,
    }
    evidence_objects = {
        reference: {
            "uri": evidence_uri(commit, reference),
            "generation": str(50 + number),
            "sha256": sha(raw),
        }
        for number, (reference, raw) in enumerate(sorted(payloads.items()))
    }
    index = {
        "schema_version": "42_staging_release_v3",
        "repository": "https://github.com/jhbanalytics-pixel/42-Ogilvy-Intelligence",
        "commit": commit,
        "app_build": APP_BUILD,
        "engine_build": ENGINE_BUILD,
        "engine_tree_sha256": sha(payloads["engine_tree"]),
        "app_tree_sha256": sha(payloads["app_tree"]),
        "ops_tree_sha256": sha(payloads["ops_tree"]),
        "engine_image": ENGINE_IMAGE_NAME + "@sha256:" + sha(ENGINE_MANIFEST),
        "app_image": IMAGE_NAME + "@sha256:" + NEW_IMAGE,
        "resource_manifest_sha256": MANIFEST_SHA256,
        "policy_digest": scenario.policy["policy_digest"],
        "deployment_digest": scenario.binding["deployment_digest"],
        "contract_digests": {"question_timeout": sha(CONTRACT)},
        "raw_authority_objects": [
            {
                "name": "activation_ledger",
                "uri": f"gs://{BUCKET}/{release.LEDGER_OBJECT}",
                "generation": "1789827973845547",
                "sha256": sha(LEDGER_AFTER),
            },
            {
                "name": "deployment_binding",
                "uri": f"gs://{BUCKET}/authority/grant.json",
                "generation": "7",
                "sha256": sha(AUTHORITY_OBJECT),
            },
        ],
        "evidence_mode": "retained_baseline",
        "evidence_objects": evidence_objects,
        "source_binding": {
            "uri": f"gs://{BUCKET}/{release.LEDGER_OBJECT}",
            "generation": "1789425528246577",
            "sha256": sha(LEDGER_BEFORE),
        },
        "served_assets": {"assets/app.js": sha(ASSET)},
    }
    policy_raw = canonical(
        {key: value for key, value in scenario.policy.items() if key != "policy_digest"}
    )
    binding_raw = canonical(
        {
            key: value
            for key, value in scenario.binding.items()
            if key != "deployment_digest"
        }
    )
    references = {
        "commit": commit.encode(),
        "engine_image": ENGINE_MANIFEST,
        "app_image": APP_MANIFEST,
        "policy": policy_raw,
        "deployment": binding_raw,
        "asset:assets/app.js": ASSET,
        **payloads,
    }
    return index, references


class IndexCloud:
    """Answers Cloud Build, the registry, the bucket, the tagged revision and the
    pricing page from in-memory sources; every request is kept with its headers."""

    def __init__(self, scenario, index, references):
        self.requests = []
        self.objects = {
            name: {
                "generation": entry["generation"],
                "raw": base64.b64decode(entry["raw_b64"]),
            }
            for name, entry in scenario.state["objects"].items()
        }
        for reference, item in index["evidence_objects"].items():
            self.objects[item["uri"].split("/", 3)[3]] = {
                "generation": item["generation"],
                "raw": references[reference],
            }
        self.builds = {
            index["app_build"]: self._receipt(
                index["app_build"],
                IMAGE_NAME,
                NEW_IMAGE,
                references["commit"].decode(),
            ),
            index["engine_build"]: self._receipt(
                index["engine_build"],
                ENGINE_IMAGE_NAME,
                sha(references["engine_image"]),
                references["commit"].decode(),
            ),
        }
        self.build = self.builds[index["app_build"]]
        self.manifests = {
            sha(references["engine_image"]): references["engine_image"],
            sha(references["app_image"]): references["app_image"],
        }
        self.assets = {
            "assets/app.js": references["asset:assets/app.js"],
        }
        self.pages = {PRICING_URL: b"<html>pricing</html>"}
        self.tag = tag_for()

    @staticmethod
    def _receipt(name, image, digest, revision):
        return {
            "name": name,
            "projectId": "ogilvy-trends-v2",
            "status": "SUCCESS",
            "source": {
                "connectedRepository": {
                    "repository": "projects/ogilvy-trends-v2/locations/us-central1/"
                    "connections/tev2-gh/repositories/"
                    "jhbanalytics-pixel-42-ogilvy-intelligence",
                    "revision": revision,
                }
            },
            "results": {
                "images": [
                    {"digest": "sha256:" + digest, "name": image + ":" + revision}
                ]
            },
        }

    def __call__(self, request):
        self.requests.append(request)
        parsed = urllib.parse.urlsplit(request["url"])
        query = dict(urllib.parse.parse_qsl(parsed.query))
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        assert request["method"] == "GET"
        if base.startswith(BUILD_ENDPOINT):
            build = self.builds.get(base[len(BUILD_ENDPOINT) :])
            if build is None:
                return _json(404, {"error": {"code": 404}})
            return _json(200, build)
        if base.startswith(REGISTRY):
            image, _, tail = base[len(REGISTRY) :].partition("/manifests/")
            digest = tail.removeprefix("sha256:")
            raw = self.manifests.get(digest)
            if raw is None or not image:
                return _json(404, {"errors": [{"code": "MANIFEST_UNKNOWN"}]})
            return {"status": 200, "headers": {}, "body": raw}
        if base.startswith(native.STORAGE_ENDPOINT):
            prefix = native.STORAGE_ENDPOINT + f"b/{BUCKET}/o/"
            assert base.startswith(prefix), base
            name = urllib.parse.unquote(base[len(prefix) :])
            stored = self.objects.get(name)
            if stored is None or (
                "generation" in query and query["generation"] != stored["generation"]
            ):
                return _json(404, {"error": {"code": 404}})
            if query.get("alt") == "media":
                return {"status": 200, "headers": {}, "body": stored["raw"]}
            return _json(
                200,
                {
                    "name": name,
                    "generation": stored["generation"],
                    "size": str(len(stored["raw"])),
                },
            )
        origin = "https://" + self.tag + "---" + AUDIENCE.removeprefix("https://")
        if base.startswith(origin + "/"):
            raw = self.assets.get(base[len(origin) + 1 :])
            if raw is None:
                return {"status": 404, "headers": {}, "body": b"not found"}
            return {"status": 200, "headers": {}, "body": raw}
        if request["url"] in self.pages:
            return {"status": 200, "headers": {}, "body": self.pages[request["url"]]}
        raise AssertionError("unexpected request " + request["url"])


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(native, "_gcloud_token", lambda: TOKEN)
    monkeypatch.setattr(native, "_adc_token", lambda: TOKEN)


def bound_reader(clients, index):
    origin = release.asset_origin(AUDIENCE, tag_for())
    return clients["bytes"].reader(index, asset_origin=origin)


# The native byte reader


def test_native_reader_resolves_every_reference_kind_from_its_native_source(
    token, tmp_path
):
    scenario = Scenario(tmp_path)
    index, references = make_index(scenario)
    cloud = IndexCloud(scenario, index, references)
    clients = native.build("owner-gcloud", transport=cloud)
    reader = bound_reader(clients, index)
    resolved = {name: reader(name) for name in sorted(references)}
    assert {name for name, raw in resolved.items() if raw is None} == set()
    assert resolved == references
    urls = [request["url"] for request in cloud.requests]
    for name in (APP_BUILD, ENGINE_BUILD):
        build_request = next(
            r for r in cloud.requests if r["url"] == BUILD_ENDPOINT + name
        )
        assert build_request["headers"]["Authorization"] == "Bearer " + TOKEN
        assert build_request["headers"]["Accept"] == "application/json"
    manifest_urls = [
        url for url in urls if url.startswith(REGISTRY) and "/manifests/" in url
    ]
    assert sorted(manifest_urls) == [
        REGISTRY + "engine/manifests/sha256:" + sha(ENGINE_MANIFEST),
        REGISTRY + "listening-post-question/manifests/sha256:" + NEW_IMAGE,
    ]
    for request in cloud.requests:
        if request["url"] in manifest_urls:
            assert request["headers"]["Authorization"] == "Bearer " + TOKEN
            assert request["headers"]["Accept"] == MANIFEST_ACCEPT
    policy_name = PREFIX + f"policies/{scenario.policy['policy_digest']}/policy.json"
    binding_name = (
        PREFIX + f"deployments/{scenario.binding['deployment_digest']}/binding.json"
    )
    storage = native.STORAGE_ENDPOINT + f"b/{BUCKET}/o/"
    for name in (policy_name, binding_name):
        quoted = urllib.parse.quote(name, safe="")
        assert storage + quoted in urls
        assert storage + quoted + "?alt=media&generation=41" in urls or (
            storage + quoted + "?alt=media&generation=42" in urls
        )
    for reference, item in index["evidence_objects"].items():
        name = urllib.parse.quote(item["uri"].split("/", 3)[3], safe="")
        assert storage + name + "?generation=" + item["generation"] in urls
        assert storage + name + "?alt=media&generation=" + item["generation"] in urls, (
            reference
        )
    asset_url = (
        "https://"
        + tag_for()
        + "---listening-post-staging-fibxg5ynpq-uc.a.run.app/assets/app.js"
    )
    assert asset_url in urls
    asset_request = next(r for r in cloud.requests if r["url"] == asset_url)
    assert asset_request["headers"] == {}
    assert all(
        TOKEN not in str(value)
        for request in cloud.requests
        if request["url"] == asset_url
        for value in request["headers"].values()
    )
    record = clients["_native"].record
    assert all(TOKEN not in json.dumps(entry) for entry in record)
    assert reader("revision") is None
    assert reader("object:not-a-uri") is None
    ledger_path = urllib.parse.quote(release.LEDGER_OBJECT, safe="")
    assert index["source_binding"]["uri"].endswith(release.LEDGER_OBJECT)
    assert not [url for url in urls if ledger_path in url], (
        "the source binding and the authority ledger are provenance only; the "
        "generation they name is superseded the next time anything writes that "
        "object, so the validator must read the pinned evidence instead"
    )


def test_validator_reads_no_reference_from_a_mutable_object_path(token, tmp_path):
    """The ruling behind v3: a reference that has to resolve later may not point
    at an object another writer can supersede. The only object paths the
    validator may read are the release prefix, whose objects are written once at
    generation zero, and the two digest named paths, whose name is their
    content."""
    scenario = Scenario(tmp_path)
    index, references = make_index(scenario)
    cloud = IndexCloud(scenario, index, references)
    clients = native.build("owner-gcloud", transport=cloud)
    release.validate_release_index(index, byte_reader=bound_reader(clients, index))
    storage = native.STORAGE_ENDPOINT + f"b/{BUCKET}/o/"
    read_names = {
        urllib.parse.unquote(request["url"][len(storage) :].partition("?")[0])
        for request in cloud.requests
        if request["url"].startswith(storage)
    }
    allowed_prefix = RELEASE_PREFIX + index["commit"] + "/"
    for name in read_names:
        assert name.startswith(allowed_prefix) or name in (
            PREFIX + f"policies/{index['policy_digest']}/policy.json",
            PREFIX + f"deployments/{index['deployment_digest']}/binding.json",
        ), name
    assert len(read_names) == len(index["evidence_objects"]) + 2


def test_native_reader_ties_the_commit_to_the_build_that_pushed_the_app_image(
    token, tmp_path
):
    scenario = Scenario(tmp_path)
    index, references = make_index(scenario)
    cloud = IndexCloud(scenario, index, references)
    cloud.build["results"]["images"][0]["digest"] = "sha256:" + "f" * 64
    clients = native.build("owner-gcloud", transport=cloud)
    with pytest.raises(ValueError, match="release_index_build_image_mismatch"):
        bound_reader(clients, index)("commit")
    cloud = IndexCloud(scenario, index, references)
    cloud.build["status"] = "FAILURE"
    clients = native.build("owner-gcloud", transport=cloud)
    with pytest.raises(ValueError, match="release_index_build_not_successful"):
        bound_reader(clients, index)("commit")
    cloud = IndexCloud(scenario, index, references)
    cloud.build["projectId"] = "another-project"
    clients = native.build("owner-gcloud", transport=cloud)
    with pytest.raises(ValueError, match="release_index_build_project_mismatch"):
        bound_reader(clients, index)("commit")
    cloud = IndexCloud(scenario, index, references)
    del cloud.builds[index["app_build"]]
    clients = native.build("owner-gcloud", transport=cloud)
    assert bound_reader(clients, index)("commit") is None


def test_verify_refuses_an_index_edited_to_name_an_engine_build_of_another_commit(
    token, tmp_path
):
    """Build time ties the engine build to the commit; verify must hold it too.

    The index is edited after it was built so that engine_build names a build
    of another commit that pushed the same engine digest. Only the build
    receipt, which the index does not supply, says which commit that was.
    """
    scenario = Scenario(tmp_path)
    index, references = make_index(scenario)
    cloud = IndexCloud(scenario, index, references)
    clients = native.build("owner-gcloud", transport=cloud)
    release.validate_release_index(index, byte_reader=bound_reader(clients, index))

    other_build = ENGINE_BUILD.rpartition("/")[0] + "/" + str(uuid.UUID(int=7))
    edited = copy.deepcopy(index)
    edited["engine_build"] = other_build
    cloud = IndexCloud(scenario, index, references)
    cloud.builds[other_build] = IndexCloud._receipt(
        other_build,
        ENGINE_IMAGE_NAME,
        sha(references["engine_image"]),
        "e" * 40,
    )
    clients = native.build("owner-gcloud", transport=cloud)
    release.validate_release_index_shape(edited)
    with pytest.raises(ValueError, match="engine_build_commit_mismatch"):
        release.validate_release_index(
            edited, byte_reader=bound_reader(clients, edited)
        )
    assert not [
        request for request in cloud.requests if "/manifests/" in request["url"]
    ]

    # The same receipt under the build name the index was built with.
    cloud = IndexCloud(scenario, index, references)
    revision = cloud.builds[index["engine_build"]]["source"]["connectedRepository"]
    revision["revision"] = "e" * 40
    clients = native.build("owner-gcloud", transport=cloud)
    with pytest.raises(ValueError, match="engine_build_commit_mismatch"):
        bound_reader(clients, index)("engine_image")


def test_native_reader_returns_none_for_an_asset_or_manifest_the_source_lacks(
    token, tmp_path
):
    scenario = Scenario(tmp_path)
    index, references = make_index(scenario)
    cloud = IndexCloud(scenario, index, references)
    del cloud.assets["assets/app.js"]
    del cloud.manifests[NEW_IMAGE]
    clients = native.build("owner-gcloud", transport=cloud)
    reader = bound_reader(clients, index)
    assert reader("asset:assets/app.js") is None
    assert reader("app_image") is None
    assert reader("asset:../secret") is None
    assert reader("asset:/absolute") is None
    assert reader("asset:https://elsewhere.example/x.js") is None
    assert all(
        not request["url"].startswith("https://elsewhere") for request in cloud.requests
    )


def test_native_reader_ties_each_image_to_the_build_that_pushed_it(token, tmp_path):
    """An image digest hashing its own manifest proves only that the registry
    holds that manifest. Each image is therefore corroborated by the build
    receipt the index names, which the index did not supply the digest for."""
    scenario = Scenario(tmp_path)
    index, references = make_index(scenario)
    for reference, build_field in (
        ("engine_image", "engine_build"),
        ("app_image", "app_build"),
    ):
        cloud = IndexCloud(scenario, index, references)
        cloud.builds[index[build_field]]["results"]["images"][0]["digest"] = (
            "sha256:" + "a" * 64
        )
        clients = native.build("owner-gcloud", transport=cloud)
        with pytest.raises(ValueError, match="release_index_build_image_mismatch"):
            bound_reader(clients, index)(reference)
        assert not [
            request for request in cloud.requests if "/manifests/" in request["url"]
        ], reference
        cloud = IndexCloud(scenario, index, references)
        cloud.builds[index[build_field]]["status"] = "FAILURE"
        clients = native.build("owner-gcloud", transport=cloud)
        with pytest.raises(ValueError, match="release_index_build_not_successful"):
            bound_reader(clients, index)(reference)
        cloud = IndexCloud(scenario, index, references)
        del cloud.builds[index[build_field]]
        clients = native.build("owner-gcloud", transport=cloud)
        assert bound_reader(clients, index)(reference) is None
    cloud = IndexCloud(scenario, index, references)
    clients = native.build("owner-gcloud", transport=cloud)
    reader = bound_reader(clients, index)
    assert reader("engine_image") == ENGINE_MANIFEST
    assert reader("engine_image") == ENGINE_MANIFEST
    builds = [r for r in cloud.requests if r["url"].startswith(BUILD_ENDPOINT)]
    assert len(builds) == 1, "the build receipt is read once per reader"


def test_preimage_refuses_a_stored_object_that_is_not_the_canonical_bytes(
    token, tmp_path
):
    """The digest is minted over canonical bytes, so a stored object that merely
    parses to the same value is not the object the digest names. Recanonicalising
    before comparing would accept all four of these."""
    scenario = Scenario(tmp_path)
    index, references = make_index(scenario)
    name = PREFIX + f"policies/{scenario.policy['policy_digest']}/policy.json"
    canonical_raw = canonical(scenario.policy)
    variants = {
        "pretty printed": json.dumps(scenario.policy, indent=2).encode("utf-8"),
        "ascii escaped": json.dumps(scenario.policy, ensure_ascii=True).encode("utf-8"),
        "whitespace padded": canonical_raw + b"\n",
        "duplicate key": canonical_raw[:1]
        + b'"policy_id":"other",'
        + canonical_raw[1:],
    }
    for label, raw in variants.items():
        assert raw != canonical_raw, label
        assert json.loads(raw.decode("utf-8")) is not None, label
        cloud = IndexCloud(scenario, index, references)
        cloud.objects[name] = {"generation": "41", "raw": raw}
        clients = native.build("owner-gcloud", transport=cloud)
        assert bound_reader(clients, index)("policy") is None, label
    cloud = IndexCloud(scenario, index, references)
    clients = native.build("owner-gcloud", transport=cloud)
    assert bound_reader(clients, index)("policy") == references["policy"]


def test_a_dot_segment_in_an_image_path_never_reaches_a_registry_request(
    token, tmp_path
):
    """The manifest this names is present and the build receipt corroborates it,
    so the only thing that can refuse the read is the path check itself. The
    first version of this test deleted the manifest first, which made it pass
    whether the check existed or not."""
    scenario = Scenario(tmp_path)
    index, references = make_index(scenario)
    cloud = IndexCloud(scenario, index, references)
    clients = native.build("owner-gcloud", transport=cloud)
    assert bound_reader(clients, index)("app_image") == APP_MANIFEST
    before = len(cloud.requests)
    traversal = copy.deepcopy(index)
    traversal["app_image"] = (
        release.IMAGE_REGISTRY + "../../other/image@sha256:" + NEW_IMAGE
    )
    assert bound_reader(clients, traversal)("app_image") is None
    assert len(cloud.requests) == before, "no request at all is sent for that path"
    assert all("/../" not in request["url"] for request in cloud.requests)


def test_native_reader_fetches_the_pricing_source_anonymously_from_its_host_only(
    token, tmp_path
):
    scenario = Scenario(tmp_path)
    index, references = make_index(scenario)
    cloud = IndexCloud(scenario, index, references)
    clients = native.build("owner-gcloud", transport=cloud)
    raw = clients["bytes"].read("source:" + PRICING_URL)
    assert raw == b"<html>pricing</html>"
    request = cloud.requests[-1]
    assert request["url"] == PRICING_URL
    assert request["method"] == "GET"
    assert request["headers"] == {}
    assert clients["_native"]._token is None
    assert clients["bytes"].read("source:http://cloud.google.com/pricing") is None
    assert (
        clients["bytes"].read("source:https://cloud.google.com.evil.example/") is None
    )
    assert clients["bytes"].read("source:https://example.com/pricing") is None
    assert clients["bytes"].read("source:https://cloud.google.com:8443/pricing") is None
    assert clients["bytes"].read("source:") is None
    assert len(cloud.requests) == 1
    assert frozenset({"cloud.google.com"}) == native.SOURCE_HOSTS


# The v2 index shape and the validator against native bytes


def test_v3_index_shape_is_accepted_and_the_older_versions_are_refused(tmp_path):
    scenario = Scenario(tmp_path)
    index, _references = make_index(scenario)
    images = release.validate_release_index_shape(index)
    assert images == {"engine_image": sha(ENGINE_MANIFEST), "app_image": NEW_IMAGE}
    v1 = copy.deepcopy(index)
    v1["schema_version"] = "42_staging_release_v1"
    for field in ("app_build", "engine_build", "evidence_objects"):
        del v1[field]
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index_shape(v1)
    v2 = copy.deepcopy(index)
    v2["schema_version"] = "42_staging_release_v2"
    del v2["engine_build"]
    v2["raw_authority_objects"] = [
        {key: value for key, value in item.items() if key != "name"}
        for item in v2["raw_authority_objects"]
    ]
    for reference in list(v2["evidence_objects"]):
        if reference.startswith("authority:") or reference == "source_binding":
            del v2["evidence_objects"][reference]
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index_shape(v2)
    unnamed = copy.deepcopy(index)
    del unnamed["raw_authority_objects"][0]["name"]
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index_shape(unnamed)
    duplicate = copy.deepcopy(index)
    duplicate["raw_authority_objects"][1]["name"] = duplicate["raw_authority_objects"][
        0
    ]["name"]
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index_shape(duplicate)
    for reference in ("authority:activation_ledger", "source_binding"):
        unpinned = copy.deepcopy(index)
        del unpinned["evidence_objects"][reference]
        with pytest.raises(ValueError, match="release_index_invalid"):
            release.validate_release_index_shape(unpinned)
        disagreeing = copy.deepcopy(index)
        disagreeing["evidence_objects"][reference]["sha256"] = "9" * 64
        with pytest.raises(ValueError, match="release_index_invalid"):
            release.validate_release_index_shape(disagreeing)
    for field, value in (
        ("engine_build", "projects/p/locations/us-central1/builds/not-a-uuid"),
        ("app_build", "projects/other/locations/us-central1/builds/" + APP_BUILD[-36:]),
        ("app_build", "projects/p/locations/us-central1/builds/not-a-uuid"),
        ("app_build", "projects/p/locations/europe-west1/builds/" + APP_BUILD[-36:]),
        ("app_build", BUILD_ENDPOINT + APP_BUILD),
        ("evidence_objects", {}),
        ("evidence_objects", []),
    ):
        broken = copy.deepcopy(index)
        broken[field] = value
        with pytest.raises(ValueError, match="release_index_invalid"):
            release.validate_release_index_shape(broken)
    extra = copy.deepcopy(index)
    extra["evidence_objects"]["commit"] = extra["evidence_objects"]["engine_tree"]
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index_shape(extra)
    missing = copy.deepcopy(index)
    del missing["evidence_objects"]["contract:question_timeout"]
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index_shape(missing)
    moved = copy.deepcopy(index)
    moved["evidence_objects"]["ops_tree"]["uri"] = evidence_uri(LP_COMMIT, "ops_tree2")
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index_shape(moved)
    other_commit = copy.deepcopy(index)
    other_commit["evidence_objects"]["ops_tree"]["uri"] = evidence_uri(
        ENGINE_COMMIT, "ops_tree"
    )
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index_shape(other_commit)
    disagreeing = copy.deepcopy(index)
    disagreeing["evidence_objects"]["app_tree"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index_shape(disagreeing)
    contract_name = copy.deepcopy(index)
    contract_name["contract_digests"] = {"bad name": sha(CONTRACT)}
    contract_name["evidence_objects"]["contract:bad name"] = contract_name[
        "evidence_objects"
    ].pop("contract:question_timeout")
    contract_name["evidence_objects"]["contract:bad name"]["uri"] = evidence_uri(
        LP_COMMIT, "contract:bad name"
    )
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index_shape(contract_name)


def test_validate_release_index_accepts_native_bytes_and_refuses_each_alteration(
    token, tmp_path
):
    scenario = Scenario(tmp_path)
    index, references = make_index(scenario)
    cloud = IndexCloud(scenario, index, references)
    clients = native.build("owner-gcloud", transport=cloud)
    validated = release.validate_release_index(
        index, byte_reader=bound_reader(clients, index)
    )
    assert validated == index
    assert validated is not index
    assert len(references) == 14
    assert sorted(references) == [
        "app_image",
        "app_tree",
        "asset:assets/app.js",
        "authority:activation_ledger",
        "authority:deployment_binding",
        "commit",
        "contract:question_timeout",
        "deployment",
        "engine_image",
        "engine_tree",
        "ops_tree",
        "policy",
        "resource_manifest",
        "source_binding",
    ]

    def altered(reference):
        broken = IndexCloud(scenario, index, references)
        raw = references[reference] + b"x"
        if reference == "commit":
            broken.builds[index["app_build"]]["source"]["connectedRepository"][
                "revision"
            ] = "e" * 40
        elif reference in ("engine_image", "app_image"):
            digest = sha(references[reference])
            broken.manifests[digest] = raw
        elif reference in ("policy", "deployment"):
            field = "policy" if reference == "policy" else "binding"
            value = copy.deepcopy(getattr(scenario, field))
            value["pricing" if field == "policy" else "sdk_version"] = "altered"
            name = (
                PREFIX + f"policies/{scenario.policy['policy_digest']}/policy.json"
                if field == "policy"
                else PREFIX
                + f"deployments/{scenario.binding['deployment_digest']}/binding.json"
            )
            broken.objects[name]["raw"] = canonical(value)
        elif reference.startswith("asset:"):
            broken.assets[reference[len("asset:") :]] = raw
        else:
            item = index["evidence_objects"][reference]
            broken.objects[item["uri"].split("/", 3)[3]]["raw"] = raw
        return broken

    for reference in sorted(references):
        broken = altered(reference)
        clients = native.build("owner-gcloud", transport=broken)
        with pytest.raises(ValueError, match="release_index_digest_mismatch"):
            release.validate_release_index(
                index, byte_reader=bound_reader(clients, index)
            )

    def unavailable(reference):
        missing = IndexCloud(scenario, index, references)
        if reference == "commit":
            del missing.builds[index["app_build"]]
        elif reference in ("engine_image", "app_image"):
            del missing.manifests[sha(references[reference])]
        elif reference.startswith("asset:"):
            del missing.assets[reference[len("asset:") :]]
        elif reference in ("policy", "deployment"):
            name = (
                PREFIX + f"policies/{scenario.policy['policy_digest']}/policy.json"
                if reference == "policy"
                else PREFIX
                + f"deployments/{scenario.binding['deployment_digest']}/binding.json"
            )
            del missing.objects[name]
        else:
            item = index["evidence_objects"][reference]
            del missing.objects[item["uri"].split("/", 3)[3]]
        return missing

    for reference in sorted(references):
        clients = native.build("owner-gcloud", transport=unavailable(reference))
        with pytest.raises(ValueError, match="release_index_bytes_unavailable"):
            release.validate_release_index(
                index, byte_reader=bound_reader(clients, index)
            )


# The producer: build from a checkout, publish through the adapter


PRODUCER_SCRIPT = ROOT / "ops" / "deploy" / "release_index.py"
SCRATCH_FILES = {
    "engine/src/a.py": b"print('engine a')\n",
    "engine/configs/open_intelligence/origin_contracts/c.json": b'{"c": 1}\n',
    "engine/z.txt": b"last engine file\r\nwith a crlf line\r\n",
    "app/src/b.py": b"print('app b')\n",
    "app/web/index.html": b"<html></html>\n",
    "ops/deploy/release.py": b"# not the real one\n",
    "ops/deploy/resource_manifest.json": MANIFEST_PATH.read_bytes(),
    "infra/runtime/daily-staging.json": b"{}\n",
    "cloudbuild.app.publish.yaml": b"steps: []\n",
    "docs/contracts/client-onboarding-v1.md": b"# contract\n",
    "README.md": b"# scratch\n",
}


def git(repo, *args, check=True):
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False
    )
    if check:
        assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return completed.stdout.decode("utf-8", "replace").strip()


def scratch_checkout(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "user.name", "scratch")
    git(repo, "config", "user.email", "scratch@example.invalid")
    for name, raw in SCRATCH_FILES.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "scratch release commit")
    return repo, git(repo, "rev-parse", "HEAD")


def build_receipt(name, image, digest, revision, *, directory=None):
    build = {
        "name": name,
        "id": name.rsplit("/", 1)[1],
        "projectId": "ogilvy-trends-v2",
        "status": "SUCCESS",
        "source": {
            "connectedRepository": {
                "repository": "projects/ogilvy-trends-v2/locations/us-central1/"
                "connections/tev2-gh/repositories/"
                "jhbanalytics-pixel-42-ogilvy-intelligence",
                "revision": revision,
            }
        },
        "results": {
            "images": [{"name": image + ":" + revision, "digest": "sha256:" + digest}]
        },
        "steps": [],
    }
    if directory:
        build["source"]["connectedRepository"]["dir"] = directory
    return build


class Release:
    """One scratch release: a checkout at a commit, the receipts an activation
    and a routed release leave behind, and a ledger copy from before activation."""

    ENGINE_BUILD = (
        "projects/590353929363/locations/us-central1/builds/"
        "889fd1ff-0f4e-4ea1-8c11-9fb1f5992308"
    )

    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.repo, self.commit = scratch_checkout(tmp_path)
        self.policy = make_policy()
        self.binding = make_binding(
            self.policy,
            revision="listening-post-staging-q-new",
            image_digest=NEW_IMAGE,
            lp_commit=self.commit,
        )
        old_policy = make_policy()
        old_binding = make_binding(
            old_policy,
            revision="listening-post-staging-q-old",
            image_digest="b" * 64,
            lp_commit="1" * 40,
        )
        self.before = make_ledger(old_binding, old_policy)
        self.after = make_ledger(
            self.binding, self.policy, extra_bindings=[(old_binding, old_policy)]
        )
        self.activation = make_activation(self.binding, self.policy, self.after)
        binding_name = PREFIX + (
            f"deployments/{self.binding['deployment_digest']}/binding.json"
        )
        self.activation["chain"] = {
            "binding_object": {
                "action": "created",
                "object": binding_name,
                "generation": "1789827970905879",
                "sha256": sha(canonical(self.binding)),
            },
            "ledger": {
                "action": "written",
                "object": release.LEDGER_OBJECT,
                "generation": "1789827973845547",
                "if_generation_match": 1789425528246577,
                "sha256": sha(canonical(self.after)),
            },
            "ledger_generation_before": "1789425528246577",
            "queue": {"action": "paused", "state": "PAUSED"},
            "tag": tag_for(),
        }
        self.result = {
            "contract_version": "42_release_receipt_v1",
            "kind": "release",
            "state": "traffic_verified_100_queue_paused",
            "binding": self.binding,
            "expected": {"tag": tag_for(), "audience": AUDIENCE},
        }
        self.ledger_copy = canonical(self.before)
        self.paths = {
            "app_build": write_json(
                tmp_path / "app-build.json",
                build_receipt(APP_BUILD, IMAGE_NAME, NEW_IMAGE, self.commit),
            ),
            "engine_build": write_json(
                tmp_path / "engine-build.json",
                build_receipt(
                    self.ENGINE_BUILD,
                    IMAGE_NAME.replace("listening-post-question", "engine"),
                    sha(ENGINE_MANIFEST),
                    self.commit,
                    directory="engine",
                ),
            ),
            "activation": write_json(tmp_path / "activation.json", self.activation),
            "release_result": write_json(tmp_path / "release-result.json", self.result),
            "served_assets": write_json(
                tmp_path / "served-assets.json", {"assets/app.js": sha(ASSET)}
            ),
        }
        (tmp_path / "ledger.json").write_bytes(self.ledger_copy)
        self.paths["ledger_copy"] = tmp_path / "ledger.json"
        self.output = tmp_path / "index-build"

    def build_args(self, **overrides):
        values = {
            "repo": self.repo,
            "commit": self.commit,
            "output": self.output,
            **self.paths,
        }
        values.update(overrides)
        args = ["build"]
        for key, value in values.items():
            args += ["--" + key.replace("_", "-"), str(value)]
        args += [
            "--contract",
            "brain_read=engine/configs/open_intelligence/origin_contracts/c.json",
            "--contract",
            "client_onboarding=docs/contracts/client-onboarding-v1.md",
            "--evidence-mode",
            "retained_baseline",
            "--resources",
            str(MANIFEST_PATH),
        ]
        return args


def inventory(prefixes):
    """Recomputed here from the files the test wrote, never from the producer."""
    lines = []
    for name in sorted(SCRATCH_FILES):
        if name.startswith(prefixes) or name in prefixes:
            lines.append(f"{sha(SCRATCH_FILES[name])}  {name}\n")
    return "".join(lines).encode("utf-8")


def summary_error(capsys):
    return json.loads(capsys.readouterr().out.splitlines()[-1])["error"]


def test_build_writes_the_draft_and_payloads_the_test_recomputes_from_the_files(
    tmp_path,
):
    scratch = Release(tmp_path)
    code = release_index.execute(scratch.build_args())
    assert code == 0
    draft = json.loads((scratch.output / "draft.json").read_text(encoding="utf-8"))
    assert draft["contract_version"] == "42_staging_release_draft_v1"
    index = draft["index"]
    assert "evidence_objects" not in index
    expected_engine = inventory(("engine/",))
    expected_app = inventory(("app/",))
    expected_ops = inventory(("ops/", "infra/", "cloudbuild.app.publish.yaml"))
    assert expected_engine.count(b"\n") == 3
    assert expected_ops.count(b"\n") == 4
    # Sorted by path, as the engine build's inventory step sorts, never by digest.
    assert expected_ops.startswith(
        sha(SCRATCH_FILES["cloudbuild.app.publish.yaml"]).encode()
    )
    assert [line.split(b"  ", 1)[1] for line in expected_engine.splitlines()] == [
        b"engine/configs/open_intelligence/origin_contracts/c.json",
        b"engine/src/a.py",
        b"engine/z.txt",
    ]
    payloads = {
        reference: (scratch.output / item["file"]).read_bytes()
        for reference, item in draft["payloads"].items()
    }
    assert payloads == {
        "engine_tree": expected_engine,
        "app_tree": expected_app,
        "ops_tree": expected_ops,
        "resource_manifest": MANIFEST_PATH.read_bytes(),
        "contract:brain_read": SCRATCH_FILES[
            "engine/configs/open_intelligence/origin_contracts/c.json"
        ],
        "contract:client_onboarding": SCRATCH_FILES[
            "docs/contracts/client-onboarding-v1.md"
        ],
        "authority:deployment_binding": canonical(scratch.binding),
        "authority:activation_ledger": canonical(scratch.after),
        "source_binding": scratch.ledger_copy,
    }
    for reference, item in draft["payloads"].items():
        assert item["sha256"] == sha(payloads[reference])
        assert item["object"] == release.evidence_object_name(scratch.commit, reference)
    assert index["schema_version"] == "42_staging_release_v3"
    assert index["commit"] == scratch.commit
    assert index["app_build"] == APP_BUILD
    assert index["engine_build"] == Release.ENGINE_BUILD
    assert index["engine_tree_sha256"] == sha(expected_engine)
    assert index["app_tree_sha256"] == sha(expected_app)
    assert index["ops_tree_sha256"] == sha(expected_ops)
    assert index["resource_manifest_sha256"] == MANIFEST_SHA256
    assert index["engine_image"] == (
        IMAGE_NAME.replace("listening-post-question", "engine")
        + "@sha256:"
        + sha(ENGINE_MANIFEST)
    )
    assert index["app_image"] == IMAGE_NAME + "@sha256:" + NEW_IMAGE
    assert index["policy_digest"] == scratch.policy["policy_digest"]
    assert index["deployment_digest"] == scratch.binding["deployment_digest"]
    assert index["contract_digests"] == {
        "brain_read": sha(payloads["contract:brain_read"]),
        "client_onboarding": sha(payloads["contract:client_onboarding"]),
    }
    chain = scratch.activation["chain"]
    assert index["raw_authority_objects"] == [
        {
            "name": "deployment_binding",
            "uri": f"gs://{BUCKET}/" + chain["binding_object"]["object"],
            "generation": chain["binding_object"]["generation"],
            "sha256": chain["binding_object"]["sha256"],
        },
        {
            "name": "activation_ledger",
            "uri": f"gs://{BUCKET}/" + chain["ledger"]["object"],
            "generation": chain["ledger"]["generation"],
            "sha256": chain["ledger"]["sha256"],
        },
    ]
    assert index["source_binding"] == {
        "uri": f"gs://{BUCKET}/" + release.LEDGER_OBJECT,
        "generation": chain["ledger_generation_before"],
        "sha256": sha(scratch.ledger_copy),
    }
    # Each provenance record's digest is repeated by the pinned copy, and the
    # pinned copy is what the validator reads.
    for reference in (
        "authority:deployment_binding",
        "authority:activation_ledger",
        "source_binding",
    ):
        assert draft["payloads"][reference]["sha256"] == sha(payloads[reference])
    assert (
        draft["payloads"]["authority:activation_ledger"]["sha256"]
        == (chain["ledger"]["sha256"])
    )
    assert (
        draft["payloads"]["authority:deployment_binding"]["sha256"]
        == (chain["binding_object"]["sha256"])
    )
    assert index["evidence_mode"] == "retained_baseline"
    assert index["served_assets"] == {"assets/app.js": sha(ASSET)}
    assert index["repository"] == release.REPOSITORY
    with_dummy = dict(index)
    with_dummy["evidence_objects"] = {
        reference: {
            "uri": f"gs://{BUCKET}/" + item["object"],
            "generation": "1",
            "sha256": item["sha256"],
        }
        for reference, item in draft["payloads"].items()
    }
    release.validate_release_index_shape(with_dummy)


def test_build_refuses_a_dirty_checkout_a_wrong_commit_and_an_existing_output(
    tmp_path, capsys
):
    scratch = Release(tmp_path)
    (scratch.repo / "engine" / "src" / "a.py").write_bytes(b"print('changed')\n")
    assert release_index.execute(scratch.build_args()) == 1
    assert summary_error(capsys) == "checkout_dirty"
    assert not scratch.output.exists()
    git(scratch.repo, "checkout", "--", "engine/src/a.py")
    assert release_index.execute(scratch.build_args(commit="e" * 40)) == 1
    assert summary_error(capsys) == "checkout_not_at_commit"
    assert not scratch.output.exists()
    scratch.output.mkdir()
    assert release_index.execute(scratch.build_args()) == 1
    assert summary_error(capsys) == "output_exists"
    assert list(scratch.output.iterdir()) == []


def test_build_refuses_an_engine_build_of_another_commit(tmp_path, capsys):
    """The engine image is tied to the release commit the way the app image is."""
    scratch = Release(tmp_path)
    engine_build = json.loads(scratch.paths["engine_build"].read_text(encoding="utf-8"))
    assert engine_build["source"]["connectedRepository"]["revision"] == scratch.commit
    other = copy.deepcopy(engine_build)
    other["source"]["connectedRepository"]["revision"] = "e" * 40
    path = write_json(tmp_path / "engine-build-other.json", other)
    assert release_index.execute(scratch.build_args(engine_build=path)) == 1
    assert summary_error(capsys) == "engine_build_commit_mismatch"
    assert not scratch.output.exists()


def test_build_refuses_receipts_that_disagree_with_the_commit_or_each_other(
    tmp_path, capsys
):
    scratch = Release(tmp_path)
    cases = []
    app_build = json.loads(scratch.paths["app_build"].read_text(encoding="utf-8"))
    other = copy.deepcopy(app_build)
    other["source"]["connectedRepository"]["revision"] = "e" * 40
    cases.append(("app_build", other, "app_build_commit_mismatch"))
    failed = copy.deepcopy(app_build)
    failed["status"] = "FAILURE"
    cases.append(("app_build", failed, "app_build_not_successful"))
    wrong_image = copy.deepcopy(app_build)
    wrong_image["results"]["images"][0]["digest"] = "sha256:" + "f" * 64
    cases.append(("app_build", wrong_image, "app_build_image_mismatch"))
    activation = copy.deepcopy(scratch.activation)
    activation["chain"]["ledger"]["object"] = "elsewhere/ledger.json"
    cases.append(("activation", activation, "activation_chain_invalid"))
    result = copy.deepcopy(scratch.result)
    result["state"] = "unproven"
    cases.append(("release_result", result, "release_result_unverified"))
    for key, value, error in cases:
        path = write_json(tmp_path / f"{key}-broken.json", value)
        assert release_index.execute(scratch.build_args(**{key: path})) == 1, error
        assert summary_error(capsys) == error
        assert not scratch.output.exists()
    (tmp_path / "ledger-broken.json").write_bytes(b"{}")
    assert (
        release_index.execute(
            scratch.build_args(ledger_copy=tmp_path / "ledger-broken.json")
        )
        == 1
    )
    assert summary_error(capsys) == "ledger_invalid"
    assert not scratch.output.exists()
    pretty = tmp_path / "ledger-pretty.json"
    pretty.write_bytes(json.dumps(scratch.before, indent=1).encode("utf-8"))
    assert release_index.execute(scratch.build_args(ledger_copy=pretty)) == 1
    assert summary_error(capsys) == "ledger_copy_not_canonical"
    other = copy.deepcopy(scratch.before)
    other["reserved_microusd"] = 100_000
    other["requests"] = {
        RID: {
            "deployment_digest": next(iter(other["bindings"])),
            "policy_digest": other["bindings"][next(iter(other["bindings"]))],
            "reserved_microusd": 100_000,
            "admitted_at": "2026-09-19T08:00:00.000000Z",
            "deadline_at": "2026-09-19T08:04:00.000000Z",
            "client_scope_id": "ogilvy_default",
        }
    }
    wrong = tmp_path / "ledger-other.json"
    wrong.write_bytes(canonical(other))
    assert release_index.execute(scratch.build_args(ledger_copy=wrong)) == 1
    assert summary_error(capsys) == "ledger_copy_not_the_activation_source"
    assert not scratch.output.exists()


def published_scenario(tmp_path):
    scratch = Release(tmp_path)
    assert release_index.execute(scratch.build_args()) == 0
    state = {
        "service": {"name": "unused"},
        "revisions": {},
        "queue": {"name": "unused", "state": "PAUSED"},
        "tasks": [],
        "objects": {},
        "operations": {},
        "failures": {},
        "bytes": {},
        "journal": [],
        "reads": [],
        "next_generation": 700,
        "clock": {"now": "2026-09-19T08:00:00+00:00", "monotonic": 0.0},
    }
    state_path = write_json(tmp_path / "state.json", state)
    return scratch, state_path


def publish_args(scratch, state_path, output):
    return [
        "publish",
        "--draft",
        str(scratch.output),
        "--adapter",
        str(ADAPTER_PATH) + ":clients",
        "--adapter-state",
        str(state_path),
        "--resources",
        str(MANIFEST_PATH),
        "--output",
        str(output),
    ]


def journal_of(state_path):
    return json.loads(state_path.read_text(encoding="utf-8"))["journal"]


def test_publish_uploads_each_payload_once_after_the_guard_and_writes_the_index(
    tmp_path, monkeypatch, capsys
):
    scratch, state_path = published_scenario(tmp_path)
    clients = fake.clients(state_path)
    state = clients["_state"]
    real_guard = release.resource_guard.assert_allowed

    def spy(resource, action, manifest):
        real_guard(resource, action, manifest)
        state.journal({"call": "guard", "resource": resource, "action": action})

    monkeypatch.setattr(release.resource_guard, "assert_allowed", spy)
    output = tmp_path / "release-index.json"
    code = release_index.execute(
        publish_args(scratch, state_path, output), clients=clients
    )
    assert code == 0
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["state"] == "published"
    journal = journal_of(state_path)
    calls = [entry["call"] for entry in journal]
    draft = json.loads((scratch.output / "draft.json").read_text(encoding="utf-8"))
    references = sorted(draft["payloads"])
    assert len(references) == 9
    assert calls == ["guard", "create_object"] * len(references)
    for number, reference in enumerate(references):
        guard, upload = journal[2 * number], journal[2 * number + 1]
        assert guard == {
            "call": "guard",
            "resource": release.BUCKET_RESOURCE,
            "action": "write",
        }
        assert upload["name"] == release.evidence_object_name(scratch.commit, reference)
        assert upload["if_generation_match"] == 0
        assert upload["sha256"] == draft["payloads"][reference]["sha256"]
    index = json.loads(output.read_text(encoding="utf-8"))
    release.validate_release_index_shape(index)
    for number, reference in enumerate(references):
        item = index["evidence_objects"][reference]
        assert item["generation"] == str(700 + number)
        assert item["uri"] == f"gs://{BUCKET}/" + draft["payloads"][reference]["object"]
    without_evidence = {k: v for k, v in index.items() if k != "evidence_objects"}
    assert without_evidence == draft["index"]
    stored = json.loads(state_path.read_text(encoding="utf-8"))["objects"]
    for item in draft["payloads"].values():
        assert (
            base64.b64decode(stored[item["object"]]["raw_b64"])
            == (scratch.output / item["file"]).read_bytes()
        )
    assert release_index.execute(publish_args(scratch, state_path, output)) == 1
    assert summary_error(capsys) == "output_exists"
    assert [entry["call"] for entry in journal_of(state_path)] == calls


def test_publish_reuses_an_identical_existing_object_and_refuses_a_different_one(
    tmp_path, capsys
):
    scratch, state_path = published_scenario(tmp_path)
    draft = json.loads((scratch.output / "draft.json").read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    ops_tree = draft["payloads"]["ops_tree"]
    state["objects"][ops_tree["object"]] = {
        "generation": "321",
        "raw_b64": base64.b64encode(
            (scratch.output / ops_tree["file"]).read_bytes()
        ).decode("ascii"),
    }
    write_json(state_path, state)
    output = tmp_path / "release-index.json"
    assert release_index.execute(publish_args(scratch, state_path, output)) == 0
    index = json.loads(output.read_text(encoding="utf-8"))
    assert index["evidence_objects"]["ops_tree"]["generation"] == "321"
    uploads = [
        entry["name"]
        for entry in journal_of(state_path)
        if entry["call"] == "create_object"
    ]
    assert ops_tree["object"] in uploads
    state = json.loads(state_path.read_text(encoding="utf-8"))
    app_tree = draft["payloads"]["app_tree"]
    state["objects"][app_tree["object"]] = {
        "generation": "322",
        "raw_b64": base64.b64encode(b"another inventory\n").decode("ascii"),
    }
    write_json(state_path, state)
    other = tmp_path / "release-index-2.json"
    assert release_index.execute(publish_args(scratch, state_path, other)) == 1
    assert summary_error(capsys) == "evidence_object_conflict"
    assert not other.exists()


def test_published_index_verifies_with_the_ledger_object_absent_from_the_bucket(
    tmp_path,
):
    """The whole point of pinning: the ledger object can be gone, or hold some
    later generation, and the published index still verifies, because the
    authority ledger and the source binding are read from the release prefix.
    Nothing substitutes a reader here; the index is the one publish wrote and
    the bytes are the ones publish uploaded."""
    scratch, state_path = published_scenario(tmp_path)
    output = tmp_path / "release-index.json"
    assert release_index.execute(publish_args(scratch, state_path, output)) == 0
    index = json.loads(output.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    policy_name = PREFIX + f"policies/{scratch.policy['policy_digest']}/policy.json"
    binding_name = (
        PREFIX + f"deployments/{scratch.binding['deployment_digest']}/binding.json"
    )

    def entry(raw, generation):
        return {
            "generation": str(generation),
            "raw_b64": base64.b64encode(raw).decode("ascii"),
        }

    state["objects"][policy_name] = entry(canonical(scratch.policy), 41)
    state["objects"][binding_name] = entry(
        canonical(scratch.binding),
        scratch.activation["chain"]["binding_object"]["generation"],
    )
    assert release.LEDGER_OBJECT not in state["objects"]
    state["bytes"] = {
        "commit": base64.b64encode(scratch.commit.encode()).decode("ascii"),
        "engine_image": base64.b64encode(ENGINE_MANIFEST).decode("ascii"),
        "app_image": base64.b64encode(APP_MANIFEST).decode("ascii"),
        "asset:assets/app.js": base64.b64encode(ASSET).decode("ascii"),
    }
    write_json(state_path, state)
    origin = release.asset_origin(AUDIENCE, tag_for())
    seen = []

    def counted(reference, reader=None):
        seen.append(reference)
        return reader(reference)

    clients = fake.clients(state_path)
    bound = clients["bytes"].reader(index, asset_origin=origin)
    validated = release.validate_release_index(
        index, byte_reader=lambda reference: counted(reference, bound)
    )
    assert validated == index
    assert seen == release.release_index_references(index)
    assert len(seen) == 15
    assert "source_binding" in seen
    assert "authority:activation_ledger" in seen
    # And a later write to the ledger, which is what the pricing renewal does,
    # changes nothing, because no reference resolves through that object.
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["objects"][release.LEDGER_OBJECT] = entry(b'{"some": "later ledger"}', 999)
    write_json(state_path, state)
    clients = fake.clients(state_path)
    assert (
        release.validate_release_index(
            index, byte_reader=clients["bytes"].reader(index, asset_origin=origin)
        )
        == index
    )


def test_documented_producer_commands_only_name_existing_flags():
    text = (ROOT / "docs" / "operations" / "release.md").read_text(encoding="utf-8")
    documented = {}
    for match in re.finditer(
        r"release_index\.py (build|publish)((?:\s+--[a-z-]+(?:\s+[^\s`-][^\s`]*)?)+)",
        text,
    ):
        documented.setdefault(match.group(1), set()).update(
            re.findall(r"--[a-z-]+", match.group(2))
        )
    assert set(documented) == {"build", "publish"}
    for command, flags in documented.items():
        completed = subprocess.run(
            [sys.executable, str(PRODUCER_SCRIPT), command, "--help"],
            capture_output=True,
            cwd=str(ROOT),
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        help_text = completed.stdout.decode("utf-8", "replace")
        for flag in flags:
            assert flag in help_text, (command, flag)
        assert "--output" in help_text
