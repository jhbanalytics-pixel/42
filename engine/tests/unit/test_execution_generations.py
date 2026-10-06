import dataclasses
import hashlib
import importlib
import importlib.util
import inspect
import json
import re
import shutil
from pathlib import Path
from types import MappingProxyType

import pytest
from src.analysis.open_intelligence import execution_origins

ENGINE_ROOT = Path(__file__).resolve().parents[2]
# The active generation: the bridge generation, whose manifest is the amendment e manifest
# under the bridge registry digest.
REGISTRY_RELATIVE = "configs/open_intelligence/execution_origins_bridge_v3.json"
RESOURCE_RELATIVE = "configs/open_intelligence/resource_manifest_bridge_v3.json"
# Amendment g added the route A capture's artifact bucket to the bridge manifest, so its
# digest moved from 1643e4ce to ab7802f4; the registry stays 573deeea.
# The daily contract revision then registered three daily operations
# and bounded exposure, so the pair moved again: registry 573deeea to e6b95e35, manifest
# ab7802f4 to b7553041.
# Tightening the date regex to ASCII digits then moved them to f23001ed and 592ed45f.
REGISTRY_SHA256 = "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2"
RESOURCE_SHA256 = "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe"
ARTIFACT_BUCKET = (
    "//storage.googleapis.com/projects/_/buckets/ogilvy-trends-v2-oi-source-artifacts-staging"
)
# Amendment e, retained for reading the records written under it.
AMENDMENT_E_REGISTRY_RELATIVE = "configs/open_intelligence/execution_origins_v1.json"
AMENDMENT_E_RESOURCE_RELATIVE = "configs/open_intelligence/resource_manifest_v1.json"
AMENDMENT_E_REGISTRY_SHA256 = "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179"
AMENDMENT_E_RESOURCE_SHA256 = "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09"
UNKNOWN_SHA256 = hashlib.sha256(b"not a trusted generation").hexdigest()
HEX_64 = re.compile(r"[0-9a-f]{64}")


def generations():
    return importlib.import_module("src.analysis.open_intelligence.execution_generations")


def refusal(code):
    return pytest.raises(execution_origins.OriginRefusal, match=f"^{code}$")


def temp_root(tmp_path, monkeypatch):
    """Copy the packaged configuration into a private root the catalogue reads from."""
    root = tmp_path / "engine"
    shutil.copytree(ENGINE_ROOT / "configs/open_intelligence", root / "configs/open_intelligence")
    monkeypatch.setattr(generations(), "_PACKAGE_ROOT", root)
    return root


def point_catalogue_at(monkeypatch, registry_sha, resource_sha):
    module = generations()
    entry = (registry_sha, resource_sha, REGISTRY_RELATIVE, RESOURCE_RELATIVE)
    monkeypatch.setattr(module, "_TRUSTED_GENERATIONS", (entry,))
    monkeypatch.setattr(module, "ACTIVE_GENERATION_PAIR", (registry_sha, resource_sha))


