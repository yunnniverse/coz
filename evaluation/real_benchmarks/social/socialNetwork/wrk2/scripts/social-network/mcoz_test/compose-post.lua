local fixed_username = "username_1"
local fixed_user_id = "1"
local fixed_text = "fixed-compose-post payload @username_2 http://example.com/fixed"
local fixed_media_ids = "[\"111111111111111111\"]"
local fixed_media_types = "[\"png\"]"
local fixed_body = "username=" .. fixed_username ..
    "&user_id=" .. fixed_user_id ..
    "&text=" .. fixed_text ..
    "&media_ids=" .. fixed_media_ids ..
    "&media_types=" .. fixed_media_types ..
    "&post_type=0"
local sent_responses = 0
local max_requests = tonumber(os.getenv("max_requests")) or 50

request = function()
  local method = "POST"
  local path = "http://localhost:8080/wrk2-api/post/compose"
  local headers = {}
  headers["Content-Type"] = "application/x-www-form-urlencoded"ㅁㄴㅇㄻㄴㅇㄹ
  return wrk.format(method, path, headers, fixed_body)
end

response = function(status, headers, body)
  sent_responses = sent_responses + 1
  if sent_responses >= max_requests then
    wrk.thread:stop()
  end
end
