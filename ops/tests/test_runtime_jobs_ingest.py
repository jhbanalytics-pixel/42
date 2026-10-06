import hashlib
import io
import json
from pathlib import Path

import pytest

from ops.deploy import ingest_runtime, runtime_jobs
from ops.runners.managed_runtime import (
    CONFIGURATION_ANNOTATION,
    canonical_bytes,
    canonical_sha256,
)

ROOT = Path(__file__).resolve().parents[2]
INGEST_FILE = ROOT / "infra" / "runtime" / "ingest-staging.json"
DAILY_FILE = ROOT / "infra" / "runtime" / "daily-staging.json"
MANIFEST_FILE = ROOT / "ops" / "deploy" / "resource_manifest.json"
DELTA_FILE = ROOT / "ops" / "deploy" / "iam_delta_v1.json"
INGEST_JOB_ID = "intelligence-42-ingest-staging"
INGEST_RESOURCE = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/"
    + INGEST_JOB_ID
)
INGEST_ACCOUNT = "intelligence-42-ingest@ogilvy-trends-v2.iam.gserviceaccount.com"
REPOSITORY = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine"
DIGEST = "sha256:" + "4" * 64
COMMIT = "c" * 40
GCLOUD = "gcloud.cmd"
APPROVED = {
    "applied": False,
    "approved_at": "2026-09-13T08:00:00Z",
    "approved_by": "reviewer",
    "state": "approved",
}
STATIC_NAMES = [
    "TRENDS_ENV",
    "BIGQUERY_DATASET",
    "GCP_PROJECT",
    "COLLECTION_POLICY_SHA256",
    "COLLECTION_PROFILE_SHA256",
]


def _build_record(source=True):
    record = {
        "id": "b0a1",
        "status": "SUCCESS",
        "results": {"images": [{"name": f"{REPOSITORY}:{COMMIT}", "digest": DIGEST}]},
    }
    if source:
        record["source"] = {"connectedRepository": {"revision": COMMIT}}
    return record


def _tree(tmp_path, *, config=None, build=None):
    root = tmp_path / "tree"
    root.mkdir(parents=True)
    manifest_bytes = MANIFEST_FILE.read_bytes()
    delta = json.loads(DELTA_FILE.read_bytes())
    delta["approval"] = dict(APPROVED)
    paths = {
        "runtime": root / "ingest-staging.json",
        "manifest": root / "resource_manifest.json",
        "engine": root / "resource_manifest_v1.json",
        "delta": root / "iam_delta_v1.json",
        "build": root / "build.json",
        "output": root / "out" / "ingest-plan.json",
    }
    paths["runtime"].write_bytes(
        INGEST_FILE.read_bytes() if config is None else canonical_bytes(config)
    )
    paths["manifest"].write_bytes(manifest_bytes)
    paths["engine"].write_bytes(manifest_bytes)
    paths["delta"].write_bytes(canonical_bytes(delta))
    paths["build"].write_text(
        json.dumps(_build_record() if build is None else build), encoding="utf-8"
    )
    return paths


def _render(paths):
    return runtime_jobs.render_plan(
        runtime_path=paths["runtime"],
        image_digest=DIGEST,
        build_record_path=paths["build"],
        output_path=paths["output"],
        resource_manifest_path=paths["manifest"],
        engine_manifest_path=paths["engine"],
        iam_delta_path=paths["delta"],
    )


def _config():
    return json.loads(INGEST_FILE.read_bytes())


def test_the_ingest_file_is_canonical_and_loads_as_the_ingest_job():
    raw = INGEST_FILE.read_bytes()
    config = ingest_runtime.load_ingest_configuration(INGEST_FILE)
    assert canonical_bytes(config) == raw
    assert config["contract_version"] == "42_managed_runtime_ingest_v1"
    assert config["credential_source"] == "attached_service_account"
    assert list(config["jobs"]) == [INGEST_JOB_ID]
    daily = json.loads(DAILY_FILE.read_bytes())
    assert config["resource_manifest_sha256"] == daily["resource_manifest_sha256"]
    entry = config["jobs"][INGEST_JOB_ID]
    assert entry["job_resource"] == INGEST_RESOURCE
    assert entry["service_account"] == INGEST_ACCOUNT
    assert entry["image_repository"] == REPOSITORY
    assert entry["command"] == ["python"]
    assert entry["args"] == ["-m", "scripts.staging.collect_42_sources"]
    assert entry["mode"] is None
    assert [item["name"] for item in entry["env"]] == STATIC_NAMES
    assert entry["env"][:3] == [
        {"name": "TRENDS_ENV", "value": "staging"},
        {"name": "BIGQUERY_DATASET", "value": "intelligence_42_sources_staging"},
        {"name": "GCP_PROJECT", "value": "ogilvy-trends-v2"},
    ]
    assert (entry["max_retries"], entry["task_count"], entry["parallelism"]) == (
        0,
        1,
        1,
    )
    assert entry["timeout_seconds"] == 2700
    assert ingest_runtime.load_job_configuration(INGEST_FILE) == config


