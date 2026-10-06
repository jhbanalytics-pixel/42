#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE="listening-post-staging"
PROJECT="ogilvy-trends-v2"
REGION="us-central1"
BRANCH="feat/42-redesign-staging"
SERVICE_ACCOUNT="listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
CACHE_PREFIX="open-intelligence/v2/staging/"

readback_matches_contract() {
  jq -e \
    --arg service "${SERVICE}" \
    --arg dataset "trends_v2_staging" \
    --arg identity "${SERVICE_ACCOUNT}" \
    --arg bucket "listening-post-staging-cache" \
    --arg prefix "${CACHE_PREFIX}" \
    --arg application_source "open-intelligence-staging" \
    --arg source_sha "${SOURCE_SHA}" \
    '
      .metadata.name == $service
      and .metadata.labels.environment == "staging"
      and .metadata.labels["application-source"] == $application_source
      and .spec.template.metadata.labels.environment == "staging"
      and .spec.template.metadata.labels["application-source"] == $application_source
      and .spec.template.spec.serviceAccountName == $identity
      and (.spec.template.spec.containers[0].env | map(select(.name == "BQ_DATASET") | .value) | first) == $dataset
      and (.spec.template.spec.containers[0].env | map(select(.name == "CACHE_BUCKET") | .value) | first) == $bucket
      and (.spec.template.spec.containers[0].env | map(select(.name == "CACHE_PREFIX") | .value) | first) == $prefix
      and (.spec.template.spec.containers[0].env | map(select(.name == "APPLICATION_SOURCE") | .value) | first) == $application_source
      and (.spec.template.spec.containers[0].env | map(select(.name == "DEPLOYMENT_PROFILE") | .value) | first) == "open-intelligence-staging"
      and (.spec.template.spec.containers[0].env | map(select(.name == "SOURCE_SHA") | .value) | first) == $source_sha
      and .metadata.labels["source-sha"] == $source_sha
      and .spec.template.metadata.labels["source-sha"] == $source_sha
      and (.spec.template.spec.containers[0].env | map(select(.valueFrom.secretKeyRef.name? != null) | .valueFrom.secretKeyRef.name) | sort) == ["ui-passcode-staging"]
    '
}

latest_ready_revision() {
  jq -r '.status.latestReadyRevisionName // empty'
}

require_previous_ready_revision() {
  if [[ -z "${1:-}" ]]; then
    echo "No previous ready revision was returned for ${SERVICE}." >&2
    return 1
  fi
}

print_rollback_command() {
  printf 'Readback passed. Roll back with: gcloud run services update-traffic %q --project %q --region %q --to-revisions %q=100\n' \
    "${SERVICE}" "${PROJECT}" "${REGION}" "${1}"
}

main() {
if [[ "${1:-}" != "" && "${1:-}" != "--plan" ]]; then
  echo "Usage: $0 [--plan]" >&2
  exit 2
fi
PLAN_MODE=false
if [[ "${1:-}" == "--plan" ]]; then
  PLAN_MODE=true
fi

GIT_TOPLEVEL="$(git -C "${ROOT}" rev-parse --show-toplevel)"
if [[ "$(cd "${GIT_TOPLEVEL}" && pwd)" != "${ROOT}" ]]; then
  echo "Refusing to deploy outside the 42 worktree." >&2
  exit 1
fi
if [[ "$(git -C "${ROOT}" branch --show-current)" != "${BRANCH}" ]]; then
  echo "Refusing to deploy outside branch ${BRANCH}." >&2
  exit 1
fi
if [[ -n "$(git -C "${ROOT}" status --porcelain --untracked-files=no)" ]]; then
  echo "Refusing dirty tracked worktree." >&2
  exit 1
fi

SOURCE_SHA="$(git -C "${ROOT}" rev-parse HEAD)"
if [[ ! "${SOURCE_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Refusing a non-full current commit SHA." >&2
  exit 1
fi

ENV_VARS="GCP_PROJECT=${PROJECT}"
ENV_VARS="${ENV_VARS},BQ_DATASET=trends_v2_staging"
ENV_VARS="${ENV_VARS},CACHE_TTL=86400"
ENV_VARS="${ENV_VARS},CACHE_BUCKET=listening-post-staging-cache"
ENV_VARS="${ENV_VARS},CACHE_PREFIX=${CACHE_PREFIX}"
ENV_VARS="${ENV_VARS},APPLICATION_SOURCE=open-intelligence-staging"
ENV_VARS="${ENV_VARS},DEPLOYMENT_PROFILE=open-intelligence-staging"
ENV_VARS="${ENV_VARS},SOURCE_SHA=${SOURCE_SHA}"
LABELS="environment=staging,application-source=open-intelligence-staging,source-sha=${SOURCE_SHA}"
SECRETS="UI_PASSCODE=ui-passcode-staging:latest"
DEPLOY_ARGS=(
  gcloud run deploy "${SERVICE}"
  --source "${ROOT}"
  --project "${PROJECT}"
  --region "${REGION}"
  --allow-unauthenticated
  --min-instances 0
  --max-instances 1
  --memory 1Gi
  --timeout 120
  --service-account "${SERVICE_ACCOUNT}"
  --set-env-vars "${ENV_VARS}"
  --set-secrets "${SECRETS}"
  --labels "${LABELS}"
)

if ${PLAN_MODE}; then
  printf 'PLAN_DEPLOY'
  printf ' %q' "${DEPLOY_ARGS[@]}"
  printf '\n'
  printf 'PLAN_READBACK service=%s dataset=%s identity=%s bucket=%s prefix=%s application-source=%s source-sha=%s secret=ui-passcode-staging\n' \
    "${SERVICE}" "trends_v2_staging" "${SERVICE_ACCOUNT}" "listening-post-staging-cache" \
    "${CACHE_PREFIX}" "open-intelligence-staging" "${SOURCE_SHA}"
  exit 0
fi

command -v gcloud >/dev/null 2>&1 || { echo "gcloud is required." >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "jq is required for deployment readback." >&2; exit 1; }

PREVIOUS_READY_REVISION="$(gcloud run services describe "${SERVICE}" --project "${PROJECT}" --region "${REGION}" --format=json | latest_ready_revision)"
require_previous_ready_revision "${PREVIOUS_READY_REVISION}"
printf 'Previous ready revision: %s\n' "${PREVIOUS_READY_REVISION}"

(
  cd "${ROOT}/frontend"
  bun install --frozen-lockfile
  bun run build
)
if [[ ! -f "${ROOT}/web/dist/index.html" ]]; then
  echo "Frontend build did not produce web/dist/index.html." >&2
  exit 1
fi

"${DEPLOY_ARGS[@]}"

SERVICE_JSON="$(gcloud run services describe "${SERVICE}" --project "${PROJECT}" --region "${REGION}" --format=json)"
if ! readback_matches_contract <<<"${SERVICE_JSON}" >/dev/null; then
  echo "Deployment readback failed the staging isolation contract." >&2
  exit 1
fi

print_rollback_command "${PREVIOUS_READY_REVISION}"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
