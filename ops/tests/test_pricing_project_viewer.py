import hashlib
import importlib.util
import json
import copy
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter

import pytest


TEST_ROOT = Path(__file__).parent
HELPER_PATH = TEST_ROOT.parent / "deploy" / "pricing_project_viewer.py"
FIXTURES = TEST_ROOT / "fixtures" / "pricing_project_viewer"
PREIMAGE_PATH = FIXTURES / "project-policy.json"
PROJECT_PATH = FIXTURES / "project.json"
V3_PREIMAGE_PATH = FIXTURES / "project-policy-v3.json"
PREIMAGE_SHA256 = "d6facb445b5cdb902f8cce8ab28671c7b31ae9047a590634abcbbd5d142e679e"
PROJECT_SHA256 = "0bc9285a6a38658e0780ac7c85521ce6ee0212d939ccd3e7bbb3c992b454f959"
V3_PREIMAGE_SHA256 = "d6facb445b5cdb902f8cce8ab28671c7b31ae9047a590634abcbbd5d142e679e"


def load_helper():
    assert HELPER_PATH.is_file(), "project viewer helper has not been implemented"
    spec = importlib.util.spec_from_file_location("project_viewer_iam", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan_adds_only_the_requested_project_viewer_binding():
    raw = PREIMAGE_PATH.read_bytes()
    descriptor_raw = PROJECT_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PREIMAGE_SHA256
    assert hashlib.sha256(descriptor_raw).hexdigest() == PROJECT_SHA256
    policy = json.loads(raw.decode("utf-8"))
    descriptor = json.loads(descriptor_raw.decode("utf-8"))

    plan = load_helper().build_plan(
        policy,
        PREIMAGE_SHA256,
        descriptor,
        PROJECT_SHA256,
    )

    request = plan["set_request"]
    after = request["policy"]
    assert plan["resource"] == "projects/590353929363"
    assert plan["role"] == "roles/run.viewer"
    assert plan["member"] == "serviceAccount:intelligence-42-price-policy@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert request["updateMask"] == "bindings,etag"
    assert after["version"] == 3
    assert after["etag"] == "BwSyntheticPolicyEtag="
    assert after["bindings"][:-1] == policy["bindings"]
    assert after["bindings"][-1] == {
        "role": "roles/run.viewer",
        "members": ["serviceAccount:intelligence-42-price-policy@ogilvy-trends-v2.iam.gserviceaccount.com"],
    }
    assert len(after["bindings"]) == 37
    assert "auditConfigs" not in after


def test_plan_uses_only_local_inputs_and_writes_a_pinned_plan(tmp_path):
    helper = load_helper()
    preimage = tmp_path / "project-policy.json"
    descriptor = tmp_path / "project.json"
    preimage.write_bytes(PREIMAGE_PATH.read_bytes())
    descriptor.write_bytes(PROJECT_PATH.read_bytes())

    result = helper.plan(
        tmp_path,
        preimage,
        PREIMAGE_SHA256,
        descriptor,
        PROJECT_SHA256,
    )

    plan_raw = (tmp_path / "plan.json").read_bytes()
    plan = json.loads(plan_raw.decode("utf-8"))
    assert hashlib.sha256(plan_raw).hexdigest() == result["plan_sha256"]
    assert result["resource"] == "projects/590353929363"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["plan.json", "project-policy.json", "project.json"]
    assert plan["set_request"]["policy"]["bindings"][-1] == {
        "role": "roles/run.viewer",
        "members": ["serviceAccount:intelligence-42-price-policy@ogilvy-trends-v2.iam.gserviceaccount.com"],
    }


def test_plan_preserves_audit_configuration_and_all_existing_conditions():
    policy = json.loads(PREIMAGE_PATH.read_text(encoding="utf-8"))
    policy["auditConfigs"] = [
        {
            "service": "allServices",
            "auditLogConfigs": [
                {"logType": "ADMIN_READ"},
                {"logType": "DATA_READ", "exemptedMembers": ["user:auditor@example.com"]},
            ],
        }
    ]
    before = copy.deepcopy(policy)
    descriptor = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
    preimage_sha256 = hashlib.sha256(json_bytes(policy)).hexdigest()

    plan = load_helper().build_plan(
        policy,
        preimage_sha256,
        descriptor,
        PROJECT_SHA256,
    )

    after = plan["set_request"]["policy"]
    assert after["bindings"][:-1] == before["bindings"]
    assert sum("condition" in binding for binding in after["bindings"][:-1]) == 3
    assert after["auditConfigs"] == before["auditConfigs"]


def test_plan_refuses_a_different_project_number():
    policy = json.loads(PREIMAGE_PATH.read_text(encoding="utf-8"))
    descriptor = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
    descriptor["projectNumber"] = "123"
    descriptor_sha256 = hashlib.sha256(json_bytes(descriptor)).hexdigest()

    with pytest.raises(RuntimeError, match="project_number_mismatch"):
        load_helper().build_plan(policy, PREIMAGE_SHA256, descriptor, descriptor_sha256)


def test_plan_refuses_a_different_project_id():
    policy = json.loads(PREIMAGE_PATH.read_text(encoding="utf-8"))
    descriptor = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
    descriptor["projectId"] = "other-project"
    descriptor_sha256 = hashlib.sha256(json_bytes(descriptor)).hexdigest()

    with pytest.raises(RuntimeError, match="project_id_mismatch"):
        load_helper().build_plan(policy, PREIMAGE_SHA256, descriptor, descriptor_sha256)


def test_plan_refuses_an_existing_viewer_binding():
    policy = json.loads(PREIMAGE_PATH.read_text(encoding="utf-8"))
    policy["bindings"].append({"role": "roles/run.viewer", "members": ["user:other@example.com"]})
    descriptor = json.loads(PROJECT_PATH.read_text(encoding="utf-8"))
    preimage_sha256 = hashlib.sha256(json_bytes(policy)).hexdigest()

    with pytest.raises(RuntimeError, match="run_viewer_binding_already_present"):
        load_helper().build_plan(policy, preimage_sha256, descriptor, PROJECT_SHA256)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def write_case(root, policy=None):
    helper = load_helper()
    if policy is None:
        preimage_raw = PREIMAGE_PATH.read_bytes()
        policy = json.loads(preimage_raw.decode("utf-8"))
    else:
        preimage_raw = json_bytes(policy)
    descriptor_raw = PROJECT_PATH.read_bytes()
    preimage = root / "project-policy.json"
    descriptor_path = root / "project.json"
    plan_path = root / "plan.json"
    approval_manifest = root / "approval-manifest.json"
    brief = root / "brief.md"
    preimage.write_bytes(preimage_raw)
    descriptor_path.write_bytes(descriptor_raw)
    case = {
        "root": root,
        "preimage": preimage,
        "preimage_sha256": hashlib.sha256(preimage_raw).hexdigest(),
        "project_descriptor": descriptor_path,
        "project_descriptor_sha256": hashlib.sha256(descriptor_raw).hexdigest(),
        "plan_path": plan_path,
        "approval_manifest": approval_manifest,
        "brief": brief,
        "now": datetime(2026, 9, 28, 19, 59, tzinfo=timezone.utc),
    }
    plan = helper.build_plan(
        policy,
        case["preimage_sha256"],
        json.loads(descriptor_raw.decode("utf-8")),
        case["project_descriptor_sha256"],
    )
    refresh_approval(case, plan)
    return case


def refresh_approval(case, plan):
    plan_raw = json_bytes(plan)
    case["plan_path"].write_bytes(plan_raw)
    case["plan_sha256"] = hashlib.sha256(plan_raw).hexdigest()
    helper_sha256 = hashlib.sha256(HELPER_PATH.read_bytes()).hexdigest()
    manifest = {
        "contract": "42_pricing_project_viewer_approval_v1",
        "preimage_sha256": case["preimage_sha256"],
        "plan_sha256": case["plan_sha256"],
        "helper_sha256": helper_sha256,
    }
    manifest_raw = json_bytes(manifest)
    case["approval_manifest"].write_bytes(manifest_raw)
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    brief_raw = ("pricing_project_viewer_approval_manifest_sha256=" + manifest_sha256 + "\n").encode("utf-8")
    case["brief"].write_bytes(brief_raw)
    case["brief_sha256"] = hashlib.sha256(brief_raw).hexdigest()
    (case["root"] / "go.json").write_bytes(
        json_bytes(
            {
                "brief_sha256": case["brief_sha256"],
                "approval_manifest_sha256": manifest_sha256,
                "go_received_at": "2026-09-28T19:58:00Z",
            }
        )
    )


class FakeAdapter:
    def __init__(self, policy, set_status=200, apply_set=True):
        self.current = copy.deepcopy(policy)
        self.set_status = set_status
        self.apply_set = apply_set
        self.calls = []
        self.token_calls = 0
        self.set_count = 0
        self.next_etag = "BwSyntheticPolicyEtag-NEW"

    def token(self):
        self.token_calls += 1
        return "test-token"

    def transport(self, request):
        self.calls.append(copy.deepcopy(request))
        if request["url"] == "https://cloudresourcemanager.googleapis.com/v3/projects/590353929363:getIamPolicy":
            return {"status": 200, "url": request["url"], "body": json_bytes(self.current)}
        if request["url"] == "https://cloudresourcemanager.googleapis.com/v3/projects/590353929363:setIamPolicy":
            self.apply_set_request(request)
            answer = self.current if self.set_status == 200 else {"error": "gateway_timeout"}
            return {"status": self.set_status, "url": request["url"], "body": json_bytes(answer)}
        pytest.fail("transport received an unapproved endpoint")

    def apply_set_request(self, request):
        body = json.loads(request["body"].decode("utf-8"))
        self.set_count += 1
        if self.apply_set:
            policy = body["policy"]
            assert policy["etag"] == self.current["etag"]
            assert body["updateMask"] == "bindings,etag"
            updated = copy.deepcopy(self.current)
            updated["bindings"] = copy.deepcopy(policy["bindings"])
            updated["etag"] = self.next_etag
            self.current = updated


def apply_case(case, adapter, now=None, clock=None):
    clock = clock or (lambda: now or case["now"])
    return load_helper().apply(
        case["root"],
        case["preimage"],
        case["preimage_sha256"],
        case["project_descriptor"],
        case["project_descriptor_sha256"],
        case["plan_sha256"],
        case["approval_manifest"],
        case["brief"],
        case["brief_sha256"],
        adapter=adapter,
        clock=clock,
    )


def test_apply_sends_v3_full_policy_cas_and_reads_back_every_field(tmp_path):
    policy = json.loads(PREIMAGE_PATH.read_text(encoding="utf-8"))
    policy["auditConfigs"] = [
        {"service": "allServices", "auditLogConfigs": [{"logType": "ADMIN_READ"}]}
    ]
    case = write_case(tmp_path, policy)
    adapter = FakeAdapter(policy)

    result = apply_case(case, adapter)

    assert result["verified"] is True
    assert result["before_etag"] == "BwSyntheticPolicyEtag="
    assert result["after_etag"] == adapter.next_etag
    set_calls = [call for call in adapter.calls if call["url"].endswith(":setIamPolicy")]
    assert len(set_calls) == 1
    assert set_calls[0]["method"] == "POST"
    assert set_calls[0]["url"] == "https://cloudresourcemanager.googleapis.com/v3/projects/590353929363:setIamPolicy"
    assert set_calls[0]["headers"]["Content-Type"] == "application/json"
    set_body = json.loads(set_calls[0]["body"].decode("utf-8"))
    assert set_body["updateMask"] == "bindings,etag"
    assert set_body["policy"]["etag"] == "BwSyntheticPolicyEtag="
    assert set_body["policy"]["bindings"][:-1] == policy["bindings"]
    assert set_body["policy"]["bindings"][-1] == {
        "role": "roles/run.viewer",
        "members": ["serviceAccount:intelligence-42-price-policy@ogilvy-trends-v2.iam.gserviceaccount.com"],
    }
    assert set_body["policy"]["auditConfigs"] == policy["auditConfigs"]
    assert adapter.current["auditConfigs"] == policy["auditConfigs"]
    assert adapter.current["bindings"] == set_body["policy"]["bindings"]
    get_calls = [call for call in adapter.calls if call["url"].endswith(":getIamPolicy")]
    assert len(get_calls) == 3
    assert all(call["method"] == "POST" for call in get_calls)
    assert all(
        json.loads(call["body"].decode("utf-8")) == {"options": {"requestedPolicyVersion": 3}}
        for call in get_calls
    )


def test_apply_preserves_all_synthetic_preimage_bindings_and_conditions_byte_for_byte(tmp_path):
    policy = json.loads(PREIMAGE_PATH.read_text(encoding="utf-8"))
    case = write_case(tmp_path)
    adapter = FakeAdapter(policy)

    apply_case(case, adapter)

    write_request = next(call for call in adapter.calls if call["url"].endswith(":setIamPolicy"))
    outgoing = json.loads(write_request["body"].decode("utf-8"))["policy"]
    readback = adapter.current
    encode_bindings = lambda bindings: Counter(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
        for binding in bindings
    )
    new_binding = {
        "role": "roles/run.viewer",
        "members": ["serviceAccount:intelligence-42-price-policy@ogilvy-trends-v2.iam.gserviceaccount.com"],
    }
    expected_bindings = encode_bindings(policy["bindings"]) + Counter(
        [json.dumps(new_binding, sort_keys=True, separators=(",", ":")).encode("utf-8")]
    )
    assert len(policy["bindings"]) == 36
    assert sum("condition" in binding for binding in policy["bindings"]) == 3
    assert outgoing["version"] == 3
    assert outgoing["etag"] == "BwSyntheticPolicyEtag="
    assert encode_bindings(outgoing["bindings"]) == expected_bindings
    assert encode_bindings(readback["bindings"]) == expected_bindings
    assert sum("condition" in binding for binding in outgoing["bindings"]) == 3
    assert sum("condition" in binding for binding in readback["bindings"]) == 3
    assert readback["version"] == 3
    assert readback["etag"] == adapter.next_etag


@pytest.mark.parametrize(
    ("case_name", "mutate"),
    [
        ("different_project", lambda plan: plan.update(resource="projects/123")),
        (
            "service_resource",
            lambda plan: plan.update(resource="projects/590353929363/locations/us-central1/services/price-policy"),
        ),
        (
            "role_tamper",
            lambda plan: (plan.update(role="roles/viewer"), plan["set_request"]["policy"]["bindings"][-1].update(role="roles/viewer")),
        ),
        (
            "member_tamper",
            lambda plan: (plan.update(member="serviceAccount:other@example-project.iam.gserviceaccount.com"), plan["set_request"]["policy"]["bindings"][-1].update(members=["serviceAccount:other@example-project.iam.gserviceaccount.com"])),
        ),
        (
            "existing_member_tamper",
            lambda plan: plan["set_request"]["policy"]["bindings"][0]["members"].__setitem__(0, "user:changed@example.com"),
        ),
        (
            "existing_condition_tamper",
            lambda plan: next(binding for binding in plan["set_request"]["policy"]["bindings"] if "condition" in binding)["condition"].update(expression="false"),
        ),
        ("version_tamper", lambda plan: plan["set_request"]["policy"].update(version=1)),
        ("etag_tamper", lambda plan: plan["set_request"]["policy"].update(etag="changed-etag")),
    ],
)
def test_apply_refuses_tampered_project_role_or_member_before_any_request(tmp_path, case_name, mutate):
    case_root = tmp_path / case_name
    case_root.mkdir()
    case = write_case(case_root)
    plan = json.loads(case["plan_path"].read_text(encoding="utf-8"))
    mutate(plan)
    refresh_approval(case, plan)
    adapter = FakeAdapter(json.loads(case["preimage"].read_text(encoding="utf-8")))

    with pytest.raises(RuntimeError, match="plan_no_longer_matches_preimage"):
        apply_case(case, adapter)

    assert adapter.calls == []
    assert adapter.token_calls == 0


def test_apply_refuses_missing_approval_before_any_request(tmp_path):
    case = write_case(tmp_path)
    case["approval_manifest"].unlink()
    adapter = FakeAdapter(json.loads(case["preimage"].read_text(encoding="utf-8")))

    with pytest.raises(FileNotFoundError):
        apply_case(case, adapter)

    assert adapter.calls == []
    assert adapter.token_calls == 0


def test_apply_refuses_missing_go_before_any_request(tmp_path):
    case = write_case(tmp_path)
    (tmp_path / "go.json").unlink()
    adapter = FakeAdapter(json.loads(case["preimage"].read_text(encoding="utf-8")))

    with pytest.raises(FileNotFoundError):
        apply_case(case, adapter)

    assert adapter.calls == []
    assert adapter.token_calls == 0


def test_apply_refuses_changed_preimage_before_any_request(tmp_path):
    case = write_case(tmp_path)
    changed = json.loads(case["preimage"].read_text(encoding="utf-8"))
    changed["etag"] = "stale"
    case["preimage"].write_bytes(json_bytes(changed))
    adapter = FakeAdapter(changed)

    with pytest.raises(RuntimeError, match="preimage_changed"):
        apply_case(case, adapter)

    assert adapter.calls == []
    assert adapter.token_calls == 0


def test_apply_refuses_after_20z_before_any_request(tmp_path):
    case = write_case(tmp_path)
    adapter = FakeAdapter(json.loads(case["preimage"].read_text(encoding="utf-8")))

    with pytest.raises(RuntimeError, match="write_clock_gate_refused"):
        apply_case(case, adapter, datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc))

    assert adapter.calls == []
    assert adapter.token_calls == 0


