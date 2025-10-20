import re

# 대상 서비스 이름 목록 (필터링에 사용했던 것과 동일)
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

input_file = "filtered_result.txt"

# 각 서비스별로 값들을 저장할 딕셔너리: {서비스명: [값1, 값2, 값3]}
service_values = {}

with open(input_file, "r") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        # 추출: 중괄호 뒤에 오는 숫자 값을 가져오기
        m_val = re.search(r'\}\s*([0-9.eE+-]+)$', line)
        if not m_val:
            continue
        try:
            value = float(m_val.group(1))
        except ValueError:
            continue

        # 중괄호 안의 라벨들 추출
        m_labels = re.search(r'\{([^}]+)\}', line)
        if not m_labels:
            continue
        labels_str = m_labels.group(1)
        labels = {}
        for part in labels_str.split(","):
            kv = part.split("=", 1)
            if len(kv) == 2:
                key = kv[0].strip()
                val = kv[1].strip().strip('"')
                labels[key] = val
        
        # 대상 서비스 확인
        container_name = labels.get("container_name")
        if container_name not in target_services:
            continue

        # 서비스별로 값 추가
        if container_name not in service_values:
            service_values[container_name] = []
        service_values[container_name].append(value)

# 각 서비스에서 첫 번째와 세 번째 값을 이용하여 차이를 구한 후 합산
total_difference = 0.0
for service, values in service_values.items():
    # if len(values) < 3:
    #     print(f"경고: {service}에 값이 3개 미만입니다. 현재 값: {values}")
    #     continue
    diff = values[1] - values[0]
    print(f"{service}: {values[1]} - {values[0]} = {diff}")
    total_difference += diff

print("전체 합계 (각 서비스별 마지막 - 첫 번째 값의 합):", total_difference)
