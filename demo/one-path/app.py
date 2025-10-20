# #!/usr/bin/env python3
# import json
# import os
# import sys
# import time
# import uuid
# import socket
# from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
# from urllib.parse import urlparse, parse_qs
# from urllib.request import urlopen, Request

# ROLE = os.environ.get("ROLE", "front").strip().lower()
# PORT = int(os.environ.get("PORT", "8080"))
# NEXT_SERVICE = os.environ.get("NEXT_SERVICE", "").strip()  # front→one, one→two, two→three
# REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "30"))
# NODE_NAME = os.environ.get("NODE_NAME", socket.gethostname())


# def log(msg):
#     print(f"[demo][{ROLE}][node={NODE_NAME}] {msg}", flush=True)


# def is_prime(n: int) -> bool:
#     if n < 2:
#         return False
#     if n % 2 == 0:
#         return n == 2
#     f = 3
#     while f * f <= n:
#         if n % f == 0:
#             return False
#         f += 2
#     return True


# def compute_primes_until_nth(target_nth: int):
#     """
#     Compute primes until reaching the Nth prime (goal-based work).
#     Progress is logged by count; timing is measured by callers using wall time.
#     """
#     target = int(target_nth)
#     if target <= 0:
#         return {"nth": target, "prime": None}
#     count = 0
#     candidate = 1
#     # Log progress every ~10% (at least every 1)
#     step = max(1, target // 10)
#     last_log = 0
#     while count < target:
#         candidate += 1
#         if is_prime(candidate):
#             count += 1
#             if count - last_log >= step or count == target:
#                 pct = (count / target) * 100.0
#                 log(f"progress nth={target} found={count} ({pct:.1f}%) last={candidate}")
#                 last_log = count
#     return {"nth": target, "prime": candidate}


# def call_next(next_name: str, seconds: int, trace_id: str):
#     if not next_name:
#         return {"status": "done", "service": ROLE, "trace_id": trace_id}
#     # Pass both legacy 'second' and new 'nth' parameter
#     url = f"http://{next_name}:8080/work?second={seconds}&nth={seconds}&trace_id={trace_id}"
#     req = Request(url, headers={"User-Agent": "demo-numberchain/1.0"})
#     with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
#         body = resp.read().decode("utf-8")
#         return json.loads(body)


# class Handler(BaseHTTPRequestHandler):
#     server_version = "demo-numberchain/1.0"

#     def _json(self, code: int, obj):
#         body = json.dumps(obj).encode("utf-8")
#         self.send_response(code)
#         self.send_header("Content-Type", "application/json")
#         self.send_header("Content-Length", str(len(body)))
#         self.end_headers()
#         self.wfile.write(body)

#     def do_GET(self):  # noqa: N802
#         parsed = urlparse(self.path)
#         path = parsed.path
#         qs = parse_qs(parsed.query)

#         if path == "/healthz":
#             return self._json(200, {"ok": True, "role": ROLE})

#         if ROLE == "front" and path == "/start":
#             # Accept 'nth' or legacy 'second'
#             nth_str = qs.get("nth", [None])[0]
#             if nth_str is None:
#                 nth_str = qs.get("second", ["0"])[0]
#             try:
#                 nth = int(nth_str)
#             except Exception:
#                 return self._json(400, {"error": "nth must be an integer > 0"})
#             if nth <= 0:
#                 return self._json(400, {"error": "nth must be > 0"})
#             trace_id = uuid.uuid4().hex
#             log(f"start trace_id={trace_id} nth={nth}")
#             t0_wall = time.time()
#             try:
#                 res = call_next("one" if not NEXT_SERVICE else NEXT_SERVICE, nth, trace_id)
#             except Exception as e:
#                 log(f"error calling next: {e}")
#                 return self._json(502, {"error": "call_next failed", "detail": str(e)})
#             wall_elapsed = time.time() - t0_wall
#             log(f"done trace_id={trace_id} wall_elapsed={wall_elapsed:.7f}s")
#             return self._json(200, {"trace_id": trace_id,
#                                     "wall_elapsed": round(wall_elapsed, 7),
#                                     "result": res})