def test_apply_refuses_stale_live_etag_before_any_policy_write(tmp_path):
    case = write_case(tmp_path)
    live = json.loads(case["preimage"].read_text(encoding="utf-8"))
    live["etag"] = "changed-etag"
    adapter = FakeAdapter(live)

    with pytest.raises(RuntimeError, match="project_iam_changed_since_review"):
        apply_case(case, adapter)

    assert len(adapter.calls) == 1
    assert all(not call["url"].endswith(":setIamPolicy") for call in adapter.calls)


def test_apply_skips_set_when_fresh_policy_already_has_requested_member(tmp_path):
    case = write_case(tmp_path)
    policy = json.loads(case["preimage"].read_text(encoding="utf-8"))
    adapter = FakeAdapter(policy)
    original_transport = adapter.transport
    read_count = 0
    new_binding = {
        "role": "roles/run.viewer",
        "members": ["serviceAccount:intelligence-42-price-policy@ogilvy-trends-v2.iam.gserviceaccount.com"],
    }

    def transport(request):
        nonlocal read_count
        response = original_transport(request)
        if request["url"].endswith(":getIamPolicy"):
            read_count += 1
            if read_count == 1:
                adapter.current = copy.deepcopy(policy)
                adapter.current["bindings"].append(copy.deepcopy(new_binding))
                adapter.current["etag"] = adapter.next_etag
        return response

    adapter.transport = transport
    result = apply_case(case, adapter)

    assert result["verified"] is True
    assert result["no_op"] is True
    assert read_count == 2
    assert adapter.set_count == 0
    assert len([call for call in adapter.calls if call["url"].endswith(":setIamPolicy")]) == 0
    assert new_binding in adapter.current["bindings"]


