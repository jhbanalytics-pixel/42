#!/usr/bin/env bash
# Candidate deploy of f42-agent and f42-api (W8-REL 2.10), staging only: project ogilvy-trends-v2.
#   bash core/api/deploy_candidate.sh <manifest path> <manifest sha256 read from the Freeze readback>
# Deploys the image of the frozen manifest, by digest, as a new revision of each service that takes no traffic and
# carries the release tag. It does not build, smoke or touch traffic: the services must already be pinned to their
# serving revisions by name, and the helper phases (BeforeCandidate before, BeforeSmoke after) judge the result.
# The ordinary deploy.sh is for services that follow LATEST and refuses these.
set -euo pipefail

PROJECT=ogilvy-trends-v2
REGION=us-central1
REPO="${REGION}-docker.pkg.dev/${PROJECT}/intelligence-42/f42-web"
SA() { echo "$1@${PROJECT}.iam.gserviceaccount.com"; }

if [ "$#" -ne 2 ]; then
  echo "deploy_candidate.sh: usage: deploy_candidate.sh <manifest path> <manifest sha256>" >&2
  exit 64
fi
MANIFEST=$1
WANT_HASH=$2

# 1. The manifest is the whole input. The hash argument is the value the Freeze readback recorded; it must equal the
# file's own recomputed hash, the schema version must be known, and the tag, image reference and tag URLs must have
# the exact forms the helper derives. Every value printed below has passed a pattern, so it is safe to eval.
VALUES=$(py -3.13 -c '
import hashlib
import json
import re
import shlex
import sys


def stop(code, text):
    sys.stderr.write(f"deploy_candidate.sh: STOP: {code}: {text}\n")
    sys.exit(65)


manifest_path, want_hash, repo = sys.argv[1:4]
manifest = json.load(open(manifest_path, encoding="utf-8"))
body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
have = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
if not re.fullmatch(r"[0-9a-f]{64}", want_hash) or have != want_hash or manifest.get("manifest_sha256") != have:
    stop("MANIFEST_HASH", "the hash argument is not the manifest hash")
if manifest.get("schema_version") != 1 or manifest.get("mode") != "services-only":
    stop("SCHEMA_VERSION", "unknown manifest version or mode")
rid = manifest.get("release_id")
source = manifest.get("source", {})
if not isinstance(rid, str) or not re.fullmatch(r"rel-[0-9a-f]{7}-[0-9]{2}", rid):
    stop("TAG_GRAMMAR", "the release id is not rel-<sha7>-<nn>")
commit, tree, short12 = source.get("commit", ""), source.get("tree", ""), source.get("short12", "")
if not (re.fullmatch(r"[0-9a-f]{40}", commit) and re.fullmatch(r"[0-9a-f]{40}", tree) and short12 == commit[:12]
        and source.get("short7") == commit[:7] and rid[4:11] == commit[:7]):
    stop("SOURCE", "the source commit, tree or short forms are malformed or disagree")
digest = manifest.get("image", {}).get("digest", "")
if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or manifest["image"].get("reference") != f"{repo}@{digest}":
    stop("DIGEST", "the image must be the service repository at the manifest digest")
canon = manifest.get("canonical", {})
tag_urls = {}
for name, key in (("f42-agent", "agent_url"), ("f42-api", "api_url")):
    url = canon.get(key, "")
    if not re.fullmatch(r"https://[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)+", url):
        stop("CANONICAL", f"the canonical {name} URL is malformed")
    host = url[len("https://"):]
    candidate = manifest.get("candidates", {}).get(name, {})
    if candidate.get("tag") != rid or candidate.get("revision") != f"{name}-{rid}":
        stop("TAG_GRAMMAR", f"the {name} candidate does not carry the release id")
    if len(rid) + 3 + len(host.split(".")[0]) > 63:
        stop("TAG_GRAMMAR", f"the tag host label for {name} is longer than 63")
    if candidate.get("tag_url") != f"https://{rid}---{host}":
        stop("TAG_URL", f"the {name} tag URL is not the release id and the canonical host")
    tag_urls[name] = candidate["tag_url"]
allowed = manifest.get("allowed_revisions", {})
for name in ("f42-agent", "f42-api"):
    names = allowed.get(name)
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and re.fullmatch(r"[a-z0-9-]+", n) for n in names):
        stop("UNRELATED_REVISION", f"the allowed revisions of {name} are malformed")
