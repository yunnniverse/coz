#!/usr/bin/env python3

import json
import http.client
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PORT = int(os.getenv("MCOZ_GATE_PORT", "19092"))
POD_NAME = os.getenv("POD_NAME", socket.gethostname())
POD_NAMESPACE = os.getenv("POD_NAMESPACE", "default")
CONTAINER_NAME = os.getenv("MCOZ_CONTAINER", "app")
SOURCE_ID = os.getenv(
    "MCOZ_SOURCE_ID", f"{POD_NAMESPACE}/{POD_NAME}/{CONTAINER_NAME}"
).strip()
TARGET_MODE = os.getenv("MCOZ_TARGET_MODE", "override").strip().lower()
TARGET_POD_NAME = os.getenv("MCOZ_TARGET_POD", POD_NAME)
TARGET_POD_NAMESPACE = os.getenv("MCOZ_TARGET_NAMESPACE", POD_NAMESPACE)
TARGET_CONTAINER_NAME = os.getenv("MCOZ_TARGET_CONTAINER", CONTAINER_NAME)
ARM_URL = os.getenv(
    "MCOZ_ARM_URL",
    "http://coz-control-local.mcoz-system.svc.cluster.local:19091/arm",
)
DIRECT_ARM = os.getenv("MCOZ_DIRECT_ARM", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
DEFAULT_DELAY_NS = int(os.getenv("MCOZ_DELAY_NS", "10000000"))
DEFAULT_COUNT = int(os.getenv("MCOZ_COUNT", "1"))
TIMEOUT_SEC = float(os.getenv("MCOZ_ARM_TIMEOUT_SEC", "0.2"))
MATCH_MODE = os.getenv("MCOZ_MATCH_MODE", "all").strip().lower()
MATCH_HEADER = os.getenv("MCOZ_MATCH_HEADER", "x-mcoz-enable").strip().lower()
MATCH_HEADER_VALUE = os.getenv("MCOZ_MATCH_HEADER_VALUE", "1").strip()
MATCH_PATH_PREFIX = os.getenv("MCOZ_MATCH_PATH_PREFIX", "").strip()
DEBUG_HEADERS = os.getenv("MCOZ_DEBUG_HEADERS", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
FAIL_OPEN_ON_ARM_UNAVAILABLE = os.getenv(
    "MCOZ_FAIL_OPEN_ON_ARM_UNAVAILABLE", "true"
).strip().lower() in ("1", "true", "yes", "on")
ARM_UNAVAILABLE_STATUSES_RAW = os.getenv(
    "MCOZ_ARM_UNAVAILABLE_STATUSES", "429,500,502,503,504"
)
ARM_SUSPEND_SEC = float(os.getenv("MCOZ_ARM_SUSPEND_SEC", "5.0"))
ARM_ACTIVE_DEFAULT = os.getenv("MCOZ_ARM_ACTIVE_DEFAULT", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
REFILL_TARGET_REQ = int(os.getenv("MCOZ_REFILL_TARGET_REQ", "16"))
REFILL_LOW_WATER_REQ = int(os.getenv("MCOZ_REFILL_LOW_WATER_REQ", "4"))
REFILL_BATCH_REQ = int(os.getenv("MCOZ_REFILL_BATCH_REQ", "8"))
SYNC_REFILL_ON_MISS = os.getenv("MCOZ_SYNC_REFILL_ON_MISS", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
VERBOSE_EVENTS = os.getenv("MCOZ_VERBOSE_EVENTS", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

if REFILL_TARGET_REQ <= 0:
    REFILL_TARGET_REQ = 16
if REFILL_LOW_WATER_REQ < 0:
    REFILL_LOW_WATER_REQ = 0
if REFILL_LOW_WATER_REQ >= REFILL_TARGET_REQ:
    REFILL_LOW_WATER_REQ = max(0, REFILL_TARGET_REQ // 2)
if REFILL_BATCH_REQ <= 0:
    REFILL_BATCH_REQ = 8

ARM_URL_PARSED = urllib.parse.urlparse(ARM_URL)
ARM_URL_HOST = ARM_URL_PARSED.hostname or ""
ARM_URL_PORT = ARM_URL_PARSED.port or (443 if ARM_URL_PARSED.scheme == "https" else 80)
ARM_URL_PATH = ARM_URL_PARSED.path or "/arm"
if ARM_URL_PARSED.query:
    ARM_URL_PATH += f"?{ARM_URL_PARSED.query}"
ARM_URL_SCHEME = ARM_URL_PARSED.scheme or "http"


def _parse_status_set(raw):
    values = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.add(int(token))
        except ValueError:
            continue
    return values


ARM_UNAVAILABLE_STATUSES = _parse_status_set(ARM_UNAVAILABLE_STATUSES_RAW)

LOCK = threading.Lock()
ARM_SUSPENDED_UNTIL = 0.0
ARM_SUSPEND_REASON = ""
ARM_ACTIVE = ARM_ACTIVE_DEFAULT
ARM_DELAY_NS = DEFAULT_DELAY_NS
ARM_COUNT = DEFAULT_COUNT
CREDIT_BALANCE_REQ = 0
REFILL_EVENT = threading.Event()
ARM_HTTP_LOCK = threading.Lock()
ARM_HTTP_CONN = None
STATS = {
    "triggered": 0,
    "armed_ok": 0,
    "armed_fail": 0,
    "arm_suppressed": 0,
    "arm_suspended_count": 0,
    "arm_suspend_until_unix": 0,
    "arm_suspend_reason": "",
    "arm_active": 1 if ARM_ACTIVE_DEFAULT else 0,
    "arm_toggle_count": 0,
    "disabled_skip": 0,
    "skipped": 0,
    "arm_delay_ns": DEFAULT_DELAY_NS,
    "arm_count": DEFAULT_COUNT,
    "credit_balance_req": 0,
    "credit_miss": 0,
    "refill_request": 0,
    "refill_ok": 0,
    "refill_fail": 0,
    "refill_tokens_added": 0,
    "refill_credits_added": 0,
    "last_refill_ms": 0.0,
    "config_skip": 0,
    "last_error": "",
    "last_path": "",
    "last_request_id": "",
}


def _header_val(headers, key, default=""):
    return headers.get(key, headers.get(key.lower(), default))


def _extract_original_path(handler):
    hdr = handler.headers
    for key in (
        "x-envoy-original-path",
        "x-original-path",
        "x-request-path",
        ":path",
    ):
        value = _header_val(hdr, key, "")
        if value:
            return value
    return handler.path


def _normalize_headers(headers):
    if not headers:
        return {}
    if hasattr(headers, "items"):
        return {str(k).lower(): str(v) for k, v in headers.items()}
    out = {}
    if isinstance(headers, dict):
        for key, value in headers.items():
            out[str(key).lower()] = str(value)
    return out


def _should_arm(path, headers):
    # Control/health paths must never arm credits.
    lower_path = (path or "").lower()
    if (
        "/set_enabled" in lower_path
        or "/set_target" in lower_path
        or "/healthz" in lower_path
    ):
        return False

    if MATCH_MODE == "all":
        return True

    header_ok = True
    if MATCH_MODE in ("header", "header_or_path", "header_and_path"):
        got = _header_val(headers, MATCH_HEADER, "")
        header_ok = (got == MATCH_HEADER_VALUE)

    path_ok = True
    if MATCH_MODE in ("path", "header_or_path", "header_and_path"):
        if not MATCH_PATH_PREFIX:
            path_ok = True
        else:
            path_ok = path.startswith(MATCH_PATH_PREFIX)

    if MATCH_MODE == "header":
        return header_ok
    if MATCH_MODE == "path":
        return path_ok
    if MATCH_MODE == "header_or_path":
        return header_ok or path_ok
    if MATCH_MODE == "header_and_path":
        return header_ok and path_ok
    return True


def _process_trigger(path, headers, request_id):
    started = time.time()
    normalized_headers = _normalize_headers(headers)
    request_id = str(request_id or f"no-id-{int(started * 1e6)}")
    path = str(path or "/trigger")

    with LOCK:
        STATS["triggered"] += 1
        STATS["last_path"] = path
        STATS["last_request_id"] = request_id

    should_arm = _should_arm(path, normalized_headers)
    armed = False
    arm_suspended = False
    arm_disabled = False
    err = ""
    if not _is_arm_active():
        arm_disabled = True
        with LOCK:
            STATS["disabled_skip"] += 1
    elif should_arm:
        current_delay_ns, current_count = _get_arm_config()
        if current_delay_ns <= 0 or current_count <= 0:
            with LOCK:
                STATS["config_skip"] += 1
        else:
            now = time.time()
            suspended, until, reason = _check_arm_suspended(now)
            if suspended:
                arm_suspended = True
                remaining = until - now
                err = f"arm suspended remaining={remaining:.3f}s reason={reason[:120]}"
                with LOCK:
                    STATS["arm_suppressed"] += 1
            elif DIRECT_ARM:
                ok, status_code, body = _arm_once(
                    request_id, path, current_delay_ns, current_count
                )
                if ok:
                    with LOCK:
                        STATS["armed_ok"] += 1
                    armed = True
                    _clear_arm_suspended()
                    if VERBOSE_EVENTS:
                        print(
                            f"[MCOZ-GATE] state=ARMED req_id={request_id} path={path} direct=1",
                            flush=True,
                        )
                else:
                    err = (
                        f"direct arm failed status={status_code if status_code is not None else 'exception'} "
                        f"body={str(body)[:200]}"
                    )
                    with LOCK:
                        STATS["armed_fail"] += 1
                        STATS["last_error"] = err
                    arm_suspended = _maybe_suspend_arm(status_code, err)
            else:
                consumed, balance_after = _consume_credit_token()
                if consumed:
                    with LOCK:
                        STATS["armed_ok"] += 1
                    armed = True
                    if balance_after <= REFILL_LOW_WATER_REQ:
                        _signal_refill()
                    if VERBOSE_EVENTS:
                        print(
                            f"[MCOZ-GATE] state=ARMED req_id={request_id} path={path} balance={balance_after}",
                            flush=True,
                        )
                else:
                    _signal_refill()
                    if SYNC_REFILL_ON_MISS:
                        ok, status_code, body, _ = _do_refill(max(1, REFILL_BATCH_REQ), "sync-miss")
                        if ok:
                            consumed2, balance_after2 = _consume_credit_token()
                            if consumed2:
                                with LOCK:
                                    STATS["armed_ok"] += 1
                                armed = True
                                if balance_after2 <= REFILL_LOW_WATER_REQ:
                                    _signal_refill()
                            else:
                                err = "credit miss after successful sync refill"
                                with LOCK:
                                    STATS["armed_fail"] += 1
                                    STATS["last_error"] = err
                        else:
                            err = (
                                f"sync refill failed status={status_code if status_code is not None else 'exception'} "
                                f"body={str(body)[:200]}"
                            )
                            with LOCK:
                                STATS["armed_fail"] += 1
                                STATS["last_error"] = err
                            arm_suspended = _maybe_suspend_arm(status_code, err)
                    else:
                        err = "credit miss; refill queued"
                        with LOCK:
                            STATS["armed_fail"] += 1
                            STATS["last_error"] = err
                        if VERBOSE_EVENTS:
                            print(
                                f"[MCOZ-GATE] state=MISS req_id={request_id} path={path} {err}",
                                flush=True,
                            )
    else:
        with LOCK:
            STATS["skipped"] += 1
        if VERBOSE_EVENTS:
            print(
                f"[MCOZ-GATE] state=TRIGGERED req_id={request_id} path={path} skipped=1",
                flush=True,
            )

    elapsed_us = int((time.time() - started) * 1_000_000)
    current_delay_ns, current_count = _get_arm_config()
    return {
        "ok": True,
        "triggered": True,
        "request_id": request_id,
        "path": path,
        "armed": armed,
        "delay_ns": current_delay_ns,
        "count": current_count,
        "gate_us": elapsed_us,
        "arm_suspended": arm_suspended,
        "arm_active": _is_arm_active(),
        "arm_disabled": arm_disabled,
        "arm_error": err,
    }


def _resolve_udp_target():
    if not ARM_URL_HOST or ARM_URL_PORT <= 0:
        raise RuntimeError("invalid udp arm target")
    infos = socket.getaddrinfo(
        ARM_URL_HOST, ARM_URL_PORT, socket.AF_UNSPEC, socket.SOCK_DGRAM
    )
    if not infos:
        raise RuntimeError("no udp arm target addresses")
    family, socktype, proto, _, sockaddr = infos[0]
    return family, socktype, proto, sockaddr


def _arm_transport_post(payload_obj, payload_bytes, request_id, path):
    global ARM_HTTP_CONN

    headers = {
        "Content-Type": "application/json",
        "X-MCOZ-Request-Id": request_id,
        "X-MCOZ-Path": path,
    }

    if ARM_URL_SCHEME == "udp":
        body = urllib.parse.urlencode(
            {
                "namespace": payload_obj.get("namespace", ""),
                "pod": payload_obj.get("pod", ""),
                "container": payload_obj.get("container", ""),
                "source": payload_obj.get("source", ""),
                "delay_ns": str(int(payload_obj.get("delay_ns", 0))),
                "count": str(int(payload_obj.get("count", 0))),
                "request_id": request_id,
                "path": path,
            }
        ).encode("utf-8")
        try:
            family, socktype, proto, sockaddr = _resolve_udp_target()
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(TIMEOUT_SEC)
                sent = sock.sendto(body, sockaddr)
            if sent != len(body):
                return False, None, f"partial udp send {sent}/{len(body)}"
            return True, 202, "udp-sent"
        except Exception as exc:
            return False, None, str(exc)

    if ARM_URL_SCHEME == "http":
        with ARM_HTTP_LOCK:
            try:
                if ARM_HTTP_CONN is None:
                    ARM_HTTP_CONN = http.client.HTTPConnection(
                        ARM_URL_HOST, ARM_URL_PORT, timeout=TIMEOUT_SEC
                    )
                ARM_HTTP_CONN.request("POST", ARM_URL_PATH, body=payload_bytes, headers=headers)
                resp = ARM_HTTP_CONN.getresponse()
                body = resp.read().decode("utf-8", errors="replace")
                return (200 <= resp.status < 300), resp.status, body
            except Exception as exc:
                try:
                    if ARM_HTTP_CONN is not None:
                        ARM_HTTP_CONN.close()
                except Exception:
                    pass
                ARM_HTTP_CONN = None
                return False, None, str(exc)

    req = urllib.request.Request(url=ARM_URL, data=payload_bytes, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-MCOZ-Request-Id", request_id)
    req.add_header("X-MCOZ-Path", path)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return (200 <= resp.status < 300), resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, int(exc.code), body
    except Exception as exc:
        return False, None, str(exc)


def _arm_once(request_id, path, delay_ns, count):
    target_namespace, target_pod, target_container = _get_target()
    payload_obj = {
        "namespace": target_namespace,
        "pod": target_pod,
        "container": target_container,
        "source": SOURCE_ID,
        "delay_ns": delay_ns,
        "count": count,
    }
    payload = json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
    return _arm_transport_post(payload_obj, payload, request_id, path)


def _current_refill_need():
    if DIRECT_ARM:
        return 0, 0, 0, 0
    with LOCK:
        if not ARM_ACTIVE:
            return 0, ARM_DELAY_NS, ARM_COUNT, CREDIT_BALANCE_REQ
        delay_ns = ARM_DELAY_NS
        count = ARM_COUNT
        balance = CREDIT_BALANCE_REQ
    if delay_ns <= 0 or count <= 0:
        return 0, delay_ns, count, balance
    if balance >= REFILL_LOW_WATER_REQ:
        return 0, delay_ns, count, balance
    need = max(REFILL_BATCH_REQ, REFILL_TARGET_REQ - balance)
    if need <= 0:
        need = REFILL_BATCH_REQ
    return need, delay_ns, count, balance


def _do_refill(tokens_req, reason):
    if DIRECT_ARM:
        return False, None, "direct-arm-mode", 0.0
    if tokens_req <= 0:
        return True, 0, "", 0.0
    with LOCK:
        delay_ns = ARM_DELAY_NS
        count = ARM_COUNT
        active = ARM_ACTIVE
    if (not active) or delay_ns <= 0 or count <= 0:
        return False, None, "inactive-or-invalid-config", 0.0

    credits = tokens_req * count
    req_id = f"refill-{int(time.time() * 1e6)}"
    t0 = time.perf_counter_ns()
    ok, status_code, body = _arm_once(req_id, f"/refill/{reason}", delay_ns, credits)
    elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0

    if ok:
        _clear_arm_suspended()
        with LOCK:
            global CREDIT_BALANCE_REQ
            CREDIT_BALANCE_REQ += tokens_req
            STATS["credit_balance_req"] = CREDIT_BALANCE_REQ
            STATS["refill_ok"] += 1
            STATS["refill_tokens_added"] += tokens_req
            STATS["refill_credits_added"] += credits
            STATS["last_refill_ms"] = round(elapsed_ms, 3)
        return True, status_code, body, elapsed_ms

    err = f"refill status={status_code if status_code is not None else 'exception'} body={str(body)[:200]}"
    with LOCK:
        STATS["refill_fail"] += 1
        STATS["last_error"] = err
        STATS["last_refill_ms"] = round(elapsed_ms, 3)
    _maybe_suspend_arm(status_code, err)
    return False, status_code, body, elapsed_ms


def _signal_refill():
    if DIRECT_ARM:
        return
    with LOCK:
        STATS["refill_request"] += 1
    REFILL_EVENT.set()


def _consume_credit_token():
    global CREDIT_BALANCE_REQ
    with LOCK:
        if CREDIT_BALANCE_REQ > 0:
            CREDIT_BALANCE_REQ -= 1
            STATS["credit_balance_req"] = CREDIT_BALANCE_REQ
            return True, CREDIT_BALANCE_REQ
        STATS["credit_miss"] += 1
        return False, CREDIT_BALANCE_REQ


def _prime_refill_on_enable():
    if DIRECT_ARM:
        return {"attempted": False, "ok": True, "reason": "direct-arm-mode", "balance": 0}
    with LOCK:
        active = ARM_ACTIVE
        delay_ns = ARM_DELAY_NS
        count = ARM_COUNT
        balance = CREDIT_BALANCE_REQ
    if (not active) or delay_ns <= 0 or count <= 0:
        return {"attempted": False, "ok": True, "reason": "inactive-or-invalid-config", "balance": balance}
    need = REFILL_TARGET_REQ - balance
    if need <= 0:
        return {"attempted": False, "ok": True, "reason": "already-primed", "balance": balance}
    ok, status_code, _, elapsed_ms = _do_refill(need, "enable")
    with LOCK:
        now_balance = CREDIT_BALANCE_REQ
    return {
        "attempted": True,
        "ok": ok,
        "status_code": status_code,
        "elapsed_ms": round(elapsed_ms, 3),
        "requested_tokens": need,
        "balance": now_balance,
    }


def _clear_arm_suspended():
    global ARM_SUSPENDED_UNTIL
    global ARM_SUSPEND_REASON
    with LOCK:
        ARM_SUSPENDED_UNTIL = 0.0
        ARM_SUSPEND_REASON = ""
        STATS["arm_suspend_until_unix"] = 0
        STATS["arm_suspend_reason"] = ""


def _suspend_arm(reason):
    global ARM_SUSPENDED_UNTIL
    global ARM_SUSPEND_REASON
    until = time.time() + ARM_SUSPEND_SEC
    with LOCK:
        ARM_SUSPENDED_UNTIL = until
        ARM_SUSPEND_REASON = reason[:200]
        STATS["arm_suspended_count"] += 1
        STATS["arm_suspend_until_unix"] = round(until, 6)
        STATS["arm_suspend_reason"] = ARM_SUSPEND_REASON
    print(
        f"[MCOZ-GATE] state=SUSPENDED until={round(until, 3)} reason={reason[:120]}",
        flush=True,
    )


def _check_arm_suspended(now):
    with LOCK:
        until = ARM_SUSPENDED_UNTIL
        reason = ARM_SUSPEND_REASON
    return (now < until), until, reason


def _refill_worker():
    while True:
        REFILL_EVENT.wait(timeout=0.5)
        REFILL_EVENT.clear()
        while True:
            now = time.time()
            suspended, _, _ = _check_arm_suspended(now)
            if suspended:
                break
            need, _, _, _ = _current_refill_need()
            if need <= 0:
                break
            ok, _, _, _ = _do_refill(need, "async")
            if not ok:
                break


def _maybe_suspend_arm(status_code, err_text):
    if not FAIL_OPEN_ON_ARM_UNAVAILABLE:
        return False
    if status_code is not None and status_code not in ARM_UNAVAILABLE_STATUSES:
        return False
    _suspend_arm(
        f"arm unavailable status={status_code if status_code is not None else 'exception'} err={err_text[:120]}"
    )
    return True


def _set_arm_active(active):
    global ARM_ACTIVE
    global CREDIT_BALANCE_REQ
    prime_result = None
    with LOCK:
        changed = ARM_ACTIVE != active
        ARM_ACTIVE = active
        STATS["arm_active"] = 1 if ARM_ACTIVE else 0
        if changed:
            STATS["arm_toggle_count"] += 1
        if not ARM_ACTIVE:
            CREDIT_BALANCE_REQ = 0
            STATS["credit_balance_req"] = 0
    print(f"[MCOZ-GATE] state={'ENABLED' if active else 'DISABLED'}", flush=True)
    if ARM_ACTIVE and not DIRECT_ARM:
        prime_result = _prime_refill_on_enable()
        _signal_refill()
    else:
        _clear_arm_suspended()
    if prime_result is not None and VERBOSE_EVENTS:
        print(f"[MCOZ-GATE] prime={prime_result}", flush=True)
    return changed


def _is_arm_active():
    with LOCK:
        return ARM_ACTIVE


def _to_int(value):
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _get_arm_config():
    with LOCK:
        return ARM_DELAY_NS, ARM_COUNT


def _set_arm_config(delay_ns=None, count=None):
    global ARM_DELAY_NS
    global ARM_COUNT
    global CREDIT_BALANCE_REQ
    changed = False
    with LOCK:
        if delay_ns is not None and delay_ns >= 0 and delay_ns != ARM_DELAY_NS:
            ARM_DELAY_NS = delay_ns
            changed = True
        if count is not None and count >= 0 and count != ARM_COUNT:
            ARM_COUNT = count
            changed = True
        if changed:
            CREDIT_BALANCE_REQ = 0
            STATS["credit_balance_req"] = 0
        STATS["arm_delay_ns"] = ARM_DELAY_NS
        STATS["arm_count"] = ARM_COUNT
        current_delay_ns = ARM_DELAY_NS
        current_count = ARM_COUNT
    return changed, current_delay_ns, current_count


def _get_target():
    with LOCK:
        if TARGET_MODE == "self":
            return POD_NAMESPACE, POD_NAME, CONTAINER_NAME
        return TARGET_POD_NAMESPACE, TARGET_POD_NAME, TARGET_CONTAINER_NAME


def _set_target(pod=None, namespace=None, container=None):
    global TARGET_POD_NAME
    global TARGET_POD_NAMESPACE
    global TARGET_CONTAINER_NAME

    changed = False
    with LOCK:
        if TARGET_MODE == "self":
            return False, (POD_NAMESPACE, POD_NAME, CONTAINER_NAME)
        if namespace is not None and namespace != TARGET_POD_NAMESPACE:
            TARGET_POD_NAMESPACE = namespace
            changed = True
        if pod is not None and pod != TARGET_POD_NAME:
            TARGET_POD_NAME = pod
            changed = True
        if container is not None and container != TARGET_CONTAINER_NAME:
            TARGET_CONTAINER_NAME = container
            changed = True
        current = (TARGET_POD_NAMESPACE, TARGET_POD_NAME, TARGET_CONTAINER_NAME)
    return changed, current


def _extract_target_from_query(query):
    namespace_vals = query.get("namespace", query.get("target_namespace", []))
    pod_vals = query.get("pod", query.get("target_pod", []))
    container_vals = query.get("container", query.get("target_container", []))
    namespace = str(namespace_vals[-1]).strip() if namespace_vals else None
    pod = str(pod_vals[-1]).strip() if pod_vals else None
    container = str(container_vals[-1]).strip() if container_vals else None
    return namespace or None, pod or None, container or None


def _extract_target_from_body(body, ctype, namespace, pod, container):
    if not body:
        return namespace, pod, container
    raw = body.decode("utf-8", errors="replace")
    if "application/json" in ctype:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                if namespace is None:
                    namespace = str(
                        obj.get("namespace", obj.get("target_namespace", ""))
                    ).strip()
                if pod is None:
                    pod = str(obj.get("pod", obj.get("target_pod", ""))).strip()
                if container is None:
                    container = str(
                        obj.get("container", obj.get("target_container", ""))
                    ).strip()
        except Exception:
            pass
        return namespace or None, pod or None, container or None

    form = urllib.parse.parse_qs(raw, keep_blank_values=True)
    if namespace is None and form.get("namespace"):
        namespace = form["namespace"][-1].strip()
    if namespace is None and form.get("target_namespace"):
        namespace = form["target_namespace"][-1].strip()
    if pod is None and form.get("pod"):
        pod = form["pod"][-1].strip()
    if pod is None and form.get("target_pod"):
        pod = form["target_pod"][-1].strip()
    if container is None and form.get("container"):
        container = form["container"][-1].strip()
    if container is None and form.get("target_container"):
        container = form["target_container"][-1].strip()
    return namespace or None, pod or None, container or None


def _extract_arm_config_from_query(query):
    delay_vals = query.get("delay_ns", query.get("delayNs", []))
    count_vals = query.get("count", [])
    delay_ns = _to_int(delay_vals[-1]) if delay_vals else None
    count = _to_int(count_vals[-1]) if count_vals else None
    return delay_ns, count


def _extract_arm_config_from_body(body, ctype, delay_ns, count):
    if not body:
        return delay_ns, count
    raw = body.decode("utf-8", errors="replace")
    if "application/json" in ctype:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                if delay_ns is None:
                    delay_ns = _to_int(obj.get("delay_ns", obj.get("delayNs")))
                if count is None:
                    count = _to_int(obj.get("count"))
        except Exception:
            pass
        return delay_ns, count
    form = urllib.parse.parse_qs(raw, keep_blank_values=True)
    if delay_ns is None and form.get("delay_ns"):
        delay_ns = _to_int(form["delay_ns"][-1])
    if delay_ns is None and form.get("delayNs"):
        delay_ns = _to_int(form["delayNs"][-1])
    if count is None and form.get("count"):
        count = _to_int(form["count"][-1])
    return delay_ns, count


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[mcoz-gate] {self.client_address[0]} - {fmt % args}", flush=True)

    def _send_text(self, code, body, extra_headers=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, code, obj):
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_check(self):
        path = _extract_original_path(self)
        request_id = _header_val(self.headers, "x-request-id", "")
        result = _process_trigger(path, self.headers, request_id)
        headers = {}
        if DEBUG_HEADERS:
            headers = {
                "x-mcoz-triggered": "1",
                "x-mcoz-armed": "1" if result["armed"] else "0",
                "x-mcoz-delay-ns": str(result["delay_ns"]),
                "x-mcoz-count": str(result["count"]),
                "x-mcoz-gate-us": str(result["gate_us"]),
                "x-mcoz-arm-suspended": "1" if result["arm_suspended"] else "0",
                "x-mcoz-arm-active": "1" if result["arm_active"] else "0",
                "x-mcoz-arm-disabled": "1" if result["arm_disabled"] else "0",
            }
            if result["arm_error"]:
                headers["x-mcoz-arm-error"] = result["arm_error"][:120]

        # ext_authz contract: HTTP 200 => allow
        self._send_text(200, "OK", headers)

    def do_GET(self):
        if self.path.startswith("/healthz"):
            target_namespace, target_pod, target_container = _get_target()
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "mcoz-gate",
                    "pod": POD_NAME,
                    "namespace": POD_NAMESPACE,
                    "container": CONTAINER_NAME,
                    "source": SOURCE_ID,
                    "target_mode": TARGET_MODE,
                    "target_pod": target_pod,
                    "target_namespace": target_namespace,
                    "target_container": target_container,
                    "arm_active": _is_arm_active(),
                },
            )
            return
        if self.path.startswith("/metrics"):
            with LOCK:
                payload = dict(STATS)
            current_delay_ns, current_count = _get_arm_config()
            target_namespace, target_pod, target_container = _get_target()
            payload.update(
                {
                    "mode": "request-trigger-gate",
                    "trigger_endpoint": "/trigger",
                    "arm_url": ARM_URL,
                    "delay_ns": current_delay_ns,
                    "count": current_count,
                    "container": CONTAINER_NAME,
                    "source": SOURCE_ID,
                    "target_mode": TARGET_MODE,
                    "target_pod": target_pod,
                    "target_namespace": target_namespace,
                    "target_container": target_container,
                    "match_mode": MATCH_MODE,
                    "match_header": MATCH_HEADER,
                    "match_header_value": MATCH_HEADER_VALUE,
                    "match_path_prefix": MATCH_PATH_PREFIX,
                    "fail_open_on_arm_unavailable": FAIL_OPEN_ON_ARM_UNAVAILABLE,
                    "arm_unavailable_statuses": sorted(ARM_UNAVAILABLE_STATUSES),
                    "arm_suspend_sec": ARM_SUSPEND_SEC,
                    "arm_active_default": ARM_ACTIVE_DEFAULT,
                    "direct_arm": DIRECT_ARM,
                    "arm_transport": ARM_URL_SCHEME,
                    "refill_target_req": REFILL_TARGET_REQ,
                    "refill_low_water_req": REFILL_LOW_WATER_REQ,
                    "refill_batch_req": REFILL_BATCH_REQ,
                    "sync_refill_on_miss": SYNC_REFILL_ON_MISS,
                    "verbose_events": VERBOSE_EVENTS,
                }
            )
            self._send_json(200, payload)
            return
        if self.path.startswith("/trigger"):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            path = query.get("path", ["/trigger"])[-1]
            request_id = query.get("request_id", [""])[-1]
            headers = {}
            for key, values in query.items():
                if key.startswith("header.") and values:
                    headers[key[7:]] = values[-1]
            result = _process_trigger(path, headers, request_id)
            self._send_json(200, result)
            return
        if self.path.startswith("/set_target"):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            namespace, pod, container = _extract_target_from_query(query)
            current_namespace, current_pod, current_container = _get_target()
            namespace = namespace or current_namespace
            pod = pod or current_pod
            container = container or current_container
            if not pod:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": "missing pod",
                        "example": "/set_target?namespace=social&pod=unique-id-service-xxxxx&container=unique-id-service",
                    },
                )
                return
            changed, current = _set_target(
                pod=pod, namespace=namespace, container=container
            )
            self._send_json(
                200,
                {
                    "ok": True,
                    "changed": changed,
                    "target_namespace": current[0],
                    "target_pod": current[1],
                    "target_container": current[2],
                    "target_mode": TARGET_MODE,
                    "service": "mcoz-gate",
                    "pod": POD_NAME,
                    "namespace": POD_NAMESPACE,
                    "container": CONTAINER_NAME,
                },
            )
            return
        if self.path.startswith("/set_enabled"):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            requested = query.get("enabled", query.get("active", []))
            if not requested:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": "missing enabled",
                        "example": "/set_enabled?enabled=1",
                    },
                )
                return
            enabled = str(requested[-1]).strip().lower() in ("1", "true", "yes", "on")
            delay_ns, count = _extract_arm_config_from_query(query)
            cfg_changed, current_delay_ns, current_count = _set_arm_config(
                delay_ns=delay_ns, count=count
            )
            changed = _set_arm_active(enabled)
            prime_result = None
            if enabled and cfg_changed and not changed and not DIRECT_ARM:
                prime_result = _prime_refill_on_enable()
                _signal_refill()
            with LOCK:
                balance = CREDIT_BALANCE_REQ
            target_namespace, target_pod, target_container = _get_target()
            self._send_json(
                200,
                {
                    "ok": True,
                    "enabled": enabled,
                    "changed": changed,
                    "config_changed": cfg_changed,
                    "delay_ns": current_delay_ns,
                    "count": current_count,
                    "credit_balance_req": balance,
                    "prime": prime_result,
                    "service": "mcoz-gate",
                    "pod": POD_NAME,
                    "namespace": POD_NAMESPACE,
                    "container": CONTAINER_NAME,
                    "source": SOURCE_ID,
                    "target_pod": target_pod,
                    "target_namespace": target_namespace,
                    "target_container": target_container,
                },
            )
            return
        self._handle_check()

    def do_POST(self):
        # ext_authz requests are typically POST checks.
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = b""
        if length > 0:
            body = self.rfile.read(length)
        if parsed.path.startswith("/set_enabled"):
            enabled = None
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            requested = query.get("enabled", query.get("active", []))
            delay_ns, count = _extract_arm_config_from_query(query)
            if requested:
                enabled = str(requested[-1]).strip().lower() in ("1", "true", "yes", "on")
            ctype = self.headers.get("Content-Type", "")
            delay_ns, count = _extract_arm_config_from_body(body, ctype, delay_ns, count)
            if enabled is None and body:
                raw = body.decode("utf-8", errors="replace")
                if "application/json" in ctype:
                    try:
                        obj = json.loads(raw)
                        enabled_val = obj.get("enabled", obj.get("active"))
                        if enabled_val is not None:
                            enabled = str(enabled_val).strip().lower() in (
                                "1",
                                "true",
                                "yes",
                                "on",
                            )
                    except Exception:
                        enabled = None
                else:
                    form = urllib.parse.parse_qs(raw, keep_blank_values=True)
                    if form.get("enabled"):
                        enabled = str(form["enabled"][-1]).strip().lower() in (
                            "1",
                            "true",
                            "yes",
                            "on",
                        )
            if enabled is None:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": "missing enabled",
                        "example": "POST /set_enabled?enabled=1",
                    },
                )
                return
            cfg_changed, current_delay_ns, current_count = _set_arm_config(
                delay_ns=delay_ns, count=count
            )
            changed = _set_arm_active(enabled)
            prime_result = None
            if enabled and cfg_changed and not changed and not DIRECT_ARM:
                prime_result = _prime_refill_on_enable()
                _signal_refill()
            with LOCK:
                balance = CREDIT_BALANCE_REQ
            target_namespace, target_pod, target_container = _get_target()
            self._send_json(
                200,
                {
                    "ok": True,
                    "enabled": enabled,
                    "changed": changed,
                    "config_changed": cfg_changed,
                    "delay_ns": current_delay_ns,
                    "count": current_count,
                    "credit_balance_req": balance,
                    "prime": prime_result,
                    "service": "mcoz-gate",
                    "pod": POD_NAME,
                    "namespace": POD_NAMESPACE,
                    "container": CONTAINER_NAME,
                    "target_pod": target_pod,
                    "target_namespace": target_namespace,
                    "target_container": target_container,
                },
            )
            return
        if parsed.path.startswith("/set_target"):
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            namespace, pod, container = _extract_target_from_query(query)
            ctype = self.headers.get("Content-Type", "")
            namespace, pod, container = _extract_target_from_body(
                body, ctype, namespace, pod, container
            )
            current_namespace, current_pod, current_container = _get_target()
            namespace = namespace or current_namespace
            pod = pod or current_pod
            container = container or current_container
            if not pod:
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": "missing pod",
                        "example": "POST /set_target {\"namespace\":\"social\",\"pod\":\"unique-id-service-xxxxx\",\"container\":\"unique-id-service\"}",
                    },
                )
                return
            changed, current = _set_target(
                pod=pod, namespace=namespace, container=container
            )
            self._send_json(
                200,
                {
                    "ok": True,
                    "changed": changed,
                    "target_namespace": current[0],
                    "target_pod": current[1],
                    "target_container": current[2],
                    "target_mode": TARGET_MODE,
                    "service": "mcoz-gate",
                    "pod": POD_NAME,
                    "namespace": POD_NAMESPACE,
                    "container": CONTAINER_NAME,
                    "source": SOURCE_ID,
                },
            )
            return
        if parsed.path.startswith("/trigger"):
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            path = query.get("path", ["/trigger"])[-1]
            request_id = query.get("request_id", [""])[-1]
            headers = {}
            for key, values in query.items():
                if key.startswith("header.") and values:
                    headers[key[7:]] = values[-1]
            ctype = self.headers.get("Content-Type", "")
            if body:
                raw = body.decode("utf-8", errors="replace")
                if "application/json" in ctype:
                    try:
                        obj = json.loads(raw)
                        if isinstance(obj, dict):
                            path = str(obj.get("path", path))
                            request_id = str(obj.get("request_id", request_id))
                            header_obj = obj.get("headers", {})
                            if isinstance(header_obj, dict):
                                for key, value in header_obj.items():
                                    headers[str(key)] = str(value)
                    except Exception:
                        pass
                else:
                    form = urllib.parse.parse_qs(raw, keep_blank_values=True)
                    if form.get("path"):
                        path = form["path"][-1]
                    if form.get("request_id"):
                        request_id = form["request_id"][-1]
                    for key, values in form.items():
                        if key.startswith("header.") and values:
                            headers[key[7:]] = values[-1]
            result = _process_trigger(path, headers, request_id)
            self._send_json(200, result)
            return
        self._handle_check()


def main():
    target_namespace, target_pod, target_container = _get_target()
    print(
        f"[mcoz-gate] listening on 0.0.0.0:{PORT} "
        f"(pod={POD_NAME}, ns={POD_NAMESPACE}, target={target_namespace}/{target_pod}:{target_container}, arm={ARM_URL})",
        flush=True,
    )
    threading.Thread(target=_refill_worker, name="mcoz-gate-refill", daemon=True).start()
    if _is_arm_active():
        _signal_refill()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
