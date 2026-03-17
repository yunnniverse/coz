#!/usr/bin/env python3

import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


NAMESPACE = os.getenv("NAMESPACE", "mcoz-system")
POD_NAME = os.getenv("POD_NAME", socket.gethostname())
POD_IP = os.getenv("POD_IP", "")
CONTROL_PORT = int(os.getenv("CONTROL_PORT", "19091"))
DEFAULT_SPEEDUP = os.getenv("SPEEDUP", "0.25")
PEER_LABEL = os.getenv("COZ_PEER_LABEL", "app=coz")
FORWARD_TIMEOUT_SEC = float(os.getenv("CONTROL_FORWARD_TIMEOUT_SEC", "2.0"))
CLEAR_LOCAL_URL = os.getenv("CLEAR_LOCAL_URL", "http://127.0.0.1:19090/clear")
REARM_LOCAL_URL = os.getenv("REARM_LOCAL_URL", "http://127.0.0.1:19090/rearm")
ARM_LOCAL_URL = os.getenv("ARM_LOCAL_URL", "http://127.0.0.1:19090/arm")
STATUS_LOCAL_URL = os.getenv("STATUS_LOCAL_URL", "http://127.0.0.1:19090/status")
SYSCALL_PROFILE_LOCAL_URL = os.getenv(
    "SYSCALL_PROFILE_LOCAL_URL", "http://127.0.0.1:19090/syscall_profile"
)
CONSUME_POLICY_LOCAL_URL = os.getenv(
    "CONSUME_POLICY_LOCAL_URL", "http://127.0.0.1:19090/consume_policy"
)
DEFAULT_FIXED_DELAY_NS = os.getenv("DEFAULT_FIXED_DELAY_NS", "10000000")
GATE_CONTROL_ENABLED = os.getenv("MCOZ_GATE_CONTROL_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)
GATE_CONTROL_PORT = int(os.getenv("MCOZ_GATE_CONTROL_PORT", "19093"))
GATE_CONTROL_PATH = os.getenv("MCOZ_GATE_CONTROL_PATH", "/set_enabled")
GATE_CONTROL_TIMEOUT_SEC = float(os.getenv("MCOZ_GATE_CONTROL_TIMEOUT_SEC", "0.5"))
GATE_CONTROL_NAMESPACES = [
    ns.strip()
    for ns in os.getenv("MCOZ_GATE_CONTROL_NAMESPACES", "trace-demo,social").split(",")
    if ns.strip()
]
GATE_CONTAINER_NAME = os.getenv("MCOZ_GATE_CONTAINER_NAME", "mcoz-gate")
TRACE_ANALYZER_SCRIPT = os.getenv(
    "TRACE_ANALYZER_SCRIPT", "/usr/local/bin/mcoz_trace_analyzer.py"
)
TRACE_ANALYZER_PYTHON = os.getenv("TRACE_ANALYZER_PYTHON", "python3")
TRACE_ANALYZER_TIMEOUT_SEC = float(os.getenv("TRACE_ANALYZER_TIMEOUT_SEC", "45"))
TRACE_ANALYZER_OUT_DIR = os.getenv("TRACE_ANALYZER_OUT_DIR", "/tmp")
TRACE_POLL_TIMEOUT_S = float(os.getenv("TRACE_ANALYZER_POLL_TIMEOUT_S", "15"))
TRACE_POLL_INTERVAL_S = float(os.getenv("TRACE_ANALYZER_POLL_INTERVAL_S", "0.5"))
TRACE_MIN_OVERLAP_MS = float(os.getenv("TRACE_ANALYZER_MIN_OVERLAP_MS", "0.5"))
TRACE_SETTLE_S = float(os.getenv("TRACE_ANALYZER_TRACE_SETTLE_S", "2.0"))
TRACE_REQUEST_TIMEOUT_S = float(os.getenv("TRACE_ANALYZER_REQUEST_TIMEOUT_S", "10"))
CLEAR_REGEX = re.compile(r"from\s+(\d+)\s+to\s+0")
SIBLING_SET_REGEX = re.compile(r"\{([0-9,\s]+)\}")
FORCE_LOCAL_SCOPE = True

LOCK = threading.Lock()

CONSUME_PATH_FLAGS = {
    "recvfrom": 1 << 0,
    "recvmsg": 1 << 1,
    "recvmmsg": 1 << 2,
    "read": 1 << 3,
    "readv": 1 << 4,
    "pread64": 1 << 5,
    "io_uring": 1 << 6,
}


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _to_bool(val, default=False):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _split_csv(values):
    out = []
    for v in values:
        for item in str(v).split(","):
            item = item.strip()
            if item:
                out.append(item)
    return out


def _coz_status():
    rc, out, err = _run(["cozctl", "status"])
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    running = bool(lines) and lines[0] != "not running"
    pids = []
    if running:
        for line in lines:
            head = line.split(" ", 1)[0]
            if head.isdigit():
                pids.append(int(head))
    return {
        "ok": rc == 0,
        "running": running,
        "pids": pids,
        "raw": out if out else err,
    }


def _coz_stop():
    rc, out, err = _run(["cozctl", "stop"])
    return {"ok": rc == 0, "stdout": out, "stderr": err}


def _coz_start(
    target_pod,
    speedup,
    protect,
    protect_cpus,
    others_cpus,
    isolate_cores,
    victim_pids,
    fixed_delay_ns,
    period_ms,
    victim_finder,
    request_aware,
    request_credit,
    refund_on_fail,
    enable_read_hook,
):
    cmd = [
        "cozctl",
        "start",
        "--speedup",
        str(speedup),
    ]
    if target_pod:
        cmd.extend(["--target-pod", target_pod])
    for p in protect:
        cmd.extend(["--protect", p])
    if protect_cpus:
        cmd.extend(["--protect-cpus", protect_cpus])
    if others_cpus:
        cmd.extend(["--others-cpus", others_cpus])
    if isolate_cores:
        cmd.append("--isolate-cores")
    if victim_pids:
        cmd.extend(["--victim-pids", victim_pids])
    if fixed_delay_ns:
        cmd.extend(["--fixed-delay-ns", str(fixed_delay_ns)])
    if period_ms:
        cmd.extend(["--period-ms", str(period_ms)])
    if victim_finder:
        cmd.extend(["--victim-finder", victim_finder])
    if request_aware:
        cmd.append("--request-aware")
    if request_credit:
        cmd.append("--request-credit")
    if not refund_on_fail:
        cmd.append("--no-refund-on-fail")
    if enable_read_hook:
        cmd.append("--enable-read-hook")

    rc, out, err = _run(cmd)
    return {"ok": rc == 0, "stdout": out, "stderr": err, "cmd": cmd}


def _peer_ips():
    jsonpath = r'jsonpath={range .items[*]}{.status.podIP}{"\n"}{end}'
    rc, out, _ = _run(
        [
            "kubectl",
            "get",
            "pods",
            "-n",
            NAMESPACE,
            "-l",
            PEER_LABEL,
            "-o",
            jsonpath,
        ]
    )
    if rc != 0:
        return []
    ips = sorted({line.strip() for line in out.splitlines() if line.strip()})
    return ips


def _forward(path, params):
    peer_results = []
    for ip in _peer_ips():
        if POD_IP and ip == POD_IP:
            continue
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"http://{ip}:{CONTROL_PORT}{path}?{query}"
        req = urllib.request.Request(url=url, method="POST")
        req.add_header("X-MCOZ-Forwarded", "1")
        try:
            with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT_SEC) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                peer_results.append(
                    {"peer_ip": ip, "ok": True, "status_code": resp.status, "body": body}
                )
        except Exception as exc:
            peer_results.append(
                {"peer_ip": ip, "ok": False, "status_code": 0, "error": str(exc)}
            )
    return peer_results


