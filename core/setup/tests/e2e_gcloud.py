"""A stand-in for the gcloud program, for the end to end release test (core/setup/tests/test_release_e2e.py).

The paste and the programs it starts run this file where they would run gcloud.cmd. It answers from a simulated Cloud Run world kept
in a JSON file and changes that world the way the real commands do: builds submit makes a build and a registry image, run deploy
makes a revision that takes no traffic and carries a tag, update-traffic moves traffic and removes tags. Every call is appended to a
log file. A command it does not know exits 97, so nothing the release does can pass unseen.

Nothing is seeded that the release itself writes: a build, a registry digest, a revision or a tag exists in this world only because
a command of the release made it.

It is started with -S so the shim that fakes the Python libraries (e2e_shim) is not loaded into it, and it reads the repository
that holds the test worlds from F42_E2E_TESTS_ROOT.
"""
import datetime as dt
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.environ["F42_E2E_TESTS_ROOT"])

from core.setup.tests import release_world as rw  # noqa: E402

STATE = Path(os.environ["F42_E2E_WORLD"])
LOG = Path(os.environ["F42_E2E_CALLS"])
PROJECT = rw.PROJECT
STAGING = "gs://ogilvy-trends-v2-f42-media-staging/build-source"


def load():
    world = rw.World()
    world.__dict__.update(json.loads(STATE.read_text(encoding="utf-8")))
    return world


def save(world):
    STATE.write_text(json.dumps(world.__dict__), encoding="utf-8")


def log(args, code):
    with LOG.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"tool": "gcloud", "argv": args, "cwd": os.getcwd(), "exit": code}) + "\n")


