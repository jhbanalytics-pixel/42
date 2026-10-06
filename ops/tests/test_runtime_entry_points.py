"""E03 runtime entry points: the non HTTP half of the checked in inventory.

docs/operations/runtime-entry-points.json records the Cloud Run service, the
task queue that calls its worker route, every Cloud Run job and scheduler the
approved resource manifest declares, the image and command each deployable one
runs, the service account it runs as, the roles that account holds in the
approved provisioning delta, and every container entrypoint in the repository.
Each test here rebuilds one of those sections from the files the ops tooling
deploys from and fails on any difference, so the inventory cannot go stale.
The HTTP routes are checked by app/tests/unit/test_runtime_entry_points.py.
"""

import importlib
import json
from pathlib import Path

from ops.deploy import release, runtime_jobs
from ops.runners import managed_runtime

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = json.loads(
    (ROOT / "docs" / "operations" / "runtime-entry-points.json").read_text(
        encoding="utf-8"
    )
)
MANIFEST = json.loads(runtime_jobs.RESOURCE_MANIFEST.read_text(encoding="utf-8"))
DELTA = json.loads(runtime_jobs.IAM_DELTA.read_text(encoding="utf-8"))
# Every runtime file under infra/runtime that declares jobs: the daily file for
# the daily and price policy jobs, the ingest file for the ingest job alone.
RUNTIME_FILES = {
    path.relative_to(ROOT).as_posix(): json.loads(path.read_text(encoding="utf-8"))
    for path in sorted((ROOT / "infra" / "runtime").glob("*.json"))
    if "jobs" in json.loads(path.read_text(encoding="utf-8"))
}
SCHEDULERS = json.loads(
    (ROOT / "infra" / "runtime" / "scheduler-staging.json").read_text(encoding="utf-8")
)
JOB_PREFIX = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/"
)
SCHEDULER_PREFIX = "//cloudscheduler.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/jobs/"
SERVICE_PREFIX = (
    "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1/services/"
)
# The execution origin registries: the one the resource manifest pins by digest
# and the one the active execution generation names. Each binds an operation to
# the job it runs in and the identity it runs as.
ORIGIN_REGISTRIES = (
    "engine/configs/open_intelligence/execution_origins_v1.json",
    "engine/configs/open_intelligence/execution_origins_bridge_v3.json",
)
# Job entry scripts no runtime file declares: the script, and the constant in it
# that names the one job whose executions it accepts. The test reads the script
# and checks the constant, the execution variable and the program guard.
ENTRY_SCRIPTS = {
    "intelligence-42-ingest-staging": (
        "engine/scripts/staging/collect_42_sources.py",
        "INGEST_JOB",
    ),
}
CONTAINER_FILES = (
    "app/Dockerfile.general-question",
    "engine/Dockerfile.staging",
    "engine/Dockerfile.tests",
    "ops/build/Dockerfile.context-builder",
    "ops/tests/Dockerfile.linux-gates",
)
PROCFILES = ("app/Procfile",)
CLOUDBUILD_FILES = (
    "cloudbuild.app.publish.yaml",
    "engine/cloudbuild.publish.yaml",
    "engine/cloudbuild.staging.yaml",
)


def _manifest_names(prefix):
    return sorted(
        resource["name"][len(prefix) :]
        for resource in MANIFEST["resources"]
        if resource["name"].startswith(prefix)
    )


def _grant_rows():
    for row in DELTA["bindings"]:
        yield "bindings", row
    for row in DELTA["retained"]:
        yield "retained", row
    for amendment in DELTA["amendments"]:
        for row in amendment["bindings"]:
            yield "amendment_" + amendment["amendment"], row


