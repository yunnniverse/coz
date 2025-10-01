Demo: Chained Prime Computation (front → one → two → three)

Overview
- Four services: front, one, two, three.
- front receives /start?second=N, forwards the job to one.
- one, two, three each perform CPU work (prime search) for N seconds.
- one calls two; two calls three; three replies back → two → one → front.
- front logs total end-to-end time.

Build
- docker build -t demo-numberchain:latest demo

Deploy
- kubectl apply -f demo/k8s.yaml
- Port-forward to front for local testing:
  - kubectl -n demo port-forward svc/front 8080:8080

Run
- curl "http://127.0.0.1:8080/start?second=3"

Notes
- All services expose /healthz and listen on port 8080.
- Service DNS names match their roles (front, one, two, three).
