#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBECTL="${KUBECTL:-kubectl}"
NS_MCOZ="${NS_MCOZ:-mcoz-system}"
APPLY_SYSCTL_PERF="${APPLY_SYSCTL_PERF:-true}"
APPLY_GLOBAL_DELAY="${APPLY_GLOBAL_DELAY:-true}"

if [[ "${NS_MCOZ}" != "mcoz-system" ]]; then
  echo "k8s manifests are currently pinned to namespace mcoz-system" >&2
  echo "Set NS_MCOZ=mcoz-system or update the yaml manifests first." >&2
  exit 1
fi

${KUBECTL} apply -f "${ROOT_DIR}/k8s/namespace.yaml"
${KUBECTL} apply -f "${ROOT_DIR}/k8s/rbac.yaml"

if [[ "${APPLY_GLOBAL_DELAY}" == "true" ]]; then
  ${KUBECTL} apply -f "${ROOT_DIR}/k8s/delay_things/crd.yaml"
  ${KUBECTL} apply -f "${ROOT_DIR}/k8s/delay_things/global_delay.yaml"
fi

${KUBECTL} -n "${NS_MCOZ}" create configmap mcoz-control-script \
  --from-file=mcoz_control_api.py="${ROOT_DIR}/scripts/mcoz_control_api.py" \
  -o yaml --dry-run=client | ${KUBECTL} apply -f -

${KUBECTL} apply -f "${ROOT_DIR}/k8s/services.yaml"
${KUBECTL} apply -f "${ROOT_DIR}/k8s/daemonset.yaml"

if [[ "${APPLY_SYSCTL_PERF}" == "true" ]]; then
  ${KUBECTL} apply -f "${ROOT_DIR}/k8s/sysctl-perf.yaml"
fi

${KUBECTL} -n "${NS_MCOZ}" rollout status daemonset/coz --timeout=180s

echo "[mcoz] base daemon deployment applied"