def test_apply_refuses_ambiguous_write_response_without_retry(tmp_path):
    case = write_case(tmp_path)
    policy = json.loads(case["preimage"].read_text(encoding="utf-8"))
    adapter = FakeAdapter(policy, set_status=504, apply_set=True)

    with pytest.raises(RuntimeError, match="write_response_lost_stop"):
        apply_case(case, adapter)

    assert adapter.set_count == 1
    assert len([call for call in adapter.calls if call["url"].endswith(":setIamPolicy")]) == 1
    assert len([call for call in adapter.calls if call["url"].endswith(":getIamPolicy")]) == 3
    assert adapter.current["bindings"][-1] == {
        "role": "roles/run.viewer",
        "members": ["serviceAccount:intelligence-42-price-policy@ogilvy-trends-v2.iam.gserviceaccount.com"],
    }


def test_apply_accepts_server_reordering_of_only_the_outer_binding_array(tmp_path):
    raw = V3_PREIMAGE_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == V3_PREIMAGE_SHA256
    policy = json.loads(raw.decode("utf-8"))
    roles = [binding["role"] for binding in policy["bindings"]]
    assert roles == sorted(roles)
    case = write_case(tmp_path, policy)
    adapter = FakeAdapter(policy)
    original_transport = adapter.transport

    class OrderedTransport:
        def __init__(self):
            self.get_count = 0

        def reorder(self, response):
            adapter.current["bindings"].sort(key=lambda binding: binding["role"])
            payload = json.loads(response["body"].decode("utf-8"))
            payload["bindings"] = copy.deepcopy(adapter.current["bindings"])
            response["body"] = json_bytes(payload)
            return response

        def __call__(self, request):
            response = original_transport(request)
            if request["url"].endswith(":setIamPolicy"):
                return self.reorder(response)
            if request["url"].endswith(":getIamPolicy"):
                self.get_count += 1
                if self.get_count >= 2:
                    return self.reorder(response)
            return response

    adapter.transport = OrderedTransport()
    result = apply_case(case, adapter)
    write_request = next(call for call in adapter.calls if call["url"].endswith(":setIamPolicy"))
    outgoing = json.loads(write_request["body"].decode("utf-8"))["policy"]
    new_binding = {
        "role": "roles/run.viewer",
        "members": ["serviceAccount:intelligence-42-price-policy@ogilvy-trends-v2.iam.gserviceaccount.com"],
    }
    encoded = lambda bindings: Counter(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
        for binding in bindings
    )
    expected = encoded(policy["bindings"]) + Counter(
        [json.dumps(new_binding, sort_keys=True, separators=(",", ":")).encode("utf-8")]
    )

    assert result["verified"] is True
    assert encoded(outgoing["bindings"]) == expected
    assert encoded(adapter.current["bindings"]) == expected
    assert sum("condition" in binding for binding in adapter.current["bindings"]) == 3
    assert [binding["role"] for binding in adapter.current["bindings"]] == sorted(
        binding["role"] for binding in adapter.current["bindings"]
    )
    assert adapter.set_count == 1


