#!/usr/bin/env python3
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request

ROLE = os.environ.get("ROLE", "front").strip().lower()
PORT = int(os.environ.get("PORT", "8080"))
NEXT_SERVICE = os.environ.get("NEXT_SERVICE", "").strip()  # front→one, one→two, two→three
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "30"))


def log(msg):
    print(f"[demo][{ROLE}] {msg}", flush=True)


def is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    f = 3
    while f * f <= n:
        if n % f == 0:
            return False
        f += 2
    return True


def compute_primes_for(seconds: int):
    deadline = time.time() + seconds
    count = 0
    candidate = 1
    while time.time() < deadline:
        candidate += 1
        if is_prime(candidate):
            count += 1
    return {"count": count, "last": candidate}


def call_next(next_name: str, seconds: int, trace_id: str):
    if not next_name:
        return {"status": "done", "service": ROLE, "trace_id": trace_id}
    url = f"http://{next_name}:8080/work?second={seconds}&trace_id={trace_id}"
    req = Request(url, headers={"User-Agent": "demo-numberchain/1.0"})
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


class Handler(BaseHTTPRequestHandler):
    server_version = "demo-numberchain/1.0"

    def _json(self, code: int, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/healthz":
            return self._json(200, {"ok": True, "role": ROLE})

        if ROLE == "front" and path == "/start":
            sec = int(qs.get("second", ["0"])[0])
            if sec <= 0:
                return self._json(400, {"error": "second must be > 0"})
            trace_id = uuid.uuid4().hex
            log(f"start trace_id={trace_id} second={sec}")
            t0 = time.time()
            res = call_next("one" if not NEXT_SERVICE else NEXT_SERVICE, sec, trace_id)
            elapsed = time.time() - t0
            log(f"done trace_id={trace_id} elapsed={elapsed:.7f}s result={res}")
            return self._json(200, {"trace_id": trace_id, "elapsed": round(elapsed, 7), "result": res})

        if path == "/work":
            sec = int(qs.get("second", ["0"])[0])
            if sec <= 0:
                return self._json(400, {"error": "second must be > 0"})
            trace_id = qs.get("trace_id", [uuid.uuid4().hex])[0]
            log(f"compute second={sec} trace_id={trace_id}")
            stats = compute_primes_for(sec)
            log(f"compute-done primes={stats['count']} last={stats['last']} trace_id={trace_id}")
            if ROLE in ("one", "two"):
                nxt = NEXT_SERVICE or ("two" if ROLE == "one" else "three")
                next_res = call_next(nxt, sec, trace_id)
                return self._json(200, {"service": ROLE, "trace_id": trace_id, "stats": stats, "next": next_res})
            elif ROLE == "three":
                return self._json(200, {"service": ROLE, "trace_id": trace_id, "stats": stats, "status": "done"})
            else:
                return self._json(400, {"error": f"/work not valid on role={ROLE}"})

        self._json(404, {"error": "not found", "role": ROLE, "path": path})

    def log_message(self, fmt, *args):  # silence default noisy logs
        log(fmt % args)


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"listening on :{PORT} role={ROLE} next={NEXT_SERVICE or '-'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    sys.exit(main())