def test_the_daily_loader_still_refuses_the_ingest_file_and_the_other_way_round():
    with pytest.raises(ValueError, match=r"^runtime_configuration_invalid$"):
        ingest_runtime.load_runtime_configuration(INGEST_FILE)
    with pytest.raises(ValueError, match=r"^runtime_configuration_invalid$"):
        ingest_runtime.load_ingest_configuration(DAILY_FILE)
    assert ingest_runtime.load_job_configuration(
        DAILY_FILE
    ) == ingest_runtime.load_runtime_configuration(DAILY_FILE)


def _entry_edit(name, value):
    def edit(config):
        config["jobs"][INGEST_JOB_ID][name] = value

    return edit


def _env_edit(index, value):
    def edit(config):
        config["jobs"][INGEST_JOB_ID]["env"][index]["value"] = value

    return edit


@pytest.mark.parametrize(
    "edit",
    [
        _entry_edit(
            "service_account",
            "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
        ),
        _entry_edit("args", ["-m", "scripts.run_rss_now"]),
        _entry_edit("command", ["python3"]),
        _entry_edit("mode", "daily"),
        _entry_edit("max_retries", 1),
        _entry_edit("timeout_seconds", 3600),
        _env_edit(0, "prod"),
        _env_edit(1, "trends_v2"),
        _env_edit(2, "another-project"),
        _env_edit(3, "not-a-digest"),
        _env_edit(4, "F" * 64),
        lambda config: config["jobs"][INGEST_JOB_ID]["env"].append(
            {"name": "COLLECTION_IMAGE_URI", "value": f"{REPOSITORY}@{DIGEST}"}
        ),
        lambda config: config["jobs"][INGEST_JOB_ID]["env"].reverse(),
        lambda config: config["jobs"].update(
            {"intelligence-42-daily-staging": config["jobs"][INGEST_JOB_ID]}
        ),
        lambda config: config.update(contract_version="42_managed_runtime_jobs_v1"),
        lambda config: config.update(runtime_job=INGEST_JOB_ID),
        lambda config: config.update(project="another-project"),
    ],
)
def test_the_ingest_loader_refuses_any_other_definition(tmp_path, edit):
    config = _config()
    edit(config)
    path = tmp_path / "ingest-staging.json"
    path.write_bytes(canonical_bytes(config))
    with pytest.raises(ValueError, match=r"^runtime_configuration_invalid$"):
        ingest_runtime.load_ingest_configuration(path)


def test_the_plan_creates_only_the_ingest_job_with_its_build_bound_facts(tmp_path):
    paths = _tree(tmp_path)
    plan = _render(paths)
    assert [job["job_id"] for job in plan["jobs"]] == [INGEST_JOB_ID]
    job = plan["jobs"][0]
    expected = job["expected"]
    image = f"{REPOSITORY}@{DIGEST}"
    assert expected["image"] == image
    assert expected["service_account"] == INGEST_ACCOUNT
    assert expected["args"] == ["-m", "scripts.staging.collect_42_sources"]
    assert expected["env"] == _config()["jobs"][INGEST_JOB_ID]["env"] + [
        {"name": "COLLECTION_SOURCE_SHA", "value": COMMIT},
        {"name": "COLLECTION_IMAGE_URI", "value": image},
    ]
    assert expected["timeout_seconds"] == 2700
    assert expected["max_retries"] == 0
    assert expected["annotations"] == {
        CONFIGURATION_ANNOTATION: canonical_sha256(_config()),
        runtime_jobs.MANIFEST_ANNOTATION: hashlib.sha256(
            MANIFEST_FILE.read_bytes()
        ).hexdigest(),
    }
    assert (
        plan["runtime_configuration_sha256"]
        == hashlib.sha256(INGEST_FILE.read_bytes()).hexdigest()
    )
    assert job["bindings"] == [
        {
            "member": "serviceAccount:intelligence-42-deploy@ogilvy-trends-v2.iam.gserviceaccount.com",
            "role": "roles/run.developer",
        },
        {
            "member": "serviceAccount:intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
            "role": "roles/run.invoker",
        },
    ]
    assert [operation["kind"] for operation in plan["operations"]] == [
        "precheck",
        "create_job",
        "add_binding",
        "add_binding",
    ]
    assert plan["operations"][1]["argv"] == [
        "gcloud",
        "run",
        "jobs",
        "replace",
        "ingest-plan-intelligence-42-ingest-staging.job.yaml",
        "--project=ogilvy-trends-v2",
        "--region=us-central1",
        "--format=json",
        "--quiet",
    ]
    container = job["specification"]["spec"]["template"]["spec"]["template"]["spec"]
    assert container["serviceAccountName"] == INGEST_ACCOUNT
    assert container["containers"][0]["env"] == expected["env"]
    assert runtime_jobs.verify_plan(plan) == plan["plan_sha256"]


