import re

# 필터할 서비스 이름 목록
target_services = {
    "compose-post-service",
    "home-timeline-service",
    "media-service",
    "user-service",
    "user-timeline-service",
    "text-service",
    "nginx-web-server",
    "post-storage-service",
    "social-graph-service",
    "url-shorten-service",
    "user-mention-service",
    "unique-id-service",
}

input_file = "kepler_result.txt"
output_file = "filtered_result.txt"

with open(input_file, "r") as f, open(output_file, "w") as out:
    for line in f:
        line = line.strip()
        # 메트릭 라인만 처리 (헤더나 주석은 건너뛰기)
        if not line or line.startswith("#"):
            continue
        # 메트릭 이름이 kepler_container_joules_total 인지 체크
        if not line.startswith("kepler_container_joules_total"):
            continue
        
        # 중괄호 안의 라벨들을 추출
        m = re.search(r'\{([^}]+)\}', line)
        if not m:
            continue
        labels_str = m.group(1)
        # 각 라벨은 쉼표로 구분되어 있으므로 분리
        labels = {}
        for part in labels_str.split(","):
            # key="value" 형식
            kv = part.split("=", 1)
            if len(kv) == 2:
                key = kv[0].strip()
                # 양쪽의 큰따옴표 제거
                value = kv[1].strip().strip('"')
                labels[key] = value
        
        # mode가 dynamic 인지 확인
        if labels.get("mode") != "dynamic":
            continue
        
        # container_name 이 대상 서비스에 포함되는지 확인
        if labels.get("container_name") not in target_services:
            continue
        
        # 조건을 만족하면 해당 라인을 출력 (또는 저장)
        out.write(line + "\n")
        print(line)
