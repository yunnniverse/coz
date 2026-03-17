#!/usr/bin/env python3
"""
Run the SocialNetwork Path 1 request-credit experiment with auto-matched actual work.

This script keeps Case 1 and Case 2 on the original baseline configuration,
then calibrates the actual unique-id-service work so that the local
`compose_unique_id_server` span is increased by approximately the same amount as
`fixedDelayNs` at the victim process. After calibration, it runs Case 3:

1) Baseline Path 1 spin set + fixedDelayNs=0
2) Baseline Path 1 spin set + fixedDelayNs=N
3) Auto-calibrated unique-id spin set + fixedDelayNs=0

Important:
- The 1:1 matching target is the local unique-id-service server span increase,
  not the end-to-end compose-post latency.
- Social uses *_SPIN_US as a wall-clock busy-work duration in microseconds.
- The actual-work knob is changed inside the same running unique-id-service pod
  via a runtime override file, so Case 3 changes only the target pod.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import shlex
import time
import urllib.parse
from typing import Any

from mcoz_social_paths import default_results_dir
import mcoz_social_path1_unique_id_experiment as base


DEFAULT_RESULTS_DIR = default_results_dir("path1_target_unique-id-matched-delay")
JAEGER_SERVICE = "jaeger"
JAEGER_OPERATION = "compose_unique_id_server"
DEFAULT_RUNTIME_OVERRIDE_DIR = "/tmp/mcoz-spin-overrides"


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def _trimmed_mean(values: list[float], trim_fraction: float) -> float:
    if not values:
        return float("nan")
    if trim_fraction <= 0:
        return _mean(values)
    s = sorted(values)
    trim_n = int(len(s) * trim_fraction)
    if trim_n * 2 >= len(s):
        return _mean(s)
    kept = s[trim_n : len(s) - trim_n]
    return _mean(kept)


def _aggregate(values: list[float], mode: str, trim_fraction: float) -> float:
    if not values:
        return float("nan")
    if mode == "median":
        return base.median(values)
    if mode == "trimmed-mean":
        return _trimmed_mean(values, trim_fraction)
    return _mean(values)


def _stats(values: list[float], aggregate_mode: str, trim_fraction: float) -> dict[str, float]:
    if not values:
        return {
            "count": 0.0,
            "aggregate_us": float("nan"),
            "mean_us": float("nan"),
            "median_us": float("nan"),
            "p95_us": float("nan"),
            "p99_us": float("nan"),
            "min_us": float("nan"),
            "max_us": float("nan"),
        }
    return {
        "count": float(len(values)),
        "aggregate_us": _aggregate(values, aggregate_mode, trim_fraction),
        "mean_us": _mean(values),
        "median_us": base.median(values),
        "p95_us": base.percentile(values, 0.95),
        "p99_us": base.percentile(values, 0.99),
        "min_us": min(values),
        "max_us": max(values),
    }


def _get_service_url(namespace: str, service: str, port_name: str, path: str = "") -> str:
    ip = base.run(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "svc",
            service,
            "-o",
            "jsonpath={.spec.clusterIP}",
        ]
    ).strip()
    port = base.run(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "svc",
            service,
            "-o",
            f"jsonpath={{.spec.ports[?(@.name==\"{port_name}\")].port}}",
        ]
    ).strip()
    if not port:
        raise RuntimeError(f"could not find port {port_name} on service {service} in ns={namespace}")
    return f"http://{ip}:{port}{path}"


def _get_jaeger_url(namespace: str) -> str:
    return _get_service_url(namespace, JAEGER_SERVICE, "16686")


def _override_file_path(override_dir: str, env_name: str) -> str:
    return f"{override_dir.rstrip('/')}/{env_name}"


def _runtime_override_state(
    namespace: str,
    pod: str,
    container: str,
    override_dir: str,
    env_name: str,
) -> dict[str, Any]:
    path = _override_file_path(override_dir, env_name)
    script = (
        f"path={shlex.quote(path)}; "
        "if [ -f \"$path\" ]; then "
        "printf 'present\\n'; cat \"$path\"; "
        "else printf 'missing\\n'; fi"
    )
    output = base.run(
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
) -> dict[str, Any]:
    path = _override_file_path(override_dir, env_name)
    script = (
        f"mkdir -p {shlex.quote(override_dir)} && "
        f"printf '%s' {shlex.quote(str(int(value)))} > {shlex.quote(path)}"
    )
    base.run(
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
) -> dict[str, Any]:
    path = _override_file_path(override_dir, env_name)
    script = f"rm -f {shlex.quote(path)}"
    base.run(
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
    if state.get("present"):
        raise RuntimeError(f"failed to clear runtime spin override {env_name}: {state}")
    return state


def _query_jaeger_traces(
    jaeger_url: str,
    service: str,
    operation: str,
    limit: int,
    lookback: str,
    start_us: int,
    end_us: int,
) -> dict[str, Any]:
    params = {
        "service": service,
        "operation": operation,
        "limit": str(int(limit)),
        "lookback": lookback,
        "start": str(int(start_us)),
        "end": str(int(end_us)),
    }
    url = f"{jaeger_url.rstrip('/')}/api/traces?{urllib.parse.urlencode(params)}"
    last_exc = None
    for _ in range(3):
        try:
            return base._http_json(url, timeout_sec=20.0)
        except Exception as exc:
            last_exc = exc
            time.sleep(0.5)
    raise RuntimeError(f"jaeger trace query failed for {service}/{operation}: {last_exc}")


def _extract_matching_spans(
    traces_obj: dict[str, Any],
    service: str,
    operation: str,
    start_us: int,
    end_us: int,
) -> list[dict[str, Any]]:
    out = []
    for trace in traces_obj.get("data", []):
        processes = trace.get("processes") or {}
        trace_id = str(trace.get("traceID") or "")
        for span in trace.get("spans", []):
            if span.get("operationName") != operation:
                continue
            process = processes.get(span.get("processID")) or {}
            if process.get("serviceName") != service:
                continue
            span_start_us = int(span.get("startTime") or 0)
            if span_start_us < start_us or span_start_us > end_us:
                continue
            out.append(
                {
                    "trace_id": trace_id,
                    "span_id": str(span.get("spanID") or ""),
                    "start_us": span_start_us,
                    "duration_us": float(span.get("duration") or 0.0),
                }
            )
    out.sort(key=lambda x: (x["start_us"], x["span_id"]))
    return out


def _poll_matching_spans(
    jaeger_url: str,
    service: str,
    operation: str,
    start_us: int,
    end_us: int,
    expected_min_count: int,
    lookback: str,
    query_timeout_sec: float,
    limit: int,
) -> list[dict[str, Any]]:
    deadline = time.time() + query_timeout_sec
    best = []
    while time.time() < deadline:
        traces_obj = _query_jaeger_traces(
            jaeger_url=jaeger_url,
            service=service,
            operation=operation,
            limit=limit,
            lookback=lookback,
            start_us=start_us,
            end_us=end_us,
        )
        spans = _extract_matching_spans(
            traces_obj=traces_obj,
            service=service,
            operation=operation,
            start_us=start_us,
            end_us=end_us,
        )
        if len(spans) > len(best):
            best = spans
        if len(spans) >= expected_min_count:
            return spans
        time.sleep(1.0)
    return best


def _send_request_batch(
    entry_url: str,
    repeat: int,
    timeout_sec: float,
    seq_start: int,
) -> tuple[dict[str, Any], int]:
    latencies_us: list[float] = []
    ok = 0
    errors = 0
    seq = seq_start
    responses = {}
    window_start_us = time.time_ns() // 1000
    for _ in range(repeat):
        t0_ns = time.perf_counter_ns()
        try:
            status, body = base._send_compose_post(entry_url, seq, timeout_sec)
            key = str(status) if base._is_valid_compose_post(status, body) else f"{status}/INVALID"
            if base._is_valid_compose_post(status, body):
                ok += 1
            else:
                errors += 1
            responses[key] = responses.get(key, 0) + 1
        except Exception as exc:
            errors += 1
            key = exc.__class__.__name__
            responses[key] = responses.get(key, 0) + 1
        latencies_us.append((time.perf_counter_ns() - t0_ns) / 1000.0)
        seq += 1
    window_end_us = time.time_ns() // 1000
    root_stats = _stats(latencies_us, aggregate_mode="mean", trim_fraction=0.0)
    root_stats["ok"] = float(ok)
    root_stats["errors"] = float(errors)
    root_stats["window_start_us"] = float(window_start_us)
    root_stats["window_end_us"] = float(window_end_us)
    root_stats["responses"] = responses
    return root_stats, seq


def _measure_unique_id_local_span(
    namespace: str,
    entry_url: str,
    jaeger_url: str,
    target_container: str,
    target_pod: str,
    override_dir: str,
    baseline_unique_id_spin_us: int,
    unique_id_spin_us: int,
    repeat: int,
    warmup_repeat: int,
    timeout_sec: float,
    settle_sec: float,
    query_timeout_sec: float,
    lookback: str,
    span_aggregate_mode: str,
    trim_fraction: float,
    seq_start: int,
) -> tuple[dict[str, Any], int]:
    current_target_pod = base._wait_for_single_ready_pod(namespace, f"service={base.TARGET_SERVICE}")[
        "metadata"
    ]["name"]
    if current_target_pod != target_pod:
        raise RuntimeError(
            f"target pod changed during runtime calibration: expected {target_pod}, got {current_target_pod}"
        )

    override_env_name = base.SPIN_ENV_BY_SERVICE[base.TARGET_SERVICE]
    if int(unique_id_spin_us) == int(baseline_unique_id_spin_us):
        override_state = _clear_runtime_spin_override(
            namespace=namespace,
            pod=target_pod,
            container=target_container,
            override_dir=override_dir,
            env_name=override_env_name,
        )
    else:
        override_state = _set_runtime_spin_override(
            namespace=namespace,
            pod=target_pod,
            container=target_container,
            override_dir=override_dir,
            env_name=override_env_name,
            value=int(unique_id_spin_us),
        )
    if settle_sec > 0:
        time.sleep(settle_sec)

    warmup_ok = 0
    warmup_errors = 0
    seq = seq_start
    for _ in range(warmup_repeat):
        try:
            status, body = base._send_compose_post(entry_url, seq, timeout_sec)
            if base._is_valid_compose_post(status, body):
                warmup_ok += 1
            else:
                warmup_errors += 1
        except Exception:
            warmup_errors += 1
        seq += 1

    root_batch, seq = _send_request_batch(
        entry_url=entry_url,
        repeat=repeat,
        timeout_sec=timeout_sec,
        seq_start=seq,
    )

    query_start_us = int(root_batch["window_start_us"])
    query_end_us = int(root_batch["window_end_us"]) + 750_000
    expected_min_spans = max(1, repeat)
    spans = _poll_matching_spans(
        jaeger_url=jaeger_url,
        service=base.TARGET_SERVICE,
        operation=JAEGER_OPERATION,
        start_us=query_start_us,
        end_us=query_end_us,
        expected_min_count=expected_min_spans,
        lookback=lookback,
        query_timeout_sec=query_timeout_sec,
        limit=max(200, repeat * 20),
    )
    if len(spans) > expected_min_spans:
        spans = spans[:expected_min_spans]
    if len(spans) < max(3, int(expected_min_spans * 0.8)):
        raise RuntimeError(
            "not enough matching unique-id spans from Jaeger: "
            f"expected~{expected_min_spans}, got={len(spans)}, "
            f"window_us=[{query_start_us}, {query_end_us}]"
        )

    span_durations_us = [float(x["duration_us"]) for x in spans]
    span_stats = _stats(
        span_durations_us,
        aggregate_mode=span_aggregate_mode,
        trim_fraction=trim_fraction,
    )
    return (
        {
            "unique_id_spin_us": int(unique_id_spin_us),
            "spin_snapshot": base._get_spin_snapshot(namespace),
            "target_pod": target_pod,
            "override_state": override_state,
            "warmup_ok": warmup_ok,
            "warmup_errors": warmup_errors,
            "request_ok": int(root_batch["ok"]),
            "request_errors": int(root_batch["errors"]),
            "request_responses": root_batch["responses"],
            "request_window_start_us": int(root_batch["window_start_us"]),
            "request_window_end_us": int(root_batch["window_end_us"]),
            "root_mean_us": root_batch["mean_us"],
            "root_median_us": root_batch["median_us"],
            "root_p95_us": root_batch["p95_us"],
            "root_max_us": root_batch["max_us"],
            "span_count": int(span_stats["count"]),
            "span_aggregate_us": span_stats["aggregate_us"],
            "span_mean_us": span_stats["mean_us"],
            "span_median_us": span_stats["median_us"],
            "span_p95_us": span_stats["p95_us"],
            "span_p99_us": span_stats["p99_us"],
            "span_min_us": span_stats["min_us"],
            "span_max_us": span_stats["max_us"],
            "span_sample_trace_ids": [x["trace_id"] for x in spans[:5]],
        },
        seq,
    )


def _measurement_brief(measurement: dict[str, Any]) -> dict[str, Any]:
    return {
        "spin_us": measurement["unique_id_spin_us"],
        "target_pod": measurement["target_pod"],
        "span_count": measurement["span_count"],
        "span_aggregate_us": round(float(measurement["span_aggregate_us"]), 3),
        "span_mean_us": round(float(measurement["span_mean_us"]), 3),
        "span_p95_us": round(float(measurement["span_p95_us"]), 3),
        "root_mean_us": round(float(measurement["root_mean_us"]), 3),
        "request_ok": measurement["request_ok"],
        "request_errors": measurement["request_errors"],
    }


def _choose_better_measurement(
    current_best: dict[str, Any] | None,
    candidate: dict[str, Any],
    target_delay_us: float,
) -> dict[str, Any]:
    if current_best is None:
        return candidate
    best_err = abs(float(current_best["delay_error_us"]))
    cand_err = abs(float(candidate["delay_error_us"]))
    if cand_err < best_err:
        return candidate
    if cand_err == best_err and int(candidate["unique_id_spin_us"]) > int(
        current_best["unique_id_spin_us"]
    ):
        return candidate
    return current_best


def _calibrate_unique_id_spin(
    namespace: str,
    entry_url: str,
    jaeger_url: str,
    baseline_spin: dict[str, int],
    target_container: str,
    target_pod: str,
    override_dir: str,
    fixed_delay_ns: int,
    calibration_repeat: int,
    calibration_warmup_repeat: int,
    timeout_sec: float,
    settle_sec: float,
    query_timeout_sec: float,
    lookback: str,
    span_aggregate_mode: str,
    trim_fraction: float,
    min_spin_us: int,
    max_spin_us: int,
    max_iterations: int,
    tolerance_us: float,
    seq_start: int,
) -> tuple[dict[str, Any], int]:
    target_delay_us = fixed_delay_ns / 1000.0
    baseline_measure, seq = _measure_unique_id_local_span(
        namespace=namespace,
        entry_url=entry_url,
        jaeger_url=jaeger_url,
        target_container=target_container,
        target_pod=target_pod,
        override_dir=override_dir,
        baseline_unique_id_spin_us=baseline_spin[base.TARGET_SERVICE],
        unique_id_spin_us=baseline_spin[base.TARGET_SERVICE],
        repeat=calibration_repeat,
        warmup_repeat=calibration_warmup_repeat,
        timeout_sec=timeout_sec,
        settle_sec=settle_sec,
        query_timeout_sec=query_timeout_sec,
        lookback=lookback,
        span_aggregate_mode=span_aggregate_mode,
        trim_fraction=trim_fraction,
        seq_start=seq_start,
    )
    baseline_local_us = float(baseline_measure["span_aggregate_us"])
    baseline_measure["local_delay_us"] = 0.0
    baseline_measure["delay_error_us"] = -target_delay_us
    print(f"calibration baseline={_measurement_brief(baseline_measure)}")

    if target_delay_us <= 0:
        return (
            {
                "target_delay_us": target_delay_us,
                "selected_spin_us": baseline_spin[base.TARGET_SERVICE],
                "selected_added_delay_us": 0.0,
                "selected_error_us": -target_delay_us,
                "selected_measure": baseline_measure,
                "baseline_measure": baseline_measure,
                "measurements": [baseline_measure],
                "aggregate_mode": span_aggregate_mode,
                "trim_fraction": trim_fraction,
                "monotonicity_broken": False,
                "search_note": "fixedDelayNs <= 0; reused baseline spin",
            },
            seq,
        )

    baseline_spin_us = int(baseline_spin[base.TARGET_SERVICE])
    low_spin_us = baseline_spin_us
    high_spin_us = max(baseline_spin_us + 1, int(max_spin_us))
    measurements_by_spin: dict[int, dict[str, Any]] = {
        baseline_spin_us: baseline_measure,
    }
    best = baseline_measure

    if high_spin_us <= baseline_spin_us:
        return (
            {
                "target_delay_us": target_delay_us,
                "selected_spin_us": baseline_spin_us,
                "selected_added_delay_us": 0.0,
                "selected_error_us": -target_delay_us,
                "selected_measure": baseline_measure,
                "baseline_measure": baseline_measure,
                "measurements": [baseline_measure],
                "aggregate_mode": span_aggregate_mode,
                "trim_fraction": trim_fraction,
                "monotonicity_broken": False,
                "search_note": "max_spin_us <= baseline spin; no upward calibration range",
            },
            seq,
        )

    if min_spin_us > 0 and min_spin_us != baseline_spin_us:
        print(
            f"calibration note={{'ignored_min_spin_us': {min_spin_us}, "
            f"'reason': 'delay matching searches only upward from baseline'}}"
        )

    upper_measure = None
    probe_spin = min(high_spin_us, max(baseline_spin_us + 1, baseline_spin_us * 2))
    expansion_count = 0
    while True:
        probe_measure, seq = _measure_unique_id_local_span(
            namespace=namespace,
            entry_url=entry_url,
            jaeger_url=jaeger_url,
            target_container=target_container,
            target_pod=target_pod,
            override_dir=override_dir,
            baseline_unique_id_spin_us=baseline_spin[base.TARGET_SERVICE],
            unique_id_spin_us=probe_spin,
            repeat=calibration_repeat,
            warmup_repeat=calibration_warmup_repeat,
            timeout_sec=timeout_sec,
            settle_sec=settle_sec,
            query_timeout_sec=query_timeout_sec,
            lookback=lookback,
            span_aggregate_mode=span_aggregate_mode,
            trim_fraction=trim_fraction,
            seq_start=seq,
        )
        probe_measure["local_delay_us"] = float(probe_measure["span_aggregate_us"]) - baseline_local_us
        probe_measure["delay_error_us"] = probe_measure["local_delay_us"] - target_delay_us
        measurements_by_spin[probe_spin] = probe_measure
        best = _choose_better_measurement(best, probe_measure, target_delay_us)
        expansion_count += 1
        label = "upper" if probe_spin == high_spin_us else f"expand={expansion_count}"
        print(f"calibration {label}={_measurement_brief(probe_measure)}")
        upper_measure = probe_measure
        if abs(float(probe_measure["delay_error_us"])) <= tolerance_us:
            best = probe_measure
            break
        if float(probe_measure["local_delay_us"]) >= target_delay_us:
            break
        if probe_spin >= high_spin_us:
            break
        probe_spin = min(high_spin_us, max(probe_spin + 1, probe_spin * 2))

    max_added_delay_us = float((upper_measure or baseline_measure).get("local_delay_us", 0.0))
    if max_added_delay_us <= target_delay_us + tolerance_us:
        selected = best
        return (
            {
                "target_delay_us": target_delay_us,
                "selected_spin_us": int(selected["unique_id_spin_us"]),
                "selected_added_delay_us": float(selected["local_delay_us"]),
                "selected_error_us": float(selected["delay_error_us"]),
                "selected_measure": selected,
                "baseline_measure": baseline_measure,
                "measurements": list(
                    sorted(measurements_by_spin.values(), key=lambda x: int(x["unique_id_spin_us"]))
                ),
                "aggregate_mode": span_aggregate_mode,
                "trim_fraction": trim_fraction,
                "monotonicity_broken": any(
                    int(m["unique_id_spin_us"]) > baseline_spin_us
                    and float(m.get("local_delay_us", 0.0)) < -tolerance_us
                    for m in measurements_by_spin.values()
                ),
                "search_note": "target delay exceeds max measurable local delay; selected best available",
            },
            seq,
        )

    lo_spin = baseline_spin_us
    hi_spin = int((upper_measure or baseline_measure)["unique_id_spin_us"])
    for iteration in range(1, max_iterations + 1):
        if hi_spin - lo_spin <= 1:
            break
        mid_spin = (lo_spin + hi_spin) // 2
        if mid_spin in measurements_by_spin:
            break
        mid_measure, seq = _measure_unique_id_local_span(
            namespace=namespace,
            entry_url=entry_url,
            jaeger_url=jaeger_url,
            target_container=target_container,
            target_pod=target_pod,
            override_dir=override_dir,
            baseline_unique_id_spin_us=baseline_spin[base.TARGET_SERVICE],
            unique_id_spin_us=mid_spin,
            repeat=calibration_repeat,
            warmup_repeat=calibration_warmup_repeat,
            timeout_sec=timeout_sec,
            settle_sec=settle_sec,
            query_timeout_sec=query_timeout_sec,
            lookback=lookback,
            span_aggregate_mode=span_aggregate_mode,
            trim_fraction=trim_fraction,
            seq_start=seq,
        )
        mid_measure["local_delay_us"] = float(mid_measure["span_aggregate_us"]) - baseline_local_us
        mid_measure["delay_error_us"] = mid_measure["local_delay_us"] - target_delay_us
        measurements_by_spin[mid_spin] = mid_measure
        best = _choose_better_measurement(best, mid_measure, target_delay_us)
        print(f"calibration iter={iteration} mid={_measurement_brief(mid_measure)}")
        if abs(float(mid_measure["delay_error_us"])) <= tolerance_us:
            best = mid_measure
            break
        if float(mid_measure["local_delay_us"]) < target_delay_us:
            lo_spin = mid_spin
        else:
            hi_spin = mid_spin

    selected = best
    selected_spin_us = int(selected["unique_id_spin_us"])
    monotonicity_broken = any(
        int(m["unique_id_spin_us"]) > baseline_spin_us
        and float(m.get("local_delay_us", 0.0)) < -tolerance_us
        for m in measurements_by_spin.values()
    )
    return (
        {
            "target_delay_us": target_delay_us,
            "selected_spin_us": selected_spin_us,
            "selected_added_delay_us": float(selected["local_delay_us"]),
            "selected_error_us": float(selected["delay_error_us"]),
            "selected_measure": selected,
            "baseline_measure": baseline_measure,
            "measurements": list(
                sorted(measurements_by_spin.values(), key=lambda x: int(x["unique_id_spin_us"]))
            ),
            "aggregate_mode": span_aggregate_mode,
            "trim_fraction": trim_fraction,
            "monotonicity_broken": monotonicity_broken,
            "search_note": "binary search on unique-id spin using local server span increase",
        },
        seq,
    )


def _calibration_json_path(results_dir: str, fixed_delay_ns: int) -> str:
    return os.path.join(results_dir, f"calibration_delay-{fixed_delay_ns}.json")


def _write_json(path: str, obj: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, sort_keys=True)


def regenerate_summary(results_dir: str) -> str:
    rows = []
    for path in sorted(glob.glob(os.path.join(results_dir, "result_delay-*.csv"))):
        meta = None
        case_rows = {}
        analysis = None
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if row["row_type"] == "summary_meta":
                    meta = row
                elif row["row_type"] == "summary_case":
                    case_rows[row["case"]] = row
                elif row["row_type"] == "summary_analysis":
                    analysis = row
        if not meta or not analysis:
            continue
        delay_ns = int(meta["file_delay_ns"])
        cal_path = _calibration_json_path(results_dir, delay_ns)
        cal = {}
        if os.path.exists(cal_path):
            with open(cal_path, encoding="utf-8") as handle:
                cal = json.load(handle)
        selected_measure = cal.get("selected_measure") if isinstance(cal, dict) else {}
        rows.append(
            {
                "csv_file": os.path.basename(path),
                "calibration_json": os.path.basename(cal_path) if cal else "",
                "fixed_delay_ns": meta["file_delay_ns"],
                "baseline_text_spin_us": meta["baseline_text_spin_us"],
                "baseline_user_spin_us": meta["baseline_user_spin_us"],
                "baseline_media_spin_us": meta["baseline_media_spin_us"],
                "baseline_unique_id_spin_us": meta["baseline_unique_id_spin_us"],
                "actual_unique_id_spin_us": meta["actual_unique_id_spin_us"],
                "calibration_target_delay_us": cal.get("target_delay_us", ""),
                "calibration_selected_local_delay_us": cal.get("selected_added_delay_us", ""),
                "calibration_selected_error_us": cal.get("selected_error_us", ""),
                "calibration_baseline_local_span_us": (
                    (cal.get("baseline_measure") or {}).get("span_aggregate_us", "")
                ),
                "calibration_actual_local_span_us": selected_measure.get("span_aggregate_us", "")
                if isinstance(selected_measure, dict)
                else "",
                "target_pod": meta["target_pod"],
                "case1_avg_us": (case_rows.get(base.CASE_ORDER[0]) or {}).get(
                    "case_selected_avg_us", ""
                ),
                "case2_avg_us": (case_rows.get(base.CASE_ORDER[1]) or {}).get(
                    "case_selected_avg_us", ""
                ),
                "case3_avg_us": (case_rows.get(base.CASE_ORDER[2]) or {}).get(
                    "case_selected_avg_us", ""
                ),
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
            "calibration_json",
            "fixed_delay_ns",
            "baseline_text_spin_us",
            "baseline_user_spin_us",
            "baseline_media_spin_us",
            "baseline_unique_id_spin_us",
            "actual_unique_id_spin_us",
            "calibration_target_delay_us",
            "calibration_selected_local_delay_us",
            "calibration_selected_error_us",
            "calibration_baseline_local_span_us",
            "calibration_actual_local_span_us",
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run SocialNetwork Path 1 request-credit experiment with automatic "
            "matching of fixedDelayNs to unique-id-service local span increase."
        )
    )
    parser.add_argument("--namespace", default="social")
    parser.add_argument("--entry-service", default=base.ENTRY_SERVICE)
    parser.add_argument("--entry-path", default=base.ENTRY_PATH)
    parser.add_argument("--entry-url", default="")
    parser.add_argument("--jaeger-url", default="")
    parser.add_argument("--mcoz-url", default="http://127.0.0.1:19091")
    parser.add_argument("--fixed-delay-ns", type=int, required=True)
    parser.add_argument("--baseline-text-spin-us", type=int, default=200)
    parser.add_argument("--baseline-user-spin-us", type=int, default=850)
    parser.add_argument("--baseline-media-spin-us", type=int, default=850)
    parser.add_argument("--baseline-unique-id-spin-us", type=int, default=1260)
    parser.add_argument("--repeat", type=int, default=100)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup-repeat", type=int, default=3)
    parser.add_argument("--request-timeout-sec", type=float, default=10.0)
    parser.add_argument("--tail-drop-top-runs", type=int, default=0)
    parser.add_argument("--aggregate-mode", choices=["mean", "median"], default="mean")
    parser.add_argument("--force-start", action="store_true")
    parser.add_argument("--target-container", default=base.TARGET_SERVICE)
    parser.add_argument("--runtime-override-dir", default=DEFAULT_RUNTIME_OVERRIDE_DIR)
    parser.add_argument("--apply-policy-duration-ms", type=int, default=2000)
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--csv-out", default="")
    parser.add_argument("--no-restore-baseline-after", action="store_true")
    parser.add_argument("--calibration-repeat", type=int, default=30)
    parser.add_argument("--calibration-warmup-repeat", type=int, default=10)
    parser.add_argument("--calibration-settle-sec", type=float, default=2.0)
    parser.add_argument("--calibration-query-timeout-sec", type=float, default=12.0)
    parser.add_argument("--calibration-lookback", default="1h")
    parser.add_argument(
        "--calibration-span-aggregate",
        choices=["mean", "median", "trimmed-mean"],
        default="trimmed-mean",
    )
    parser.add_argument("--calibration-trim-fraction", type=float, default=0.1)
    parser.add_argument("--calibration-min-spin-us", type=int, default=0)
    parser.add_argument(
        "--calibration-max-spin-us",
        type=int,
        default=-1,
        help="upper bound for unique-id spin search; default -1 means max(baseline*4, baseline+2000)",
    )
    parser.add_argument("--calibration-max-iterations", type=int, default=6)
    parser.add_argument("--calibration-tolerance-us", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.fixed_delay_ns < 0:
        raise SystemExit("fixed-delay-ns must be >= 0")
    if args.repeat <= 0 or args.runs <= 0:
        raise SystemExit("repeat/runs must be > 0")
    if args.tail_drop_top_runs < 0 or args.tail_drop_top_runs >= args.runs:
        raise SystemExit("tail-drop-top-runs must satisfy 0 <= drop < runs")
    if args.calibration_repeat <= 0:
        raise SystemExit("calibration-repeat must be > 0")
    if not (0.0 <= args.calibration_trim_fraction < 0.5):
        raise SystemExit("calibration-trim-fraction must satisfy 0 <= x < 0.5")
    if args.calibration_max_iterations <= 0:
        raise SystemExit("calibration-max-iterations must be > 0")

    baseline_spin = {
        "text-service": args.baseline_text_spin_us,
        "user-service": args.baseline_user_spin_us,
        "media-service": args.baseline_media_spin_us,
        "unique-id-service": args.baseline_unique_id_spin_us,
    }

    entry_url = args.entry_url or base._get_entry_url(
        args.namespace, args.entry_service, args.entry_path
    )
    jaeger_url = args.jaeger_url or _get_jaeger_url(args.namespace)
    print(f"entry_url={entry_url}")
    print(f"jaeger_url={jaeger_url}")
    print(f"baseline_spin={baseline_spin}")
    print(f"fixed_delay_ns={args.fixed_delay_ns}")

    all_rows = []
    policy_state: dict[str, object] = {}
    seq = 1
    if args.csv_out:
        csv_out = args.csv_out
    else:
        os.makedirs(args.results_dir, exist_ok=True)
        csv_out = os.path.join(args.results_dir, f"result_delay-{args.fixed_delay_ns}.csv")
    calibration_path = _calibration_json_path(args.results_dir, args.fixed_delay_ns)

    try:
        baseline_snapshot = base._set_spin_env(args.namespace, baseline_spin)
        baseline_target_pod = base._wait_for_single_ready_pod(
            args.namespace, f"service={base.TARGET_SERVICE}"
        )["metadata"]["name"]
        cleared_override = _clear_runtime_spin_override(
            namespace=args.namespace,
            pod=baseline_target_pod,
            container=args.target_container,
            override_dir=args.runtime_override_dir,
            env_name=base.SPIN_ENV_BY_SERVICE[base.TARGET_SERVICE],
        )
        print(f"baseline_spin_snapshot={baseline_snapshot}")
        print(
            "baseline_runtime_override="
            f"{{'target_pod': '{baseline_target_pod}', 'state': {cleared_override}}}"
        )

        case1, seq = base.run_case(
            name=base.CASE_ORDER[0],
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
            all_rows=all_rows,
            policy_state=policy_state,
            seq_start=seq,
        )
        base._cleanup_between_cases(args.mcoz_url, policy_state, "between_case1_case2")
        case2, seq = base.run_case(
            name=base.CASE_ORDER[1],
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
            all_rows=all_rows,
            policy_state=policy_state,
            seq_start=seq,
        )
        base._cleanup_between_cases(args.mcoz_url, policy_state, "between_case2_case3")
        max_spin_us = (
            args.calibration_max_spin_us
            if args.calibration_max_spin_us > 0
            else max(
                args.baseline_unique_id_spin_us * 4,
                args.baseline_unique_id_spin_us + 2000,
            )
        )
        calibration, seq = _calibrate_unique_id_spin(
            namespace=args.namespace,
            entry_url=entry_url,
            jaeger_url=jaeger_url,
            baseline_spin=baseline_spin,
            target_container=args.target_container,
            target_pod=case2["target_pod"],
            override_dir=args.runtime_override_dir,
            fixed_delay_ns=args.fixed_delay_ns,
            calibration_repeat=args.calibration_repeat,
            calibration_warmup_repeat=args.calibration_warmup_repeat,
            timeout_sec=args.request_timeout_sec,
            settle_sec=args.calibration_settle_sec,
            query_timeout_sec=args.calibration_query_timeout_sec,
            lookback=args.calibration_lookback,
            span_aggregate_mode=args.calibration_span_aggregate,
            trim_fraction=args.calibration_trim_fraction,
            min_spin_us=args.calibration_min_spin_us,
            max_spin_us=max_spin_us,
            max_iterations=args.calibration_max_iterations,
            tolerance_us=args.calibration_tolerance_us,
            seq_start=seq,
        )
        actual_spin = dict(baseline_spin)
        actual_spin["unique-id-service"] = int(calibration["selected_spin_us"])
        calibration["actual_spin"] = actual_spin
        calibration["baseline_spin"] = baseline_spin
        calibration["fixed_delay_ns"] = args.fixed_delay_ns
        calibration["entry_url"] = entry_url
        calibration["jaeger_url"] = jaeger_url
        _write_json(calibration_path, calibration)

        print(
            "calibration selected="
            f"{{'unique_id_spin_us': {calibration['selected_spin_us']}, "
            f"'target_delay_us': {calibration['target_delay_us']:.3f}, "
            f"'selected_local_delay_us': {calibration['selected_added_delay_us']:.3f}, "
            f"'selected_error_us': {calibration['selected_error_us']:+.3f}}}"
        )
        print(f"actual_spin={actual_spin}")
        if calibration.get("monotonicity_broken"):
            raise RuntimeError(
                "calibration invalid: increasing UNIQUE_ID_SERVICE_SPIN_US produced a smaller "
                "compose_unique_id_server span on at least one rollout. This means per-pod "
                "spin calibration is unstable, so matched actual-delay cannot be trusted."
            )
        if (
            args.fixed_delay_ns > 0
            and int(calibration["selected_spin_us"]) == int(baseline_spin["unique-id-service"])
        ):
            raise RuntimeError(
                "calibration failed: could not find a non-baseline unique-id spin that matches "
                "the requested fixed delay. Refusing to run Case 3 with a rolled baseline pod."
            )

        actual_override = _set_runtime_spin_override(
            namespace=args.namespace,
            pod=case2["target_pod"],
            container=args.target_container,
            override_dir=args.runtime_override_dir,
            env_name=base.SPIN_ENV_BY_SERVICE[base.TARGET_SERVICE],
            value=int(calibration["selected_spin_us"]),
        )
        print(
            "case3_runtime_override="
            f"{{'target_pod': '{case2['target_pod']}', 'state': {actual_override}}}"
        )
        case3, seq = base.run_case(
            name=base.CASE_ORDER[2],
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
            all_rows=all_rows,
            policy_state=policy_state,
            seq_start=seq,
        )

        case_results = {
            base.CASE_ORDER[0]: case1,
            base.CASE_ORDER[1]: case2,
            base.CASE_ORDER[2]: case3,
        }
        case_stats = base.compute_case_stats(all_rows, selected_only=True)
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
        rows = base.build_output_rows(all_rows, case_stats, case_results, analysis, meta)
        base.write_csv(csv_out, rows)
        summary_path = regenerate_summary(args.results_dir)

        print("\n=== summary ===")
        print(f"{base.CASE_ORDER[0]} avg_ms={case1['avg_us'] / 1000.0:.3f} runs_us={case1['runs_us']}")
        print(f"{base.CASE_ORDER[1]} avg_ms={case2['avg_us'] / 1000.0:.3f} runs_us={case2['runs_us']}")
        print(f"{base.CASE_ORDER[2]} avg_ms={case3['avg_us'] / 1000.0:.3f} runs_us={case3['runs_us']}")
        print(f"fixed_delay_ms={args.fixed_delay_ns / 1_000_000.0:.3f}")
        print(f"virtual_gain_ms={analysis['virtual_gain_ms']:.3f}")
        print(f"actual_gain_ms={analysis['actual_gain_ms']:.3f}")
        print(f"signed_error_ms={analysis['signed_error_ms']:+.3f}")
        print(f"signed_error_rate_pct={analysis['signed_error_rate_pct']:+.3f}")
        print(
            "matched_local_delay_us="
            f"{calibration['selected_added_delay_us']:.3f} "
            f"(target={calibration['target_delay_us']:.3f}, "
            f"error={calibration['selected_error_us']:+.3f})"
        )
        print(f"matched_unique_id_spin_us={calibration['selected_spin_us']}")
        print(f"policy_applied_paths={policy_state.get('applied_paths', [])}")
        print(f"csv_out={csv_out}")
        print(f"calibration_out={calibration_path}")
        print(f"summary_out={summary_path}")
        return 0
    finally:
        cleanup = base._cleanup_mcoz(args.mcoz_url)
        restore = {"ok": True, "skipped": bool(args.no_restore_baseline_after)}
        if not args.no_restore_baseline_after:
            try:
                base._set_spin_env(args.namespace, baseline_spin)
                restored_target_pod = base._wait_for_single_ready_pod(
                    args.namespace, f"service={base.TARGET_SERVICE}"
                )["metadata"]["name"]
                cleared_override = _clear_runtime_spin_override(
                    namespace=args.namespace,
                    pod=restored_target_pod,
                    container=args.target_container,
                    override_dir=args.runtime_override_dir,
                    env_name=base.SPIN_ENV_BY_SERVICE[base.TARGET_SERVICE],
                )
                base._set_gate_targets(args.namespace, restored_target_pod, args.target_container)
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
