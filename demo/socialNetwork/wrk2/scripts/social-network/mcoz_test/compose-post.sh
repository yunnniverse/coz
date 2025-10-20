#!/bin/bash

nginx_ip=$(kubectl get svc nginx-web-server -o jsonpath='{.spec.clusterIP}')

echo "=== [Step 1] Starting wrk (100req/s, 10s) ==="
wrk -D exp -t 2 -c 2 -d 1 -L -s compose-post.lua http://$nginx_ip:8080 -R 10
# 스레드 하나, 연결 2개, 10초, 초당 100개 연결