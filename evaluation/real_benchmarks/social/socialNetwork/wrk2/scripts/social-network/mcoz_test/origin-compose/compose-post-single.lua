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

request = function()
  local method = "POST"
  local path = "http://localhost:8080/wrk2-api/post/compose"
  local headers = {}
  headers["Content-Type"] = "application/x-www-form-urlencoded"
  return wrk.format(method, path, headers, fixed_body)
end
