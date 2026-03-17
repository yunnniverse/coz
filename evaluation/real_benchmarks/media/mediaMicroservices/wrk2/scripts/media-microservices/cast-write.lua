-- 완성
init = function(args)
  -- 각 스레드에서 counter를 0부터 시작
  counter = 1000050
end

request = function()
  counter = counter + 1

  -- 각 필드에 대한 랜덤 값 생성
  local cast_info_id = counter
  local name_random = counter
  -- gender는 boolean 타입; true 또는 false 랜덤 선택
  local gender = (math.random(0, 1) == 1)
  local intro = counter
  
  local path = url
  local method = "POST"
  
  -- JSON 형식의 바디 구성 (cjson.decode에서 요구하는 모든 키 포함)
  local body = string.format(
    '{"cast_info_id": %d, "name": "%s", "gender": %s, "intro": "%s"}',
    cast_info_id,
    name_random,
    tostring(gender),
    intro
  )
    
  local headers = {}
  headers["Content-Type"] = "application/json"
  return wrk.format(method, path, headers, body)
ends

  
function urlEncode(s)
  s = string.gsub(s, "([^%w%.%- ])", function(c) return string.format("%%%02X", string.byte(c)) end)
  return string.gsub(s, " ", "+")
end
