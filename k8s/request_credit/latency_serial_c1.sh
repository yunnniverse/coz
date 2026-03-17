#!/usr/bin/env bash
set -euo pipefail

URL="${1:-http://a.trace-demo.svc.cluster.local:5000/entry}"
REQUESTS="${2:-50}"
MODE="${MODE:-both}"                     # off|on|both
WARMUP_REQUESTS="${WARMUP_REQUESTS:-3}"  # per phase
INTERVAL_MS="${INTERVAL_MS:-200}"        # gap between requests
REQUEST_TIMEOUT_S="${REQUEST_TIMEOUT_S:-10}"
ON_DELAY_NS="${ON_DELAY_NS:-10000000}"
STOP_AFTER="${STOP_AFTER:-true}"
NS_MCOZ="${NS_MCOZ:-mcoz-system}"
CONTROL_URL="${CONTROL_URL:-http://coz-control.${NS_MCOZ}.svc.cluster.local:19091}"
OUT_DIR="${OUT_DIR:-/tmp/mcoz-latency-c1-$(date +%Y%m%d-%H%M%S)}"

if [[ "${MODE}" != "off" && "${MODE}" != "on" && "${MODE}" != "both" ]]; then
  echo "MODE must be one of: off, on, both"
  exit 1
fi
if ! [[ "${REQUESTS}" =~ ^[0-9]+$ ]] || [[ "${REQUESTS}" -le 0 ]]; then
  echo "REQUESTS must be a positive integer"
  exit 1
fi
if ! [[ "${WARMUP_REQUESTS}" =~ ^[0-9]+$ ]]; then
  echo "WARMUP_REQUESTS must be a non-negative integer"
  exit 1
fi
if ! [[ "${INTERVAL_MS}" =~ ^[0-9]+$ ]]; then
  echo "INTERVAL_MS must be a non-negative integer"
  exit 1
fi

mkdir -p "${OUT_DIR}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

sleep_ms() {
  local ms="$1"
  if [[ "${ms}" -le 0 ]]; then
    return 0
  fi
  python3 - <<PY
import time
time.sleep(${ms} / 1000.0)
PY
}

