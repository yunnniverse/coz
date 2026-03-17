# Social Work Delay

`*_SPIN_US` now means "busy CPU work for approximately that many microseconds". The hot path does not call `sleep`; it spins on `steady_clock` until the configured duration elapses.

Set a service's `*_SPIN_US` to `0` to disable the extra busy work entirely.

`*_SPIN_PCT` is no longer used.

## Async coverage

- `compose-post`: `text-service`, `user-service`, `media-service`, `unique-id-service`, `url-shorten-service`, `user-mention-service`
- `follow-with-username` / `unfollow-with-username`: `user-service`

The default benchmark values only enable the `compose-post` fanout services above. `post-storage-service`, `home-timeline-service`, and `social-graph-service` are intentionally left out because they are not on the async fanout path for `compose-post`.

## Configure with Docker Compose

Edit [`docker-compose.work-delay.yml`](./docker-compose.work-delay.yml). Values are in microseconds. `0` or an unset variable disables the extra work.

```bash
cd evaluation/real_benchmarks/social/socialNetwork
docker compose -f docker-compose.yml -f docker-compose.work-delay.yml up -d
```

To stop:

```bash
docker compose -f docker-compose.yml -f docker-compose.work-delay.yml down
```

## Configure with Helm

Edit [`work-delay-values.yaml`](./work-delay-values.yaml), then deploy:

```bash
cd evaluation/real_benchmarks/social/socialNetwork/helm-chart/socialnetwork
helm dependency update
helm upgrade --install social-workdelay . -f ../../work-delay-values.yaml
```

## Build

```bash
cd evaluation/real_benchmarks/social/socialNetwork
docker build -t deathstarbench/social-network-microservices:latest .
```

## Verify

```bash
docker compose -f docker-compose.yml -f docker-compose.work-delay.yml ps
docker compose -f docker-compose.yml -f docker-compose.work-delay.yml logs --tail=20 compose-post-service
```
