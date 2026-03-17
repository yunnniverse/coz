# MCOZ Control API

`coz-sync` polling 대신 DaemonSet 각 Pod가 제어 API를 제공합니다.

## Endpoints

- `GET /healthz`
- `GET /status`
- `POST /start`
- `POST /stop`
- `POST /clear`
- `POST /rearm`
- `POST /arm`
- `POST /syscall_profile` (alias: `/syscall-profile`)

기본 포트: `19091`

내부 request-credit arm/consume 경로는 `coz-daemon-local.mcoz-system.svc.cluster.local:19090`
서비스를 통해 hostctl 로컬 제어 포트(`/arm,/status`)에 직접 연결할 수 있습니다.

## Start Example

```bash
curl -sS -X POST "http://coz-control.mcoz-system.svc.cluster.local:19091/start" \
  -H "Content-Type: application/json" \
  -d '{"targetPod":"trace-demo/d-xxxxx","speedup":"0.5","scope":"all","force":true}'
```

### Parameters

- `targetPod` (required unless `requestCredit + autoDiscoverGates`): `namespace/pod`
- `speedup` (optional): default `0.25`
- `scope` (optional): `all` or `local` (default `all`)
- `force` (optional): `true`면 기존 실행 중 프로세스를 내리고 재시작
- `protect` (optional): 배열 또는 comma-separated
- `protectCpus` (optional)
- `othersCpus` (optional)
- `isolateCores` (optional): boolean
- `requestCredit` (optional): `true`면 request-credit 모드 활성화
- `fixedDelayNs` (optional): request-credit 기본 지연(ns), 기본 `10000000` (10ms)
- `gateCount` (optional): gate가 `/arm`에 전달할 credit count (기본 `1`)
- `autoDiscoverGates` (optional, default `true` when `requestCredit=true`): sidecar 붙은 gate pod 자동 발견
- `gateNamespaces` (optional): auto-discovery namespace 목록(csv 또는 배열). 기본은 `MCOZ_GATE_CONTROL_NAMESPACES`
- `gateTargetMode` (optional, default `self` in auto-discovery): `/healthz.target_mode` 필터. `self|any`
- `autoProfileGates` (optional, default `true` when auto-discovery enabled): discovered victim들에 `/syscall_profile(apply_policy)` 자동 적용
- `gateProfileDurationMs`, `gateProfileTopK`, `gateProfileApplyPolicy`: auto-profile 옵션
- `noRefundOnFail` (optional, default `true`): `true`면 ESRCH 환불 비활성화
- `enableReadHook` (optional): `true`면 `sys_exit_read(ret>0)`도 consume 트리거로 사용
- `tracePrephase` (optional): `true`면 start 전에 `mcoz_trace_analyzer`를 실행
- `traceEntryUrl` (required when `tracePrephase=true`): analyzer가 1회 호출할 엔트리 URL
- `traceRequestPath` (optional): analyzer 호출 경로 (`/entry` 등)
- `traceJaegerUrl` (required when `tracePrephase=true`): Jaeger query URL
- `targetServiceId` (required when `tracePrephase=true`, unless inferable from `targetPod`): analyzer의 `service_index` 기준 target ID
- `traceAutoArm` (optional): default `true`, sibling pod들 자동 `/arm`
- `traceArmCount` (optional): default `1`, sibling pod별 arm credit
- `traceContainer` (optional): default `app`, `/arm` 대상 container명
- `tracePollTimeoutS`, `tracePollIntervalS`, `traceMinOverlapMs`, `traceSettleS`, `traceRequestTimeoutS`:
  analyzer 옵션 override

### Trace Pre-phase Example

```bash
curl -sS -X POST "http://coz-control.mcoz-system.svc.cluster.local:19091/start" \
  -H "Content-Type: application/json" \
  -d '{
    "scope":"all",
    "force":true,
    "requestCredit":true,
    "fixedDelayNs":"10000000",
    "tracePrephase":true,
    "traceEntryUrl":"http://a.trace-demo.svc.cluster.local:5000",
    "traceRequestPath":"/entry",
    "traceJaegerUrl":"http://jaeger-query.istio-system:16686",
    "targetServiceId":"3"
  }'
```

위 예시는 sibling set이 `{2,3,4}`라면 `3`을 제외한 `2,4`의 pod를 찾아 자동 arm합니다.

### Request-Credit Auto-discovery Example

전제:
- `/start` 전에 실험 대상 pod들에 `mcoz-gate` sidecar 배포 완료
- gate는 보통 `target_mode=self`

```bash
curl -sS -X POST "http://coz-control.mcoz-system.svc.cluster.local:19091/start" \
  -H "Content-Type: application/json" \
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

이 모드에서는 `targetPod`를 지정하지 않아도 됩니다. `/start`가:
- gate sidecar pod 자동 발견
- gate `/healthz`로 victim pod/container 확인
- discovered victim들에 `/syscall_profile(apply_policy=true)` 적용
- gate enable 수행

즉 실험 topology는 `sidecar 배포 상태`가 표현하고, control plane은 그것을 자동 인식합니다.

## Stop Example

```bash
curl -sS -X POST "http://coz-control.mcoz-system.svc.cluster.local:19091/stop?scope=all"
```

## Clear Example

```bash
curl -sS -X POST "http://coz-control.mcoz-system.svc.cluster.local:19091/clear?scope=all"
```

## Rearm Example

```bash
curl -sS -X POST "http://coz-control.mcoz-system.svc.cluster.local:19091/rearm?scope=all"
```

`request-credit` 모드에서는 `/rearm`는 no-op이며 상태 카운터만 남깁니다.

## Arm Example

```bash
curl -sS -X POST "http://coz-control.mcoz-system.svc.cluster.local:19091/arm" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"trace-demo","pod":"c-xxxxx","container":"app","delay_ns":10000000,"count":1}'
```

동작:
- `(namespace,pod,container) -> cgroup_id` 해석
- victim credit 증가
- eBPF consume 시점에 TID 기준 syscall 449 주입

참고:
- `/start`의 `fixedDelayNs`는 hostctl 기본값뿐 아니라, gate enable 알람 시 gate runtime `delay_ns`로도 전달됩니다.

## Syscall Profile Example

사전 진단용으로 특정 Pod의 요청 처리 syscall 분포를 짧게 샘플링합니다.

```bash
curl -sS -X POST "http://coz-control.mcoz-system.svc.cluster.local:19091/syscall_profile" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"basic","pod":"c-xxxxx","container":"app","duration_ms":2000,"top_k":12,"apply_policy":true}'
```

응답에는 `syscalls`(상위 syscall 카운트)와 `recommendation`(`enable_read_hook`, `suggested_consume_paths`)가 포함됩니다.
`apply_policy=true`면 추천 consume 경로를 해당 cgroup의 raw consume policy로 즉시 반영합니다.

## Status Example

```bash
curl -sS "http://coz-control.mcoz-system.svc.cluster.local:19091/status"
```

`request-credit` 모드에서는 status에 다음이 포함됩니다.
- `credits` (cgroup별)
- `injected_ok`
- `inject_fail`
- `bpf_events`
- `ringbuf_drops`