#         if path == "/work":
#             # Accept both 'nth' and legacy 'second'
#             nth_str = qs.get("nth", [None])[0]
#             if nth_str is None:
#                 nth_str = qs.get("second", ["0"])[0]
#             try:
#                 nth = int(nth_str)
#             except Exception:
#                 return self._json(400, {"error": "nth must be an integer > 0"})
#             if nth <= 0:
#                 return self._json(400, {"error": "nth must be > 0"})
#             trace_id = qs.get("trace_id", [uuid.uuid4().hex])[0]
#             log(f"compute nth={nth} trace_id={trace_id}")
#             try:
#                 start_wall = time.time()
#                 stats = compute_primes_until_nth(nth)
#                 wall_time = time.time() - start_wall
#                 stats["wall_time"] = round(wall_time, 7)
#             except Exception as e:
#                 log(f"compute error: {e}")
#                 return self._json(500, {"error": "compute failed", "detail": str(e)})
#             log(f"compute-done nth={stats['nth']} prime={stats['prime']} wall_time={stats['wall_time']}s trace_id={trace_id}")
#             if ROLE in ("one", "two"):
#                 nxt = NEXT_SERVICE or ("two" if ROLE == "one" else "three")
#                 try:
#                     next_res = call_next(nxt, nth, trace_id)
#                 except Exception as e:
#                     log(f"next call error: {e}")
#                     return self._json(502, {"service": ROLE, "trace_id": trace_id, "stats": stats,
#                                              "error": "next call failed", "detail": str(e)})
#                 return self._json(200, {"service": ROLE, "trace_id": trace_id, "stats": stats, "next": next_res})
#             elif ROLE == "three":
#                 return self._json(200, {"service": ROLE, "trace_id": trace_id, "stats": stats, "status": "done"})
#             else:
#                 return self._json(400, {"error": f"/work not valid on role={ROLE}"})

#         self._json(404, {"error": "not found", "role": ROLE, "path": path})

#     def log_message(self, fmt, *args):  # silence default noisy logs
#         log(fmt % args)


# def main():
#     srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
#     log(f"listening on :{PORT} role={ROLE} next={NEXT_SERVICE or '-'}")
#     try:
#         srv.serve_forever()
#     except KeyboardInterrupt:
#         pass
#     finally:
#         srv.server_close()


# if __name__ == "__main__":
#     sys.exit(main())

#!/usr/bin/env python3
import json
import os
import sys
import time
import uuid
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request

ROLE = os.environ.get("ROLE", "front").strip().lower()
PORT = int(os.environ.get("PORT", "8080"))
NEXT_SERVICE = os.environ.get("NEXT_SERVICE", "").strip()  # front→one, one→two, two→three
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "30"))
NODE_NAME = os.environ.get("NODE_NAME", socket.gethostname())
# front 전용: 비동기 분기 대상으로 호출할 서비스명
FORK_SERVICE = os.environ.get("FORK_SERVICE", "extra").strip().lower()


def log(msg):
    print(f"[demo][{ROLE}][node={NODE_NAME}] {msg}", flush=True)


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


