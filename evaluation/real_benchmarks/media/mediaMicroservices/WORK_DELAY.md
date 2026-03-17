# Media Work Delay

`*_SPIN_US` now means "synthetic CPU work calibrated from microseconds". The request hot path does fixed iterations, not `sleep`, and not wall-clock waiting. A one-time startup calibration estimates `iterations/us`, then each request runs that many hash-style loop iterations.

## Async coverage

- `compose-review`: `user-service`, `movie-id-service`, `text-service`, `unique-id-service`, `rating-service`, `compose-review-service`, `review-storage-service`, `user-review-service`, `movie-review-service`
- `read-page`: `movie-info-service`, `movie-review-service`, `cast-info-service`, `plot-service`

## Configure with Docker Compose

Edit [`docker-compose.work-delay.yml`](./docker-compose.work-delay.yml). Values are in microseconds. `0` or an unset variable disables the extra work.

```bash
cd evaluation/real_benchmarks/media/mediaMicroservices
docker compose -f docker-compose.yml -f docker-compose.work-delay.yml up -d
```

To stop:

```bash
docker compose -f docker-compose.yml -f docker-compose.work-delay.yml down
```

## Configure with Helm

Edit [`work-delay-values.yaml`](./work-delay-values.yaml), then deploy:

```bash
cd evaluation/real_benchmarks/media/mediaMicroservices/helm-chart/mediamicroservices
helm dependency update
helm upgrade --install media-workdelay . -f ../../work-delay-values.yaml
```

## Build

```bash
cd evaluation/real_benchmarks/media/mediaMicroservices
docker build -t yg397/media-microservices:latest .
```

## Verify

```bash
docker compose -f docker-compose.yml -f docker-compose.work-delay.yml ps
docker compose -f docker-compose.yml -f docker-compose.work-delay.yml logs --tail=20 compose-review-service
```
