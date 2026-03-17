# Real Benchmarks

이 디렉터리는 다른 노드에서 `mcoz` 배포 후 바로 실험을 재현할 수 있게 필요한 real benchmark 자산을 모아둔 곳입니다.

## 디렉터리

- `social/socialNetwork/`: SocialNetwork benchmark 소스, compose/openshift/helm 자산, work-delay 설정
- `social/*.py`: social request-credit 실험 자동화 스크립트
- `media/mediaMicroservices/`: Media microservices benchmark 소스와 배포 자산

## GitHub에 올릴 기준

이 디렉터리 아래에서는 다음만 추적 대상으로 봅니다.

- benchmark 소스 코드
- Dockerfile / compose / helm / openshift yaml
- experiment automation script
- 실험에 필요한 입력 텍스트와 설정 파일

다음은 추적 대상에서 제외합니다.

- `build/`, `build-workdelay/`, `CMakeFiles/`, `__pycache__/`
- 실험 실행 중 생성되는 `results/`
- `social/socialNetwork/social-spin-v1.tar`

`social-spin-v1.tar`는 약 317MB라 일반 GitHub push 한도를 넘습니다. 이 파일이 꼭 필요하면 GitHub Release asset이나 별도 object storage로 두는 쪽이 맞습니다.

## 빠른 경로

1. base mcoz daemon 배포: `bash k8s/apply.sh`
2. request-credit sidecar 부착: `bash k8s/request_credit/apply.sh` 또는 `python3 k8s/request_credit/apply_trigger_adapter.py ...`
3. social benchmark 배포/설정: [`social/socialNetwork/README.md`](./social/socialNetwork/README.md)
4. social work-delay 설정: [`social/socialNetwork/WORK_DELAY.md`](./social/socialNetwork/WORK_DELAY.md)
5. media benchmark 배포/설정: [`media/mediaMicroservices/README.md`](./media/mediaMicroservices/README.md)
6. media work-delay 설정: [`media/mediaMicroservices/WORK_DELAY.md`](./media/mediaMicroservices/WORK_DELAY.md)

## Social 실험 스크립트 기본 출력

social 실험 스크립트의 기본 결과 경로는 이제 `evaluation/real_benchmarks/social/results/...` 입니다.
원하면 `MCOZ_SOCIAL_RESULTS_ROOT=/path/to/results` 로 바꿀 수 있습니다.
