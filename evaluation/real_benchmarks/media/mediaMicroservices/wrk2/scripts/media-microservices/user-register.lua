-- 완성

init = function(args)
  -- 각 스레드에서 counter를 0부터 시작
  counter = 1000000
end  


request = function()
  counter = counter + 1
  local user_id = counter

  local path = url -- url 변수가 아니라 실제 엔드포인트로 지정
  local method = "POST"

  local headers = {}
  headers["Content-Type"] = "application/x-www-form-urlencoded" 

  local body = string.format(
    "first_name=first_name_%d&last_name=last_name_%d&username=username_%d&password=password_%d",
    user_id, user_id, user_id, user_id
  )
  
  return wrk.format(method, path, headers, body)

end
  