for key, value in (("RID", rid), ("COMMIT", commit), ("TREE", tree), ("SHORT12", short12), ("IMAGE_REF", manifest["image"]["reference"]),
                   ("CANON_AGENT", canon["agent_url"]), ("CANON_API", canon["api_url"]),
                   ("AGENT_TAG_URL", tag_urls["f42-agent"]), ("API_TAG_URL", tag_urls["f42-api"])):
    print(f"{key}={shlex.quote(value)}")
' "$MANIFEST" "$WANT_HASH" "$REPO")
eval "$VALUES"

# HEAD must be the manifest source, and TAG (the F42_VERSION the services carry) the manifest's short form.
if [ "$(git rev-parse HEAD)" != "$COMMIT" ] || [ "$(git rev-parse 'HEAD^{tree}')" != "$TREE" ] \
   || [ "$(git rev-parse --short=12 HEAD)" != "$SHORT12" ]; then
  echo "deploy_candidate.sh: STOP: SOURCE: HEAD is not the commit and tree of the manifest." >&2
  exit 65
fi
TAG=$SHORT12

# Shared Cloud Run definitions, unchanged.
source core/api/deploy_flags.env

# 2. The private-agent preflight of deploy.sh, unchanged in logic.
gcloud run services describe f42-agent --project "$PROJECT" --region "$REGION" --format=json | py -3.13 -c '
import json
import sys
service = json.load(sys.stdin)
disabled = service.get("metadata", {}).get("annotations", {}).get("run.googleapis.com/invoker-iam-disabled")
if disabled in ("true", True):
    sys.exit("f42-agent has its invoker IAM check disabled. IAM approval is required before redeploying.")
'
gcloud run services get-iam-policy f42-agent --project "$PROJECT" --region "$REGION" --format=json | py -3.13 -c '
import json
import sys
policy = json.load(sys.stdin)
if any(member in ("allUsers", "allAuthenticatedUsers")
       for binding in policy.get("bindings", []) if binding.get("role") == "roles/run.invoker"
       for member in binding.get("members", [])):
    sys.exit("f42-agent permits public invocation. IAM approval is required before redeploying.")
'

# 3. Refuse a service that already holds the tag or the candidate revision, still follows LATEST (the Pin is missing),
# lists a revision outside the manifest's allowed set, or whose live URL is not the manifest's canonical URL.
for service in f42-agent f42-api; do
  DESC=$(gcloud run services describe "$service" --project "$PROJECT" --region "$REGION" --format=json)
  REVS=$(gcloud run revisions list --service "$service" --project "$PROJECT" --region "$REGION" --format=json)
  DESC="$DESC" REVS="$REVS" py -3.13 -c '
import json
import os
import sys

manifest_path, service, rid, canonical = sys.argv[1:5]
manifest = json.load(open(manifest_path, encoding="utf-8"))
desc = json.loads(os.environ["DESC"])
live = {r.get("metadata", {}).get("name") for r in json.loads(os.environ["REVS"])}


def stop(code, text):
    sys.stderr.write(f"deploy_candidate.sh: STOP: {code}: {service}: {text}\n")
    sys.exit(65)


if desc.get("status", {}).get("url") != canonical:
    stop("CANONICAL", "the live service URL is not the manifest canonical URL")
entries = [e for part in ("spec", "status") for e in desc.get(part, {}).get("traffic", [])]
if any(e.get("latestRevision") for e in entries):
    stop("NOT_PINNED", "the service follows LATEST, so the Pin has not been applied")
if any(e.get("tag") == rid for e in entries):
    stop("TAG_MAPPING", "the service already holds the release tag")
if f"{service}-{rid}" in live:
    stop("UNRELATED_REVISION", "the candidate revision already exists")