def test_apply_rechecks_clock_after_token_lookup_crosses_20z(tmp_path):
    case = write_case(tmp_path)
    policy = json.loads(case["preimage"].read_text(encoding="utf-8"))
    current_time = [case["now"]]

    class CrossingAdapter(FakeAdapter):
        def token(self):
            self.token_calls += 1
            if self.token_calls == 2:
                current_time[0] = datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc)
            return "test-token"

    adapter = CrossingAdapter(policy)

    with pytest.raises(RuntimeError, match="write_clock_gate_refused"):
        apply_case(case, adapter, clock=lambda: current_time[0])

    assert adapter.token_calls == 2
    assert adapter.set_count == 0
    assert not any(call["url"].endswith(":setIamPolicy") for call in adapter.calls)


def test_private_cli_resolves_core_from_cwd_with_network_blocked(tmp_path):
    case_root = tmp_path / "case"
    case_root.mkdir()
    case = write_case(case_root)
    go_path = case_root / "go.json"
    go = json.loads(go_path.read_text(encoding="utf-8"))
    go["go_received_at"] = "2026-09-28T00:00:00Z"
    go_path.write_bytes(json_bytes(go))
    private_briefs = tmp_path / "private" / "briefs"
    private_briefs.mkdir(parents=True)
    private_cli = private_briefs / "pricing-project-viewer-cas.py"
    shutil.copyfile(HELPER_PATH, private_cli)
    shim = tmp_path / "shim"
    shim.mkdir()
    trace = tmp_path / "blocked-network.txt"
    (shim / "sitecustomize.py").write_text(
        "import builtins, os, socket, sys\n"
        "def blocked(*args, **kwargs): raise RuntimeError('blocked_network')\n"
        "socket.create_connection = blocked\n"
        "socket.socket.connect = blocked\n"
        "class BlockedAdapter:\n"
        " def __init__(self, state): pass\n"
        " def token(self): return 'test-token'\n"
        " def transport(self, request):\n"
        "  with open(os.environ['PRICE_VIEWER_TRACE'], 'a', encoding='utf-8') as out: out.write(request['method'] + ' ' + request['url'] + '\\n')\n"
        "  raise RuntimeError('blocked_network')\n"
        "original_import = builtins.__import__\n"
        "def import_hook(name, globals=None, locals=None, fromlist=(), level=0):\n"
        " result = original_import(name, globals, locals, fromlist, level)\n"
        " if name in ('ops.deploy', 'ops.deploy.runtime_native_adapter') and 'runtime_native_adapter' in (fromlist or ()):\n"
        "  module = sys.modules.get('ops.deploy.runtime_native_adapter')\n"
        "  if module is not None: module.NativeAdapter = BlockedAdapter\n"
        " return result\n"
        "builtins.__import__ = import_hook\n",
        encoding="utf-8",
    )
    core_root = HELPER_PATH.resolve().parents[2]
    command = [
        sys.executable,
        str(private_cli),
        "apply",
        str(case["root"]),
        "--preimage",
        str(case["preimage"]),
        "--preimage-sha256",
        case["preimage_sha256"],
        "--project-descriptor",
        str(case["project_descriptor"]),
        "--project-descriptor-sha256",
        case["project_descriptor_sha256"],
        "--plan-sha256",
        case["plan_sha256"],
        "--approval-manifest",
        str(case["approval_manifest"]),
        "--brief",
        str(case["brief"]),
        "--brief-sha256",
        case["brief_sha256"],
    ]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONPATH"] = str(shim)
    env["PRICE_VIEWER_TRACE"] = str(trace)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    assert Path(env["PYTHONPATH"]).resolve() == shim.resolve()
    assert not (shim / "ops").exists()

    wrong_root = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert wrong_root.returncode != 0
    assert "core_repo_root_invalid" in wrong_root.stderr
    assert not trace.exists()

    result = subprocess.run(
        command,
        cwd=core_root,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode != 0
    assert "blocked_network" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    trace_lines = trace.read_text(encoding="utf-8").splitlines()
    assert trace_lines == ["POST https://cloudresourcemanager.googleapis.com/v3/projects/590353929363:getIamPolicy"]
