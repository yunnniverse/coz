#!/bin/bash

# wrk 명령어들을 백그라운드에서 실행
echo "[$(date '+%Y-%m-%d %H:%M:%S')] wrk 실행 시작"
# wrk -D exp -t 1 -c 1 -d 1800 -L -s wrk2/scripts/media-microservices/compose-review.lua http://10.104.184.4:8080/wrk2-api/review/compose -R 4 &
# wrk -D exp -t 1 -c 1 -d 1800 -L -s wrk2/scripts/media-microservices/movie-register.lua http://10.104.184.4:8080/wrk2-api/movie/register -R 4 &
# wrk -D exp -t 1 -c 1 -d 1800 -L -s wrk2/scripts/media-microservices/user-register.lua http://10.104.184.4:8080/wrk2-api/user/register -R 4 &
# 위 wrk 명령어들은 300초 동안 실행됩니다.
wrk_pid=$!

# curl 요청을 별도로 반복: 3개 보내고 2초 대기
j=3010000
while true; do

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] curl 배치, j = $j"
    
    a=$(( RANDOM % 3 ))
    b=$(( RANDOM % 3 ))
    c=$(( RANDOM % 4 ))
    d=$(( RANDOM % 3 ))
    e=$(( RANDOM % 4 ))
    f=$(( RANDOM % 3 ))
    
    total=$(( a + b + c + d + e + f ))
    
    echo "배치 요청 분포: cast=$a, movie=$b, plot=$c, user-reg=$d, movie-reg=$e, review=$((f+1)) (총 $total 건)"
    
    # 각 그룹별로 요청을 보내고 PID를 저장
    pids=()
    # for (( i=0; i<a; i++ )); do
    #     curl -X POST \
    #       -H "Content-Type: application/json" \
    #       -d "{\"cast_info_id\": $((j+i)), \"name\": \"$(printf '%s' $((j+i)))\", \"gender\": true, \"intro\": \"$(printf '%s' $((j+i)))\"}" \
    #       "http://10.104.184.4:8080/wrk2-api/cast-info/write" &
    #     pids+=($!)
    # done

    # for (( i=0; i<b; i++ )); do
    #     curl -X POST \
    #       -H "Content-Type: application/json" \
    #       -d "{\"movie_id\": \"$(printf '%s' $((j+i)))\", \"title\": \"$(printf '%s' $((j+i)))\", \"casts\": [{\"cast_id\": $((j+i)), \"charactor\": \"$(printf '%s' $((j+i)))\", \"cast_info_id\": $((j+i))}], \"plot_id\": $((j+i)), \"thumbnail_ids\": [\"$(printf '%s' $((j+i)))\"], \"photo_ids\": [], \"video_ids\": [], \"avg_rating\": 7.57, \"num_rating\": 5267}" \
    #       "http://10.104.184.4:8080/wrk2-api/movie-info/write" &
    #     pids+=($!)
    # done

    # for (( i=0; i<c; i++ )); do
    #     curl -X POST \
    #       -H "Content-Type: application/json" \
    #       -d "{\"plot_id\": \"$(printf '%s' $((j+i)))\", \"plot\": \"$(printf '%s' $((j+i)))\"}" \
    #       "http://10.104.184.4:8080/wrk2-api/plot/write" &
    #     pids+=($!)
    # done

    # for (( i=0; i<d; i++ )); do
    #     curl -d "first_name=first_name_$((j+i))&last_name=last_name_$((j+i))&username=username_$((j+i))&password=password_$((j+i))" \
    #       "http://10.104.184.4:8080/wrk2-api/user/register" &
    #     pids+=($!)
    # done

    # for (( i=0; i<e; i++ )); do
    #     curl -d "title=title_$((j+i))&movie_id=movie_id_$((j+i))" \
    #       "http://10.104.184.4:8080/wrk2-api/movie/register" &
    #     pids+=($!)
    # done

    # wrk 호출에서 review 건수 f 사용 (단, $f 사용)
    wrk -D exp -t 1 -c 1 -d 1 -L -s wrk2/scripts/media-microservices/compose-review.lua \
        "http://10.104.184.4:8080/wrk2-api/review/compose" -R $((f+1))

    # 해당 배치의 모든 요청이 완료될 때까지 기다림
    for pid in "${pids[@]}"; do
        wait $pid
    done

    # 다음 배치까지 대기
    sleep 1

    # j 값 증가: 사용한 총 요청 건수만큼 증가
    j=$(( j + total ))
done

wait 20