def observed_service_accounts():
    accounts = {}
    for source, row in _grant_rows():
        kind, _, email = row["member"].partition(":")
        if kind != "serviceAccount":
            continue
        accounts.setdefault(email, []).append(
            {
                "resource": row["resource"],
                "role": row["role"],
                "condition": row["condition"],
                "source": source,
            }
        )
    for rows in accounts.values():
        rows.sort(key=lambda item: (item["resource"], item["role"], item["source"]))
    return dict(sorted(accounts.items()))


def _resource_iam(resource):
    return sorted(
        (
            {"member": row["member"], "role": row["role"]}
            for _source, row in _grant_rows()
            if row["resource"] == resource
        ),
        key=lambda item: (item["role"], item["member"]),
    )


def _entry_module(args):
    return args[args.index("-m") + 1] if "-m" in args else None


def _origin_bindings(name):
    job = "projects/ogilvy-trends-v2/locations/us-central1/jobs/" + name
    rows = []
    for source in ORIGIN_REGISTRIES:
        registry = json.loads((ROOT / source).read_text(encoding="utf-8"))
        for row in registry["rows"]:
            for operation, binding in row["exact_operation_bindings"].items():
                if binding["job_resource"] == job:
                    rows.append(
                        {
                            "source": source,
                            "operation": operation,
                            "service_identity": binding["service_identity"],
                            "image_repository": row["image_repository"],
                            "datasets": binding["datasets"],
                        }
                    )
    return rows


def _entry_script(name):
    if name not in ENTRY_SCRIPTS:
        return None
    source, constant = ENTRY_SCRIPTS[name]
    return {"source": source, "job_constant": constant}


def observed_jobs():
    jobs = []
    for name in _manifest_names(JOB_PREFIX):
        resource = JOB_PREFIX + name
        declared = [path for path, runtime in RUNTIME_FILES.items() if name in runtime["jobs"]]
        assert len(declared) <= 1, (name, declared)
        spec = None
        if declared:
            entry = RUNTIME_FILES[declared[0]]["jobs"][name]
            spec = {
                "declared_in": declared[0],
                "image_repository": entry["image_repository"],
                "command": entry["command"],
                "args": entry["args"],
                "env_names": sorted(item["name"] for item in entry["env"]),
                "mode": entry["mode"],
                "service_account": entry["service_account"],
                "entry_module": _entry_module(entry["args"]),
            }
        triggers = sorted(
            scheduler
            for scheduler, value in SCHEDULERS["schedulers"].items()
            if value["job"]["httpTarget"]["uri"].endswith(f"/jobs/{name}:run")
        )
        jobs.append(
            {
                "name": name,
                "resource": resource,
                "runtime": spec,
                "entry_script": _entry_script(name),
                "origin_bindings": _origin_bindings(name),
                "triggered_by": triggers,
                "resource_iam": _resource_iam(resource),
            }
        )
    return jobs


def observed_schedulers():
    rows = []
    for name in _manifest_names(SCHEDULER_PREFIX):
        job = SCHEDULERS["schedulers"][name]["job"]
        target = job["httpTarget"]
        rows.append(
            {
                "name": name,
                "resource": SCHEDULER_PREFIX + name,
                "schedule": job["schedule"],
                "time_zone": job["timeZone"],
                "state": job["state"],
                "http_method": target["httpMethod"],
                "target_uri": target["uri"],
                "service_account": target["oauthToken"]["serviceAccountEmail"],
            }
        )
    return rows


def _instruction_lines(path):
    joined, lines = "", []
    for raw in (ROOT / path).read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not joined and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            joined += stripped[:-1] + " "
            continue
        lines.append(joined + stripped)
        joined = ""
    return lines


def _exec_form(value):
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    return parsed


