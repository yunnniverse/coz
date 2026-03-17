local socket = require("socket")
local time = socket.gettime() * 1000
math.randomseed(time)
math.random(); math.random(); math.random()

-- Load environment variables
local max_user_index = tonumber(os.getenv("max_user_index")) or 962

request = function()
  local user_id = tostring(math.random(0, max_user_index - 1))
  local start = tostring(math.random(0, 100))
  local stop = tostring(start + 10)
  
  local args = "user_id=" .. user_id .. "&start=" .. start .. "&stop=" .. stop
  local method = "GET"
  local headers = {}
  headers["Content-Type"] = "application/x-www-form-urlencoded"
  
  -- 50:50 확률로 home-timeline 또는 user-timeline 요청 실행
  if math.random() < 0.5 then
    local path = "http://localhost:8080/wrk2-api/home-timeline/read?" .. args
    return wrk.format(method, path, headers, nil)
  else
    local path = "http://localhost:8080/wrk2-api/user-timeline/read?" .. args
    return wrk.format(method, path, headers, nil)
  end
end

