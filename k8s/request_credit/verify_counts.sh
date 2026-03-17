#!/usr/bin/env bash
set -euo pipefail

NS_MCOZ="${NS_MCOZ:-mcoz-system}"
NS_TRACE="${NS_TRACE:-trace-demo}"
CONTROL_URL="${CONTROL_URL:-http://coz-control.${NS_MCOZ}.svc.cluster.local:19091}"
VICTIM_APP="${VICTIM_APP:-c}"
STATUS_JSON="$(curl -sS "${CONTROL_URL}/status" || true)"

echo "[1/4] mcoz daemon status"
printf '%s' "${STATUS_JSON}" | jq '.daemon.payload.global // .daemon.global // .global // .daemon // .'

echo
echo "[2/4] victim cgroup rows"
printf '%s' "${STATUS_JSON}" | jq '.daemon.payload.cgroups // .daemon.cgroups // .cgroups // []'

echo
echo "[3/4] ext_authz gate log counters (TRIGGERED/ARMED/FAILED)"
POD="$(kubectl -n "${NS_TRACE}" get pod -l app="${VICTIM_APP}" -o jsonpath='{.items[0].metadata.name}')"
kubectl -n "${NS_TRACE}" logs "${POD}" -c mcoz-gate --tail=2000 \
  | awk '
      /state=TRIGGERED/ {t++}
      /state=ARMED/ {a++}
      /state=FAILED/ {f++}
      END {
        printf("triggered=%d armed=%d failed=%d\n", t+0, a+0, f+0)
      }'

echo
echo "[4/4] hostctl state counters (CONSUME_TRIGGER/INJECTED/FAILED)"
kubectl -n "${NS_MCOZ}" logs ds/coz -c coz --tail=4000 \
  | awk '
      /state=CONSUME_TRIGGER/ {c++}
      /state=INJECTED/ {i++}
      /state=FAILED/ {f++}
      END {
        printf("consume_trigger=%d injected=%d failed=%d\n", c+0, i+0, f+0)
      }'

cat <<'EOF'

[Optional] Kernel-level bpftrace check (id==449 and recv* syscall counts):
  TARGET_CGID=$(curl -s "${CONTROL_URL}/status" | jq -r '.daemon.payload.cgroups[0].cgroup_id')
  sudo bpftrace -D TARGET_CGID=${TARGET_CGID} k8s/request_credit/bpftrace_request_credit.bt
EOF