calc_summary() {
  local csv_path="$1"
  local summary_path="$2"
  python3 - "$csv_path" "$summary_path" <<'PY'
import csv
import math
import statistics
import sys

csv_path = sys.argv[1]
summary_path = sys.argv[2]

rows = []
with open(csv_path, "r", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        rows.append(row)

ok_times = []
all_times = []
non_200 = 0
curl_err = 0
for r in rows:
    rc = int(r["curl_rc"])
    code = int(r["http_code"])
    t = float(r["time_ms"])
    if math.isfinite(t):
        all_times.append(t)
    if rc != 0:
        curl_err += 1
    if code != 200:
        non_200 += 1
    if rc == 0 and code == 200 and math.isfinite(t):
        ok_times.append(t)

def pct(values, p):
    if not values:
        return float("nan")
    v = sorted(values)
    k = max(0, min(len(v) - 1, math.ceil(p * len(v)) - 1))
    return v[k]

lines = []
lines.append(f"rows={len(rows)}")
lines.append(f"ok_rows={len(ok_times)}")
lines.append(f"non_200={non_200}")
lines.append(f"curl_err={curl_err}")

target = ok_times if ok_times else all_times
if target:
    mean = statistics.fmean(target)
    stdev = statistics.pstdev(target) if len(target) > 1 else 0.0
    lines.append(f"mean_ms={mean:.3f}")
    lines.append(f"stddev_ms={stdev:.3f}")
    lines.append(f"min_ms={min(target):.3f}")
    lines.append(f"p50_ms={pct(target, 0.50):.3f}")
    lines.append(f"p90_ms={pct(target, 0.90):.3f}")
    lines.append(f"p95_ms={pct(target, 0.95):.3f}")
    lines.append(f"p99_ms={pct(target, 0.99):.3f}")
    lines.append(f"max_ms={max(target):.3f}")
else:
    lines.append("mean_ms=nan")
    lines.append("stddev_ms=nan")
    lines.append("min_ms=nan")
    lines.append("p50_ms=nan")
    lines.append("p90_ms=nan")
    lines.append("p95_ms=nan")
    lines.append("p99_ms=nan")
    lines.append("max_ms=nan")

with open(summary_path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print("\n".join(lines))
PY
}

run_phase() {
  local phase="$1"
  local csv_path="${OUT_DIR}/${phase}_serial.csv"
  local summary_path="${OUT_DIR}/${phase}_summary.txt"

  echo "idx,http_code,time_ms,curl_rc,request_id" > "${csv_path}"

  echo "[${phase}] warmup=${WARMUP_REQUESTS}"
  for i in $(seq 1 "${WARMUP_REQUESTS}"); do
    curl -sS -m "${REQUEST_TIMEOUT_S}" -o /dev/null "${URL}" || true
    sleep_ms "${INTERVAL_MS}"
  done

  echo "[${phase}] run requests=${REQUESTS} (strict serial c=1)"
  for i in $(seq 1 "${REQUESTS}"); do
    request_id="${phase}-$(date +%s%N)-${i}"
    curl_rc=0
    out="$(curl -sS -m "${REQUEST_TIMEOUT_S}" \
      -H "x-request-id: ${request_id}" \
      -o "${TMP_DIR}/body-${phase}-${i}.txt" \
      -w "%{http_code},%{time_total}" \
      "${URL}")" || curl_rc=$?

    http_code="${out%%,*}"
    time_total="${out#*,}"
    if [[ -z "${http_code}" || "${http_code}" == "${out}" ]]; then
      http_code="0"
      time_total="nan"
    fi

    time_ms="$(python3 - <<PY
import math
v = "${time_total}"
try:
    x = float(v)
    print(f"{x * 1000.0:.6f}" if math.isfinite(x) else "nan")
except Exception:
    print("nan")
PY
)"

    echo "${i},${http_code},${time_ms},${curl_rc},${request_id}" >> "${csv_path}"
    printf '[%s] %03d code=%s time_ms=%s rc=%s\n' "${phase}" "${i}" "${http_code}" "${time_ms}" "${curl_rc}"
    sleep_ms "${INTERVAL_MS}"
  done

  echo "[${phase}] summary"
  calc_summary "${csv_path}" "${summary_path}"
}

echo "URL=${URL}"
echo "CONTROL_URL=${CONTROL_URL}"
echo "MODE=${MODE}"
echo "OUT_DIR=${OUT_DIR}"

if [[ "${MODE}" == "off" || "${MODE}" == "both" ]]; then
  echo "[OFF] stop + clear(credits)"
  curl -sS -X POST "${CONTROL_URL}/stop?scope=all" >/dev/null || true
  curl -sS -X POST "${CONTROL_URL}/clear?scope=all&clear_credits=true" >/dev/null || true
  run_phase "off"
fi

if [[ "${MODE}" == "on" || "${MODE}" == "both" ]]; then
  echo "[ON] start request-credit delay_ns=${ON_DELAY_NS}"
  curl -sS -X POST "${CONTROL_URL}/start" \
    -H "Content-Type: application/json" \
    -d "{\"scope\":\"all\",\"force\":true,\"requestCredit\":true,\"fixedDelayNs\":\"${ON_DELAY_NS}\"}" >/dev/null
  curl -sS -X POST "${CONTROL_URL}/clear?scope=all&clear_credits=true" >/dev/null || true
  run_phase "on"
fi

if [[ "${STOP_AFTER}" == "true" ]]; then
  echo "[DONE] stop"
  curl -sS -X POST "${CONTROL_URL}/stop?scope=all" >/dev/null || true
fi

echo "saved: ${OUT_DIR}"