def observed_dockerfile(path):
    """The final stage of a Dockerfile: base, user, entrypoint and command."""
    stage = {}
    for line in _instruction_lines(path):
        instruction, _, value = line.partition(" ")
        instruction = instruction.upper()
        if instruction == "FROM":
            words = [word for word in value.split() if not word.startswith("--")]
            stage = {
                "file": path,
                "final_stage_from": words[0],
                "user": None,
                "entrypoint": None,
                "cmd": None,
            }
        elif instruction == "USER":
            stage["user"] = value.strip()
        elif instruction == "ENTRYPOINT":
            stage["entrypoint"] = _exec_form(value.strip())
            stage["cmd"] = None
        elif instruction == "CMD":
            stage["cmd"] = _exec_form(value.strip())
    return stage


def observed_procfile(path):
    processes = {}
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            name, _, command = line.partition(":")
            processes[name.strip()] = command.strip()
    return {"file": path, "processes": processes}


DOCKERFILE_FLAGS = ("-f", "--file")


def _dockerfile_arguments(text):
    """The Dockerfile of every docker build flag in the YAML argument lists.

    Accepts the flag and its value as two list items (-f PATH, --file PATH) and
    as one (--file=PATH, -f=PATH).
    """
    items = [
        line.strip()[2:].strip()
        for line in text.splitlines()
        if line.strip().startswith("- ")
    ]
    for index, item in enumerate(items):
        if item in DOCKERFILE_FLAGS and index + 1 < len(items):
            yield items[index + 1]
        for flag in DOCKERFILE_FLAGS:
            if item.startswith(flag + "="):
                yield item[len(flag) + 1 :]


