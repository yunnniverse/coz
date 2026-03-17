#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS_TRACE="${NS_TRACE:-trace-demo}"

kubectl -n "${NS_TRACE}" create configmap mcoz-gate-script \
  --from-file=mcoz_gate.py="${ROOT_DIR}/scripts/mcoz_gate.py" \
  -o yaml --dry-run=client | kubectl apply -f -

# Disable request-credit hook on c (if previously enabled)
kubectl delete envoyfilter -n "${NS_TRACE}" c-mcoz-ext-authz --ignore-not-found=true
kubectl -n "${NS_TRACE}" patch deployment c --type='strategic' -p '{
  "spec": {
    "template": {
      "spec": {
        "containers": [{"name":"mcoz-gate","$patch":"delete"}],
        "volumes": [{"name":"mcoz-gate-script","$patch":"delete"}]
      }
    }
  }
}' || true

# Enable request-credit hook on d
kubectl apply -f "${ROOT_DIR}/k8s/request_credit/trace-demo-d-request-credit.yaml"

kubectl -n "${NS_TRACE}" rollout status deploy/c --timeout=180s
kubectl -n "${NS_TRACE}" rollout status deploy/d --timeout=180s

echo "[mcoz] request-credit victim switched: c -> d"
