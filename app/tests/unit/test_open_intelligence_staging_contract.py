"""Executable contract tests for Open Intelligence staging isolation."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import json
from copy import deepcopy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = ROOT / "infra" / "cloud-run" / "deploy-open-intelligence-staging.sh"


def _resolve_bash() -> Path | None:
    """A bash able to run the deploy script, on whatever platform this is.

    Resolved lazily and defensively. Reading LOCALAPPDATA at import time made
    this module importable only on Windows, and a KeyError during collection
    takes the whole suite down with it rather than failing one test.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidate = Path(local_app_data) / "Programs" / "Git" / "bin" / "bash.exe"
        if candidate.exists():
            return candidate
    found = shutil.which("bash")
    return Path(found) if found else None


GIT_BASH = _resolve_bash()
FULL_SHA = "a" * 40
READBACK_SHA = "b" * 40
STAGING_IDENTITY = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"

READBACK_FIXTURE = {
    "metadata": {
        "name": "listening-post-staging",
        "labels": {
            "environment": "staging",
            "application-source": "open-intelligence-staging",
            "source-sha": READBACK_SHA,
        },
    },
    "spec": {
        "template": {
            "metadata": {
                "labels": {
                    "environment": "staging",
                    "application-source": "open-intelligence-staging",
                    "source-sha": READBACK_SHA,
                },
            },
            "spec": {
                "serviceAccountName": STAGING_IDENTITY,
                "containers": [
                    {
                        "env": [
                            {"name": "BQ_DATASET", "value": "trends_v2_staging"},
                            {"name": "CACHE_BUCKET", "value": "listening-post-staging-cache"},
                            {"name": "CACHE_PREFIX", "value": "open-intelligence/v2/staging/"},
                            {"name": "APPLICATION_SOURCE", "value": "open-intelligence-staging"},
                            {"name": "DEPLOYMENT_PROFILE", "value": "open-intelligence-staging"},
                            {"name": "SOURCE_SHA", "value": READBACK_SHA},
                            {
                                "name": "UI_PASSCODE",
                                "valueFrom": {"secretKeyRef": {"name": "ui-passcode-staging"}},
                            },
                        ],
                    },
                ],
            },
        },
    },
}


def _staging_env(**overrides: str) -> dict[str, str]:
    """Literal approved values, derived from the approved section 12 contract."""
    env = {
        "DEPLOYMENT_PROFILE": "open-intelligence-staging",
        "K_SERVICE": "listening-post-staging",
        "BQ_DATASET": "trends_v2_staging",
        "CACHE_BUCKET": "listening-post-staging-cache",
        "CACHE_PREFIX": "open-intelligence/v2/staging/",
        "APPLICATION_SOURCE": "open-intelligence-staging",
        "SOURCE_SHA": FULL_SHA,
    }
    env.update(overrides)
    return env


