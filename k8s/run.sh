#!/usr/bin/env bash
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NS_TARGET="${NS_TARGET:-trace-demo}"
TARGET_LABEL="${TARGET_LABEL:-app=d}"
NS_MCOZ="${NS_MCOZ:-mcoz-system}"
CONTROL_URL="${CONTROL_URL:-http://coz-control.${NS_MCOZ}.svc.cluster.local:19091}"
SPEEDUP="${SPEEDUP:-0.5}"
SCOPE="${SCOPE:-all}"
FORCE="${FORCE:-true}"

TARGET_POD="${TARGET_POD:-$(${KUBECTL} -n "${NS_TARGET}" get pod -l "${TARGET_LABEL}" -o jsonpath='{.items[0].metadata.name}')}"

if [[ -z "${TARGET_POD}" ]]; then
  echo "failed to resolve target pod from namespace=${NS_TARGET} label=${TARGET_LABEL}" >&2
  exit 1
fi

curl -sS -X POST "${CONTROL_URL}/start" \
  -H "Content-Type: application/json" \
  -d "{\"targetPod\":\"${NS_TARGET}/${TARGET_POD}\",\"speedup\":\"${SPEEDUP}\",\"scope\":\"${SCOPE}\",\"force\":${FORCE}}"
echo
