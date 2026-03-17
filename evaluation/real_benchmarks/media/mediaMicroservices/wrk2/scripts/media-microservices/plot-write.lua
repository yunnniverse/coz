-- 완성

init = function(args)
  -- 각 스레드에서 counter를 0부터 시작
  counter = 1000000
end


request = function()
  counter = counter + 1
  local plot_id = counter

  local path = url
  local method = "POST"

  -- JSON 형식의 바디 구성
  local body = string.format('{"plot_id": %d, "plot": "%s"}', plot_id, plot_text)

  local headers = {}
  headers["Content-Type"] = "application/json"

  return wrk.format(method, path, headers, body)
end
