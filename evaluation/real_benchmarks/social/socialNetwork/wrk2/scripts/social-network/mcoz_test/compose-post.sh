nginx_ip=$(kubectl -n default get svc nginx-web-server -o jsonpath='{.spec.clusterIP}')
max_requests="${1:-50}"
max_requests="$max_requests" wrk -t1 -c1 -d600s -R1 -L -s compose-post.lua http://$nginx_ip:8080