extra = sorted(live - set(manifest["allowed_revisions"][service]))
if extra:
    stop("UNRELATED_REVISION", f"revisions outside the allowed set: {len(extra)}")
' "$MANIFEST" "$service" "$RID" "$([ "$service" = f42-agent ] && echo "$CANON_AGENT" || echo "$CANON_API")"
done

# After a deploy, the service must show the tag once, on the candidate revision, at 0%, with the expected URL.
check_tag() {
  DESC=$(gcloud run services describe "$1" --project "$PROJECT" --region "$REGION" --format=json)
  DESC="$DESC" py -3.13 -c '
import json
import os
import sys

service, rid, tag_url = sys.argv[1:4]
desc = json.loads(os.environ["DESC"])
entries = [e for e in desc.get("status", {}).get("traffic", []) if e.get("tag") == rid]
ok = (len(entries) == 1 and entries[0].get("revisionName") == f"{service}-{rid}" and entries[0].get("percent", 0) == 0
      and entries[0].get("url") == tag_url)
if not ok:
    sys.stderr.write(f"deploy_candidate.sh: STOP: TAG_MAPPING: {service}: the tag is not on the candidate revision at 0% with the expected URL\n")
    sys.exit(65)
' "$1" "$RID" "$2"
}

# 3b. The declared environment removal. --update-env-vars merges into the existing environment, so a variable the release
# declares removed stays on the service unless the deploy also names it for removal. The names are not in the repository: the
# live variable names of f42-agent are hashed and matched against core/setup/release/declared_env_removals.py, and only the
# matches are removed. A name is never printed, only the count. A matched name that is not a plain identifier stops here,
# before any deploy.
AGENT_LIVE=$(gcloud run services describe f42-agent --project "$PROJECT" --region "$REGION" --format=json)
REMOVE_ENV=$(AGENT_LIVE="$AGENT_LIVE" py -3.13 -c '
import json
import os
import sys

from core.setup.release.declared_env_removals import PLAIN_NAME, declared_live_names

matched = declared_live_names("f42-agent", json.loads(os.environ["AGENT_LIVE"]))
if not all(PLAIN_NAME.fullmatch(name) for name in matched):
    sys.stderr.write("deploy_candidate.sh: STOP: DECLARED_ENV: f42-agent: a declared variable name is not a plain identifier\n")
    sys.exit(65)
if matched:
    sys.stderr.write(f"deploy_candidate.sh: removing {len(matched)} declared environment variable(s) from f42-agent\n")
print(",".join(matched))
')
REMOVE_ARGS=()
if [ -n "$REMOVE_ENV" ]; then REMOVE_ARGS=(--remove-env-vars "$REMOVE_ENV"); fi

# 4 and 5. The agent first. The API is not deployed unless the agent tag is right.
gcloud run deploy f42-agent --project "$PROJECT" --region "$REGION" --image "$IMAGE_REF" \
  --revision-suffix "$RID" --tag "$RID" --no-traffic \
  --service-account "$(SA "$AGENT_SA")" $AGENT_FLAGS \
  --update-env-vars "$AGENT_ENV" ${REMOVE_ARGS[@]+"${REMOVE_ARGS[@]}"} \
  --set-secrets "$AGENT_SECRETS"
check_tag f42-agent "$AGENT_TAG_URL"

# 6 and 7. The API reaches the agent candidate through its tag URL and mints the token for the canonical URL. Both
# values come from the manifest.
gcloud run deploy f42-api --project "$PROJECT" --region "$REGION" --image "$IMAGE_REF" \
  --revision-suffix "$RID" --tag "$RID" --no-traffic \
  --service-account "$(SA "$API_SA")" $API_FLAGS \
  --update-env-vars "${API_ENV},AGENT_URL=${AGENT_TAG_URL},AGENT_AUDIENCE=${CANON_AGENT}" \
  --set-secrets "$API_SECRETS"
check_tag f42-api "$API_TAG_URL"

echo "deploy_candidate.sh: ${RID} deployed to f42-agent and f42-api with no traffic." >&2