def opt(args, flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default


def say(value):
    sys.stdout.write(json.dumps(value))
    return 0


def fail(text, code=1):
    sys.stderr.write(text + "\n")
    return code


def builds_submit(world, args):
    if world.__dict__.get("fail_build"):
        return fail("ERROR: (gcloud.builds.submit) build failed to start", 1)
    config, substitutions = opt(args, "--config"), opt(args, "--substitutions")
    sa, staging = opt(args, "--service-account"), opt(args, "--gcs-source-staging-dir")
    if args[-1] != "." or not config or not Path(config).is_file():
        return fail("ERROR: (gcloud.builds.submit) the build config is not in the directory being uploaded", 1)
    image = substitutions.split("=", 1)[1]
    files = sorted(p.relative_to(".").as_posix() for p in Path(".").rglob("*") if p.is_file())
    now = dt.datetime.now(dt.timezone.utc)
    build_id = str(uuid.uuid4())
    bucket, _, prefix = staging[len("gs://"):].partition("/")
    obj = f"{prefix}/{now.timestamp():.6f}-{uuid.uuid4().hex}.tgz"
    world.builds[build_id] = {
        "id": build_id, "status": "SUCCESS", "serviceAccount": sa, "createTime": now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z",
        "substitutions": {"_IMAGE": image}, "source": {"storageSource": {"bucket": bucket, "object": obj}},
        "results": {"images": [{"name": image, "digest": rw.CAND_DIGEST}]}}
    world.registry[image] = rw.CAND_DIGEST
    world.__dict__.setdefault("uploads", {})[obj] = files
    uploaded = f"gs://{bucket}/{obj}"
    row = f"{build_id}  {now.strftime('%Y-%m-%dT%H:%M:%S')}+00:00  2M22S     {uploaded}  {image}  SUCCESS"
    sys.stderr.write(f"Creating temporary archive of {len(files)} file(s) totalling 1.0 MiB before compression.\r\n"
                     f"Uploading tarball of [.] to [{uploaded}]\r\n"
                     f"Created [https://cloudbuild.googleapis.com/v1/projects/{PROJECT}/locations/us-central1/builds/{build_id}].\r\n"
                     f"Logs are available at [ https://console.cloud.google.com/cloud-build/builds;region=us-central1/{build_id}?project=1 ].\n\n"
                     "gcloud builds submit only displays logs from Cloud Storage. To view logs from Cloud Logging, run:\ngcloud beta builds submit\n\n"
                     "Waiting for build to complete. Polling interval: 1 second(s).\n")
    sys.stdout.write("ID                                    CREATE_TIME                DURATION  SOURCE  IMAGES  STATUS\r\n" + row + "\r\n")
    return 0


def parse_env(text):
    out = {}
    for pair in (text or "").split(","):
        if pair:
            key, _, value = pair.partition("=")
            out[key] = value
    return out


def run_deploy(world, args):
    name, image, suffix, tag = args[2], opt(args, "--image"), opt(args, "--revision-suffix"), opt(args, "--tag")
    if name not in world.svc or "--no-traffic" not in args or not image or "@sha256:" not in image:
        return fail("ERROR: (gcloud.run.deploy) unexpected deploy arguments", 1)
    revision = f"{name}-{suffix}"
    if revision in world.revisions:
        return fail(f"ERROR: (gcloud.run.deploy) revision {revision} already exists", 1)
    state = world.svc[name]
    env = json.loads(json.dumps(state["env"]))
    env.update(parse_env(opt(args, "--update-env-vars")))
    for key in (opt(args, "--remove-env-vars") or "").split(","):
        env.pop(key, None)
    for pair in (opt(args, "--set-secrets") or "").split(","):
        if pair:
            key, _, ref = pair.partition("=")
            secret, _, version = ref.partition(":")
            env[key] = {"valueFrom": {"secretKeyRef": {"name": secret, "key": version}}}
    annotations = dict(state["tmpl_ann"])
    for flag, key in (("--min-instances", "autoscaling.knative.dev/minScale"), ("--max-instances", "autoscaling.knative.dev/maxScale")):
        if opt(args, flag) is not None:
            annotations[key] = opt(args, flag)
    if "--no-cpu-throttling" in args:
        annotations["run.googleapis.com/cpu-throttling"] = "false"
    if "--no-invoker-iam-check" in args:
        state["annotations"]["run.googleapis.com/invoker-iam-disabled"] = "true"
    digest = image.split("@", 1)[1]
    raw = rw.revision_raw(revision, name, image, digest, env, annotations)
    service_account = opt(args, "--service-account")
    if service_account:
        raw["spec"]["serviceAccountName"] = service_account
    if opt(args, "--timeout"):
        raw["spec"]["timeoutSeconds"] = int(opt(args, "--timeout"))
    if opt(args, "--memory"):
        raw["spec"]["containers"][0]["resources"]["limits"]["memory"] = opt(args, "--memory")
    world.revisions[revision] = raw
    state.update(image=image, env=env, latest_created=revision, latest_ready=revision, tmpl_ann=annotations)
    state["traffic"].append({"revisionName": revision, "percent": 0, "tag": tag})
    world.revision_names[name].append(revision)
    sys.stderr.write(f"Service [{name}] revision [{revision}] has been deployed and is serving 0 percent of traffic.\n")
    return 0


def update_traffic(world, args):
    name = args[3]
    if name not in world.svc:
        return fail("ERROR: (gcloud.run.services.update-traffic) the service does not exist", 1)
    state = world.svc[name]
    target = next((a[len("--to-revisions="):] for a in args if a.startswith("--to-revisions=")), None)
    if target is not None:
        revision, _, percent = target.partition("=")
        if percent != "100" or revision not in world.revisions:
            return fail("ERROR: (gcloud.run.services.update-traffic) unexpected traffic arguments", 1)
        tagged = [dict(e, percent=0) for e in state["traffic"] if e.get("tag")]
        entry = next((e for e in tagged if e["revisionName"] == revision), None)
        if entry is None:
            entry = {"revisionName": revision, "percent": 100}
            tagged.insert(0, entry)
        entry["percent"] = 100
        state["traffic"] = tagged
    removed = opt(args, "--remove-tags")
    if removed is not None:
        kept = []
        for e in state["traffic"]:
            if e.get("tag") == removed:
                if e.get("percent"):
                    kept.append({k: v for k, v in e.items() if k != "tag"})
            else:
                kept.append(e)
        state["traffic"] = kept
    sys.stderr.write(f"Traffic updated for service [{name}].\n")
    return 0


def main(args):
    world = load()
    head = tuple(args[:3])
    if args[:2] == ["config", "list"]:
        code = say(world.config)
    elif head == ("run", "services", "describe") and args[3] in world.svc:
        code = say(world.service_raw(args[3]))
    elif head == ("run", "services", "get-iam-policy"):
        code = say(world.policy)
    elif head == ("run", "revisions", "describe"):
        code = say(world.revisions[args[3]]) if args[3] in world.revisions else fail("ERROR: NOT_FOUND: the revision does not exist")
    elif head == ("run", "revisions", "list"):
        code = say([{"metadata": {"name": n}} for n in world.revision_names[opt(args, "--service")]])
    elif head == ("run", "jobs", "describe"):
        code = say(world.jobs[args[3]])
    elif head == ("artifacts", "docker", "images"):
        digest = world.registry.get(args[4]) if args[3] == "describe" else None
        code = say({"image_summary": {"digest": digest}}) if digest else fail("ERROR: NOT_FOUND: the image was not found")
    elif head == ("builds", "describe"):
        code = say(world.builds[args[3]]) if args[3] in world.builds else fail("ERROR: NOT_FOUND: the build was not found")
    elif args[:2] == ["auth", "print-access-token"]:
        sys.stdout.write("e2e-cli-token\n")
        code = 0
    elif args[:2] == ["builds", "submit"]:
        code = builds_submit(world, args)
    elif args[:2] == ["run", "deploy"]:
        code = run_deploy(world, args)
    elif head == ("run", "services", "update-traffic"):
        code = update_traffic(world, args)
    else:
        code = fail("e2e gcloud: UNEXPECTED COMMAND", 97)
    save(world)
    log(args, code)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
