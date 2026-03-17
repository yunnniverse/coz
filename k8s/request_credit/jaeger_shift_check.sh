#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-16686}"
SERVICE="${SERVICE:-c.trace-demo}"
LOOKBACK="${LOOKBACK:-1h}"
LIMIT="${LIMIT:-20}"
OUT="${OUT:-/tmp/jaeger-${SERVICE//\//_}-$(date +%Y%m%d-%H%M%S).json}"

kubectl -n istio-system port-forward svc/tracing "${PORT}:80" >/tmp/mcoz-jaeger-pf.log 2>&1 &
PF_PID=$!
trap 'kill ${PF_PID} >/dev/null 2>&1 || true' EXIT
sleep 2

curl -sS "http://127.0.0.1:${PORT}/jaeger/api/traces?service=${SERVICE}&limit=${LIMIT}&lookback=${LOOKBACK}" >"${OUT}"
echo "saved: ${OUT}"

jq '{
  trace_count: (.data | length),
  samples: (.data[:5] | map({
    traceID,
    span_count: (.spans | length),
    max_span_ms: ((.spans | map(.duration) | max) / 1000.0)
  }))
}' "${OUT}"

cat <<'EOF'

Tip:
- OFF/ON 각각 이 스크립트를 실행해 `max_span_ms` 분포를 비교하세요.
- gate 오버헤드는 응답 헤더 `x-mcoz-gate-us` 및 gate /metrics에서 분리 확인 가능합니다.
EOF
