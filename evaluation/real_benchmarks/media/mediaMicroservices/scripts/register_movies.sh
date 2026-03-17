#!/usr/bin/env bash

for i in {1..10}; do
  curl -d "title=title_"$i"&movie_id=movie_id_"$i \
      http://10.104.184.4:8080/wrk2-api/movie/register
done