#!/usr/bin/env bash
set -euo pipefail

if ! command -v hey >/dev/null 2>&1; then
  echo "hey is required (https://github.com/rakyll/hey)."
  exit 1
fi

URL="${1:-http://a.trace-demo.svc.cluster.local:5000/entry}"
NS_MCOZ="${NS_MCOZ:-mcoz-system}"
CONTROL_URL="${CONTROL_URL:-http://coz-control.${NS_MCOZ}.svc.cluster.local:19091}"
OUT_DIR="${OUT_DIR:-/tmp/mcoz-latency-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "${OUT_DIR}"

echo "[OFF] stop + clear"
curl -sS -X POST "${CONTROL_URL}/stop?scope=all" >/dev/null || true
curl -sS -X POST "${CONTROL_URL}/clear?scope=all" >/dev/null || true

echo "[OFF] c=1"
hey -n 50 -c 1 "${URL}" | tee "${OUT_DIR}/off_c1.txt"
echo "[OFF] c=10"
hey -n 200 -c 10 "${URL}" | tee "${OUT_DIR}/off_c10.txt"

echo "[ON] request-credit mode start (10ms)"
curl -sS -X POST "${CONTROL_URL}/start" \
  -H "Content-Type: application/json" \
  -d '{"scope":"all","force":true,"requestCredit":true,"fixedDelayNs":"10000000"}' >/dev/null
curl -sS -X POST "${CONTROL_URL}/clear?scope=all" >/dev/null || true

echo "[ON] c=1"
hey -n 50 -c 1 "${URL}" | tee "${OUT_DIR}/on_c1.txt"
echo "[ON] c=10"
hey -n 200 -c 10 "${URL}" | tee "${OUT_DIR}/on_c10.txt"

echo
echo "saved: ${OUT_DIR}"
