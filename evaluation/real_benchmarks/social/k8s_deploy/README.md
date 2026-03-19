# Social Demo Kubernetes Deploy Bundle

이 디렉토리는 `social-demo`를 별도 namespace에 재배포하고, `socfb-Reed98` 초기화 후 compose-post RR 실험까지 재현하기 위한 self-contained 묶음이다.

## 포함 내용
- `helm/socialnetwork/`: 배포용 Helm chart 복사본
- `values/values-social-demo-live-match.yaml`: live `social`에 최대한 맞춘 plain 배포값
- `values/values-social-demo-live-match-rammongo.yaml`: MongoDB를 `tmpfs`(`/data/db`)로 올리는 RAM-backed 배포값
- `datasets/social-graph/socfb-Reed98/`: 초기화용 그래프 데이터셋
- `scripts/init_social_graph.py`: `socfb-Reed98` user/follow 초기화 스크립트
- `scripts/run_compose_rr_experiment.py`: compose-post one-by-one RR 1000회 측정 스크립트
- `scripts/deploy_social_demo.sh`: Helm 배포 + wait 스크립트

## 전제 조건
- `helm`, `kubectl`, `python3`
- Python 패키지: `aiohttp`
- 현재 kube context가 대상 클러스터를 가리켜야 함
- 기본 namespace/release 이름은 `social-demo`

## 1. 배포
RAM-backed Mongo 배포:

```bash
cd ~/mcoz/mcoz/m-coz/evaluation/real_benchmarks/social/k8s_deploy
./scripts/deploy_social_demo.sh ./values/values-social-demo-live-match-rammongo.yaml
```

plain 배포:

```bash
cd ~/mcoz/mcoz/m-coz/evaluation/real_benchmarks/social/k8s_deploy
./scripts/deploy_social_demo.sh ./values/values-social-demo-live-match.yaml
```

## 2. 포트포워드
별도 터미널에서:

```bash
kubectl -n social-demo port-forward svc/nginx-web-server 18084:8080
```

## 3. 데이터 초기화
`init`는 여러 번 돌려 상태를 안정화할 수 있다. 아래 예시는 3회 반복이다.

```bash
cd ~/mcoz/mcoz/m-coz/evaluation/real_benchmarks/social/k8s_deploy
for i in 1 2 3; do
  python3 ./scripts/init_social_graph.py --graph=socfb-Reed98 --ip=127.0.0.1 --port=18084
done
```

## 4. compose-post RR 1000회 측정
기본 규칙:
- `urllib.request.urlopen()`
- strict one-by-one
- `1..200` probe 후 성공 ID만 사용
- probe latency 상위 꼬리를 제외하려면 `--probe-quantile` 사용

예시:

```bash
cd ~/mcoz/mcoz/m-coz/evaluation/real_benchmarks/social/k8s_deploy
python3 ./scripts/run_compose_rr_experiment.py \
  --base-url http://127.0.0.1:18084/wrk2-api/post/compose \
  --count 1000 \
  --id-start 1 \
  --id-end 200 \
  --probe-quantile 0.90
```

결과 파일은 `results/` 아래에 저장된다.

## 5. 현재 권장 시나리오
1. `values-social-demo-live-match-rammongo.yaml`로 배포
2. `socfb-Reed98` init 3회 반복
3. `run_compose_rr_experiment.py --probe-quantile 0.90`

## 참고
- chart 복사본에는 원본 배포 경로에서 사용하던 `_baseDeployment.tpl` 수정과 global CPU limit 제거가 반영돼 있다.
- 이 묶음은 `social-demo` 재현용이다. live `social`의 MCOZ/Istio sidecar 구조까지 포함하지는 않는다.
