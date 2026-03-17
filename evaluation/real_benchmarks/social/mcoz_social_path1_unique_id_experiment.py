#!/usr/bin/env python3
"""
Run the SocialNetwork Path 1 request-credit experiment for target unique-id-service.

Path 1:
- compose-post -> text-service
- compose-post -> user-service
- compose-post -> media-service
- compose-post -> unique-id-service

Sidecar rule for this experiment:
- gate sidecars live only on text-service, user-service, media-service
- unique-id-service is the actual-work target and must not have a gate sidecar
- virtual slowdown is injected on the sibling services themselves

Cases:
1) Baseline Path 1 spin set + fixedDelayNs=0
2) Baseline Path 1 spin set + fixedDelayNs=N
3) Explicit actual unique-id spin set + fixedDelayNs=0

Important:
- Social uses *_SPIN_US as a wall-clock busy-work duration in microseconds.
- Case 3 does not derive spin changes from fixedDelayNs. The actual unique-id
  spin value must be chosen explicitly.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from mcoz_social_paths import default_results_dir


DEFAULT_RESULTS_DIR = default_results_dir("path1_target_unique-id")
DEFAULT_RUNTIME_OVERRIDE_DIR = "/tmp/mcoz-spin-overrides"
SIBLING_SERVICES = ("text-service", "user-service", "media-service")
TARGET_SERVICE = "unique-id-service"
ENTRY_SERVICE = "nginx-web-server"
ENTRY_PATH = "/wrk2-api/post/compose"
SPIN_ENV_BY_SERVICE = {
    "text-service": "TEXT_SERVICE_SPIN_US",
    "user-service": "USER_SERVICE_SPIN_US",
    "media-service": "MEDIA_SERVICE_SPIN_US",
    "unique-id-service": "UNIQUE_ID_SERVICE_SPIN_US",
}


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc.stdout.strip()


def _http_json(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout_sec: float = 20.0,
) -> dict:
    body = None
    hdrs = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url=url, method=method, data=body, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _post_json(url: str, payload: dict, timeout_sec: float = 20.0) -> dict:
    return _http_json(url, method="POST", payload=payload, timeout_sec=timeout_sec)


def _kubectl_get_json(namespace: str, kind: str, names: tuple[str, ...]) -> dict:
    return json.loads(
        run(["kubectl", "-n", namespace, "get", kind, *names, "-o", "json"])
    )


def _wait_for_single_ready_pod(
    namespace: str,
    selector: str,
    timeout_sec: float = 180.0,
) -> dict:
    deadline = time.time() + timeout_sec
    last_seen = []
    while time.time() < deadline:
        obj = json.loads(
            run(["kubectl", "-n", namespace, "get", "pods", "-l", selector, "-o", "json"])
        )
        items = obj.get("items", [])
        ready = []
        for item in items:
            metadata = item.get("metadata", {})
            status = item.get("status", {})
            if metadata.get("deletionTimestamp"):
                continue
            if status.get("phase") != "Running":
                continue
            statuses = status.get("containerStatuses") or []
            if statuses and all(bool(x.get("ready")) for x in statuses):
                ready.append(item)
        last_seen = [x.get("metadata", {}).get("name", "") for x in ready]
        if len(ready) == 1:
            return ready[0]
        time.sleep(2.0)
    raise RuntimeError(
        f"expected one ready pod for selector={selector} in ns={namespace}, saw {last_seen}"
    )


def _get_spin_snapshot(namespace: str) -> dict[str, str]:
    obj = _kubectl_get_json(
        namespace,
        "deploy",
        ("text-service", "user-service", "media-service", "unique-id-service"),
    )
    out: dict[str, str] = {}
    for item in obj.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        env_name = SPIN_ENV_BY_SERVICE.get(name)
        value = "<unset>"
        containers = item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            if container.get("name") != name:
                continue
            for env in container.get("env") or []:
                if env.get("name") == env_name:
                    value = str(env.get("value", "<unset>"))
                    break
        out[name] = value
    return out


def _set_spin_env(namespace: str, spin_map: dict[str, int]) -> dict[str, str]:
    before = _get_spin_snapshot(namespace)
    changed = []
    for svc, spin_us in spin_map.items():
        desired = str(int(spin_us))
        if before.get(svc) == desired:
            continue
        env_name = SPIN_ENV_BY_SERVICE[svc]
        run(
            [
                "kubectl",
                "-n",
                namespace,
                "set",
                "env",
                f"deploy/{svc}",
                f"--containers={svc}",
                f"{env_name}={desired}",
            ]
        )
        changed.append(svc)
    for svc in changed:
        run(["kubectl", "-n", namespace, "rollout", "status", f"deploy/{svc}", "--timeout=180s"])
    return _get_spin_snapshot(namespace)


def _override_file_path(override_dir: str, env_name: str) -> str:
    return f"{override_dir.rstrip('/')}/{env_name}"


def _runtime_override_state(
    namespace: str,
    pod: str,
    container: str,
    override_dir: str,
    env_name: str,
) -> dict:
    path = _override_file_path(override_dir, env_name)
    script = (
        f"path={shlex.quote(path)}; "
        "if [ -f \"$path\" ]; then "
        "printf 'present\\n'; cat \"$path\"; "
        "else printf 'missing\\n'; fi"
    )
    output = run(
        [
            "kubectl",
            "-n",
            namespace,
            "exec",
            pod,
            "-c",
            container,
            "--",
            "sh",
            "-lc",
            script,
        ]
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or lines[0] == "missing":
        return {"present": False, "path": path, "value": ""}
    return {"present": True, "path": path, "value": lines[-1]}


def _set_runtime_spin_override(
    namespace: str,
    pod: str,
    container: str,
    override_dir: str,
    env_name: str,
    value: int,
) -> dict:
    path = _override_file_path(override_dir, env_name)
    script = (
        f"mkdir -p {shlex.quote(override_dir)} && "
        f"printf '%s' {shlex.quote(str(int(value)))} > {shlex.quote(path)}"
    )
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "exec",
            pod,
            "-c",
            container,
            "--",
            "sh",
            "-lc",
            script,
        ]
    )
    state = _runtime_override_state(namespace, pod, container, override_dir, env_name)
    if not state.get("present") or state.get("value") != str(int(value)):
        raise RuntimeError(f"failed to set runtime spin override {env_name}={value}: {state}")
    return state


def _clear_runtime_spin_override(
    namespace: str,
    pod: str,
    container: str,
    override_dir: str,
    env_name: str,
) -> dict:
    path = _override_file_path(override_dir, env_name)
    run(
        [
            "kubectl",
            "-n",
            namespace,
            "exec",
            pod,
            "-c",
            container,
            "--",
            "sh",
            "-lc",
            f"rm -f {shlex.quote(path)}",
        ]
    )
    state = _runtime_override_state(namespace, pod, container, override_dir, env_name)
    if state.get("present"):
        raise RuntimeError(f"failed to clear runtime spin override {env_name}: {state}")
    return state


def _get_entry_url(namespace: str, entry_service: str, entry_path: str) -> str:
    ip = run(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "svc",
            entry_service,
            "-o",
            "jsonpath={.spec.clusterIP}",
        ]
    ).strip()
    port = run(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "svc",
            entry_service,
            "-o",
            "jsonpath={.spec.ports[0].port}",
        ]
    ).strip()
    return f"http://{ip}:{port}{entry_path}"


def _compose_post_request(entry_url: str, seq: int) -> urllib.request.Request:
    fields = [
        ("username", "username_1"),
        ("user_id", "1"),
        ("text", f"mcoz-social-{seq} @username_2 http://example.com/mcoz/{seq}"),
        ("media_ids", '["111111111111111111"]'),
        ("media_types", '["png"]'),
        ("post_type", "0"),
    ]
    body = urllib.parse.urlencode(fields).encode("utf-8")
    return urllib.request.Request(
        url=entry_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def _send_compose_post(entry_url: str, seq: int, timeout_sec: float) -> tuple[int, str]:
    req = _compose_post_request(entry_url, seq)
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return int(resp.status), body


def _is_valid_compose_post(status: int, body: str) -> bool:
    return 200 <= int(status) < 300 and "Successfully upload post" in body


def _list_gate_pods(namespace: str) -> list[dict]:
    out = []
    for svc in SIBLING_SERVICES:
        pod = _wait_for_single_ready_pod(namespace, f"service={svc}")
        metadata = pod.get("metadata", {})
        status = pod.get("status", {})
        out.append(
            {
                "service": svc,
                "pod": metadata.get("name", ""),
                "ip": status.get("podIP", ""),
            }
        )
    return out


def _probe_gate_targets(namespace: str) -> list[dict]:
    results = []
    for gate in _list_gate_pods(namespace):
        health = _http_json(f"http://{gate['ip']}:19093/healthz", timeout_sec=5.0)
        if not health.get("ok"):
            raise RuntimeError(f"gate healthz failed for {gate['pod']}: {health}")
        gate_result = dict(gate)
        gate_result["target_mode"] = str(health.get("target_mode", "override"))
        gate_result["target_pod"] = str(health.get("target_pod", "") or "")
        gate_result["target_container"] = str(health.get("target_container", "") or "")
        gate_result["source"] = str(health.get("source", "") or "")
        results.append(gate_result)
    return results


def _set_gate_targets(
    namespace: str,
    target_pod: str,
    target_container: str,
) -> list[dict]:
    results = []
    for gate in _probe_gate_targets(namespace):
        target_mode = gate.get("target_mode", "override")
        if target_mode == "self":
            obj = {
                "target_mode": target_mode,
                "target_pod": gate.get("target_pod"),
                "target_container": gate.get("target_container"),
            }
        else:
            url = (
                f"http://{gate['ip']}:19093/set_target?"
                f"namespace={urllib.parse.quote(namespace)}&"
                f"pod={urllib.parse.quote(target_pod)}&"
                f"container={urllib.parse.quote(target_container)}"
            )
            obj = _http_json(url, timeout_sec=5.0)
            if not obj.get("ok"):
                raise RuntimeError(f"set_target failed for {gate['pod']}: {obj}")
            if obj.get("target_pod") != target_pod:
                raise RuntimeError(f"set_target mismatch for {gate['pod']}: {obj}")
        gate_result = dict(gate)
        gate_result["target_mode"] = obj.get("target_mode", target_mode)
        gate_result["target_pod"] = obj.get("target_pod")
        gate_result["target_container"] = obj.get("target_container")
        results.append(gate_result)
    return results


def _validate_gate_alarm(start_resp: dict, namespace: str, expected_target_pod: str) -> list[dict]:
    gate_alarm = start_resp.get("gate_alarm") if isinstance(start_resp, dict) else {}
    discovery = start_resp.get("gate_discovery") if isinstance(start_resp, dict) else {}
    discovery_by_pod = {}
    if isinstance(discovery, dict):
        for item in discovery.get("results", []):
            if not isinstance(item, dict):
                continue
            pod = str(item.get("pod") or "")
            if pod:
                discovery_by_pod[pod] = item
    if not isinstance(gate_alarm, dict) or not gate_alarm.get("attempted"):
        raise RuntimeError(f"missing gate_alarm in start response: {start_resp}")
    results = []
    seen_services = set()
    for item in gate_alarm.get("results", []):
        if item.get("namespace") != namespace:
            continue
        pod = str(item.get("pod") or "")
        matched = None
        for svc in SIBLING_SERVICES:
            if pod.startswith(f"{svc}-"):
                matched = svc
                break
        if matched is None:
            raise RuntimeError(f"unexpected gate target pod in start response: {item}")
        body_json = item.get("body_json") if isinstance(item.get("body_json"), dict) else {}
        discovery_item = discovery_by_pod.get(pod, {})
        target_mode = str(
            body_json.get(
                "target_mode",
                discovery_item.get("target_mode", "override"),
            )
        )
        if target_mode == "self":
            if body_json.get("target_pod") != pod:
                raise RuntimeError(f"gate self-target mismatch for {pod}: {body_json}")
        else:
            if body_json.get("target_pod") != expected_target_pod:
                raise RuntimeError(
                    f"gate target mismatch for {pod}: expected {expected_target_pod}, got {body_json}"
                )
        seen_services.add(matched)
        results.append(
            {
                "service": matched,
                "pod": pod,
                "ok": bool(item.get("ok")),
                "delay_ns": body_json.get("delay_ns"),
                "count": body_json.get("count"),
                "target_mode": target_mode,
                "target_pod": body_json.get("target_pod"),
            }
        )
    if seen_services != set(SIBLING_SERVICES):
        raise RuntimeError(
            f"gate_alarm sibling set mismatch: expected {set(SIBLING_SERVICES)}, got {seen_services}"
        )
    return sorted(results, key=lambda x: x["service"])


def _extract_pids(start_resp: dict) -> tuple[int, ...]:
    out = []
    after = start_resp.get("after") if isinstance(start_resp, dict) else {}
    pids = after.get("pids") if isinstance(after, dict) else []
    if isinstance(pids, list):
        for item in pids:
            try:
                out.append(int(item))
            except Exception:
                pass
    return tuple(out)


def _start_mcoz(mcoz_url: str, fixed_delay_ns: int, force: bool, namespace: str) -> dict:
    payload = {
        "scope": "local",
        "force": bool(force),
        "requestCredit": True,
        "fixedDelayNs": str(int(fixed_delay_ns)),
        "resetOnStart": True,
        "autoDiscoverGates": True,
        "gateNamespaces": [namespace],
        "gateTargetMode": "self",
        "autoProfileGates": False,
    }
    obj = _post_json(f"{mcoz_url.rstrip('/')}/start", payload, timeout_sec=20.0)
    if not obj.get("ok"):
        raise RuntimeError(f"mcoz start failed: {obj}")
    return obj


def _cleanup_mcoz(mcoz_url: str) -> dict:
    result = {"stop": None, "clear": None, "ok": True}
    try:
        result["stop"] = _post_json(
            f"{mcoz_url.rstrip('/')}/stop",
            {"scope": "local"},
            timeout_sec=20.0,
        )
    except Exception as exc:
        result["stop"] = {"ok": False, "error": str(exc)}
    try:
        result["clear"] = _post_json(
            f"{mcoz_url.rstrip('/')}/clear",
            {"scope": "local", "clearCredits": True},
            timeout_sec=20.0,
        )
    except Exception as exc:
        result["clear"] = {"ok": False, "error": str(exc)}
    result["ok"] = bool((result["stop"] or {}).get("ok")) and bool((result["clear"] or {}).get("ok"))
    return result


def _cleanup_between_cases(
    mcoz_url: str,
    policy_state,
    label: str,
    settle_sec: float,
) -> dict:
    cleanup = _cleanup_mcoz(mcoz_url)
    if isinstance(policy_state, dict):
        policy_state.pop("daemon_pids", None)
    print(
        f"{label} cleanup_mcoz="
        f"{{'ok': {cleanup.get('ok')}, "
        f"'stop_ok': {(cleanup.get('stop') or {}).get('ok')}, "
        f"'clear_ok': {(cleanup.get('clear') or {}).get('ok')}}}"
    )
    if not cleanup.get("ok"):
        raise RuntimeError(f"{label} cleanup failed: {cleanup}")
    if settle_sec > 0:
        print(f"{label} settle_sec={settle_sec:.3f}")
        time.sleep(settle_sec)
    return cleanup


def _warmup_and_clear(
    mcoz_url: str,
    entry_url: str,
    warmup_repeat: int,
    timeout_sec: float,
    seq_start: int,
) -> tuple[dict, int]:
    if warmup_repeat <= 0:
        return {
            "enabled": False,
            "ok": True,
            "warmup_ok": 0,
            "warmup_errors": 0,
            "clear": None,
        }, seq_start

    ok = 0
    err = 0
    seq = seq_start
    for _ in range(warmup_repeat):
        try:
            status, body = _send_compose_post(entry_url, seq, timeout_sec)
            if _is_valid_compose_post(status, body):
                ok += 1
            else:
                err += 1
        except Exception:
            err += 1
        seq += 1

    clear_obj = _post_json(
        f"{mcoz_url.rstrip('/')}/clear",
        {"scope": "local", "clearCredits": True},
        timeout_sec=20.0,
    )
    if not clear_obj.get("ok"):
        raise RuntimeError(f"warmup clear failed: {clear_obj}")
    return {
        "enabled": True,
        "ok": err == 0 and bool(clear_obj.get("ok")),
        "warmup_ok": ok,
        "warmup_errors": err,
        "clear": clear_obj,
    }, seq


def _apply_syscall_profile_once(
    mcoz_url: str,
    namespace: str,
    pod: str,
    container: str,
    entry_url: str,
    request_timeout_sec: float,
    seq_start: int,
    duration_ms: int = 2000,
    top_k: int = 12,
) -> tuple[dict, int]:
    payload = {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "duration_ms": int(duration_ms),
        "top_k": int(top_k),
        "apply_policy": True,
    }
    deadline = time.monotonic() + max(0.2, duration_ms / 1000.0) + 0.2
    traffic = {"ok": 0, "errors": 0}
    seq_box = {"value": seq_start}
    stop_box = {"stop": False}

    def _worker() -> None:
        while (not stop_box["stop"]) and time.monotonic() < deadline:
            current_seq = seq_box["value"]
            seq_box["value"] += 1
            try:
                status, body = _send_compose_post(entry_url, current_seq, request_timeout_sec)
                if _is_valid_compose_post(status, body):
                    traffic["ok"] += 1
                else:
                    traffic["errors"] += 1
            except Exception:
                traffic["errors"] += 1

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    obj = _post_json(
        f"{mcoz_url.rstrip('/')}/syscall_profile",
        payload,
        timeout_sec=max(20.0, duration_ms / 1000.0 + 10.0),
    )
    stop_box["stop"] = True
    thread.join(timeout=1.0)
    if not obj.get("ok"):
        raise RuntimeError(f"syscall_profile failed: {obj}")
    local = obj.get("local") if isinstance(obj.get("local"), dict) else {}
    local_payload = local.get("payload") if isinstance(local.get("payload"), dict) else {}
    if not local_payload.get("apply_policy_ok"):
        raise RuntimeError(f"syscall_profile apply_policy failed: {obj}")
    obj["_traffic"] = traffic
    return obj, seq_box["value"]


def _apply_fixed_consume_policy_once(
    mcoz_url: str,
    namespace: str,
    pod: str,
    container: str,
    consume_paths: list[str],
) -> dict:
    payload = {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "consume_paths": ",".join(consume_paths),
    }
    obj = _post_json(
        f"{mcoz_url.rstrip('/')}/consume_policy",
        payload,
        timeout_sec=20.0,
    )
    if not obj.get("ok"):
        raise RuntimeError(f"consume_policy failed: {obj}")
    local = obj.get("local") if isinstance(obj.get("local"), dict) else {}
    local_result = local.get("result") if isinstance(local.get("result"), dict) else {}
    local_payload = local.get("payload") if isinstance(local.get("payload"), dict) else {}
    if not local_payload and isinstance(local_result.get("payload"), dict):
        local_payload = local_result.get("payload")
    if not local_payload.get("apply_policy_ok"):
        raise RuntimeError(f"consume_policy apply failed: {obj}")
    return obj


def _apply_syscall_profile_for_virtual_victims(
    mcoz_url: str,
    namespace: str,
    entry_url: str,
    request_timeout_sec: float,
    seq_start: int,
    duration_ms: int,
    top_k: int,
    policy_state: dict,
) -> tuple[list[dict], int]:
    applied = []
    pods_by_service = dict(policy_state.get("pods_by_service") or {})
    paths_by_service = dict(policy_state.get("applied_paths_by_service") or {})
    for service in SIBLING_SERVICES:
        pod = _wait_for_single_ready_pod(namespace, f"service={service}")["metadata"]["name"]
        container = service
        cached_paths = [str(x) for x in (paths_by_service.get(service) or []) if str(x)]
        if cached_paths:
            apply_obj = _apply_fixed_consume_policy_once(
                mcoz_url=mcoz_url,
                namespace=namespace,
                pod=pod,
                container=container,
                consume_paths=cached_paths,
            )
            local = apply_obj.get("local") if isinstance(apply_obj.get("local"), dict) else {}
            local_result = local.get("result") if isinstance(local.get("result"), dict) else {}
            local_payload = local.get("payload") if isinstance(local.get("payload"), dict) else {}
            if not local_payload and isinstance(local_result.get("payload"), dict):
                local_payload = local_result.get("payload")
            pods_by_service[service] = pod
            paths_by_service[service] = local_payload.get("applied_consume_paths") or cached_paths
            applied.append(
                {
                    "service": service,
                    "pod": pod,
                    "apply_policy_ok": local_payload.get("apply_policy_ok"),
                    "applied_paths": paths_by_service[service],
                    "traffic_ok": 0,
                    "traffic_errors": 0,
                    "reused": True,
                    "reapplied": True,
                }
            )
        else:
            profile_obj, seq_start = _apply_syscall_profile_once(
                mcoz_url=mcoz_url,
                namespace=namespace,
                pod=pod,
                container=container,
                entry_url=entry_url,
                request_timeout_sec=request_timeout_sec,
                seq_start=seq_start,
                duration_ms=duration_ms,
                top_k=top_k,
            )
            local = profile_obj.get("local") if isinstance(profile_obj.get("local"), dict) else {}
            local_payload = local.get("payload") if isinstance(local.get("payload"), dict) else {}
            traffic = profile_obj.get("_traffic", {})
            pods_by_service[service] = pod
            paths_by_service[service] = local_payload.get("applied_consume_paths") or []
            applied.append(
                {
                    "service": service,
                    "pod": pod,
                    "apply_policy_ok": local_payload.get("apply_policy_ok"),
                    "applied_paths": paths_by_service[service],
                    "traffic_ok": traffic.get("ok", 0),
                    "traffic_errors": traffic.get("errors", 0),
                    "reused": False,
                    "reapplied": False,
                }
            )
    policy_state["applied"] = True
    policy_state["pods_by_service"] = pods_by_service
    policy_state["applied_paths_by_service"] = paths_by_service
    policy_state["applied_paths"] = paths_by_service
    return applied, seq_start


def _measure_once(
    entry_url: str,
    repeat: int,
    timeout_sec: float,
    case_name: str,
    run_idx: int,
    seq_start: int,
) -> tuple[dict, int]:
    ok = 0
    err = 0
    total_ns = 0
    rows = []
    seq = seq_start
    for i in range(1, repeat + 1):
        status_text = ""
        start_ns = time.perf_counter_ns()
        try:
            status, body = _send_compose_post(entry_url, seq, timeout_sec)
            if _is_valid_compose_post(status, body):
                ok += 1
                status_text = str(status)
            else:
                err += 1
                status_text = f"{status}/INVALID_COMPOSE_POST"
        except urllib.error.HTTPError as exc:
            err += 1
            status_text = str(exc.code)
        except Exception:
            err += 1
            status_text = "ERROR"
        finally:
            latency_ns = time.perf_counter_ns() - start_ns
            total_ns += latency_ns
            rows.append(
                {
                    "case": case_name,
                    "run": run_idx,
                    "iter": i,
                    "latency_ns": latency_ns,
                    "latency_us": latency_ns / 1000.0,
                    "response": status_text,
                }
            )
            seq += 1
    avg_us = (total_ns / repeat) / 1000.0
    return {"avg_us": avg_us, "ok": ok, "errors": err, "rows": rows}, seq


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    if q <= 0:
        return min(values)
    if q >= 1:
        return max(values)
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def median(values: list[float]) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    n = len(s)
    m = n // 2
    if n % 2 == 1:
        return s[m]
    return (s[m - 1] + s[m]) / 2.0


def aggregate(values: list[float], mode: str) -> float:
    if not values:
        return float("nan")
    if mode == "median":
        return median(values)
    return sum(values) / len(values)


def pick_runs(run_vals: list[float], tail_drop_top_runs: int) -> tuple[list[int], list[dict]]:
    indexed = [(i + 1, v) for i, v in enumerate(run_vals)]
    if tail_drop_top_runs <= 0:
        return [i for i, _ in indexed], []
    keep_n = len(indexed) - tail_drop_top_runs
    kept = sorted(indexed, key=lambda x: (x[1], x[0]))[:keep_n]
    kept_ids = sorted(i for i, _ in kept)
    kept_set = set(kept_ids)
    dropped = [{"run": i, "avg_us": v} for i, v in indexed if i not in kept_set]
    return kept_ids, dropped


def compute_case_stats(rows: list[dict], selected_only: bool = True) -> dict[str, dict[str, float]]:
    by_case: dict[str, list[float]] = {}
    for row in rows:
        if selected_only and int(row.get("run_selected", 1)) != 1:
            continue
        by_case.setdefault(str(row["case"]), []).append(float(row["latency_us"]))
    out = {}
    for case_name, vals in by_case.items():
        n = len(vals)
        mean = sum(vals) / n
        var = sum((x - mean) ** 2 for x in vals) / n
        out[case_name] = {
            "count": float(n),
            "mean_us": mean,
            "variance_us2": var,
            "stddev_us": math.sqrt(var),
            "t95_us": percentile(vals, 0.95),
            "t99_us": percentile(vals, 0.99),
            "max_us": max(vals),
        }
    return out


def _result_fields() -> list[str]:
    return [
        "row_type",
        "file_delay_ns",
        "case",
        "run",
        "iter",
        "run_selected",
        "latency_us",
        "latency_ns",
        "response",
        "target_pod",
        "baseline_text_spin_us",
        "baseline_user_spin_us",
        "baseline_media_spin_us",
        "baseline_unique_id_spin_us",
        "actual_unique_id_spin_us",
        "sibling_gate_pods",
        "case_count",
        "case_selected_avg_us",
        "case_mean_us",
        "case_variance_us2",
        "case_stddev_us",
        "case_t95_us",
        "case_t99_us",
        "case_max_us",
        "analysis_virtual_gain_ms",
        "analysis_actual_gain_ms",
        "analysis_signed_error_ms",
        "analysis_signed_error_rate_pct",
    ]


def build_output_rows(
    all_rows: list[dict],
    case_stats: dict[str, dict[str, float]],
    case_results: dict[str, dict],
    analysis: dict[str, float],
    meta: dict[str, object],
) -> list[dict]:
    out = []
    out.append(
        {
            "row_type": "summary_meta",
            "file_delay_ns": meta["fixed_delay_ns"],
            "case": "social-path1-target-unique-id",
            "run": "",
            "iter": "",
            "run_selected": "",
            "latency_us": "",
            "latency_ns": "",
            "response": "",
            "target_pod": meta["target_pod"],
            "baseline_text_spin_us": meta["baseline_text_spin_us"],
            "baseline_user_spin_us": meta["baseline_user_spin_us"],
            "baseline_media_spin_us": meta["baseline_media_spin_us"],
            "baseline_unique_id_spin_us": meta["baseline_unique_id_spin_us"],
            "actual_unique_id_spin_us": meta["actual_unique_id_spin_us"],
            "sibling_gate_pods": meta["sibling_gate_pods"],
            "case_count": "",
            "case_selected_avg_us": "",
            "case_mean_us": "",
            "case_variance_us2": "",
            "case_stddev_us": "",
            "case_t95_us": "",
            "case_t99_us": "",
            "case_max_us": "",
            "analysis_virtual_gain_ms": "",
            "analysis_actual_gain_ms": "",
            "analysis_signed_error_ms": "",
            "analysis_signed_error_rate_pct": "",
        }
    )

    for case_name in CASE_ORDER:
        st = case_stats.get(case_name)
        if not st:
            continue
        case_result = case_results[case_name]
        out.append(
            {
                "row_type": "summary_case",
                "file_delay_ns": meta["fixed_delay_ns"],
                "case": case_name,
                "run": "",
                "iter": "",
                "run_selected": "",
                "latency_us": "",
                "latency_ns": "",
                "response": "",
                "target_pod": case_result["target_pod"],
                "baseline_text_spin_us": meta["baseline_text_spin_us"],
                "baseline_user_spin_us": meta["baseline_user_spin_us"],
                "baseline_media_spin_us": meta["baseline_media_spin_us"],
                "baseline_unique_id_spin_us": meta["baseline_unique_id_spin_us"],
                "actual_unique_id_spin_us": meta["actual_unique_id_spin_us"],
                "sibling_gate_pods": meta["sibling_gate_pods"],
                "case_count": int(st["count"]),
                "case_selected_avg_us": case_result["avg_us"],
                "case_mean_us": st["mean_us"],
                "case_variance_us2": st["variance_us2"],
                "case_stddev_us": st["stddev_us"],
                "case_t95_us": st["t95_us"],
                "case_t99_us": st["t99_us"],
                "case_max_us": st["max_us"],
                "analysis_virtual_gain_ms": "",
                "analysis_actual_gain_ms": "",
                "analysis_signed_error_ms": "",
                "analysis_signed_error_rate_pct": "",
            }
        )

    out.append(
        {
            "row_type": "summary_analysis",
            "file_delay_ns": meta["fixed_delay_ns"],
            "case": "analysis",
            "run": "",
            "iter": "",
            "run_selected": "",
            "latency_us": "",
            "latency_ns": "",
            "response": "",
            "target_pod": meta["target_pod"],
            "baseline_text_spin_us": meta["baseline_text_spin_us"],
            "baseline_user_spin_us": meta["baseline_user_spin_us"],
            "baseline_media_spin_us": meta["baseline_media_spin_us"],
            "baseline_unique_id_spin_us": meta["baseline_unique_id_spin_us"],
            "actual_unique_id_spin_us": meta["actual_unique_id_spin_us"],
            "sibling_gate_pods": meta["sibling_gate_pods"],
            "case_count": "",
            "case_selected_avg_us": "",
            "case_mean_us": "",
            "case_variance_us2": "",
            "case_stddev_us": "",
            "case_t95_us": "",
            "case_t99_us": "",
            "case_max_us": "",
            "analysis_virtual_gain_ms": analysis["virtual_gain_ms"],
            "analysis_actual_gain_ms": analysis["actual_gain_ms"],
            "analysis_signed_error_ms": analysis["signed_error_ms"],
            "analysis_signed_error_rate_pct": analysis["signed_error_rate_pct"],
        }
    )

    for row in all_rows:
        case_result = case_results[row["case"]]
        out.append(
            {
                "row_type": "request",
                "file_delay_ns": meta["fixed_delay_ns"],
                "case": row["case"],
                "run": row["run"],
                "iter": row["iter"],
                "run_selected": row.get("run_selected", 1),
                "latency_us": row["latency_us"],
                "latency_ns": row["latency_ns"],
                "response": row["response"],
                "target_pod": case_result["target_pod"],
                "baseline_text_spin_us": meta["baseline_text_spin_us"],
                "baseline_user_spin_us": meta["baseline_user_spin_us"],
                "baseline_media_spin_us": meta["baseline_media_spin_us"],
                "baseline_unique_id_spin_us": meta["baseline_unique_id_spin_us"],
                "actual_unique_id_spin_us": meta["actual_unique_id_spin_us"],
                "sibling_gate_pods": meta["sibling_gate_pods"],
                "case_count": "",
                "case_selected_avg_us": "",
                "case_mean_us": "",
                "case_variance_us2": "",
                "case_stddev_us": "",
                "case_t95_us": "",
                "case_t99_us": "",
                "case_max_us": "",
                "analysis_virtual_gain_ms": "",
                "analysis_actual_gain_ms": "",
                "analysis_signed_error_ms": "",
                "analysis_signed_error_rate_pct": "",
            }
        )
    return out


def write_csv(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_result_fields())
        writer.writeheader()
        writer.writerows(rows)


def regenerate_summary(results_dir: str) -> str:
    rows = []
    for path in sorted(glob.glob(os.path.join(results_dir, "result_delay-*.csv"))):
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            meta = None
            case_rows = {}
            analysis = None
            for row in reader:
                if row["row_type"] == "summary_meta":
                    meta = row
                elif row["row_type"] == "summary_case":
                    case_rows[row["case"]] = row
                elif row["row_type"] == "summary_analysis":
                    analysis = row
            if not meta or not analysis:
                continue
            rows.append(
                {
                    "csv_file": os.path.basename(path),
                    "fixed_delay_ns": meta["file_delay_ns"],
                    "baseline_text_spin_us": meta["baseline_text_spin_us"],
                    "baseline_user_spin_us": meta["baseline_user_spin_us"],
                    "baseline_media_spin_us": meta["baseline_media_spin_us"],
                    "baseline_unique_id_spin_us": meta["baseline_unique_id_spin_us"],
                    "actual_unique_id_spin_us": meta["actual_unique_id_spin_us"],
                    "target_pod": meta["target_pod"],
                    "case1_avg_us": (case_rows.get(CASE_ORDER[0]) or {}).get("case_selected_avg_us", ""),
                    "case2_avg_us": (case_rows.get(CASE_ORDER[1]) or {}).get("case_selected_avg_us", ""),
                    "case3_avg_us": (case_rows.get(CASE_ORDER[2]) or {}).get("case_selected_avg_us", ""),
                    "virtual_gain_ms": analysis.get("analysis_virtual_gain_ms", ""),
                    "actual_gain_ms": analysis.get("analysis_actual_gain_ms", ""),
                    "signed_error_ms": analysis.get("analysis_signed_error_ms", ""),
                    "signed_error_rate_pct": analysis.get("analysis_signed_error_rate_pct", ""),
                }
            )
    summary_path = os.path.join(results_dir, "summary.csv")
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "csv_file",
            "fixed_delay_ns",
            "baseline_text_spin_us",
            "baseline_user_spin_us",
            "baseline_media_spin_us",
            "baseline_unique_id_spin_us",
            "actual_unique_id_spin_us",
            "target_pod",
            "case1_avg_us",
            "case2_avg_us",
            "case3_avg_us",
            "virtual_gain_ms",
            "actual_gain_ms",
            "signed_error_ms",
            "signed_error_rate_pct",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return summary_path


def run_case(
    name: str,
    namespace: str,
    spin_map: dict[str, int],
    mcoz_url: str,
    fixed_delay_ns: int,
    force_start: bool,
    entry_url: str,
    repeat: int,
    runs: int,
    timeout_sec: float,
    warmup_repeat: int,
    tail_drop_top_runs: int,
    aggregate_mode: str,
    apply_policy_duration_ms: int,
    target_container: str,
    target_runtime_override_us: int | None,
    runtime_override_dir: str,
    all_rows: list[dict],
    policy_state: dict,
    seq_start: int,
) -> tuple[dict, int]:
    snapshot = _set_spin_env(namespace, spin_map)
    target_pod = _wait_for_single_ready_pod(namespace, f"service={TARGET_SERVICE}")["metadata"]["name"]
    print(f"{name} spin_snapshot={snapshot}")
    override_env_name = SPIN_ENV_BY_SERVICE[TARGET_SERVICE]
    if target_runtime_override_us is None:
        override_state = _clear_runtime_spin_override(
            namespace, target_pod, target_container, runtime_override_dir, override_env_name
        )
    else:
        override_state = _set_runtime_spin_override(
            namespace,
            target_pod,
            target_container,
            runtime_override_dir,
            override_env_name,
            target_runtime_override_us,
        )
    print(f"{name} runtime_override={{'target_pod': '{target_pod}', 'state': {override_state}}}")
    gate_probe = _probe_gate_targets(namespace)
    all_self_target = bool(gate_probe) and all(
        str(item.get("target_mode", "")).strip().lower() == "self" for item in gate_probe
    )

    if all_self_target:
        start_resp = _start_mcoz(mcoz_url, fixed_delay_ns, force_start, namespace)
        gate_targets = [
            {
                "service": item["service"],
                "pod": item["pod"],
                "ip": item["ip"],
                "target_mode": "self",
                "target_pod": item["pod"],
                "target_container": item.get("target_container") or item["service"],
            }
            for item in gate_probe
        ]
    else:
        gate_targets = _set_gate_targets(namespace, target_pod, target_container)
        print(f"{name} gate_target_update={{'target_pod': '{target_pod}', 'gates': {gate_targets}}}")
        start_resp = _start_mcoz(mcoz_url, fixed_delay_ns, force_start, namespace)

    if all_self_target:
        print(
            f"{name} gate_target_update="
            f"{{'target_pod': '{target_pod}', 'gates': {gate_targets}, 'start_first': True}}"
        )

    gate_alarm_targets = _validate_gate_alarm(start_resp, namespace, target_pod)
    print(f"{name} gate_alarm={gate_alarm_targets}")

    current_daemon_pids = _extract_pids(start_resp)
    sibling_policy, seq_start = _apply_syscall_profile_for_virtual_victims(
        mcoz_url=mcoz_url,
        namespace=namespace,
        entry_url=entry_url,
        request_timeout_sec=timeout_sec,
        seq_start=seq_start,
        duration_ms=apply_policy_duration_ms,
        top_k=12,
        policy_state=policy_state,
    )
    policy_state["daemon_pids"] = list(current_daemon_pids)
    print(
        f"{name} syscall_profile="
        f"{{'victims': {sibling_policy}, 'daemon_pids': {list(current_daemon_pids)}}}"
    )

    warmup, seq_start = _warmup_and_clear(
        mcoz_url=mcoz_url,
        entry_url=entry_url,
        warmup_repeat=warmup_repeat,
        timeout_sec=timeout_sec,
        seq_start=seq_start,
    )
    print(
        f"{name} warmup="
        f"{{'enabled': {warmup['enabled']}, 'ok': {warmup['ok']}, "
        f"'warmup_ok': {warmup['warmup_ok']}, 'warmup_errors': {warmup['warmup_errors']}}}"
    )

    run_vals = []
    run_rows_by_idx = {}
    for run_idx in range(1, runs + 1):
        measured, seq_start = _measure_once(
            entry_url=entry_url,
            repeat=repeat,
            timeout_sec=timeout_sec,
            case_name=name,
            run_idx=run_idx,
            seq_start=seq_start,
        )
        run_vals.append(measured["avg_us"])
        run_rows_by_idx[run_idx] = measured["rows"]
        print(
            f"{name} run={run_idx} avg_latency_us={measured['avg_us']:.3f} "
            f"ok={measured['ok']}/{repeat} errors={measured['errors']}"
        )

    selected_run_indices, dropped_runs = pick_runs(run_vals, tail_drop_top_runs)
    selected_set = set(selected_run_indices)
    for run_idx in range(1, runs + 1):
        selected = 1 if run_idx in selected_set else 0
        for row in run_rows_by_idx.get(run_idx, []):
            row["run_selected"] = selected
            all_rows.append(row)
    used_vals = [run_vals[idx - 1] for idx in selected_run_indices]
    avg_us = aggregate(used_vals, aggregate_mode)
    print(
        f"{name} selected_runs={selected_run_indices} "
        f"dropped_runs={dropped_runs if dropped_runs else '[]'} aggregate={aggregate_mode}"
    )
    return {
        "name": name,
        "avg_us": avg_us,
        "runs_us": used_vals,
        "target_pod": target_pod,
        "gate_alarm_targets": gate_alarm_targets,
        "snapshot": snapshot,
    }, seq_start


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run SocialNetwork Path 1 request-credit experiment for target unique-id-service."
    )
    parser.add_argument("--namespace", default="social")
    parser.add_argument("--entry-service", default=ENTRY_SERVICE)
    parser.add_argument("--entry-path", default=ENTRY_PATH)
    parser.add_argument("--entry-url", default="")
    parser.add_argument("--mcoz-url", default="http://127.0.0.1:19091")
    parser.add_argument("--fixed-delay-ns", type=int, required=True)
    parser.add_argument("--baseline-text-spin-us", type=int, default=200)
    parser.add_argument("--baseline-user-spin-us", type=int, default=850)
    parser.add_argument("--baseline-media-spin-us", type=int, default=850)
    parser.add_argument("--baseline-unique-id-spin-us", type=int, default=1260)
    parser.add_argument(
        "--actual-unique-id-spin-us",
        type=int,
        default=-1,
        help="explicit unique-id spin for Case 3; default -1 means reuse baseline value",
    )
    parser.add_argument("--repeat", type=int, default=100)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup-repeat", type=int, default=3)
    parser.add_argument("--request-timeout-sec", type=float, default=10.0)
    parser.add_argument("--tail-drop-top-runs", type=int, default=0)
    parser.add_argument("--aggregate-mode", choices=["mean", "median"], default="mean")
    parser.add_argument("--force-start", action="store_true")
    parser.add_argument("--target-container", default=TARGET_SERVICE)
    parser.add_argument("--apply-policy-duration-ms", type=int, default=2000)
    parser.add_argument("--runtime-override-dir", default=DEFAULT_RUNTIME_OVERRIDE_DIR)
    parser.add_argument("--case-settle-sec", type=float, default=2.0)
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--csv-out", default="")
    parser.add_argument(
        "--no-restore-baseline-after",
        action="store_true",
        help="leave the final live *_SPIN_US values at the last experiment case",
    )
    return parser.parse_args()


CASE_ORDER = (
    "Case 1 - baseline delay0",
    "Case 2 - virtual delayN",
    "Case 3 - actual unique-id delay0",
)


def main() -> int:
    args = parse_args()
    if args.fixed_delay_ns < 0:
        raise SystemExit("fixed-delay-ns must be >= 0")
    if args.repeat <= 0 or args.runs <= 0:
        raise SystemExit("repeat/runs must be > 0")
    if args.tail_drop_top_runs < 0 or args.tail_drop_top_runs >= args.runs:
        raise SystemExit("tail-drop-top-runs must satisfy 0 <= drop < runs")

    actual_unique_id_spin_us = (
        args.baseline_unique_id_spin_us
        if args.actual_unique_id_spin_us < 0
        else args.actual_unique_id_spin_us
    )
    baseline_spin = {
        "text-service": args.baseline_text_spin_us,
        "user-service": args.baseline_user_spin_us,
        "media-service": args.baseline_media_spin_us,
        "unique-id-service": args.baseline_unique_id_spin_us,
    }
    actual_spin = dict(baseline_spin)
    actual_spin["unique-id-service"] = actual_unique_id_spin_us

    entry_url = args.entry_url or _get_entry_url(args.namespace, args.entry_service, args.entry_path)
    print(f"entry_url={entry_url}")
    print(f"baseline_spin={baseline_spin}")
    print(f"actual_spin={actual_spin}")
    print(f"fixed_delay_ns={args.fixed_delay_ns}")

    all_rows = []
    policy_state: dict[str, object] = {}
    seq = 1
    if args.csv_out:
        csv_out = args.csv_out
    else:
        os.makedirs(args.results_dir, exist_ok=True)
        csv_out = os.path.join(args.results_dir, f"result_delay-{args.fixed_delay_ns}.csv")

    try:
        case1, seq = run_case(
            name=CASE_ORDER[0],
            namespace=args.namespace,
            spin_map=baseline_spin,
            mcoz_url=args.mcoz_url,
            fixed_delay_ns=0,
            force_start=args.force_start,
            entry_url=entry_url,
            repeat=args.repeat,
            runs=args.runs,
            timeout_sec=args.request_timeout_sec,
            warmup_repeat=args.warmup_repeat,
            tail_drop_top_runs=args.tail_drop_top_runs,
            aggregate_mode=args.aggregate_mode,
            apply_policy_duration_ms=args.apply_policy_duration_ms,
            target_container=args.target_container,
            target_runtime_override_us=None,
            runtime_override_dir=args.runtime_override_dir,
            all_rows=all_rows,
            policy_state=policy_state,
            seq_start=seq,
        )
        _cleanup_between_cases(
            args.mcoz_url,
            policy_state,
            "between_case1_case2",
            args.case_settle_sec,
        )
        case2, seq = run_case(
            name=CASE_ORDER[1],
            namespace=args.namespace,
            spin_map=baseline_spin,
            mcoz_url=args.mcoz_url,
            fixed_delay_ns=args.fixed_delay_ns,
            force_start=False,
            entry_url=entry_url,
            repeat=args.repeat,
            runs=args.runs,
            timeout_sec=args.request_timeout_sec,
            warmup_repeat=args.warmup_repeat,
            tail_drop_top_runs=args.tail_drop_top_runs,
            aggregate_mode=args.aggregate_mode,
            apply_policy_duration_ms=args.apply_policy_duration_ms,
            target_container=args.target_container,
            target_runtime_override_us=None,
            runtime_override_dir=args.runtime_override_dir,
            all_rows=all_rows,
            policy_state=policy_state,
            seq_start=seq,
        )
        _cleanup_between_cases(
            args.mcoz_url,
            policy_state,
            "between_case2_case3",
            args.case_settle_sec,
        )
        case3, seq = run_case(
            name=CASE_ORDER[2],
            namespace=args.namespace,
            spin_map=baseline_spin,
            mcoz_url=args.mcoz_url,
            fixed_delay_ns=0,
            force_start=False,
            entry_url=entry_url,
            repeat=args.repeat,
            runs=args.runs,
            timeout_sec=args.request_timeout_sec,
            warmup_repeat=args.warmup_repeat,
            tail_drop_top_runs=args.tail_drop_top_runs,
            aggregate_mode=args.aggregate_mode,
            apply_policy_duration_ms=args.apply_policy_duration_ms,
            target_container=args.target_container,
            target_runtime_override_us=actual_spin["unique-id-service"],
            runtime_override_dir=args.runtime_override_dir,
            all_rows=all_rows,
            policy_state=policy_state,
            seq_start=seq,
        )

        case_results = {
            CASE_ORDER[0]: case1,
            CASE_ORDER[1]: case2,
            CASE_ORDER[2]: case3,
        }
        case_stats = compute_case_stats(all_rows, selected_only=True)
        l1_us = case1["avg_us"]
        l2_us = case2["avg_us"]
        l3_us = case3["avg_us"]
        n_us = args.fixed_delay_ns / 1000.0
        l_after_pred_us = l2_us - n_us
        analysis = {
            "virtual_gain_ms": ((l1_us + n_us) - l2_us) / 1000.0,
            "actual_gain_ms": (l1_us - l3_us) / 1000.0,
            "signed_error_ms": (l_after_pred_us - l3_us) / 1000.0,
            "signed_error_rate_pct": ((l_after_pred_us - l3_us) / l3_us * 100.0)
            if l3_us != 0
            else float("inf"),
        }
        sibling_gate_pods = ",".join(
            f"{item['service']}:{item['pod']}" for item in case3["gate_alarm_targets"]
        )
        meta = {
            "fixed_delay_ns": args.fixed_delay_ns,
            "target_pod": case3["target_pod"],
            "baseline_text_spin_us": baseline_spin["text-service"],
            "baseline_user_spin_us": baseline_spin["user-service"],
            "baseline_media_spin_us": baseline_spin["media-service"],
            "baseline_unique_id_spin_us": baseline_spin["unique-id-service"],
            "actual_unique_id_spin_us": actual_spin["unique-id-service"],
            "sibling_gate_pods": sibling_gate_pods,
        }
        rows = build_output_rows(all_rows, case_stats, case_results, analysis, meta)
        write_csv(csv_out, rows)
        summary_path = regenerate_summary(args.results_dir)

        print("\n=== summary ===")
        print(f"{CASE_ORDER[0]} avg_ms={case1['avg_us'] / 1000.0:.3f} runs_us={case1['runs_us']}")
        print(f"{CASE_ORDER[1]} avg_ms={case2['avg_us'] / 1000.0:.3f} runs_us={case2['runs_us']}")
        print(f"{CASE_ORDER[2]} avg_ms={case3['avg_us'] / 1000.0:.3f} runs_us={case3['runs_us']}")
        print(f"fixed_delay_ms={args.fixed_delay_ns / 1_000_000.0:.3f}")
        print(f"virtual_gain_ms={analysis['virtual_gain_ms']:.3f}")
        print(f"actual_gain_ms={analysis['actual_gain_ms']:.3f}")
        print(f"signed_error_ms={analysis['signed_error_ms']:+.3f}")
        print(f"signed_error_rate_pct={analysis['signed_error_rate_pct']:+.3f}")
        print(f"policy_applied_paths={policy_state.get('applied_paths', [])}")
        print(f"csv_out={csv_out}")
        print(f"summary_out={summary_path}")
        return 0
    finally:
        cleanup = _cleanup_mcoz(args.mcoz_url)
        restore = {"ok": True, "skipped": bool(args.no_restore_baseline_after)}
        if not args.no_restore_baseline_after:
            try:
                _set_spin_env(args.namespace, baseline_spin)
                restored_target_pod = _wait_for_single_ready_pod(
                    args.namespace, f"service={TARGET_SERVICE}"
                )["metadata"]["name"]
                cleared_override = _clear_runtime_spin_override(
                    args.namespace,
                    restored_target_pod,
                    args.target_container,
                    args.runtime_override_dir,
                    SPIN_ENV_BY_SERVICE[TARGET_SERVICE],
                )
                _set_gate_targets(args.namespace, restored_target_pod, args.target_container)
                restore["target_pod"] = restored_target_pod
                restore["runtime_override"] = cleared_override
            except Exception as exc:
                restore = {"ok": False, "error": str(exc), "skipped": False}
        print(
            "cleanup_mcoz="
            f"{{'ok': {cleanup.get('ok')}, "
            f"'stop_ok': {(cleanup.get('stop') or {}).get('ok')}, "
            f"'clear_ok': {(cleanup.get('clear') or {}).get('ok')}}}"
        )
        print(f"restore_baseline={restore}")


if __name__ == "__main__":
    raise SystemExit(main())
