#!/usr/bin/env bash

for i in {1..10}; do
  curl -d "first_name=first_name_"$i"&last_name=last_name_"$i"&username=username_"$i"&password=password_"$i \
      http://10.104.184.4:8080/wrk2-api/user/register
done