def _resolve_gate_namespaces(raw_values=None):
    namespaces = _split_csv(raw_values or [])
    if not namespaces:
        namespaces = list(GATE_CONTROL_NAMESPACES)
    out = []
    seen = set()
    for ns in namespaces:
        value = str(ns or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _gate_target_pods(namespaces=None):
    if not GATE_CONTROL_ENABLED:
        return {"namespaces": _resolve_gate_namespaces(namespaces), "targets": []}

    resolved_namespaces = _resolve_gate_namespaces(namespaces)
    commands = []
    if not resolved_namespaces or "*" in resolved_namespaces:
        commands.append(["kubectl", "get", "pods", "-A", "-o", "json"])
    else:
        for ns in resolved_namespaces:
            commands.append(["kubectl", "get", "pods", "-n", ns, "-o", "json"])

    seen = set()
    targets = []
    for cmd in commands:
        rc, out, _ = _run(cmd)
        if rc != 0 or not out:
            continue
        try:
            obj = json.loads(out)
        except Exception:
            continue
        items = obj.get("items", [])
        if not isinstance(items, list):
            continue
        for pod in items:
            ns = pod.get("metadata", {}).get("namespace", "")
            name = pod.get("metadata", {}).get("name", "")
            deleting = pod.get("metadata", {}).get("deletionTimestamp")
            ip = pod.get("status", {}).get("podIP", "")
            phase = pod.get("status", {}).get("phase", "")
            if deleting:
                continue
            if not ns or not name or not ip or phase != "Running":
                continue
            conditions = pod.get("status", {}).get("conditions", [])
            ready = False
            if isinstance(conditions, list):
                for cond in conditions:
                    if (
                        isinstance(cond, dict)
                        and cond.get("type") == "Ready"
                        and str(cond.get("status", "")).lower() == "true"
                    ):
                        ready = True
                        break
            if not ready:
                continue
            containers = pod.get("spec", {}).get("containers", [])
            container_names = {
                str(c.get("name", "")).strip() for c in containers if isinstance(c, dict)
            }
            if GATE_CONTAINER_NAME not in container_names:
                continue
            key = (ns, name, ip)
            if key in seen:
                continue
            seen.add(key)
            targets.append({"namespace": ns, "pod": name, "ip": ip})
    targets.sort(key=lambda x: (x["namespace"], x["pod"]))
    return {"namespaces": resolved_namespaces, "targets": targets}


def _gate_inventory(namespaces=None, target_mode="any"):
    target_mode = str(target_mode or "any").strip().lower()
    pods = _gate_target_pods(namespaces)
    candidates = pods.get("targets", [])
    results = []
    usable = []
    all_ok = True

    for target in candidates:
        url = f"http://{target['ip']}:{GATE_CONTROL_PORT}/healthz"
        item = {
            "namespace": target["namespace"],
            "pod": target["pod"],
            "ip": target["ip"],
            "url": url,
        }
        try:
            with urllib.request.urlopen(url, timeout=GATE_CONTROL_TIMEOUT_SEC) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                item["status_code"] = resp.status
                try:
                    body_json = json.loads(body)
                except Exception:
                    body_json = {"raw": body}
                item["body_json"] = body_json
                ok = 200 <= resp.status < 300 and isinstance(body_json, dict) and body_json.get("ok", True)
                item["ok"] = ok
                if ok:
                    gate_target_mode = str(body_json.get("target_mode", "") or "").strip().lower()
                    item["container"] = str(body_json.get("container", "") or "").strip()
                    item["source"] = str(body_json.get("source", "") or "").strip()
                    item["target_mode"] = gate_target_mode or "unknown"
                    item["target_namespace"] = str(
                        body_json.get("target_namespace", target["namespace"]) or target["namespace"]
                    ).strip()
                    item["target_pod"] = str(
                        body_json.get("target_pod", target["pod"]) or target["pod"]
                    ).strip()
                    item["target_container"] = str(
                        body_json.get("target_container", item["container"]) or item["container"]
                    ).strip()
                    if target_mode not in ("", "any") and item["target_mode"] != target_mode:
                        item["selected"] = False
                        item["skip_reason"] = f"target_mode={item['target_mode']}"
                    elif not item["target_pod"] or not item["target_container"]:
                        item["selected"] = False
                        item["skip_reason"] = "missing-target"
                        all_ok = False
                    else:
                        item["selected"] = True
                        usable.append(item)
                else:
                    all_ok = False
        except Exception as exc:
            item["ok"] = False
            item["status_code"] = 0
            item["error"] = str(exc)
            all_ok = False
        results.append(item)

    return {
        "attempted": True,
        "ok": all_ok and bool(usable),
        "namespaces": pods.get("namespaces", []),
        "target_mode": target_mode,
        "candidate_count": len(candidates),
        "count": len(usable),
        "results": results,
        "targets": usable,
    }


def _gate_set_enabled_on_targets(targets, enabled, delay_ns=None, count=None):
    enabled = bool(enabled)
    delay_ns_i = _to_int(delay_ns, None)
    count_i = _to_int(count, None)
    # Allow delay_ns=0 as an explicit runtime override to disable per-request delay.
    if delay_ns_i is not None and delay_ns_i < 0:
        delay_ns_i = None
    if count_i is not None and count_i <= 0:
        count_i = None
    if not GATE_CONTROL_ENABLED:
        return {
            "enabled": enabled,
            "delay_ns": delay_ns_i,
            "count": count_i,
            "attempted": False,
            "ok": True,
            "reason": "disabled-by-env",
            "results": [],
        }

    if not targets:
        return {
            "enabled": enabled,
            "delay_ns": delay_ns_i,
            "count": count_i,
            "attempted": False,
            "ok": True,
            "reason": "no-gate-pods",
            "results": [],
        }

    path = GATE_CONTROL_PATH if GATE_CONTROL_PATH.startswith("/") else f"/{GATE_CONTROL_PATH}"
    results = []
    all_ok = True
    query = {"enabled": "1" if enabled else "0"}
    if delay_ns_i is not None:
        query["delay_ns"] = str(delay_ns_i)
    if count_i is not None:
        query["count"] = str(count_i)
    query_str = urllib.parse.urlencode(query)
    for t in targets:
        url = f"http://{t['ip']}:{GATE_CONTROL_PORT}{path}?{query_str}"
        req = urllib.request.Request(url=url, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-MCOZ-Gate-Alarm", "1")
        item = {
            "namespace": t["namespace"],
            "pod": t["pod"],
            "ip": t["ip"],
            "url": url,
        }
        try:
            with urllib.request.urlopen(req, timeout=GATE_CONTROL_TIMEOUT_SEC) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                item["status_code"] = resp.status
                item["ok"] = 200 <= resp.status < 300
                try:
                    item["body_json"] = json.loads(body)
                except Exception:
                    item["body"] = body[:300]
        except Exception as exc:
            item["ok"] = False
            item["status_code"] = 0
            item["error"] = str(exc)
        if not item["ok"]:
            all_ok = False
        results.append(item)

    return {
        "enabled": enabled,
        "delay_ns": delay_ns_i,
        "count": count_i,
        "attempted": True,
        "ok": all_ok,
        "results": results,
    }


def _gate_set_enabled(enabled, delay_ns=None, count=None, namespaces=None, target_mode="any"):
    if not GATE_CONTROL_ENABLED:
        return {
            "enabled": bool(enabled),
            "delay_ns": _to_int(delay_ns, None),
            "count": _to_int(count, None),
            "attempted": False,
            "ok": True,
            "reason": "disabled-by-env",
            "results": [],
            "discovery": {
                "attempted": False,
                "ok": True,
                "namespaces": _resolve_gate_namespaces(namespaces),
                "target_mode": target_mode,
                "count": 0,
                "candidate_count": 0,
                "results": [],
            },
        }

    discovery = _gate_inventory(namespaces=namespaces, target_mode=target_mode)
    if not discovery.get("targets"):
        result = {
            "enabled": bool(enabled),
            "delay_ns": _to_int(delay_ns, None),
            "count": _to_int(count, None),
            "attempted": False,
            "ok": False,
            "reason": "no-gate-pods",
            "results": [],
            "discovery": {k: v for k, v in discovery.items() if k != "targets"},
        }
        if discovery.get("candidate_count", 0) > 0:
            result["reason"] = "no-usable-gate-pods"
        return result

    gate_result = _gate_set_enabled_on_targets(
        discovery.get("targets", []),
        enabled=enabled,
        delay_ns=delay_ns,
        count=count,
    )
    gate_result["discovery"] = {k: v for k, v in discovery.items() if k != "targets"}
    return gate_result


def _auto_profile_gate_targets(targets, duration_ms, top_k, apply_policy):
    if not targets:
        return {
            "attempted": False,
            "ok": True,
            "reason": "no-targets",
            "count": 0,
            "results": [],
        }

    grouped = {}
    for item in targets:
        key = (
            str(item.get("target_namespace", "")).strip(),
            str(item.get("target_pod", "")).strip(),
            str(item.get("target_container", "")).strip(),
        )
        if not key[0] or not key[1] or not key[2]:
            continue
        grouped.setdefault(key, []).append(
            {
                "gate_namespace": item.get("namespace", ""),
                "gate_pod": item.get("pod", ""),
                "gate_ip": item.get("ip", ""),
                "source": item.get("source", ""),
                "target_mode": item.get("target_mode", ""),
            }
        )

    results = []
    all_ok = True
    for (namespace, pod, container), gates in sorted(grouped.items()):
        profile = _syscall_profile_distributed(
            namespace,
            pod,
            container,
            duration_ms=duration_ms,
            top_k=top_k,
            apply_policy=apply_policy,
        )
        item = {
            "namespace": namespace,
            "pod": pod,
            "container": container,
            "gate_refs": gates,
            "ok": bool(profile.get("ok")),
            "profile": profile,
        }
        if not item["ok"]:
            all_ok = False
        results.append(item)

    return {
        "attempted": True,
        "ok": all_ok and bool(results),
        "count": len(results),
        "duration_ms": int(duration_ms),
        "top_k": int(top_k),
        "apply_policy": bool(apply_policy),
        "results": results,
    }


def _parse_cleared_ns(body):
    match = CLEAR_REGEX.search(body or "")
    if match:
        return int(match.group(1))
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            if "previous_ns" in obj:
                return int(obj["previous_ns"])
            if "local_previous_ns" in obj:
                return int(obj["local_previous_ns"])
            if "total_previous_ns" in obj:
                return int(obj["total_previous_ns"])
    except Exception:
        pass
    raise ValueError(f"cannot parse cleared ns from body: {body!r}")


def _local_clear(clear_credits=False):
    status = _coz_status()
    if not status.get("running"):
        return {
            "ok": True,
            "status_code": 200,
            "previous_ns": 0,
            "body": "coz not running; nothing to clear\n",
            "clear_credits": clear_credits,
            "skipped": True,
        }
    url = CLEAR_LOCAL_URL
    if clear_credits:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}clear_credits=true"
    req = urllib.request.Request(url=url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            previous_ns = _parse_cleared_ns(body)
            return {
                "ok": True,
                "status_code": resp.status,
                "previous_ns": previous_ns,
                "body": body,
                "clear_credits": clear_credits,
            }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "previous_ns": 0,
            "error": str(exc),
            "clear_credits": clear_credits,
        }


def _local_rearm():
    req = urllib.request.Request(url=REARM_LOCAL_URL, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {
                "ok": True,
                "status_code": resp.status,
                "body": body,
            }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "error": str(exc),
        }


def _local_status():
    req = urllib.request.Request(url=STATUS_LOCAL_URL, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            payload = None
            try:
                payload = json.loads(body)
            except Exception:
                payload = {"raw": body}
            return {
                "ok": True,
                "status_code": resp.status,
                "payload": payload,
                "body": body,
            }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "error": str(exc),
        }


def _local_arm(namespace, pod, container, delay_ns, count, source=""):
    payload = json.dumps(
        {
            "namespace": namespace,
            "pod": pod,
            "container": container,
            "delay_ns": int(delay_ns),
            "count": int(count),
            "source": str(source or ""),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    req = urllib.request.Request(url=ARM_LOCAL_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            payload = None
            try:
                payload = json.loads(body)
            except Exception:
                payload = {"raw": body}
            return {
                "ok": 200 <= resp.status < 300 and isinstance(payload, dict) and payload.get("ok", True),
                "status_code": resp.status,
                "payload": payload,
                "body": body,
            }
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace") if err.fp else ""
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"raw": body}
        return {
            "ok": False,
            "status_code": err.code,
            "payload": payload,
            "body": body,
            "error": str(err),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "error": str(exc),
        }


def _local_syscall_profile(namespace, pod, container, duration_ms, top_k, apply_policy):
    payload = json.dumps(
        {
            "namespace": namespace,
            "pod": pod,
            "container": container,
            "duration_ms": int(duration_ms),
            "top_k": int(top_k),
            "apply_policy": bool(apply_policy),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    req = urllib.request.Request(url=SYSCALL_PROFILE_LOCAL_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=max(FORWARD_TIMEOUT_SEC, float(duration_ms) / 1000.0 + 2.0)) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            payload = None
            try:
                payload = json.loads(body)
            except Exception:
                payload = {"raw": body}
            return {
                "ok": 200 <= resp.status < 300 and isinstance(payload, dict) and payload.get("ok", True),
                "status_code": resp.status,
                "payload": payload,
                "body": body,
            }
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace") if err.fp else ""
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"raw": body}
        return {
            "ok": False,
            "status_code": err.code,
            "payload": payload,
            "body": body,
            "error": str(err),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "error": str(exc),
        }


def _consume_paths_to_flags(consume_paths):
    flags = 0
    names = []
    for raw in _split_csv(consume_paths):
        name = str(raw).strip().lower()
        if not name or name == "none":
            continue
        if name not in CONSUME_PATH_FLAGS:
            raise ValueError(f"unknown consume path: {name}")
        flags |= CONSUME_PATH_FLAGS[name]
        names.append(name)
    return flags, names


def _local_consume_policy(namespace, pod, container, raw_flags):
    payload = json.dumps(
        {
            "namespace": namespace,
            "pod": pod,
            "container": container,
            "raw_flags": int(raw_flags),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    req = urllib.request.Request(url=CONSUME_POLICY_LOCAL_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            payload = None
            try:
                payload = json.loads(body)
            except Exception:
                payload = {"raw": body}
            return {
                "ok": 200 <= resp.status < 300 and isinstance(payload, dict) and payload.get("ok", True),
                "status_code": resp.status,
                "payload": payload,
                "body": body,
            }
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace") if err.fp else ""
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"raw": body}
        return {
            "ok": False,
            "status_code": err.code,
            "payload": payload,
            "body": body,
            "error": str(err),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "error": str(exc),
        }


def _syscall_profile_distributed(namespace, pod, container, duration_ms, top_k, apply_policy):
    attempts = []

    local = _local_syscall_profile(namespace, pod, container, duration_ms, top_k, apply_policy)
    attempts.append({"target": "local", "pod": pod, "namespace": namespace, "result": local})
    if local.get("ok"):
        return {"ok": True, "target": "local", "attempts": attempts, "result": local}

    payload = {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "duration_ms": int(duration_ms),
        "top_k": int(top_k),
        "apply_policy": bool(apply_policy),
    }
    timeout_sec = max(FORWARD_TIMEOUT_SEC, float(duration_ms) / 1000.0 + 2.0)
    for ip in _peer_ips():
        if POD_IP and ip == POD_IP:
            continue
        url = f"http://{ip}:{CONTROL_PORT}/syscall_profile"
        peer = _post_json(url, payload, timeout_sec)
        attempts.append({"target": ip, "pod": pod, "namespace": namespace, "result": peer})
        if peer.get("ok"):
            return {"ok": True, "target": ip, "attempts": attempts, "result": peer}

    return {
        "ok": False,
        "target": "",
        "attempts": attempts,
        "result": attempts[-1]["result"] if attempts else {"ok": False, "error": "no attempts"},
    }


def _consume_policy_distributed(namespace, pod, container, raw_flags):
    attempts = []

    local = _local_consume_policy(namespace, pod, container, raw_flags)
    attempts.append({"target": "local", "pod": pod, "namespace": namespace, "result": local})
    if local.get("ok"):
        return {"ok": True, "target": "local", "attempts": attempts, "result": local}

    payload = {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "raw_flags": int(raw_flags),
    }
    for ip in _peer_ips():
        if POD_IP and ip == POD_IP:
            continue
        url = f"http://{ip}:{CONTROL_PORT}/consume_policy"
        peer = _post_json(url, payload, FORWARD_TIMEOUT_SEC)
        attempts.append({"target": ip, "pod": pod, "namespace": namespace, "result": peer})
        if peer.get("ok"):
            return {"ok": True, "target": ip, "attempts": attempts, "result": peer}

    return {
        "ok": False,
        "target": "",
        "attempts": attempts,
        "result": attempts[-1]["result"] if attempts else {"ok": False, "error": "no attempts"},
    }


def _to_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _to_float(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _post_json(url, payload, timeout_sec):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url=url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-MCOZ-Forwarded", "1")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                body_json = json.loads(body)
            except Exception:
                body_json = {"raw": body}
            ok = 200 <= resp.status < 300
            if isinstance(body_json, dict) and "ok" in body_json:
                ok = ok and bool(body_json.get("ok"))
            return {
                "ok": ok,
                "status_code": resp.status,
                "payload": body_json,
                "body": body,
            }
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace") if err.fp else ""
        try:
            body_json = json.loads(body)
        except Exception:
            body_json = {"raw": body}
        return {
            "ok": False,
            "status_code": err.code,
            "payload": body_json,
            "body": body,
            "error": str(err),
        }
    except Exception as exc:
        return {"ok": False, "status_code": 0, "error": str(exc)}


def _arm_distributed(namespace, pod, container, delay_ns, count, source=""):
    attempts = []

    local = _local_arm(namespace, pod, container, delay_ns, count, source)
    attempts.append({"target": "local", "pod": pod, "namespace": namespace, "result": local})
    if local.get("ok"):
        return {"ok": True, "target": "local", "attempts": attempts, "result": local}

    payload = {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "delay_ns": int(delay_ns),
        "count": int(count),
        "source": str(source or ""),
    }
    for ip in _peer_ips():
        if POD_IP and ip == POD_IP:
            continue
        url = f"http://{ip}:{CONTROL_PORT}/arm"
        peer = _post_json(url, payload, FORWARD_TIMEOUT_SEC)
        attempts.append({"target": ip, "pod": pod, "namespace": namespace, "result": peer})
        if peer.get("ok"):
            return {"ok": True, "target": ip, "attempts": attempts, "result": peer}

    return {
        "ok": False,
        "target": "",
        "attempts": attempts,
        "result": attempts[-1]["result"] if attempts else {"ok": False, "error": "no attempts"},
    }


def _parse_sibling_set(raw):
    if not isinstance(raw, str):
        return []
    match = SIBLING_SET_REGEX.fullmatch(raw.strip())
    if not match:
        return []
    values = []
    for part in match.group(1).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(int(part))
        except Exception:
            return []
    return sorted(set(values))


def _service_short_name(service_name):
    if not service_name:
        return ""
    return str(service_name).split(".", 1)[0].strip()


def _service_namespace(service_name):
    parts = str(service_name).split(".")
    if len(parts) < 2:
        return ""
    candidate = parts[1].strip()
    if not candidate or candidate in ("svc", "cluster", "local"):
        return ""
    return candidate


def _pod_hint_from_target(target_pod):
    if not target_pod:
        return "", ""
    value = str(target_pod).strip()
    if "/" in value:
        ns, pod = value.split("/", 1)
        return ns.strip(), pod.strip()
    return "", value


def _service_id_from_target_pod(target_pod, service_index):
    _, pod = _pod_hint_from_target(target_pod)
    if not pod:
        return None
    hint = pod.split("-", 1)[0]
    if not hint:
        return None
    for sid, service_name in service_index.items():
        if _service_short_name(service_name) == hint:
            return sid
    return None


def _parse_service_index(analysis):
    out = {}
    service_index = analysis.get("service_index")
    if not isinstance(service_index, dict):
        return out
    for key, value in service_index.items():
        if not isinstance(value, str):
            continue
        sid = _to_int(key)
        if sid and sid > 0:
            out[sid] = value
    return out


def _extract_sibling_ids(analysis, target_service_id):
    services = analysis.get("services")
    if isinstance(services, list):
        for item in services:
            if not isinstance(item, dict):
                continue
            sid = _to_int(item.get("service_id"))
            if sid != target_service_id:
                continue
            raw_ids = item.get("sibling_ids")
            if not isinstance(raw_ids, list):
                break
            out = []
            for v in raw_ids:
                iv = _to_int(v)
                if iv and iv > 0 and iv != target_service_id:
                    out.append(iv)
            return sorted(set(out))

    sibling_sets = analysis.get("sibling_sets")
    if isinstance(sibling_sets, list):
        for raw in sibling_sets:
            values = _parse_sibling_set(raw)
            if target_service_id in values:
                return [x for x in values if x != target_service_id]
    return []


def _run_trace_analyzer(
    entry_url,
    request_path,
    jaeger_url,
    poll_timeout_s,
    poll_interval_s,
    min_overlap_ms,
    trace_settle_s,
    request_timeout_s,
):
    if not os.path.isfile(TRACE_ANALYZER_SCRIPT):
        return {
            "ok": False,
            "error": f"trace analyzer script not found: {TRACE_ANALYZER_SCRIPT}",
            "cmd": [],
        }

    os.makedirs(TRACE_ANALYZER_OUT_DIR, exist_ok=True)
    json_out = os.path.join(
        TRACE_ANALYZER_OUT_DIR,
        f"mcoz-trace-analysis-{int(time.time() * 1000)}-{os.getpid()}.json",
    )

    cmd = [
        TRACE_ANALYZER_PYTHON,
        TRACE_ANALYZER_SCRIPT,
        "--entry-url",
        entry_url,
        "--jaeger-url",
        jaeger_url,
        "--poll-timeout-s",
        str(poll_timeout_s),
        "--poll-interval-s",
        str(poll_interval_s),
        "--min-overlap-ms",
        str(min_overlap_ms),
        "--trace-settle-s",
        str(trace_settle_s),
        "--request-timeout-s",
        str(request_timeout_s),
        "--json-out",
        json_out,
    ]
    if request_path:
        cmd.extend(["--request-path", request_path])

    timeout_sec = max(TRACE_ANALYZER_TIMEOUT_SEC, poll_timeout_s + trace_settle_s + 10)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": f"trace analyzer timeout ({timeout_sec}s): {exc}",
            "cmd": cmd,
            "stdout": (exc.stdout or "").strip(),
            "stderr": (exc.stderr or "").strip(),
            "json_out": json_out,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "cmd": cmd, "json_out": json_out}

    result = {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "cmd": cmd,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "json_out": json_out,
    }
    if proc.returncode != 0:
        return result

    try:
        with open(json_out, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"failed to load analyzer JSON: {exc}"
        return result

    result["analysis_json"] = payload
    return result


def _resolve_pods_for_service(service_name, default_namespace):
    short = _service_short_name(service_name)
    if not short:
        return []

    ns_candidates = []
    service_ns = _service_namespace(service_name)
    if service_ns:
        ns_candidates.append(service_ns)
    if default_namespace and default_namespace not in ns_candidates:
        ns_candidates.append(default_namespace)
    if not ns_candidates:
        ns_candidates.append("default")

    pod_jsonpath = r"jsonpath={range .items[*]}{.metadata.name}{'\n'}{end}"
    for ns in ns_candidates:
        for key in ("app", "app.kubernetes.io/name", "service"):
            rc, out, _ = _run(
                ["kubectl", "get", "pods", "-n", ns, "-l", f"{key}={short}", "-o", pod_jsonpath]
            )
            if rc != 0:
                continue
            names = sorted({line.strip() for line in out.splitlines() if line.strip()})
            if names:
                return [{"namespace": ns, "pod": name} for name in names]

        rc, out, _ = _run(["kubectl", "get", "pods", "-n", ns, "-o", pod_jsonpath])
        if rc != 0:
            continue
        names = sorted(
            {
                line.strip()
                for line in out.splitlines()
                if line.strip() and line.strip().startswith(f"{short}-")
            }
        )
        if names:
            return [{"namespace": ns, "pod": name} for name in names]

    return []


def _build_trace_plan(analysis_json, target_service_id, target_pod):
    if not isinstance(analysis_json, dict):
        return {"ok": False, "error": "invalid analyzer JSON payload"}

    analysis = analysis_json.get("analysis")
    if not isinstance(analysis, dict):
        return {"ok": False, "error": "missing analysis object in analyzer JSON"}

    service_index = _parse_service_index(analysis)
    if not service_index:
        return {"ok": False, "error": "empty analysis.service_index"}

    target_sid = _to_int(target_service_id)
    if not target_sid:
        target_sid = _service_id_from_target_pod(target_pod, service_index)
    if not target_sid:
        return {
            "ok": False,
            "error": "targetServiceId is required (or inferable via targetPod) when tracePrephase=true",
            "service_index": {str(k): v for k, v in sorted(service_index.items())},
        }
    if target_sid not in service_index:
        return {
            "ok": False,
            "error": f"targetServiceId={target_sid} not found in analysis.service_index",
            "service_index": {str(k): v for k, v in sorted(service_index.items())},
        }

    sibling_ids = _extract_sibling_ids(analysis, target_sid)
    sibling_services = [
        {"service_id": sid, "service": service_index[sid]}
        for sid in sibling_ids
        if sid in service_index
    ]

    target_namespace, _ = _pod_hint_from_target(target_pod)
    arm_targets = []
    seen = set()
    unresolved = []
    for item in sibling_services:
        resolved = _resolve_pods_for_service(item["service"], target_namespace)
        if not resolved:
            unresolved.append(item)
            continue
        for pod_meta in resolved:
            key = (pod_meta["namespace"], pod_meta["pod"])
            if key in seen:
                continue
            seen.add(key)
            arm_targets.append(
                {
                    "namespace": pod_meta["namespace"],
                    "pod": pod_meta["pod"],
                    "service_id": item["service_id"],
                    "service": item["service"],
                }
            )

    return {
        "ok": True,
        "target_service_id": target_sid,
        "target_service": service_index[target_sid],
        "service_index": {str(k): v for k, v in sorted(service_index.items())},
        "sibling_service_ids": sibling_ids,
        "sibling_services": sibling_services,
        "arm_targets": arm_targets,
        "unresolved_siblings": unresolved,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[mcoz-control] {self.client_address[0]} - {fmt % args}", flush=True)

    def _send_json(self, status, payload):
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _params(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0") or "0")

        if self.command == "POST" and length > 0:
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            if "application/json" in ctype:
                try:
                    obj = json.loads(body)
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if isinstance(v, list):
                                params.setdefault(k, []).extend([str(x) for x in v])
                            else:
                                params.setdefault(k, []).append(str(v))
                except Exception:
                    pass
            else:
                form = urllib.parse.parse_qs(body, keep_blank_values=True)
                for k, v in form.items():
                    params.setdefault(k, []).extend(v)
        return parsed.path, params

    @staticmethod
    def _one(params, *keys, default=None):
        for key in keys:
            vals = params.get(key)
            if vals:
                return vals[-1]
        return default

    @staticmethod
    def _scope_local_only(raw_scope):
        scope = str(raw_scope or "local").strip().lower()
        if FORCE_LOCAL_SCOPE:
            return "local"
        return scope if scope in ("local", "all") else "local"

    def _status_payload(self):
        st = _coz_status()
        daemon = _local_status() if st.get("running") else {"ok": False, "error": "not running"}
        return {
            "pod": {"name": POD_NAME, "ip": POD_IP, "namespace": NAMESPACE},
            "coz": st,
            "daemon": daemon,
            "time_unix": int(time.time()),
        }

    def _handle_status(self):
        self._send_json(200, self._status_payload())

    def _handle_start(self, params):
        target_pod = self._one(params, "targetPod", "target_pod")
        request_credit = _to_bool(
            self._one(params, "requestCredit", "request_credit", default="false")
        )
        if not target_pod and not request_credit:
            self._send_json(400, {"ok": False, "error": "missing targetPod"})
            return

        speedup = self._one(params, "speedup", default=DEFAULT_SPEEDUP)
        scope = self._scope_local_only(self._one(params, "scope", default="local"))
        force = _to_bool(self._one(params, "force", default="false"))

        protect = _split_csv(params.get("protect", []))
        protect_cpus = self._one(params, "protectCpus", "protect_cpus", default="")
        others_cpus = self._one(params, "othersCpus", "others_cpus", default="")
        isolate_cores = _to_bool(
            self._one(params, "isolateCores", "isolate_cores", default="false")
        )
        victim_pids = self._one(params, "victimPids", "victim_pids", default="")
        fixed_delay_ns = self._one(params, "fixedDelayNs", "fixed_delay_ns", default="")
        gate_count = _to_int(self._one(params, "gateCount", "gate_count", default="1"), 1)
        if gate_count is None or gate_count <= 0:
            gate_count = 1
        period_ms = self._one(params, "periodMs", "period_ms", default="")
        victim_finder = self._one(params, "victimFinder", "victim_finder", default="")
        reset_on_start = _to_bool(
            self._one(params, "resetOnStart", "reset_on_start", default="true")
        )
        request_aware = _to_bool(
            self._one(params, "requestAware", "request_aware", default="false")
        ) or _to_bool(self._one(params, "exactMode", "exact_mode", default="false"))
        refund_on_fail = not _to_bool(
            self._one(params, "noRefundOnFail", "no_refund_on_fail", default="true")
        )
        enable_read_hook = _to_bool(
            self._one(params, "enableReadHook", "enable_read_hook", default="false")
        )
        auto_discover_gates = request_credit and _to_bool(
            self._one(params, "autoDiscoverGates", "auto_discover_gates", default="true"),
            default=True,
        )
        gate_namespaces = _split_csv(
            params.get("gateNamespaces", []) + params.get("gate_namespaces", [])
        )
        gate_target_mode = str(
            self._one(
                params,
                "gateTargetMode",
                "gate_target_mode",
                default="self" if auto_discover_gates else "any",
            )
            or "any"
        ).strip().lower()
        auto_profile_gates = auto_discover_gates and _to_bool(
            self._one(params, "autoProfileGates", "auto_profile_gates", default="true"),
            default=True,
        )
        gate_profile_duration_ms = _to_int(
            self._one(
                params,
                "gateProfileDurationMs",
                "gate_profile_duration_ms",
                default="2000",
            ),
            2000,
        )
        gate_profile_top_k = _to_int(
            self._one(params, "gateProfileTopK", "gate_profile_top_k", default="12"),
            12,
        )
        gate_profile_apply_policy = _to_bool(
            self._one(
                params,
                "gateProfileApplyPolicy",
                "gate_profile_apply_policy",
                default="true",
            ),
            default=True,
        )
        if gate_profile_duration_ms is None or gate_profile_duration_ms <= 0:
            gate_profile_duration_ms = 2000
        if gate_profile_top_k is None or gate_profile_top_k <= 0:
            gate_profile_top_k = 12

        if request_credit and not fixed_delay_ns:
            fixed_delay_ns = DEFAULT_FIXED_DELAY_NS

        forwarded = self.headers.get("X-MCOZ-Forwarded", "") == "1"
        peer_results = []
        if scope == "all" and not forwarded:
            peer_params = {
                "speedup": speedup,
                "scope": "local",
                "force": "true" if force else "false",
            }
            if target_pod:
                peer_params["targetPod"] = target_pod
            if protect:
                peer_params["protect"] = protect
            if protect_cpus:
                peer_params["protectCpus"] = protect_cpus
            if others_cpus:
                peer_params["othersCpus"] = others_cpus
            if isolate_cores:
                peer_params["isolateCores"] = "true"
            if victim_pids:
                peer_params["victimPids"] = victim_pids
            if fixed_delay_ns:
                peer_params["fixedDelayNs"] = fixed_delay_ns
            peer_params["gateCount"] = str(gate_count)
            if period_ms:
                peer_params["periodMs"] = period_ms
            if victim_finder:
                peer_params["victimFinder"] = victim_finder
            if request_aware:
                peer_params["requestAware"] = "true"
            if request_credit:
                peer_params["requestCredit"] = "true"
            if auto_discover_gates:
                peer_params["autoDiscoverGates"] = "true"
                if gate_namespaces:
                    peer_params["gateNamespaces"] = gate_namespaces
                if gate_target_mode:
                    peer_params["gateTargetMode"] = gate_target_mode
                if auto_profile_gates:
                    peer_params["autoProfileGates"] = "true"
                    peer_params["gateProfileDurationMs"] = str(gate_profile_duration_ms)
                    peer_params["gateProfileTopK"] = str(gate_profile_top_k)
                    peer_params["gateProfileApplyPolicy"] = (
                        "true" if gate_profile_apply_policy else "false"
                    )
            if not refund_on_fail:
                peer_params["noRefundOnFail"] = "true"
            if enable_read_hook:
                peer_params["enableReadHook"] = "true"
            peer_results = _forward("/start", peer_params)

        with LOCK:
            before = _coz_status()
            stop_result = None
            if before["running"] and force:
                stop_result = _coz_stop()

            if before["running"] and not force:
                start_result = {
                    "ok": True,
                    "stdout": "already running; skipped (set force=true to restart)",
                    "stderr": "",
                    "cmd": [],
                }
            else:
                start_result = _coz_start(
                    target_pod,
                    speedup,
                    protect,
                    protect_cpus,
                    others_cpus,
                    isolate_cores,
                    victim_pids,
                    fixed_delay_ns,
                    period_ms,
                    victim_finder,
                    request_aware,
                    request_credit,
                    refund_on_fail,
                    enable_read_hook,
                )
            after = _coz_status()
            reset = {"clear": None, "rearm": None}
            if reset_on_start and after.get("running"):
                # Reset stale counters only. Do NOT call /rearm here, because
                # /rearm may trigger immediate injection in request-aware mode.
                reset["clear"] = _local_clear(clear_credits=request_credit)

        gate_discovery = {
            "attempted": False,
            "ok": True,
            "reason": "not-request-credit" if not request_credit else "not-attempted",
            "namespaces": _resolve_gate_namespaces(gate_namespaces),
            "target_mode": gate_target_mode,
            "count": 0,
            "candidate_count": 0,
            "results": [],
        }
        gate_syscall_profile = {
            "attempted": False,
            "ok": True,
            "reason": "auto-profile-disabled" if not auto_profile_gates else "not-attempted",
            "count": 0,
            "results": [],
        }
        gate_alarm = {
            "enabled": True,
            "attempted": False,
            "ok": True,
            "reason": "skipped-forwarded" if forwarded else "start-not-running",
            "results": [],
        }
        if not forwarded and after.get("running"):
            if auto_discover_gates:
                discovered = _gate_inventory(
                    namespaces=gate_namespaces,
                    target_mode=gate_target_mode,
                )
                gate_discovery = {k: v for k, v in discovered.items() if k != "targets"}
                discovered_targets = discovered.get("targets", [])
                if auto_profile_gates and discovered_targets:
                    gate_syscall_profile = _auto_profile_gate_targets(
                        discovered_targets,
                        duration_ms=gate_profile_duration_ms,
                        top_k=gate_profile_top_k,
                        apply_policy=gate_profile_apply_policy,
                    )
                elif auto_profile_gates and not discovered_targets:
                    gate_syscall_profile = {
                        "attempted": False,
                        "ok": False,
                        "reason": "no-discovered-targets",
                        "count": 0,
                        "results": [],
                    }

                if discovered_targets:
                    gate_alarm = _gate_set_enabled_on_targets(
                        discovered_targets,
                        True,
                        delay_ns=fixed_delay_ns,
                        count=gate_count,
                    )
                else:
                    gate_alarm = {
                        "enabled": True,
                        "attempted": False,
                        "ok": False,
                        "reason": "no-discovered-targets",
                        "results": [],
                    }
            else:
                gate_alarm = _gate_set_enabled(
                    True,
                    delay_ns=fixed_delay_ns,
                    count=gate_count,
                    namespaces=gate_namespaces,
                    target_mode=gate_target_mode,
                )
                gate_discovery = gate_alarm.get("discovery", gate_discovery)

        overall_ok = bool(start_result.get("ok"))
        if not forwarded and after.get("running") and request_credit:
            if auto_discover_gates:
                overall_ok = overall_ok and bool(gate_discovery.get("ok"))
                if auto_profile_gates:
                    overall_ok = overall_ok and bool(gate_syscall_profile.get("ok"))
                overall_ok = overall_ok and bool(gate_alarm.get("ok"))
            else:
                overall_ok = overall_ok and bool(gate_alarm.get("ok"))

        self._send_json(
            200 if overall_ok else 500,
            {
                "ok": overall_ok,
                "scope": scope,
                "forwarded": forwarded,
                "pod": {"name": POD_NAME, "ip": POD_IP},
                "before": before,
                "stop_result": stop_result,
                "start_result": start_result,
                "after": after,
                "reset": reset,
                "peers": peer_results,
                "auto_discover_gates": auto_discover_gates,
                "auto_profile_gates": auto_profile_gates,
                "gate_discovery": gate_discovery,
                "gate_syscall_profile": gate_syscall_profile,
                "gate_alarm": gate_alarm,
            },
        )

    def _handle_stop(self, params):
        scope = self._scope_local_only(self._one(params, "scope", default="local"))
        gate_namespaces = _split_csv(
            params.get("gateNamespaces", []) + params.get("gate_namespaces", [])
        )
        gate_target_mode = str(
            self._one(params, "gateTargetMode", "gate_target_mode", default="any") or "any"
        ).strip().lower()
        forwarded = self.headers.get("X-MCOZ-Forwarded", "") == "1"
        peer_results = []
        if scope == "all" and not forwarded:
            peer_params = {"scope": "local"}
            if gate_namespaces:
                peer_params["gateNamespaces"] = gate_namespaces
            if gate_target_mode:
                peer_params["gateTargetMode"] = gate_target_mode
            peer_results = _forward("/stop", peer_params)

        with LOCK:
            before = _coz_status()
            stop_result = _coz_stop()
            after = _coz_status()

        gate_alarm = {
            "enabled": False,
            "attempted": False,
            "ok": True,
            "reason": "skipped-forwarded" if forwarded else "not-attempted",
            "results": [],
        }
        if not forwarded:
            gate_alarm = _gate_set_enabled(
                False,
                namespaces=gate_namespaces,
                target_mode=gate_target_mode,
            )

        self._send_json(
            200 if stop_result["ok"] else 500,
            {
                "ok": stop_result["ok"],
                "scope": scope,
                "forwarded": forwarded,
                "pod": {"name": POD_NAME, "ip": POD_IP},
                "before": before,
                "stop_result": stop_result,
                "after": after,
                "peers": peer_results,
                "gate_alarm": gate_alarm,
            },
        )

    def _handle_clear(self, params):
        scope = self._scope_local_only(self._one(params, "scope", default="local"))
        clear_credits = _to_bool(
            self._one(params, "clearCredits", "clear_credits", default="false")
        )
        forwarded = self.headers.get("X-MCOZ-Forwarded", "") == "1"
        peer_results = []
        if scope == "all" and not forwarded:
            peer_params = {"scope": "local"}
            if clear_credits:
                peer_params["clear_credits"] = "true"
            peer_results = _forward("/clear", peer_params)

        with LOCK:
            local = _local_clear(clear_credits=clear_credits)

        peer_sum = 0
        peers_ok = True
        for peer in peer_results:
            if not peer.get("ok"):
                peers_ok = False
                continue
            body = peer.get("body", "")
            try:
                peer_sum += _parse_cleared_ns(body)
            except Exception:
                peers_ok = False

        local_prev = int(local.get("previous_ns", 0) or 0)
        total_prev = local_prev + peer_sum
        ok = bool(local.get("ok")) and peers_ok

        self._send_json(
            200 if ok else 500,
            {
                "ok": ok,
                "scope": scope,
                "forwarded": forwarded,
                "pod": {"name": POD_NAME, "ip": POD_IP},
                "local": local,
                "local_previous_ns": local_prev,
                "total_previous_ns": total_prev,
                "clear_credits": clear_credits,
                "peers": peer_results,
            },
        )

    def _handle_rearm(self, params):
        scope = self._scope_local_only(self._one(params, "scope", default="local"))
        forwarded = self.headers.get("X-MCOZ-Forwarded", "") == "1"
        peer_results = []
        if scope == "all" and not forwarded:
            peer_results = _forward("/rearm", {"scope": "local"})

        with LOCK:
            local = _local_rearm()

        peers_ok = all(peer.get("ok") for peer in peer_results)
        ok = bool(local.get("ok")) and peers_ok
        self._send_json(
            200 if ok else 500,
            {
                "ok": ok,
                "scope": scope,
                "forwarded": forwarded,
                "pod": {"name": POD_NAME, "ip": POD_IP},
                "local": local,
                "peers": peer_results,
            },
        )

    def _handle_arm(self, params):
        namespace = self._one(params, "namespace", "ns", default="")
        pod = self._one(params, "pod", default="")
        container = self._one(params, "container", default="app")
        source = self._one(params, "source", "sourceId", "source_id", default="")
        delay_ns = self._one(params, "delayNs", "delay_ns", default=DEFAULT_FIXED_DELAY_NS)
        count = self._one(params, "count", default="1")

        if not namespace or not pod:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "missing namespace/pod",
                    "required": ["namespace", "pod"],
                },
            )
            return

        try:
            delay_ns_i = int(delay_ns)
            count_i = int(count)
            if delay_ns_i < 0 or count_i < 0:
                raise ValueError("delay_ns and count must be >=0")
        except Exception:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "invalid delay_ns/count",
                    "delay_ns": delay_ns,
                    "count": count,
                },
            )
            return

        with LOCK:
            local = _local_arm(namespace, pod, container, delay_ns_i, count_i, source)

        ok = bool(local.get("ok"))
        payload = {
            "ok": ok,
            "state": "TRIGGERED" if not ok else "ARMED",
            "pod": {"name": POD_NAME, "ip": POD_IP, "namespace": NAMESPACE},
            "arm": {
                "namespace": namespace,
                "pod": pod,
                "container": container,
                "source": source,
                "delay_ns": delay_ns_i,
                "count": count_i,
            },
            "local": local,
        }
        self._send_json(200 if ok else 500, payload)

    def _handle_syscall_profile(self, params):
        namespace = self._one(params, "namespace", "ns", default="")
        pod = self._one(params, "pod", default="")
        container = self._one(params, "container", default="app")
        duration_ms = _to_int(self._one(params, "durationMs", "duration_ms", default="2000"), 2000)
        top_k = _to_int(self._one(params, "topK", "top_k", default="12"), 12)
        apply_policy = _to_bool(
            self._one(params, "applyPolicy", "apply_policy", "apply", default="false")
        )

        if not namespace or not pod:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "missing namespace/pod",
                    "required": ["namespace", "pod"],
                },
            )
            return

        if duration_ms is None or duration_ms <= 0:
            duration_ms = 2000
        if top_k is None or top_k <= 0:
            top_k = 12

        with LOCK:
            local = _local_syscall_profile(
                namespace, pod, container, duration_ms, top_k, apply_policy
            )

        ok = bool(local.get("ok"))
        payload = {
            "ok": ok,
            "pod": {"name": POD_NAME, "ip": POD_IP, "namespace": NAMESPACE},
            "profile": {
                "namespace": namespace,
                "pod": pod,
                "container": container,
                "duration_ms": int(duration_ms),
                "top_k": int(top_k),
                "apply_policy": bool(apply_policy),
            },
            "local": local,
        }
        self._send_json(200 if ok else 500, payload)

    def _handle_consume_policy(self, params):
        namespace = self._one(params, "namespace", "ns", default="")
        pod = self._one(params, "pod", default="")
        container = self._one(params, "container", default="app")
        raw_flags = _to_int(self._one(params, "rawFlags", "raw_flags", default=""), None)
        consume_paths = _split_csv(
            [self._one(params, "consumePaths", "consume_paths", default="")]
        )

        if raw_flags is None:
            try:
                raw_flags, consume_paths = _consume_paths_to_flags(consume_paths)
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
        if not namespace or not pod or raw_flags is None or raw_flags <= 0:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "missing namespace/pod/raw_flags",
                    "required": ["namespace", "pod", "raw_flags|consume_paths"],
                },
            )
            return

        with LOCK:
            local = _consume_policy_distributed(namespace, pod, container, raw_flags)

        ok = bool(local.get("ok"))
        payload = {
            "ok": ok,
            "pod": {"name": POD_NAME, "ip": POD_IP, "namespace": NAMESPACE},
            "policy": {
                "namespace": namespace,
                "pod": pod,
                "container": container,
                "raw_flags": int(raw_flags),
                "consume_paths": consume_paths,
            },
            "local": local,
        }
        self._send_json(200 if ok else 500, payload)

    def _route(self):
        path, params = self._params()
        if path == "/healthz":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "mcoz-control-api",
                    "pod": {"name": POD_NAME, "ip": POD_IP, "namespace": NAMESPACE},
                },
            )
            return
        if path == "/status":
            self._handle_status()
            return
        if path == "/start":
            self._handle_start(params)
            return
        if path == "/stop":
            self._handle_stop(params)
            return
        if path == "/clear":
            self._handle_clear(params)
            return
        if path == "/rearm":
            self._handle_rearm(params)
            return
        if path == "/arm":
            self._handle_arm(params)
            return
        if path in ("/syscall_profile", "/syscall-profile"):
            self._handle_syscall_profile(params)
            return
        if path in ("/consume_policy", "/consume-policy"):
            self._handle_consume_policy(params)
            return

        self._send_json(404, {"ok": False, "error": "not found", "path": path})

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()


def main():
    print(
        f"[mcoz-control] listening on 0.0.0.0:{CONTROL_PORT} "
        f"(pod={POD_NAME}, ip={POD_IP}, ns={NAMESPACE})",
        flush=True,
    )
    server = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
