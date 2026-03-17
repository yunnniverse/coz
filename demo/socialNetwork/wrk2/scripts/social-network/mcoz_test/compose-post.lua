-- compose-post-fixed.lua

request = function()
  local method = "POST"
  local path = "/wrk2-api/post/compose"   -- 경로만!

  local headers = {}
  headers["Content-Type"] = "application/x-www-form-urlencoded"

  -- 고정 파라미터 (매번 동일)
  local username = "username_1"
  local user_id  = "1"

  -- 고정 텍스트 (길이 고정)
  local text = "hello_world_256bytes_" ..
               "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ..
               "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ..
               "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ..
               "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

  -- media도 고정(없게 하려면 []로)
  local media_ids   = "[]"
  local media_types = "[]"

  local body =
    "username=" .. username ..
    "&user_id=" .. user_id ..
    "&text=" .. text ..
    "&media_ids=" .. media_ids ..
    "&media_types=" .. media_types ..
    "&post_type=0"

  return wrk.format(method, path, headers, body)
end