def observed_cloudbuild(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    dockerfiles = sorted(set(_dockerfile_arguments(text)))
    images = []
    if "\nimages:\n" in text:
        block = text.split("\nimages:\n", 1)[1].split("\n\n", 1)[0]
        images = [line.strip()[2:] for line in block.splitlines() if line.strip()]
    return {"file": path, "dockerfiles": dockerfiles, "pushed_images": images}


def observed_service():
    (name,) = _manifest_names(SERVICE_PREFIX)
    assert SERVICE_PREFIX + name == release.SERVICE_RESOURCE
    return {
        "name": name,
        "resource": release.SERVICE_RESOURCE,
        "image_repository": release.APP_IMAGE_NAME,
        "container": "app/Dockerfile.general-question",
        "service_account": release.IDENTITY,
        "secret_environment": {
            key: dict(value) for key, value in release._SECRET_ENVIRONMENT.items()
        },
        "resource_iam": _resource_iam(release.SERVICE_RESOURCE),
    }


def observed_queues():
    return [
        {
            "resource": release.QUEUE_RESOURCE,
            "target_service": release.SERVICE_RESOURCE,
            "target_path": release.WORKER_PATH,
            "oidc_service_account": release.IDENTITY,
            "oidc_audience": release.AUDIENCE,
        }
    ]


# The inventory against the deploy sources


def test_every_job_the_manifest_declares_is_inventoried_as_the_runtime_file_runs_it():
    assert INVENTORY["jobs"] == observed_jobs()


def test_every_scheduler_is_inventoried_with_its_target_and_identity():
    assert INVENTORY["schedulers"] == observed_schedulers()
    scheduled = {row["name"] for row in INVENTORY["schedulers"]}
    assert scheduled == set(SCHEDULERS["schedulers"])


def test_the_service_and_its_worker_queue_are_inventoried_as_release_deploys_them():
    assert INVENTORY["service"] == observed_service()
    assert INVENTORY["queues"] == observed_queues()


def test_every_service_account_carries_exactly_its_roles_in_the_approved_delta():
    assert INVENTORY["service_accounts"] == observed_service_accounts()


def test_every_identity_that_runs_something_has_its_roles_inventoried():
    running = {INVENTORY["service"]["service_account"]}
    running |= {row["service_account"] for row in INVENTORY["schedulers"]}
    running |= {
        job["runtime"]["service_account"]
        for job in INVENTORY["jobs"]
        if job["runtime"] is not None
    }
    assert running <= set(INVENTORY["service_accounts"])


def test_the_inventory_records_which_delta_it_read_and_that_it_is_not_live_state():
    assert INVENTORY["privileges"] == {
        "source": "ops/deploy/iam_delta_v1.json",
        "resource_manifest_sha256": DELTA["resource_manifest_sha256"],
        "approval_state": DELTA["approval"]["state"],
        "applied": DELTA["approval"]["applied"],
        "live_state_verified": False,
    }


def test_every_container_entrypoint_is_inventoried():
    assert INVENTORY["containers"] == {
        "dockerfiles": [observed_dockerfile(path) for path in CONTAINER_FILES],
        "procfiles": [observed_procfile(path) for path in PROCFILES],
        "cloudbuild": [observed_cloudbuild(path) for path in CLOUDBUILD_FILES],
    }
    found = sorted(
        path.relative_to(ROOT).as_posix()
        for pattern in ("**/Dockerfile*", "**/Procfile", "**/cloudbuild*.yaml")
        for path in ROOT.glob(pattern)
        if ".git" not in path.parts
        and "node_modules" not in path.parts
        and not path.name.endswith(".dockerignore")
    )
    assert found == sorted(CONTAINER_FILES + PROCFILES + CLOUDBUILD_FILES)


def _referenced_dockerfiles(path):
    """Every YAML list value in a Cloud Build file that names a Dockerfile.

    Independent of the flag spelling the build step uses, so a Dockerfile passed
    with -f, --file, --file= or any other form still has to reach the row.
    """
    names = set()
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        value = stripped[2:].strip()
        if value.startswith("-") and "=" in value:
            value = value.split("=", 1)[1]
        if Path(value).name.startswith("Dockerfile"):
            names.add(value)
    return names


def test_every_dockerfile_a_cloud_build_file_references_is_in_its_row():
    rows = {row["file"]: row for row in INVENTORY["containers"]["cloudbuild"]}
    for path in CLOUDBUILD_FILES:
        referenced = _referenced_dockerfiles(path)
        assert referenced, path
        assert referenced == set(rows[path]["dockerfiles"]), path
        assert referenced == set(observed_cloudbuild(path)["dockerfiles"]), path


def test_every_job_entry_module_exists_and_runs_as_a_program():
    for job in INVENTORY["jobs"]:
        runtime = job["runtime"]
        if runtime is None:
            continue
        module = importlib.import_module(runtime["entry_module"])
        assert callable(getattr(module, "main", None)), runtime["entry_module"]
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in source, runtime["entry_module"]


def test_every_declared_entry_script_accepts_only_its_own_job():
    declared = {job["name"] for job in INVENTORY["jobs"] if job["entry_script"]}
    assert declared == set(ENTRY_SCRIPTS)
    for name, (source, constant) in ENTRY_SCRIPTS.items():
        text = (ROOT / source).read_text(encoding="utf-8")
        assert f'{constant} = "{name}"' in text, source
        assert '"CLOUD_RUN_EXECUTION"' in text, source
        assert 'if __name__ == "__main__":' in text, source


def test_every_origin_binding_identity_is_a_manifest_identity_with_roles():
    identities = {value.rsplit("/", 1)[1] for value in MANIFEST["identities"].values()}
    for job in INVENTORY["jobs"]:
        for binding in job["origin_bindings"]:
            assert binding["service_identity"] in identities, (job["name"], binding)
            assert binding["service_identity"] in INVENTORY["service_accounts"]


def test_the_engine_modules_the_daily_job_loads_are_inventoried():
    assert INVENTORY["engine_entry_points"] == {
        "daily_job_modules": [
            "src.analysis.open_intelligence." + name
            for name in managed_runtime._DAILY_MODULES
        ],
        "daily_job_modes": list(managed_runtime.MODES),
    }
    for name in managed_runtime._DAILY_MODULES:
        path = ROOT / "engine" / "src" / "analysis" / "open_intelligence" / f"{name}.py"
        assert path.is_file(), path