def compute_primes_until_nth(target_nth: int):
    """
    N번째 소수까지 계산(목표 지향). 진행률은 count 기준으로 로그.
    """
    target = int(target_nth)
    if target <= 0:
        return {"nth": target, "prime": None}
    count = 0
    candidate = 1
    step = max(1, target // 10)
    last_log = 0
    while count < target:
        candidate += 1
        if is_prime(candidate):
            count += 1
            if count - last_log >= step or count == target:
                pct = (count / target) * 100.0
                log(f"progress nth={target} found={count} ({pct:.1f}%) last={candidate}")
                last_log = count
    return {"nth": target, "prime": candidate}


def call_next(next_name: str, seconds: int, trace_id: str):
    if not next_name:
        return {"status": "done", "service": ROLE, "trace_id": trace_id}
    # legacy second + 신규 nth 둘 다 전달
    url = f"http://{next_name}:8080/work?second={seconds}&nth={seconds}&trace_id={trace_id}"
    req = Request(url, headers={"User-Agent": "demo-numberchain/1.0"})
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


def fire_and_forget(service: str, nth: int, trace_id: str):
    """
    front 전용 비동기 분기: nth*2 까지 계산 시키고 응답은 기다리지 않음.
    """
    if not service:
        return

    def _run():
        try:
            doubled = nth * 2
            url = f"http://{service}:8080/work?second={doubled}&nth={doubled}&trace_id={trace_id}-fork"
            req = Request(url, headers={"User-Agent": "demo-numberchain/1.0"})
            # 백그라운드에서 호출·완료만 확인. body는 버림.
            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                _ = resp.read()
            log(f"fork-done service={service} trace_id={trace_id}-fork")
        except Exception as e:
            log(f"fork-error service={service} trace_id={trace_id}-fork err={e}")

    threading.Thread(target=_run, daemon=True).start()


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
            # 'nth' 우선, 없으면 legacy 'second'
            nth_str = qs.get("nth", [None])[0]
            if nth_str is None:
                nth_str = qs.get("second", ["0"])[0]
            try:
                nth = int(nth_str)
            except Exception:
                return self._json(400, {"error": "nth must be an integer > 0"})
            if nth <= 0:
                return self._json(400, {"error": "nth must be > 0"})

            trace_id = uuid.uuid4().hex
            log(f"start trace_id={trace_id} nth={nth}")

            # ① 비동기 분기(사진의 윗갈래): nth의 두 배로 extra 호출, 응답 대기 안 함
            if FORK_SERVICE:
                fire_and_forget(FORK_SERVICE, nth, trace_id)

            # ② 기존 체인(front→one→two→three) 동기 호출
            t0_wall = time.time()
            try:
                res = call_next("one" if not NEXT_SERVICE else NEXT_SERVICE, nth, trace_id)
            except Exception as e:
                log(f"error calling next: {e}")
                return self._json(502, {"error": "call_next failed", "detail": str(e)})
            wall_elapsed = time.time() - t0_wall
            log(f"done trace_id={trace_id} wall_elapsed={wall_elapsed:.7f}s")
            return self._json(200, {"trace_id": trace_id,
                                    "wall_elapsed": round(wall_elapsed, 7),
                                    "result": res})

        if path == "/work":
            nth_str = qs.get("nth", [None])[0]
            if nth_str is None:
                nth_str = qs.get("second", ["0"])[0]
            try:
                nth = int(nth_str)
            except Exception:
                return self._json(400, {"error": "nth must be an integer > 0"})
            if nth <= 0:
                return self._json(400, {"error": "nth must be > 0"})
            trace_id = qs.get("trace_id", [uuid.uuid4().hex])[0]
            log(f"compute nth={nth} trace_id={trace_id}")
            try:
                start_wall = time.time()
                stats = compute_primes_until_nth(nth)
                wall_time = time.time() - start_wall
                stats["wall_time"] = round(wall_time, 7)
            except Exception as e:
                log(f"compute error: {e}")
                return self._json(500, {"error": "compute failed", "detail": str(e)})
            log(f"compute-done nth={stats['nth']} prime={stats['prime']} wall_time={stats['wall_time']}s trace_id={trace_id}")

            if ROLE in ("one", "two"):
                nxt = NEXT_SERVICE or ("two" if ROLE == "one" else "three")
                try:
                    next_res = call_next(nxt, nth, trace_id)
                except Exception as e:
                    log(f"next call error: {e}")
                    return self._json(502, {"service": ROLE, "trace_id": trace_id, "stats": stats,
                                             "error": "next call failed", "detail": str(e)})
                return self._json(200, {"service": ROLE, "trace_id": trace_id, "stats": stats, "next": next_res})
            elif ROLE in ("three", "extra"):
                # extra는 터미널 서비스로 취급
                return self._json(200, {"service": ROLE, "trace_id": trace_id, "stats": stats, "status": "done"})
            else:
                return self._json(400, {"error": f"/work not valid on role={ROLE}"})

        self._json(404, {"error": "not found", "role": ROLE, "path": path})

    def log_message(self, fmt, *args):  # 기본 noisy 로그 억제
        log(fmt % args)


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"listening on :{PORT} role={ROLE} next={NEXT_SERVICE or '-'} fork={(FORK_SERVICE if ROLE=='front' else '-')}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    sys.exit(main())
