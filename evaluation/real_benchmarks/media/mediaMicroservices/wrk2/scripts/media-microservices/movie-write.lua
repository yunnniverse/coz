-- 완성
init = function(args)
  -- 각 스레드에서 counter를 0부터 시작
  counter = 1000050
end

function request()
  -- 랜덤 영화 정보 생성
  counter = counter + 1
  local movie_id = counter
  local title = counter
  local plot_id = counter
  
  local path = url
  local method = "POST"

  -- 캐스트 배열 생성 (여기서는 1개의 캐스트 객체 생성, 필요에 따라 개수를 늘릴 수 있음)
  local casts = {}
  local cast_obj = {}
  cast_obj["cast_id"] = counter
  cast_obj["charactor"] = counter
  cast_obj["cast_info_id"] = counter
  table.insert(casts, cast_obj)
  
  -- 썸네일, 사진, 비디오 ID 설정
  local thumbnail_ids = { counter }
  local photo_ids = { counter }    -- 빈 배열
  local video_ids = { counter }    -- 빈 배열
  
  -- 평점 관련 정보: 평균 평점은 0.1 ~ 10.0 사이의 실수, 평점 수는 정수
  local avg_rating = math.random(1, 1000) / 100.0
  local num_rating = math.random(1, 10000)
  
  -- JSON 형식의 바디 구성 (모든 필드를 포함)
  local body = string.format(
    '{"movie_id": "%d", "title": "%s", "casts": %s, "plot_id": %d, "thumbnail_ids": %s, "photo_ids": %s, "video_ids": %s, "avg_rating": %f, "num_rating": %d}',
    movie_id,
    title,
    -- casts 배열을 JSON 형식으로 변환 (직접 문자열 생성)
    '[{"cast_id": ' .. cast_obj["cast_id"] .. ', "charactor": "'
      .. cast_obj["charactor"] .. '", "cast_info_id": ' .. cast_obj["cast_info_id"] .. '}]',
    plot_id,
    -- thumbnail_ids 배열
    '["' .. thumbnail_ids[1] .. '"]',
    -- photo_ids 배열 (빈 배열)
    '[]',
    -- video_ids 배열 (빈 배열)
    '[]',
    avg_rating,
    num_rating
  )
  
  local headers = {}
  headers["Content-Type"] = "application/json"
  
  return wrk.format(method, path, headers, body)
end