def write_resource(root, payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (root / RESOURCE_RELATIVE).write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def test_load_trusted_generation_refuses_unknown_pair():
    """Successor 1: 'An unknown pair, missing annotation, changed job template or wrong
    record generation refuses.' Successor 2: 'Unknown catalogue pair refuses even if a
    native table accepted it.'"""
    module = generations()
    with refusal("execution_generation_unknown"):
        module.load_trusted_generation(REGISTRY_SHA256, UNKNOWN_SHA256)
    with refusal("execution_generation_unknown"):
        module.load_trusted_generation(UNKNOWN_SHA256, RESOURCE_SHA256)
    with refusal("execution_generation_unknown"):
        module.load_trusted_generation(RESOURCE_SHA256, REGISTRY_SHA256)


@pytest.mark.parametrize(
    "registry_sha, resource_sha",
    [
        (REGISTRY_SHA256.upper(), RESOURCE_SHA256),
        (REGISTRY_SHA256, RESOURCE_SHA256.upper()),
        (REGISTRY_SHA256[:63], RESOURCE_SHA256),
        (REGISTRY_SHA256, RESOURCE_SHA256 + "0"),
        (REGISTRY_SHA256.encode(), RESOURCE_SHA256),
        (REGISTRY_SHA256, None),
        (" " + REGISTRY_SHA256, RESOURCE_SHA256),
    ],
)
def test_load_trusted_generation_requires_lowercase_hex_grammar(registry_sha, resource_sha):
    """Successor 1: 'Both are lowercase 64-hex.' A selector that fails the grammar never
    reaches the catalogue."""
    with refusal("execution_generation_unknown"):
        generations().load_trusted_generation(registry_sha, resource_sha)


def test_active_generation_loads_the_packaged_pair():
    """Successor 1: the catalogue carries 'a separately named single active pair for fresh
    approval and consumption' and 'Paths are fixed package-relative files'."""
    module = generations()
    generation = module.active_generation()
    assert type(generation) is module.TrustedGeneration
    assert generation.origin_registry_sha256 == REGISTRY_SHA256
    assert generation.resource_manifest_sha256 == RESOURCE_SHA256
    assert type(generation.registry) is execution_origins.OriginRegistry
    assert generation.registry.sha256 == REGISTRY_SHA256
    assert generation.resource_manifest["origin_registry_sha256"] == REGISTRY_SHA256
    assert generation.registry_path == ENGINE_ROOT / REGISTRY_RELATIVE
    assert generation.resource_manifest_path == ENGINE_ROOT / RESOURCE_RELATIVE
    assert module.ACTIVE_GENERATION_PAIR == (REGISTRY_SHA256, RESOURCE_SHA256)
    same = module.load_trusted_generation(REGISTRY_SHA256, RESOURCE_SHA256)
    assert same == generation


def test_catalogue_is_an_immutable_tuple_of_literal_digests_without_placeholders():
    """Successor 1: 'an immutable source-controlled tuple of entries (origin_registry_sha256,
    resource_manifest_sha256, registry_relative_path, resource_manifest_relative_path)' and
    'Do not create a placeholder digest.'"""
    module = generations()
    catalogue = module._TRUSTED_GENERATIONS
    assert type(catalogue) is tuple
    assert catalogue
    pairs = []
    for entry in catalogue:
        assert type(entry) is tuple
        assert len(entry) == 4
        registry_sha, resource_sha, registry_relative, resource_relative = entry
        for digest in (registry_sha, resource_sha):
            assert HEX_64.fullmatch(digest) is not None
            assert len(set(digest)) > 1
        assert registry_relative.startswith("configs/open_intelligence/")
        assert resource_relative.startswith("configs/open_intelligence/")
        assert (
            hashlib.sha256((ENGINE_ROOT / registry_relative).read_bytes()).hexdigest()
            == registry_sha
        )
        assert (
            hashlib.sha256((ENGINE_ROOT / resource_relative).read_bytes()).hexdigest()
            == resource_sha
        )
        pairs.append((registry_sha, resource_sha))
    assert len(pairs) == len(set(pairs))
    assert module.ACTIVE_GENERATION_PAIR in pairs
    assert type(module.ACTIVE_GENERATION_PAIR) is tuple


def test_packaged_resource_manifest_is_the_reviewed_bytes():
    """Successor 1: 'Missing active concrete resource bytes means no native v2 authority can
    be issued.' The packaged copy is byte-identical to the reviewed proposal digest."""
    raw = (ENGINE_ROOT / RESOURCE_RELATIVE).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == RESOURCE_SHA256
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert json.loads(raw)["origin_registry_sha256"] == REGISTRY_SHA256


def test_loader_signature_takes_only_the_two_digests():
    """Successor 1: 'Request bodies and general process environment variables cannot select
    paths, expected digests or generations.'"""
    module = generations()
    assert tuple(inspect.signature(module.load_trusted_generation).parameters) == (
        "origin_registry_sha256",
        "resource_manifest_sha256",
    )
    assert tuple(inspect.signature(module.active_generation).parameters) == ()
    assert tuple(inspect.signature(module.require_active_generation).parameters) == (
        "origin_registry_sha256",
        "resource_manifest_sha256",
    )


def test_trusted_generation_is_frozen_and_its_manifest_is_read_only():
    module = generations()
    generation = module.active_generation()
    with pytest.raises(dataclasses.FrozenInstanceError):
        generation.origin_registry_sha256 = UNKNOWN_SHA256
    assert isinstance(generation.resource_manifest, MappingProxyType)
    with pytest.raises(TypeError):
        generation.resource_manifest["resources"] = ()
    assert type(generation.resource_manifest["resources"]) is tuple
    assert all(
        isinstance(row, MappingProxyType) and type(row["actions"]) is tuple
        for row in generation.resource_manifest["resources"]
    )


def test_require_active_generation_refuses_a_retained_but_inactive_pair(monkeypatch):
    """Successor 1: 'New approval and consumption require the active pair' and 'Rotation does
    not reinterpret older v2 records under the active pair.'"""
    module = generations()
    monkeypatch.setattr(module, "ACTIVE_GENERATION_PAIR", (REGISTRY_SHA256, UNKNOWN_SHA256))
    assert module.load_trusted_generation(REGISTRY_SHA256, RESOURCE_SHA256)
    with refusal("execution_generation_inactive"):
        module.require_active_generation(REGISTRY_SHA256, RESOURCE_SHA256)
    with refusal("execution_generation_unknown"):
        module.require_active_generation(UNKNOWN_SHA256, RESOURCE_SHA256)
    with refusal("execution_generation_unknown"):
        module.active_generation()


def test_registry_bytes_are_rehashed_against_the_literal_digest(tmp_path, monkeypatch):
    """Successor 1: 'Each file is bounded, regular, nonlinked and checked against its literal
    expected digest. Registry admission uses the existing sealed loader.'"""
    root = temp_root(tmp_path, monkeypatch)
    registry_path = root / REGISTRY_RELATIVE
    registry_path.write_bytes(registry_path.read_bytes() + b"\n")
    with refusal("execution_origin_registry_digest_mismatch"):
        generations().active_generation()


def test_resource_bytes_are_rehashed_against_the_literal_digest(tmp_path, monkeypatch):
    root = temp_root(tmp_path, monkeypatch)
    resource_path = root / RESOURCE_RELATIVE
    resource_path.write_bytes(resource_path.read_bytes() + b" ")
    with refusal("execution_generation_digest_mismatch"):
        generations().active_generation()


def test_resource_manifest_must_reference_the_selected_registry(tmp_path, monkeypatch):
    """Successor 1: 'Resource admission reuses the existing resource guard and verifies its
    origin_registry_sha256 equals the selected registry.'"""
    root = temp_root(tmp_path, monkeypatch)
    payload = json.loads((root / RESOURCE_RELATIVE).read_bytes())
    payload["origin_registry_sha256"] = UNKNOWN_SHA256
    resource_sha = write_resource(root, payload)
    point_catalogue_at(monkeypatch, REGISTRY_SHA256, resource_sha)
    with refusal("execution_generation_invalid"):
        generations().active_generation()


@pytest.mark.parametrize(
    "mutation",
    ["missing", "directory", "oversized", "empty", "malformed", "duplicate_key", "not_object"],
)
def test_resource_file_faults_refuse(tmp_path, monkeypatch, mutation):
    root = temp_root(tmp_path, monkeypatch)
    resource_path = root / RESOURCE_RELATIVE
    if mutation == "missing":
        resource_path.unlink()
    elif mutation == "directory":
        resource_path.unlink()
        resource_path.mkdir()
    elif mutation == "oversized":
        raw = resource_path.read_bytes()
        resource_path.write_bytes(raw + b" " * (1024 * 1024 + 1 - len(raw)))
        digest = hashlib.sha256(resource_path.read_bytes()).hexdigest()
        point_catalogue_at(monkeypatch, REGISTRY_SHA256, digest)
    elif mutation == "empty":
        resource_path.write_bytes(b"")
        point_catalogue_at(monkeypatch, REGISTRY_SHA256, hashlib.sha256(b"").hexdigest())
    elif mutation == "malformed":
        resource_path.write_bytes(b"{")
        point_catalogue_at(monkeypatch, REGISTRY_SHA256, hashlib.sha256(b"{").hexdigest())
    elif mutation == "duplicate_key":
        raw = (
            b'{"origin_registry_sha256":"' + REGISTRY_SHA256.encode() + b'",'
            b'"origin_registry_sha256":"' + REGISTRY_SHA256.encode() + b'","resources":[]}'
        )
        resource_path.write_bytes(raw)
        point_catalogue_at(monkeypatch, REGISTRY_SHA256, hashlib.sha256(raw).hexdigest())
    elif mutation == "not_object":
        raw = b"[]"
        resource_path.write_bytes(raw)
        point_catalogue_at(monkeypatch, REGISTRY_SHA256, hashlib.sha256(raw).hexdigest())
    with refusal("execution_generation_invalid"):
        generations().active_generation()


def test_linked_resource_file_refuses(tmp_path, monkeypatch):
    temp_root(tmp_path, monkeypatch)
    original = Path.is_symlink

    def linked(self):
        return self.name == Path(RESOURCE_RELATIVE).name or original(self)

    monkeypatch.setattr(Path, "is_symlink", linked)
    with refusal("execution_generation_invalid"):
        generations().active_generation()


def test_resource_shape_faults_refuse(tmp_path, monkeypatch):
    root = temp_root(tmp_path, monkeypatch)
    payload = json.loads((root / RESOURCE_RELATIVE).read_bytes())
    for change in (
        lambda value: value.__setitem__("resources", {}),
        lambda value: value["resources"].append({"name": "x", "actions": ["read"], "extra": 1}),
        lambda value: value["resources"].append({"name": 1, "actions": ["read"]}),
        lambda value: value["resources"].append({"name": "//x/y", "actions": "read"}),
        lambda value: value["resources"].append({"name": "//x/y", "actions": [1]}),
        lambda value: value.__setitem__("identities", []),
        lambda value: value["identities"].__setitem__("app", 1),
        lambda value: value.__setitem__("project", 1),
        lambda value: value.__setitem__("region", 1),
        lambda value: value.pop("region"),
        lambda value: value.pop("resources"),
    ):
        mutated = json.loads(json.dumps(payload))
        change(mutated)
        resource_sha = write_resource(root, mutated)
        point_catalogue_at(monkeypatch, REGISTRY_SHA256, resource_sha)
        with refusal("execution_generation_invalid"):
            generations().active_generation()


SECRET = (
    "//secretmanager.googleapis.com/projects/ogilvy-trends-v2/secrets/"
    "SOCIALCRAWL_OGILVY_API_KEY/versions/1"
)
JOB = "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-apply-staging"
DATASET = "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/trends_v2_staging"
MEMBERSHIP_VECTORS = (
    (SECRET, "read", True),
    (JOB, "invoke", True),
    (JOB, "deploy", True),
    (DATASET, "write", True),
    (SECRET, "write", False),
    (SECRET, "invoke", False),
    (SECRET.rsplit("/versions/", 1)[0], "read", False),
    (SECRET.replace("/versions/1", "/versions/latest"), "read", False),
    (SECRET.replace("/versions/1", "/versions/10"), "read", False),
    (SECRET.replace("/versions/1", "/versions/01"), "read", False),
    (SECRET + "/", "read", False),
    (SECRET.replace("SOCIALCRAWL", "socialcrawl"), "read", False),
    (JOB[:-8], "invoke", False),
    (JOB + "-2", "invoke", False),
    (JOB, "write", False),
    (JOB, "execute", False),
    (DATASET + "_funded", "read", True),
    (DATASET + "_qa_missing", "read", False),
    (" " + JOB, "invoke", False),
    (JOB.replace("//", "/"), "invoke", False),
    (None, "read", False),
    (JOB, None, False),
    (b"" + JOB.encode(), "invoke", False),
)


def test_resource_membership_parity_with_the_reviewed_guard():
    """Successor 1: 'Deployment/runtime use the existing exact-membership guard for static
    resources it supports' with 'no ... duplicated independent guard implementation'. The
    engine image cannot import ops, so the engine keeps only the exact membership check and
    this test proves both accept and refuse the same vectors."""
    guard_path = ENGINE_ROOT.parent / "ops/deploy/resource_guard.py"
    if not guard_path.is_file():
        pytest.skip("monorepo sibling tree is not present")
    spec = importlib.util.spec_from_file_location("reviewed_resource_guard", guard_path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    reviewed = guard.load_resource_manifest(
        ENGINE_ROOT / RESOURCE_RELATIVE, expected_sha256=RESOURCE_SHA256
    )
    module = generations()
    generation = module.active_generation()
    checks = (
        lambda resource, action: guard.assert_allowed(resource, action, reviewed),
        lambda resource, action: module.assert_resource_allowed(
            resource, action, generation=generation
        ),
    )
    for resource, action, allowed in MEMBERSHIP_VECTORS:
        for check in checks:
            if allowed:
                assert check(resource, action) is None
            else:
                with pytest.raises(ValueError, match=r"^resource_action_forbidden$"):
                    check(resource, action)


def test_membership_check_requires_a_trusted_generation():
    module = generations()
    with pytest.raises(ValueError, match=r"^resource_action_forbidden$"):
        module.assert_resource_allowed(JOB, "invoke", generation=None)
    with pytest.raises(ValueError, match=r"^resource_action_forbidden$"):
        module.assert_resource_allowed(JOB, "invoke", generation={"resources": [{"name": JOB}]})


# Amendment d is live and the ledger ran under its pair, so amendment e keeps that
# generation trusted, at its own retained copy of the reviewed bytes. The bridge
# generation keeps amendment e trusted too and makes the bridge pair the only active one.
AMENDMENT_D_RESOURCE_SHA256 = "ee809a4e81dec5242ea71ddd703990c5e0a9e237613e11135e069be0ea51ad96"
AMENDMENT_D_RESOURCE_RELATIVE = "configs/open_intelligence/resource_manifest_amendment_d.json"
AMENDMENT_E_ROWS = {
    "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/"
    "trends_v2_staging_approvals/routines/sp_reconcile_open_intelligence_daily_consumption_v1",
    "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/"
    "trends_v2_staging_approvals/routines/sp_consume_open_intelligence_source_snapshot_v3",
    "//cloudbuild.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/builds",
    *(
        "//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/"
        f"trends_v2_staging_approvals/tables/open_intelligence_execution_{name}_v2"
        for name in ("approvals", "consumptions", "results")
    ),
}


def test_the_catalogue_retains_amendments_d_and_e_and_activates_the_bridge():
    module = generations()
    assert module._TRUSTED_GENERATIONS == (
        (
            AMENDMENT_E_REGISTRY_SHA256,
            AMENDMENT_D_RESOURCE_SHA256,
            AMENDMENT_E_REGISTRY_RELATIVE,
            AMENDMENT_D_RESOURCE_RELATIVE,
        ),
        (
            AMENDMENT_E_REGISTRY_SHA256,
            AMENDMENT_E_RESOURCE_SHA256,
            AMENDMENT_E_REGISTRY_RELATIVE,
            AMENDMENT_E_RESOURCE_RELATIVE,
        ),
        # The bridge v3 generation, re-derived from the amendment e manifest and active.
        (REGISTRY_SHA256, RESOURCE_SHA256, REGISTRY_RELATIVE, RESOURCE_RELATIVE),
    )
    assert module.ACTIVE_GENERATION_PAIR == (REGISTRY_SHA256, RESOURCE_SHA256)


def test_the_amendment_d_generation_still_loads_from_its_retained_bytes():
    module = generations()
    retained = module.load_trusted_generation(
        AMENDMENT_E_REGISTRY_SHA256, AMENDMENT_D_RESOURCE_SHA256
    )
    assert retained.resource_manifest_sha256 == AMENDMENT_D_RESOURCE_SHA256
    assert retained.resource_manifest_path == ENGINE_ROOT / AMENDMENT_D_RESOURCE_RELATIVE
    raw = (ENGINE_ROOT / AMENDMENT_D_RESOURCE_RELATIVE).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == AMENDMENT_D_RESOURCE_SHA256
    old = {row["name"]: row for row in retained.resource_manifest["resources"]}
    amendment_e = module.load_trusted_generation(
        AMENDMENT_E_REGISTRY_SHA256, AMENDMENT_E_RESOURCE_SHA256
    )
    assert amendment_e.resource_manifest_path == ENGINE_ROOT / AMENDMENT_E_RESOURCE_RELATIVE
    new = {row["name"]: row for row in amendment_e.resource_manifest["resources"]}
    assert set(new) - set(old) == AMENDMENT_E_ROWS
    assert set(old) <= set(new)
    assert all(new[name] == row for name, row in old.items())
    # The active bridge manifest carries exactly the amendment e rows and the one row
    # amendment g added, the route A capture's artifact bucket.
    active = module.active_generation().resource_manifest["resources"]
    active_rows = {row["name"]: row for row in active}
    assert set(active_rows) - set(new) == {ARTIFACT_BUCKET}
    assert active_rows[ARTIFACT_BUCKET] == {"actions": ("read", "write"), "name": ARTIFACT_BUCKET}
    del active_rows[ARTIFACT_BUCKET]
    assert active_rows == new


def test_fresh_use_of_the_amendment_d_and_e_pairs_refuses_as_inactive():
    module = generations()
    with refusal("execution_generation_inactive"):
        module.require_active_generation(AMENDMENT_E_REGISTRY_SHA256, AMENDMENT_D_RESOURCE_SHA256)
    with refusal("execution_generation_inactive"):
        module.require_active_generation(AMENDMENT_E_REGISTRY_SHA256, AMENDMENT_E_RESOURCE_SHA256)
    active = module.require_active_generation(REGISTRY_SHA256, RESOURCE_SHA256)
    assert active.resource_manifest_sha256 == RESOURCE_SHA256
    assert module.active_generation().resource_manifest_sha256 == RESOURCE_SHA256
