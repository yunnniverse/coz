# MSA 비동기 fan-out / co-running 분석기

Jaeger trace의 시간 겹침(overlap)을 이용해 서비스 간 동시 실행(co-running) 관계와 fan-out(동시 downstream 호출) 후보를 추론하는 CLI 도구입니다.

## 기능

- 엔트리 URL을 **1회 호출**하면서 지정한 Trace ID를 B3/W3C 헤더로 주입
- 요청 경로를 `--request-path` 입력으로 받아 어떤 endpoint를 칠지 선택 가능
- Jaeger Query API에서 같은 Trace ID로 trace를 폴링 조회
- Jaeger base path 자동 감지
  - `.../api/services` 성공 시 base path=`""`
  - 실패 시 `.../jaeger/api/services` 성공이면 base path=`"/jaeger"`
- 서비스별 union interval 기반 co-running 계산
- parent span 아래 중첩되는 client span 그룹 기반 fan-out 후보 출력
- 분석 결과 JSON 저장(`--json-out`)

## 파일 구성

- `mcoz_trace_analyzer.py`: 메인 분석기
- `requirements.txt`: 파이썬 의존성
- `k8s-job.yaml`: 클러스터 내 Job 실행 매니페스트

## 로컬 실행 (Jaeger 포트포워드)

1. 포트포워드 (`jaeger-query`가 없으면 `tracing` 사용)

```bash
kubectl -n istio-system port-forward svc/jaeger-query 16686:16686
# 위 명령이 NotFound이면:
kubectl -n istio-system port-forward svc/tracing 16686:80
```

2. 의존성 설치

```bash
cd ~/mcoz/mcoz/m-coz/target_detector/trace-fanout-analyzer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. 실행

```bash
python3 mcoz_trace_analyzer.py \
  --entry-url "http://127.0.0.1:30500" \
  --request-path "/entry" \
  --jaeger-url "http://127.0.0.1:16686" \
  --poll-timeout-s 15 \
  --poll-interval-s 0.5 \
  --min-overlap-ms 0.5 \
  --json-out ./result.json
```

`--trace-id`를 생략하면 32-hex trace id를 자동 생성합니다.

## 클러스터 내부 실행 (Kubernetes Job)

1. 스크립트를 ConfigMap으로 생성

```bash
cd ~/mcoz/mcoz/m-coz/target_detector/trace-fanout-analyzer
kubectl -n trace-demo create configmap trace-fanout-analyzer-script \
  --from-file=mcoz_trace_analyzer.py \
  --from-file=requirements.txt
```

2. Job 적용

```bash
kubectl -n trace-demo apply -f k8s-job.yaml
```

3. 필요 시 ENTRY_URL/ENTRY_PATH/JAEGER_URL/TRACE_ID 수정 후 재적용  
`k8s-job.yaml`의 `env` 값으로 제어합니다.

4. 로그 확인

```bash
kubectl -n trace-demo logs job/trace-fanout-analyzer -f
```

## 출력 예시 (형태)

```text
Request sent: GET http://a.trace-demo:5000/entry
trace_id=0123456789abcdef0123456789abcdef span_id=89abcdef01234567 status=200 elapsed_ms=21.442

=== Co-running services (overlap inferred) ===
[Service: b] dur_ms=35.200 basis=server
  - d: overlap=12.300ms ratio=0.40
  - e: overlap=11.800ms ratio=0.38
  Async fan-out candidates:
    * parent_op='GET /' group_dur=9.800ms peers=[d.default.svc.cluster.local, e.default.svc.cluster.local] spans=2
```

## 주요 옵션

- `--entry-url` (필수): 단일 요청 대상 URL
- `--request-path`: 요청 경로(`/entry` 등). 지정 시 `--entry-url`의 path를 덮어씀
- `--jaeger-url`: Jaeger Query URL (기본 `http://jaeger-query.istio-system:16686`)
- `--trace-id`: 요청에 주입할 trace id(32 hex)
- `--span-id`: 루트 span id(16 hex)
- `--poll-timeout-s`: trace 수집 대기 시간 (기본 15)
- `--poll-interval-s`: 폴링 주기 (기본 0.5)
- `--min-overlap-ms`: co-running 최소 overlap ms (기본 0.5)
- `--json-out`: JSON 파일 저장 경로

## 알고리즘 요약

1. trace의 span/process에서 `serviceName`, `startTime`, `duration`, `span.kind`, `parentSpanID` 파싱
2. 서비스별 대표 시간 구간 생성
   - `server` span union interval 우선
   - 없으면 전체 span union interval 사용
3. 서비스 S,T 간 overlap 시간 계산
4. `overlap >= min_overlap_ms`이면 co-running으로 기록
   - `ratio = overlap / min(total_dur(S), total_dur(T))`
5. fan-out 후보 탐지
   - 같은 parent 아래 client span 2개 이상 + 시간 중첩 시 후보
   - peer 식별: `peer.service`, `http.host`, `http.url`, `upstream_cluster` 우선, 없으면 `operationName`

## 주의/한계

- 이 도구는 “동기/비동기”를 직접 판정하지 않고 **시간 겹침 기반 추론**만 수행합니다.
- trace context 전파가 끊기면(백그라운드 작업, 메시지 큐 등) 하나의 trace에 모두 담기지 않을 수 있습니다.
- Jaeger Query의 `/api/*`는 UI 내부 JSON API이며, 배포에 따라 base path가 `/jaeger`일 수 있어 자동 감지를 사용합니다.
- Istio 기본 설치 일부는 서비스명이 `jaeger-query` 대신 `tracing`일 수 있습니다.
- 로컬 실행 시 `a.trace-demo` 같은 클러스터 DNS는 해석되지 않을 수 있으므로 서비스 포트포워드(`kubectl -n trace-demo port-forward svc/a 5000:5000`) 후 `http://127.0.0.1:5000` 사용을 권장합니다.
