#!/usr/bin/env bash
# Staging deploy of f42-agent and f42-api (task 1.14) from Albert's PC, run as
# f42-builder (SETUP.md). Staging only: project ogilvy-trends-v2.
# Needs, before the first run: bootstrap.py applied (identities, UI_PASSCODE
# secret with f42-web access) and the runs columns answer and record (L1).
#   bash core/api/deploy.sh               build in Cloud Build, deploy both, smoke test
#   bash core/api/deploy.sh --local-build build with local Docker, push as f42-builder
#   bash core/api/deploy.sh --no-build    redeploy the image of the current commit
#   add --no-traffic to any of these to deploy and check but leave traffic where it is
# After the health check passes, both services send 100% of traffic to their newest revision.
# A build refuses a tree with uncommitted or untracked files (exit 65): the image is labelled with HEAD. See the
# guard below for what --no-build and --local-build also check.
set -euo pipefail

PROJECT=ogilvy-trends-v2
REGION=us-central1
TAG=$(git rev-parse --short=12 HEAD)
IMAGE="us-central1-docker.pkg.dev/${PROJECT}/intelligence-42/f42-web:${TAG}"
SA() { echo "$1@${PROJECT}.iam.gserviceaccount.com"; }

BUILD=cloud
TRAFFIC=yes
for arg in "$@"; do
  case "$arg" in
    --no-build|--local-build) BUILD="$arg" ;;
    --no-traffic) TRAFFIC=no ;;
    *) echo "deploy.sh: unknown option $arg" >&2; exit 64 ;;
  esac
done

# The image is tagged and labelled with HEAD, so what goes into it, and what runs the release, must be HEAD.
# --untracked-files=all so a repo setting that hides untracked files cannot hide one here.
#   build            the whole tree: any change or untracked file refuses it.
#   --no-build       the image already built for HEAD, but the flags, this script and the release smoke script
#                    and the core it imports run from disk, so any change under core/ refuses it.
#   --local-build    also refuses ignored files in the Docker context (core, app/frontend, docs/full-42/reference):
#                    there is no .dockerignore, so .env files or node_modules would be copied into the image.
#                    __pycache__ directories are tolerated, since the image sets PYTHONDONTWRITEBYTECODE.
# The Cloud Build upload follows the .gitignore rules, so ignored files are not checked for it.
if [ "$BUILD" = --no-build ]; then
  DIRTY=$(git status --porcelain --untracked-files=all -- core)
else
  DIRTY=$(git status --porcelain --untracked-files=all)
fi
if [ -n "$DIRTY" ]; then
  echo "deploy.sh: the working tree has uncommitted changes, so it is not commit ${TAG}. Commit or stash them first:" >&2
  echo "$DIRTY" >&2
  exit 65
fi
if [ "$BUILD" = --local-build ]; then
  IGNORED=$(git status --porcelain --ignored --untracked-files=all -- core app/frontend docs/full-42/reference \
    | grep '^!!' | grep -v '__pycache__/' || true)
  if [ -n "$IGNORED" ]; then
    echo "deploy.sh: ignored files are in the Docker build context and would be copied into the image of commit ${TAG}." >&2
    echo "Remove them, or build from a fresh clone:" >&2
    echo "$IGNORED" >&2
    exit 65
  fi
fi

# Shared build source and Cloud Run definitions.
source core/api/deploy_flags.env

# Ordinary redeploys preserve IAM. Refuse a public agent before building or deploying.
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

case "$BUILD" in
  --no-build) ;;
  --local-build)
    # A private Docker config sends the push through gcloud's credential helper,
    # so it runs as the impersonated f42-builder and never as a stored login.
    DOCKER_CONFIG=$(mktemp -d)
    export DOCKER_CONFIG
    echo '{"credHelpers": {"us-central1-docker.pkg.dev": "gcloud"}}' > "$DOCKER_CONFIG/config.json"
    DOCKER_BUILDKIT=1 docker build -f core/api/Dockerfile -t "$IMAGE" .
    docker push "$IMAGE"
    ;;
  *)
    gcloud builds submit --project "$PROJECT" --region "$REGION" \
      --config core/api/cloudbuild.yaml --substitutions "_IMAGE=${IMAGE}" \
      --gcs-source-staging-dir "$BUILD_SOURCE_STAGING_DIR" \
      --service-account "projects/${PROJECT}/serviceAccounts/$(SA f42-deployer)" .
    ;;
esac

gcloud run deploy f42-agent --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --service-account "$(SA "$AGENT_SA")" $AGENT_FLAGS \
  --update-env-vars "$AGENT_ENV" \
  --set-secrets "$AGENT_SECRETS"

AGENT_URL=$(gcloud run services describe f42-agent --project "$PROJECT" --region "$REGION" --format 'value(status.url)')

gcloud run deploy f42-api --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --service-account "$(SA "$API_SA")" $API_FLAGS \
  --update-env-vars "${API_ENV},AGENT_URL=${AGENT_URL}" \
  --set-secrets "$API_SECRETS"

API_URL=$(gcloud run services describe f42-api --project "$PROJECT" --region "$REGION" --format 'value(status.url)')

# Health check. With F42_SMOKE_PASSCODE set, the full smoke test; without it, health and the passcode gate only,
# as the staging workflow checks. A failure stops the script here, before any traffic moves. The full smoke test
# prints how old the Today brief is; set F42_SMOKE_TODAY_MAX_AGE_HOURS to also fail it on a brief older than that.
if [ -n "${F42_SMOKE_PASSCODE:-}" ]; then
  py -3.13 core/api/smoke.py "$API_URL"
else
  echo "F42_SMOKE_PASSCODE is not set: checking health and the passcode gate only." >&2
  py -3.13 - "$API_URL" <<'PY'
import sys
import httpx
from core.api import smoke
base = sys.argv[1].rstrip("/")
results = []
with httpx.Client(timeout=smoke.TIMEOUT) as client:
    for name, check in (("health", smoke.check_health), ("gate", smoke.check_gate)):
        try:
            results.append((name, *check(client, base)))
        except Exception as exc:
            results.append((name, False, smoke._failed(exc)))
for name, ok, evidence in results:
    print(f"{'PASS' if ok else 'FAIL'} {name}: {evidence}", flush=True)
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
PY
fi

if [ "$TRAFFIC" = no ]; then
  echo "--no-traffic: traffic left where it was on f42-agent and f42-api." >&2
  exit 0
fi

# Route all traffic to the newest revision, the agent first since f42-api calls it.
for service in f42-agent f42-api; do
  gcloud run services update-traffic "$service" --project "$PROJECT" --region "$REGION" --to-latest
done

# f42-api now serves this commit: its /api/health reports F42_VERSION, which is TAG.
py -3.13 - "$API_URL" "$TAG" <<'PY'
import sys
import httpx
base, tag = sys.argv[1].rstrip("/"), sys.argv[2]
try:
    version = httpx.get(f"{base}/api/health", timeout=30.0).json().get("version")
except Exception as exc:
    version = f"nothing readable ({type(exc).__name__})"
print(f"{'PASS' if version == tag else 'FAIL'} version: f42-api serves {version}, this commit is {tag}", flush=True)
sys.exit(0 if version == tag else 1)
PY
