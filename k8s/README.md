# MCOZ Kubernetes Deployment

이 디렉터리는 다른 노드에서 `mcoz daemon`, `control API`, `delay service`, `request-credit sidecar` 실험을 재현하기 위한 기본 배포 자산입니다.

## 포함 내용

- `namespace.yaml`: `mcoz-system` namespace
- `rbac.yaml`: daemon/control API가 pod/CRD를 읽기 위한 권한
- `services.yaml`: `coz-control`, `coz-daemon-local`, `coz-delay` 서비스
- `daemonset.yaml`: node-local mcoz daemon + control API
- `delay_things/`: `GlobalDelay` CRD 및 기본 객체
- `request_credit/`: gate sidecar / EnvoyFilter / 검증 스크립트

## 빠른 시작

기본 daemon/control plane 배포:

```bash
bash k8s/apply.sh
```

기본 동작:

- `mcoz-system` namespace 생성
- `mcoz-control-script` ConfigMap 생성
- RBAC, CRD, 서비스, daemonset 적용
- 선택적으로 `k8s/sysctl-perf.yaml` 적용

옵션:

- `KUBECTL=kubectl`
- `APPLY_SYSCTL_PERF=true|false`
- `APPLY_GLOBAL_DELAY=true|false`

정리:

```bash
bash k8s/delete.sh
```

## Request-Credit 예시

`trace-demo` 예제를 바로 붙이려면:

```bash
bash k8s/request_credit/apply.sh
```

임의 workload에는:

```bash
python3 k8s/request_credit/apply_trigger_adapter.py \
  --namespace social \
  --deployment text-service \
  --selector service=text-service \
  --container text-service \
  --service-name text-service \
  --protocol thrift \
  --rollout
```

세부 control API는 [`CONTROL_API.md`](./CONTROL_API.md), request-credit 동작은 [`request_credit/README.md`](./request_credit/README.md)를 보면 됩니다.
