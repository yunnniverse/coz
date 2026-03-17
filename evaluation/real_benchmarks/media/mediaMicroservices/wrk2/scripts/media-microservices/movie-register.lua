-- 완성

init = function(args)
  -- 각 스레드에서 counter를 0부터 시작
  counter = 1000000
end  


request = function()
  counter = counter + 1
  local movie_id = counter

  local path = url -- url 변수가 아니라 실제 엔드포인트로 지정
  local method = "POST"

  local headers = {}
  headers["Content-Type"] = "application/x-www-form-urlencoded"
  
  -- title과 movie_id에 대해 올바른 포맷 지정자 사용 (%d)
  local body = string.format(
    "title=title_%d&movie_id=movie_id_%d",
    movie_id, movie_id
  )
  
  return wrk.format(method, path, headers, body)
end
  

