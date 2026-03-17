#!/bin/bash

IP=$1
DURATION=${2:-10}
RATE=${3:-100}

if [ -z "$IP" ]; then
  echo "Usage: $0 <IP> [DURATION_SECONDS] [RATE]"
  exit 1
fi

echo "=== [Step 1] Starting wrk2 (${RATE}req/s, ${DURATION}s) ==="

wrk2 -t2 -c2 -d${DURATION}s -R${RATE} --latency \
    -s compose-post.lua \
    http://${IP}:8080 &

wrk_pid=$!

