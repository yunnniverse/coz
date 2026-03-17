# MCOZ Request-Credit Mode (App Code Unchanged)

이 문서는 `trigger adapter + gate + eBPF consume + syscall 449(TID)` 방식으로
요청당 1회 fixed delay를 주입하는 request-credit 모드 사용 방법입니다.

## 핵심 설계

- Trigger:
  - HTTP / gRPC: victim inbound Envoy의 `ext_authz`가 요청마다 gate 호출
  - Thrift(TFramed + TBinary): 로컬 thrift adapter가 프레임 단위 요청을 감지해 gate `/trigger` 호출
- Arm: gate가 `coz-daemon-local:19090` 또는 `coz-daemon-udp-local:19090`으로 arm 신호를 보내 credit(+1) 적립
  - 기본 sidecar 적용 스크립트는 `udp://coz-daemon-udp-local:19090/arm` + `MCOZ_DIRECT_ARM=true`를 사용
- Consume: victim app cgroup의 `raw_tracepoint/sys_enter`에서 `consume_policy`에 매칭된 syscall 발생 시 credit 1회 소모
  (기본 정책은 `recvfrom/recvmsg/recvmmsg`, `syscall_profile(apply_policy=true)`로 `read/readv/pread64` 등으로 확장 가능)
- Inject: user space가 ringbuf event(TID)를 받아 syscall `449(tid, delay_ns)` 호출

`sleep` 기반 지연은 사용하지 않습니다.

## State Machine

- `TRIGGERED`: gate 호출됨
- `ARMED`: `/arm` 성공, credit 증가
- `CONSUME_TRIGGER`: eBPF가 recv* syscall 진입 감지
- `INJECTED`: syscall 449 성공
- `FAILED`: 449 실패 (기본 ESRCH 1회 환불)

로그:
- gate: `[MCOZ-GATE] state=...`
- thrift adapter: `[mcoz-thrift-adapter] ...`
- daemon: `[MCOZ] state=ARMED|CONSUME_TRIGGER|INJECTED|FAILED ...`

## Protocol Adapters

- `http`: Istio Envoy `ext_authz` 기반 trigger
- `grpc`: Istio Envoy `ext_authz` 기반 trigger. Envoy가 HTTP/2 gRPC path(`/<Service>/<Method>`)를 그대로 gate에 전달
- `thrift`: 로컬 `mcoz-thrift-adapter.py`가 framed thrift 요청을 읽고 gate `/trigger` 호출

현재 thrift adapter는 `TFramedTransport + TBinaryProtocol` 요청 단위 감지만 지원합니다.

## Generic Apply Script

trace-demo 전용 예제 manifest 외에, 임의 workload에 protocol-aware trigger를 붙이려면:

```bash
python3 k8s/request_credit/apply_trigger_adapter.py \
  --namespace social \
  --deployment text-service \
  --selector service=text-service \
  --container text-service \
  --service-name text-service \
  --protocol thrift \
  --target-namespace social \
  --target-pod unique-id-service-xxxxx \
  --target-container unique-id-service \
  --rollout
```

프로토콜 선택:
- `--protocol http`
- `--protocol grpc`
- `--protocol thrift`

동작:
- `mcoz-gate` sidecar와 필요한 ConfigMap을 반영
- `http/grpc`는 Envoy `ext_authz` EnvoyFilter 적용
- `thrift`는 로컬 thrift adapter sidecar와 TCP reroute EnvoyFilter 적용
- gate target override(`MCOZ_TARGET_*`)를 통해 sibling pod가 victim pod credit를 arm 가능

## 배포

1. mcoz daemon + gate/envoyfilter 일괄 적용

```bash
bash k8s/request_credit/apply.sh
```

`apply.sh`는 `scripts/mcoz_gate.py`를 `trace-demo/mcoz-gate-script` ConfigMap으로 먼저 반영한 뒤,
`c` deployment sidecar + EnvoyFilter를 적용합니다.

`a/entry` 체인에서 지연 주입 대상을 `d`로 바꾸려면:

```bash
bash k8s/request_credit/switch_victim_to_d.sh
```

request-credit auto-discovery 모드에서는 추가 전제가 있습니다.

- `/start` 전에 실험 대상 pod들에 sidecar 배포 완료
- gate는 보통 `target_mode=self`
- control plane은 `sidecar가 붙은 pod = 이번 실험의 virtual victim`으로 간주

즉 app별 Path 설정 없이도, sidecar 배포 상태만으로 실험 대상을 결정할 수 있습니다.

2. request-credit 모드 시작 (10ms)

```bash
curl -sS -X POST http://coz-control.mcoz-system.svc.cluster.local:19091/start \
  -H 'Content-Type: application/json' \
  -d '{"scope":"all","force":true,"requestCredit":true,"fixedDelayNs":"10000000"}'
```

auto-discovery로 `social` namespace gate만 활성화하려면:

```bash
curl -sS -X POST http://coz-control.mcoz-system.svc.cluster.local:19091/start \
  -H 'Content-Type: application/json' \
  -d '{
    "scope":"all",
    "force":true,
    "requestCredit":true,
    "fixedDelayNs":"100000",
    "gateNamespaces":["social"],
    "autoDiscoverGates":true,
    "gateTargetMode":"self",
    "autoProfileGates":true
  }'
```

이 호출은:
- gate sidecar pod 자동 발견
- gate `/healthz`로 victim pod/container 확인
- discovered victim마다 `/syscall_profile(apply_policy=true)` 자동 적용
- gate enable

을 한 번에 수행합니다.

3. 상태 확인

```bash
curl -sS http://coz-control.mcoz-system.svc.cluster.local:19091/status | jq '.daemon.payload'
```

## 검증

1. 요청수/arm/449 카운트 대조

```bash
bash k8s/request_credit/verify_counts.sh
```

추가 커널 레벨 검증:

```bash
TARGET_CGID=$(curl -s "${CONTROL_URL:-http://coz-control.mcoz-system.svc.cluster.local:19091}/status" | jq -r '.daemon.payload.cgroups[0].cgroup_id')
sudo bpftrace -D TARGET_CGID=${TARGET_CGID} k8s/request_credit/bpftrace_request_credit.bt
```

2. Jaeger span shift 확인

```bash
bash k8s/request_credit/jaeger_shift_check.sh
```

3. latency off/on 비교 (`c=1`, `c=10`)

```bash
bash k8s/request_credit/latency_compare.sh 'http://a.trace-demo.svc.cluster.local:5000/entry'
```

동시성 1(c=1)만 고정해서 순차 요청으로 측정:

```bash
bash k8s/request_credit/latency_serial_c1.sh \
  'http://a.trace-demo.svc.cluster.local:5000/entry' 50
```

옵션(환경변수):
- `MODE=off|on|both` (기본 `both`)
- `INTERVAL_MS=200` (요청 간 간격, 기본 200ms)
- `WARMUP_REQUESTS=3`
- `CONTROL_URL=http://127.0.0.1:19091` (port-forward 사용 시)

## Istio 설정 선택 이유

`AuthorizationPolicy(CUSTOM)+ExtensionProvider`는 mesh 전역 설정 변경이 필요합니다.
이 구현은 victim pod 로컬 sidecar(127.0.0.1) 강제를 위해 `EnvoyFilter`를 기본 사용했습니다.
