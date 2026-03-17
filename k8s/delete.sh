#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KUBECTL="${KUBECTL:-kubectl}"
NS_MCOZ="${NS_MCOZ:-mcoz-system}"

${KUBECTL} delete -f "${ROOT_DIR}/job.yaml" --ignore-not-found=true
${KUBECTL} delete -f "${ROOT_DIR}/services.yaml" --ignore-not-found=true
${KUBECTL} delete -f "${ROOT_DIR}/daemonset.yaml" --ignore-not-found=true
${KUBECTL} delete -f "${ROOT_DIR}/delay_things/global_delay.yaml" --ignore-not-found=true
${KUBECTL} delete -f "${ROOT_DIR}/delay_things/crd.yaml" --ignore-not-found=true
${KUBECTL} delete -f "${ROOT_DIR}/sysctl-perf.yaml" --ignore-not-found=true
${KUBECTL} -n "${NS_MCOZ}" delete configmap mcoz-control-script --ignore-not-found=true
${KUBECTL} -n "${NS_MCOZ}" delete configmap coz-sync-deprecated --ignore-not-found=true
