#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
NAMESPACE=${NAMESPACE:-social-demo}
RELEASE=${RELEASE:-social-demo}
VALUES=${1:-$ROOT/values/values-social-demo-live-match-rammongo.yaml}
helm upgrade --install "$RELEASE" "$ROOT/helm/socialnetwork" \
  -n "$NAMESPACE" --create-namespace \
  -f "$VALUES"
kubectl wait --for=condition=Available deployment --all -n "$NAMESPACE" --timeout=10m
