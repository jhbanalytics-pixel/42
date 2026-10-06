#!/usr/bin/env bash
# Smoke test for a running 42 service.
# Curls each route against $BASE_URL and prints the HTTP status per route.
#
# Usage:
#   scripts/smoke_test.sh <BASE_URL> [PASSCODE]
#   BASE_URL=https://listening-post-xxx.run.app PASSCODE=secret scripts/smoke_test.sh
#
# The /api/* routes carry the X-Passcode header when a passcode is supplied.
# The / and /card/* routes are open and forwardable, so they take no header.
# This needs a live server; it is the deployed-verification gate, not a unit test.

set -u

BASE_URL="${1:-${BASE_URL:-}}"
PASSCODE="${2:-${PASSCODE:-}}"

if [ -z "${BASE_URL}" ]; then
  echo "usage: smoke_test.sh <BASE_URL> [PASSCODE]" >&2
  echo "   or: BASE_URL=... PASSCODE=... smoke_test.sh" >&2
  exit 2
fi

# Trim a trailing slash so route paths join cleanly.
BASE_URL="${BASE_URL%/}"

PASS_HEADER=()
if [ -n "${PASSCODE}" ]; then
  PASS_HEADER=(-H "X-Passcode: ${PASSCODE}")
fi

# Each entry: label, path, gated (1 sends the passcode header, 0 does not).
ROUTES=(
  "health|/api/health|0"
  "index|/|0"
  "today|/api/today|1"
  "rails|/api/rails|1"
  "ask|/api/ask?q=fifa|1"
  "card|/card/fifa|0"
)

fail=0
printf '%-8s %-26s %s\n' "ROUTE" "PATH" "STATUS"
for entry in "${ROUTES[@]}"; do
  IFS='|' read -r label path gated <<< "${entry}"
  url="${BASE_URL}${path}"
  if [ "${gated}" = "1" ]; then
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "${PASS_HEADER[@]}" "${url}")
  else
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "${url}")
  fi
  rc=$?
  if [ "${rc}" -ne 0 ]; then
    printf '%-8s %-26s %s\n' "${label}" "${path}" "no response (curl ${rc})"
    fail=1
    continue
  fi
  printf '%-8s %-26s %s\n' "${label}" "${path}" "${code}"
  case "${code}" in
    2*) ;;
    401) [ "${gated}" = "1" ] && [ -z "${PASSCODE}" ] && echo "  note: passcode gate is on; pass a PASSCODE to clear this" ;;
    *) fail=1 ;;
  esac
done

if [ "${fail}" -ne 0 ]; then
  echo ""
  echo "One or more routes did not return a healthy status."
  exit 1
fi
echo ""
echo "All routes responded."
