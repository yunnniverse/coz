#!/bin/bash

# 1️⃣ wrk를 먼저 시작 (20분 동안 실행)
echo "=== [Step 1] Starting wrk benchmark (20 minutes) ===" >> test/kepler_result.txt
wrk -D exp -t 1 -c 1 -d 1200 -L -s compose-post.lua http://10.106.15.18:8080 -R 1 &
wrk_pid=$!

# 2️⃣ wrk 시작 후 10분(600초) 대기한 후 첫 번째 curl 요청 실행
sleep 600
echo "=== [Step 2] First Kepler Metrics (after 10 minutes of wrk) ===" >> test/kepler_result.txt
curl 'http://localhost:9105/metrics' >> test/kepler_result.txt
echo -e "\n" >> test/kepler_result.txt

# 3️⃣ wrk가 끝나면 두 번째 curl 요청 실행
wait $wrk_pid
echo "=== [Step 3] Second Kepler Metrics (after wrk finishes) ===" >> test/kepler_result.txt
curl 'http://localhost:9105/metrics' >> test/kepler_result.txt
echo -e "\n" >> test/kepler_result.txt

sleep 3
echo "=== [Step 3] Second Kepler Metrics (after wrk finishes) ===" >> test/kepler_result.txt

# 4️⃣ 두 번째 curl 실행 후 15초 대기 후 kubectl logs 실행
sleep 10
kubectl logs pod/station-test >> test/cic_result.txt

kubectl delete pod/station-test

echo "✅ All steps completed!"
