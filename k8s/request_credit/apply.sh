#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS_TRACE="${NS_TRACE:-trace-demo}"

bash "${ROOT_DIR}/k8s/apply.sh"
kubectl -n "${NS_TRACE}" create configmap mcoz-gate-script \
  --from-file=mcoz_gate.py="${ROOT_DIR}/scripts/mcoz_gate.py" \
  -o yaml --dry-run=client | kubectl apply -f -
kubectl apply -f "${ROOT_DIR}/k8s/request_credit/trace-demo-c-request-credit.yaml"
kubectl -n "${NS_TRACE}" rollout status deploy/c --timeout=180s

echo "[mcoz] request-credit base + gate/ext_authz manifests applied"