def test_staging_runtime_accepts_the_approved_environment_and_metadata_identity():
    """Break caught: a valid staging process is rejected before it can serve."""
    from src.api import deployment_contract

    deployment_contract.validate_runtime_contract(
        _staging_env(),
        metadata_email_getter=lambda: "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("K_SERVICE", "listening-post"),
        ("BQ_DATASET", "trends_v2_dev"),
        ("CACHE_BUCKET", "listening-post-cache"),
        ("CACHE_PREFIX", "cache/"),
        ("APPLICATION_SOURCE", "listening-post"),
        ("SOURCE_SHA", "abc123"),
    ],
)
def test_staging_runtime_rejects_each_isolation_contract_drift(name: str, value: str):
    """Break caught: one approved isolation boundary silently drifts."""
    from src.api import deployment_contract

    with pytest.raises(deployment_contract.DeploymentContractError, match=name):
        deployment_contract.validate_runtime_contract(
            _staging_env(**{name: value}),
            metadata_email_getter=lambda: "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        )


@pytest.mark.parametrize(
    "bad_prefix",
    ["", "/open-intelligence/v2/staging/", "../open-intelligence/", "open\\intelligence/"],
)
def test_staging_runtime_rejects_unsafe_cache_prefixes(bad_prefix: str):
    """Break caught: a staging cache path can escape its dedicated namespace."""
    from src.api import deployment_contract

    with pytest.raises(deployment_contract.DeploymentContractError, match="CACHE_PREFIX"):
        deployment_contract.validate_runtime_contract(
            _staging_env(CACHE_PREFIX=bad_prefix),
            metadata_email_getter=lambda: "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        )


@pytest.mark.parametrize(
    "metadata_email_getter",
    [
        lambda: "590353929363-compute@developer.gserviceaccount.com",
        lambda: "not-an-email",
        lambda: (_ for _ in ()).throw(TimeoutError("metadata timeout")),
    ],
)
def test_staging_runtime_fails_closed_when_metadata_identity_is_not_exact(metadata_email_getter):
    """Break caught: default compute or a failed metadata read is accepted."""
    from src.api import deployment_contract

    with pytest.raises(deployment_contract.DeploymentContractError, match="service account"):
        deployment_contract.validate_runtime_contract(_staging_env(), metadata_email_getter=metadata_email_getter)


@pytest.mark.parametrize("vendor_name", ["RETIRED_VENDOR_API_KEY", "SOCIALCRAWL_API_KEY", "OTHER_VENDOR_API_KEY"])
def test_staging_runtime_rejects_vendor_secret_environment_presence(vendor_name: str):
    """Break caught: a staging process starts with a retired or paid vendor secret."""
    from src.api import deployment_contract

    with pytest.raises(deployment_contract.DeploymentContractError, match=vendor_name):
        deployment_contract.validate_runtime_contract(
            _staging_env(**{vendor_name: "present"}),
            metadata_email_getter=lambda: "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        )


def test_runtime_gate_keeps_non_staging_behavior_and_skips_metadata():
    """Break caught: production or local startup begins querying Cloud Run metadata."""
    from src.api import deployment_contract

    deployment_contract.validate_runtime_contract(
        {"BQ_DATASET": "trends_v2_dev"},
        metadata_email_getter=lambda: (_ for _ in ()).throw(AssertionError("metadata called")),
    )


def test_cache_object_name_keeps_production_default_and_uses_staging_namespace(monkeypatch):
    """Break caught: a cache key moves production or staging outside its contract path."""
    from src.api import synth

    monkeypatch.delenv("CACHE_PREFIX", raising=False)
    assert synth.cache_object_name(("topic", "za")) == "cache/topic_za-a2dd9061cb.json"

    monkeypatch.setenv("CACHE_PREFIX", "open-intelligence/v2/staging/")
    assert (
        synth.cache_object_name(("topic", "za"))
        == "open-intelligence/v2/staging/topic_za-a2dd9061cb.json"
    )


def test_gcs_read_and_write_use_the_same_cache_object_name(monkeypatch):
    """Break caught: GCS reads and writes use divergent cache object paths."""
    from src.api import synth

    names: list[str] = []

    class Blob:
        updated = None

        def upload_from_string(self, *_args, **_kwargs):
            return None

    class Bucket:
        def blob(self, name):
            names.append(name)
            return Blob()

        def get_blob(self, name):
            names.append(name)
            return Blob()

    monkeypatch.setenv("CACHE_PREFIX", "open-intelligence/v2/staging/")
    monkeypatch.setattr(synth, "_gcs_bucket", lambda: Bucket())
    synth._gcs_write(("topic", "za"), {"answer": 1})
    synth._gcs_read(("topic", "za"))

    assert names == [
        "open-intelligence/v2/staging/topic_za-a2dd9061cb.json",
        "open-intelligence/v2/staging/topic_za-a2dd9061cb.json",
    ]


@pytest.fixture
def deployment_checkout(tmp_path):
    root = tmp_path / "deployment-checkout"
    script = root / "infra/cloud-run/deploy-open-intelligence-staging.sh"
    script.parent.mkdir(parents=True)
    script.write_bytes(DEPLOY_SCRIPT.read_bytes())
    main = root / "src/api/main.py"
    main.parent.mkdir(parents=True)
    main.write_text("pass\n", encoding="utf-8")
    commands = [
        ["init", "--initial-branch=feat/42-redesign-staging"],
        ["config", "user.name", "Fixture"],
        ["config", "user.email", "fixture@example.invalid"],
        ["config", "core.hooksPath", str(root / "unused-hooks")],
        ["add", "."],
        ["commit", "-m", "test: prepare isolated deployment fixture"],
    ]
    for command in commands:
        subprocess.run(["git", *command], cwd=root, check=True, capture_output=True)
    return root


def _run_plan(root=ROOT) -> subprocess.CompletedProcess[str]:
    if GIT_BASH is None:
        pytest.skip("no bash available to run the deploy plan")
    bash_script = _bash_path(root / "infra/cloud-run/deploy-open-intelligence-staging.sh")
    return subprocess.run(
        [str(GIT_BASH), bash_script, "--plan"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PATH": os.environ["PATH"]},
    )


def _bash_path(path: Path) -> str:
    """A path this bash understands.

    Git Bash addresses a Windows drive as /c/..., while a POSIX path is
    already correct. Indexing Path.drive unconditionally raised IndexError off
    Windows, where drive is empty.
    """
    drive = path.drive
    if not drive:
        return path.as_posix()
    return "/" + drive[0].lower() + path.as_posix()[len(drive):]


def _run_sourced_function(function: str, payload: dict | None = None) -> subprocess.CompletedProcess[str]:
    if GIT_BASH is None:
        pytest.skip("no bash available to source the deploy script")
    command = f'source "{_bash_path(DEPLOY_SCRIPT)}"; SOURCE_SHA={READBACK_SHA}; {function}'
    return subprocess.run(
        [str(GIT_BASH), "-lc", command],
        cwd=ROOT,
        check=False,
        input=None if payload is None else json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PATH": os.environ["PATH"]},
    )


def _readback_mutation(field: str, mode: str) -> dict:
    payload = deepcopy(READBACK_FIXTURE)
    if field == "DEPLOYMENT_PROFILE":
        env = payload["spec"]["template"]["spec"]["containers"][0]["env"]
        if mode == "missing":
            env[:] = [item for item in env if item["name"] != field]
        else:
            next(item for item in env if item["name"] == field)["value"] = "production"
        return payload

    labels = payload["metadata"]["labels"] if field.startswith("resource") else payload["spec"]["template"]["metadata"]["labels"]
    name = field.removeprefix("resource_").removeprefix("template_")
    if mode == "missing":
        labels.pop(name)
    else:
        labels[name] = "production"
    return payload


def test_actual_readback_predicate_accepts_complete_service_fixture():
    """Break caught: a complete deployed staging service fails its own readback gate."""
    result = _run_sourced_function("readback_matches_contract", READBACK_FIXTURE)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("field", "mode"),
    [
        ("DEPLOYMENT_PROFILE", "missing"),
        ("DEPLOYMENT_PROFILE", "wrong"),
        ("resource_environment", "missing"),
        ("resource_environment", "wrong"),
        ("template_environment", "missing"),
        ("template_environment", "wrong"),
        ("resource_application-source", "missing"),
        ("resource_application-source", "wrong"),
        ("template_application-source", "missing"),
        ("template_application-source", "wrong"),
    ],
)
def test_actual_readback_predicate_rejects_each_new_field_omission_or_drift(field: str, mode: str):
    """Break caught: one required profile or label can disappear after deployment."""
    result = _run_sourced_function("readback_matches_contract", _readback_mutation(field, mode))

    assert result.returncode != 0


def test_rollback_uses_latest_ready_revision_not_first_traffic_entry():
    """Break caught: split traffic selects a non-ready revision for rollback."""
    payload = deepcopy(READBACK_FIXTURE)
    payload["status"] = {
        "latestReadyRevisionName": "listening-post-staging-00073-ready",
        "traffic": [
            {"revisionName": "listening-post-staging-00072-canary", "percent": 5},
            {"revisionName": "listening-post-staging-00073-ready", "percent": 95},
        ],
    }

    selected = _run_sourced_function("latest_ready_revision", payload)
    rollback = _run_sourced_function(
        f'print_rollback_command "{selected.stdout.strip()}"',
    )

    assert selected.returncode == 0, selected.stderr
    assert selected.stdout.strip() == "listening-post-staging-00073-ready"
    assert rollback.returncode == 0, rollback.stderr
    assert "--to-revisions listening-post-staging-00073-ready=100" in rollback.stdout
    assert "00072-canary" not in rollback.stdout


def test_rollback_refuses_an_empty_latest_ready_revision():
    """Break caught: deployment starts without a rollback target."""
    result = _run_sourced_function('require_previous_ready_revision ""')

    assert result.returncode != 0
    assert "No previous ready revision" in result.stderr


class _MetadataResponse:
    def __init__(self, header: str | None, body: bytes):
        self.headers = {} if header is None else {"Metadata-Flavor": header}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


def test_metadata_transport_uses_the_required_request_shape(monkeypatch):
    """Break caught: metadata identity uses the wrong endpoint, header, or timeout."""
    from src.api import deployment_contract

    observed = {}

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["header"] = request.get_header("Metadata-flavor")
        observed["timeout"] = timeout
        return _MetadataResponse("Google", STAGING_IDENTITY.encode("utf-8"))

    monkeypatch.setattr(deployment_contract, "urlopen", fake_urlopen)

    assert deployment_contract._metadata_service_account() == STAGING_IDENTITY
    assert observed == {
        "url": "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
        "header": "Google",
        "timeout": 2.0,
    }


@pytest.mark.parametrize(
    ("header", "body", "expected"),
    [
        (None, STAGING_IDENTITY.encode("utf-8"), ValueError),
        ("not-google", STAGING_IDENTITY.encode("utf-8"), ValueError),
        ("Google", b"", ValueError),
        ("Google", b"not-an-email", ValueError),
        ("Google", b"\xff", UnicodeDecodeError),
    ],
)
def test_metadata_transport_rejects_malformed_response_data(monkeypatch, header, body, expected):
    """Break caught: malformed metadata headers or bodies become a trusted identity."""
    from src.api import deployment_contract

    monkeypatch.setattr(
        deployment_contract,
        "urlopen",
        lambda _request, timeout: _MetadataResponse(header, body),
    )

    with pytest.raises(expected):
        deployment_contract._metadata_service_account()


def test_metadata_transport_propagates_a_timeout(monkeypatch):
    """Break caught: metadata timeout is converted into an accepted identity."""
    from src.api import deployment_contract

    def timeout(*_args, **_kwargs):
        raise TimeoutError("metadata timeout")

    monkeypatch.setattr(deployment_contract, "urlopen", timeout)

    with pytest.raises(TimeoutError, match="metadata timeout"):
        deployment_contract._metadata_service_account()


def test_deploy_plan_prints_the_exact_isolated_cloud_run_command(deployment_checkout):
    """Break caught: the staging deploy plan targets production or binds a vendor secret."""
    result = _run_plan(deployment_checkout)

    assert result.returncode == 0, result.stderr
    plan_line = next(line for line in result.stdout.splitlines() if line.startswith("PLAN_DEPLOY "))
    args = shlex.split(plan_line.removeprefix("PLAN_DEPLOY "))
    source_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=deployment_checkout, text=True, encoding="utf-8"
    ).strip()

    assert args[:4] == ["gcloud", "run", "deploy", "listening-post-staging"]
    bash_root = _bash_path(deployment_checkout)
    assert ["--source", bash_root] == args[args.index("--source") : args.index("--source") + 2]
    assert ["--project", "ogilvy-trends-v2"] == args[args.index("--project") : args.index("--project") + 2]
    assert ["--region", "us-central1"] == args[args.index("--region") : args.index("--region") + 2]
    assert [
        "--service-account",
        "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    ] == args[args.index("--service-account") : args.index("--service-account") + 2]
    env_vars = args[args.index("--set-env-vars") + 1]
    assert set(env_vars.split(",")) >= {
        "BQ_DATASET=trends_v2_staging",
        "CACHE_BUCKET=listening-post-staging-cache",
        "CACHE_PREFIX=open-intelligence/v2/staging/",
        "APPLICATION_SOURCE=open-intelligence-staging",
        "DEPLOYMENT_PROFILE=open-intelligence-staging",
        f"SOURCE_SHA={source_sha}",
    }
    assert args[args.index("--set-secrets") + 1] == "UI_PASSCODE=ui-passcode-staging:latest"
    assert args.count("--set-secrets") == 1
    labels = args[args.index("--labels") + 1]
    assert set(labels.split(",")) == {
        "environment=staging",
        "application-source=open-intelligence-staging",
        f"source-sha={source_sha}",
    }
    assert re.fullmatch(r"[0-9a-f]{40}", source_sha)


def test_deploy_installs_the_locked_frontend_before_building():
    """Break caught: ignored stale node_modules bypasses the reviewed lock."""
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    install = script.find("bun install --frozen-lockfile")
    build = script.find("bun run build")

    assert 0 <= install < build


def test_deploy_plan_refuses_a_dirty_tracked_worktree(deployment_checkout):
    """Break caught: a mixed source tree reaches the staging build path."""
    dirty_target = deployment_checkout / "src" / "api" / "main.py"
    original = dirty_target.read_bytes()
    try:
        dirty_target.write_bytes(original + b"\n# temporary dirty-tree contract probe\n")
        result = _run_plan(deployment_checkout)
    finally:
        dirty_target.write_bytes(original)

    assert result.returncode != 0
    assert "dirty tracked worktree" in result.stderr.lower()


def test_deploy_plan_still_refuses_an_unapproved_branch(deployment_checkout):
    subprocess.run(["git", "checkout", "-b", "unapproved"], cwd=deployment_checkout,
                   check=True, capture_output=True)
    result = _run_plan(deployment_checkout)
    assert result.returncode != 0
    assert "outside branch feat/42-redesign-staging" in result.stderr


def test_module_does_not_require_a_windows_only_environment(monkeypatch):
    """Break caught: this suite being importable only on one developer's laptop.

    Reading LOCALAPPDATA at module import made the whole file Windows-only. On
    a Linux runner that raised KeyError during collection, which aborted the
    entire Listening Post suite before a single test ran, so CI reported a
    failure that told nobody anything about the code.
    """
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    resolved = _resolve_bash()
    # Either a real bash was found, or none is available and the caller skips.
    # Neither outcome may raise.
    assert resolved is None or Path(resolved).exists()


def test_bash_path_conversion_works_without_a_windows_drive_letter():
    """Break caught: POSIX paths losing their leading slash.

    The old conversion indexed Path.drive, which is empty on POSIX, so it
    raised IndexError rather than returning a usable path.
    """
    assert _bash_path(Path("/opt/work/deploy.sh")).endswith("/opt/work/deploy.sh")
    # Only Windows parses a drive letter, so only there is there one to fold
    # into the Git Bash /c/... form. Asserting that shape on POSIX would be
    # asserting the platform, not the function.
    drive_path = Path("C:/workspace/deploy.sh")
    if drive_path.drive:
        assert _bash_path(drive_path) == "/c/workspace/deploy.sh"
    else:
        assert _bash_path(drive_path) == "C:/workspace/deploy.sh"
