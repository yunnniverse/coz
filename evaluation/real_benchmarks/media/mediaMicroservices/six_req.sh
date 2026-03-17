#!/bin/bash

# wrk 명령어들을 백그라운드에서 실행
echo "[$(date '+%Y-%m-%d %H:%M:%S')] wrk 실행 시작"
wrk -D exp -t 1 -c 1 -d 1800 -L -s wrk2/scripts/media-microservices/compose-review.lua http://10.104.184.4:8080/wrk2-api/review/compose -R 4 &
wrk -D exp -t 1 -c 1 -d 1800 -L -s wrk2/scripts/media-microservices/movie-register.lua http://10.104.184.4:8080/wrk2-api/movie/register -R 4 &
wrk -D exp -t 1 -c 1 -d 1800 -L -s wrk2/scripts/media-microservices/user-register.lua http://10.104.184.4:8080/wrk2-api/user/register -R 4 &
# 위 wrk 명령어들은 300초 동안 실행됩니다.
wrk_pid=$!

# curl 요청을 별도로 반복: 3개 보내고 2초 대기
j=1000
while true; do
    # wrk 프로세스가 살아있는지 확인
    if ! kill -0 "$wrk_pid" 2>/dev/null; then
        echo "wrk 프로세스 종료됨. 루프 종료."
        break
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] curl 배치, j = $j"
    
    # 총 요청 개수를 1~4개 사이의 랜덤 값으로 결정
    total=6
    # cast에 보낼 건수 a: 0 ~ total 사이의 랜덤 값
    a=$(( RANDOM % (total + 1) ))
    remain=$(( total - a ))
    # movie에 보낼 건수 b: 0 ~ remain 사이의 랜덤 값
    b=$(( RANDOM % (remain + 1) ))
    # plot에 보낼 건수 c: 나머지
    c=$(( total - a - b ))
    
    echo "배치 요청 분포: cast=$a, movie=$b, plot=$c (총 $total 건)"
    
    # 각 그룹별로 요청을 보내고 PID를 저장
    pids=()
    for (( i=0; i<a; i++ )); do
        curl -X POST \
          -H "Content-Type: application/json" \
          -d "{\"cast_info_id\": $((j+i)), \"name\": \"$(printf "%s" $((j+i)))\", \"gender\": true, \"intro\": \"$(printf "%s" $((j+i)))\"}" \
          "http://10.104.184.4:8080/wrk2-api/cast-info/write" &
        pids+=($!)
    done

    for (( i=0; i<b; i++ )); do
        curl -X POST \
          -H "Content-Type: application/json" \
          -d "{\"movie_id\": \"$(printf "%s" $((j+a+i)))\", \"title\": \"$(printf "%s" $((j+a+i)))\", \"casts\": [{\"cast_id\": $((j+a+i)), \"charactor\": \"$(printf "%s" $((j+a+i)))\", \"cast_info_id\": $((j+a+i))}], \"plot_id\": $((j+a+i)), \"thumbnail_ids\": [\"$(printf "%s" $((j+a+i)))\"], \"photo_ids\": [], \"video_ids\": [], \"avg_rating\": 7.57, \"num_rating\": 5267}" \
          "http://10.104.184.4:8080/wrk2-api/movie-info/write" &
        pids+=($!)
    done

    for (( i=0; i<c; i++ )); do
        curl -X POST \
          -H "Content-Type: application/json" \
          -d "{\"plot_id\": \"$(printf "%s" $((j+a+b+i)))\", \"plot\": \"$(printf "%s" $((j+a+b+i)))\"}" \
          "http://10.104.184.4:8080/wrk2-api/plot/write" &
        pids+=($!)
    done

    # 해당 배치의 모든 요청이 완료될 때까지 기다림
    for pid in "${pids[@]}"; do
        wait $pid
    done

    # 다음 배치까지 대기 (초당 4개 이하 유지되므로 초당 최대 4건)
    sleep 1

    # j 값 증가: 사용한 총 요청 건수만큼 증가
    j=$(( j + total ))
done


wait 20
kubectl logs pod/station-test >> result-a.txt
# (만약 여기 이후에 추가 작업이 있다면 적절히 배치)
kubectl delete pod/station-test