def test_a_build_record_that_names_no_single_commit_refuses_the_ingest_plan(tmp_path):
    paths = _tree(tmp_path, build=_build_record(source=False))
    with pytest.raises(ValueError, match=r"^build_source_unbound$"):
        _render(paths)
    record = _build_record()
    record["sourceProvenance"] = {"resolvedRepoSource": {"commitSha": "d" * 40}}
    paths = _tree(tmp_path / "second", build=record)
    with pytest.raises(ValueError, match=r"^build_source_unbound$"):
        _render(paths)


def test_the_daily_plan_is_unchanged_by_the_ingest_definition(tmp_path):
    paths = _tree(tmp_path)
    paths["runtime"] = paths["runtime"].with_name("daily-staging.json")
    paths["runtime"].write_bytes(DAILY_FILE.read_bytes())
    plan = _render(paths)
    assert [job["job_id"] for job in plan["jobs"]] == [
        "intelligence-42-daily-staging",
        "intelligence-42-price-policy-staging",
    ]
    for job in plan["jobs"]:
        names = [item["name"] for item in job["expected"]["env"]]
        assert "COLLECTION_SOURCE_SHA" not in names
        assert "COLLECTION_IMAGE_URI" not in names


def _described(job):
    expected = job["expected"]
    return {
        "name": f"projects/ogilvy-trends-v2/locations/us-central1/jobs/{job['job_id']}",
        "template": {
            "annotations": dict(expected["annotations"]),
            "parallelism": expected["parallelism"],
            "taskCount": expected["task_count"],
            "template": {
                "containers": [
                    {
                        "args": expected["args"],
                        "command": expected["command"],
                        "env": expected["env"],
                        "image": expected["image"],
                    }
                ],
                "serviceAccount": expected["service_account"],
                "timeout": f"{expected['timeout_seconds']}s",
            },
        },
    }


def test_the_command_line_plans_applies_and_reads_back_the_ingest_job(tmp_path):
    paths = _tree(tmp_path)
    out = io.StringIO()
    code = runtime_jobs.main(
        [
            "plan",
            "--runtime",
            str(paths["runtime"]),
            "--image-digest",
            DIGEST,
            "--build-record",
            str(paths["build"]),
            "--output",
            str(paths["output"]),
            "--resource-manifest",
            str(paths["manifest"]),
            "--engine-manifest",
            str(paths["engine"]),
            "--iam-delta",
            str(paths["delta"]),
        ],
        out=out,
    )
    assert code == 0, out.getvalue()
    planned = json.loads(out.getvalue().strip().splitlines()[-1])
    assert planned["jobs"] == [INGEST_JOB_ID]
    assert planned["operations"] == 4
    plan = json.loads(paths["output"].read_bytes())
    job = plan["jobs"][0]
    created = set()

    def runner(argv, cwd=None):
        verb = argv[3]
        if verb == "describe":
            if INGEST_JOB_ID in created:
                return 0, json.dumps(_described(job)), ""
            return (
                1,
                "",
                f"ERROR: (gcloud.run.jobs.describe) Cannot find job [{INGEST_JOB_ID}].",
            )
        if verb == "replace":
            assert (Path(cwd) / argv[4]).is_file()
            created.add(INGEST_JOB_ID)
            return 0, "{}", ""
        if verb == "add-iam-policy-binding":
            return 0, "{}", ""
        if verb == "get-iam-policy":
            roles = {}
            for binding in job["bindings"]:
                roles.setdefault(binding["role"], []).append(binding["member"])
            policy = [
                {"members": members, "role": role} for role, members in roles.items()
            ]
            return 0, json.dumps({"bindings": policy}), ""
        raise AssertionError(argv)

    receipt_path = tmp_path / "receipt.json"
    code = runtime_jobs.main(
        [
            "apply",
            "--plan",
            str(paths["output"]),
            "--output",
            str(receipt_path),
            "--gcloud",
            GCLOUD,
            "--resource-manifest",
            str(paths["manifest"]),
        ],
        runner=runner,
        out=io.StringIO(),
    )
    assert code == 0
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["status"] == "applied"
    assert [record["state"] for record in receipt["operations"]] == [
        "absent",
        "applied",
        "applied",
        "applied",
    ]
    readback_path = tmp_path / "readback.json"
    code = runtime_jobs.main(
        [
            "readback",
            "--plan",
            str(paths["output"]),
            "--output",
            str(readback_path),
            "--gcloud",
            GCLOUD,
        ],
        runner=runner,
        out=io.StringIO(),
    )
    assert code == 0
    report = json.loads(readback_path.read_bytes())
    assert report["status"] == "match"
    assert report["summary"]["jobs"] == 1
