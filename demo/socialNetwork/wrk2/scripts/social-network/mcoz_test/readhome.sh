#!/bin/bash

nginx_ip=$(kubectl get svc nginx-web-server -o jsonpath='{.spec.clusterIP}')

echo "=== [Step 1] Starting wrk (1req/s, 10s) ==="
wrk -D exp -t 1 -c 1 -d 10 -L -s read-home-timeline.lua http://$nginx_ip:8080 -R 1
# 스레드 하나, 연결 2개, 10초, 초당 100개 연결
