#!/usr/bin/env python3
"""
Analyze SocialNetwork compose-post traces with four latency metrics:

1. root latency
2. compose-post-service latency
3. Path1 max latency
4. target local latency

The script expects per-case CSVs produced by mcoz_social_path1_single_case.py.
It uses the recorded measure_start_us / measure_end_us windows to select traces
from Jaeger for each case.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from mcoz_social_paths import default_results_dir


DEFAULT_RESULTS_DIR = default_results_dir("path1_target_unique-id-separate")

ROOT_SERVICE = "nginx-web-server"
ROOT_OPERATION = "/wrk2-api/post/compose"
PATH1_SERVICES = {
    "text_us": ("text-service", "compose_text_server"),
    "user_us": ("user-service", "compose_creator_server"),
    "media_us": ("media-service", "compose_media_server"),
    "unique_us": ("unique-id-service", "compose_unique_id_server"),
}
OTHER_METRICS = {
    "compose_post_us": ("compose-post-service", "compose_post_server"),
    "user_timeline_us": ("user-timeline-service", "write_user_timeline_server"),
    "home_timeline_us": ("home-timeline-service", "write_home_timeline_server"),
    "post_storage_us": ("post-storage-service", "store_post_server"),
}


@dataclass
class CaseWindow:
    case_id: str
    case_label: str
    csv_file: str
    entry_url: str
    target_pod: str
    spin_snapshot_json: str
    measure_start_us: int
    measure_end_us: int


def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True).strip()


def _default_jaeger_url(namespace: str) -> str:
    host = _run(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "svc",
            "jaeger",
            "-o",
            "jsonpath={.spec.clusterIP}",
        ]
    )
    return f"http://{host}:16686"


def _parse_thresholds(raw: str) -> list[float]:
    values = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        values.append(float(item))
    if not values:
        raise SystemExit("at least one threshold is required")
    return values


def _percentile(sorted_xs: list[float], q: float) -> float | None:
    if not sorted_xs:
        return None
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = (len(sorted_xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_xs[lo]
    frac = pos - lo
    return sorted_xs[lo] * (1.0 - frac) + sorted_xs[hi] * frac


def _wasserstein_1d(xs: list[float], ys: list[float]) -> float | None:
    if not xs or not ys:
        return None
    sx = sorted(xs)
    sy = sorted(ys)
    n = max(len(sx), len(sy))
    if n == 1:
        return abs(sx[0] - sy[0])
    total = 0.0
    for i in range(n):
        q = i / (n - 1)
        xv = _percentile(sx, q)
        yv = _percentile(sy, q)
        assert xv is not None and yv is not None
        total += abs(xv - yv)
    return total / n


def _read_case_windows(results_dir: str) -> list[CaseWindow]:
    windows: list[CaseWindow] = []
    for name in ("result_case1.csv", "result_case2.csv", "result_case3.csv"):
        path = os.path.join(results_dir, name)
        if not os.path.exists(path):
            continue
        meta = None
        summary = None
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row["row_type"] == "summary_meta":
                    meta = row
                elif row["row_type"] == "summary_case":
                    summary = row
                if meta and summary:
                    break
        if not meta or not summary:
            raise SystemExit(
                f"{path} is missing summary_meta/summary_case; rerun with the updated single-case script"
            )
        windows.append(
            CaseWindow(
                case_id=summary["case_id"],
                case_label=summary["case_label"],
                csv_file=path,
                entry_url=meta["entry_url"],
                target_pod=summary["target_pod"],
                spin_snapshot_json=summary["spin_snapshot_json"],
                measure_start_us=int(meta["measure_start_us"]),
                measure_end_us=int(meta["measure_end_us"]),
            )
        )
    if not windows:
        raise SystemExit(f"no result_case*.csv files found in {results_dir}")
    windows.sort(key=lambda item: item.measure_start_us)
    return windows


def _fetch_traces(jaeger_url: str, lookback: str, limit: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "service": ROOT_SERVICE,
            "operation": ROOT_OPERATION,
            "lookback": lookback,
            "limit": str(limit),
        }
    )
    with urllib.request.urlopen(f"{jaeger_url.rstrip('/')}/api/traces?{params}", timeout=20) as resp:
        payload = json.load(resp)
    return payload.get("data", [])


def _extract_trace_metrics(trace: dict[str, Any]) -> dict[str, Any] | None:
    processes = trace["processes"]
    root_candidates: list[dict[str, Any]] = []
    spans_by_metric: dict[str, list[dict[str, Any]]] = {
        "compose_post_us": [],
        "user_timeline_us": [],
        "home_timeline_us": [],
        "post_storage_us": [],
        "text_us": [],
        "user_us": [],
        "media_us": [],
        "unique_us": [],
    }
    for span in trace["spans"]:
        service = processes[span["processID"]]["serviceName"]
        operation = span["operationName"]
        if service == ROOT_SERVICE and operation == ROOT_OPERATION:
            root_candidates.append(span)
            continue
        for metric, (want_service, want_operation) in OTHER_METRICS.items():
            if service == want_service and operation == want_operation:
                spans_by_metric[metric].append(span)
        for metric, (want_service, want_operation) in PATH1_SERVICES.items():
            if service == want_service and operation == want_operation:
                spans_by_metric[metric].append(span)
    if not root_candidates:
        return None
    root_span = min(root_candidates, key=lambda sp: (sp["startTime"], -sp["duration"]))
    metrics: dict[str, Any] = {
        "trace_id": trace["traceID"],
        "root_start_us": root_span["startTime"],
        "root_us": root_span["duration"],
    }
    for metric, spans in spans_by_metric.items():
        if not spans:
            metrics[metric] = None
            continue
        chosen = max(spans, key=lambda sp: sp["duration"])
        metrics[metric] = chosen["duration"]
    path_vals = [
        metrics["text_us"],
        metrics["user_us"],
        metrics["media_us"],
        metrics["unique_us"],
    ]
    path_vals = [float(v) for v in path_vals if v is not None]
    metrics["path1_max_us"] = max(path_vals) if path_vals else None
    metrics["target_local_us"] = metrics["unique_us"]
    return metrics


def _quantile_stats(xs: list[float], thresholds: list[float]) -> dict[str, Any]:
    if not xs:
        return {
            "count": 0,
            "mean_us": None,
            "stdev_us": None,
            "median_us": None,
            "p95_us": None,
            "p99_us": None,
            **{f"gt_{int(t)}_rate": None for t in thresholds},
        }
    sorted_xs = sorted(xs)
    stats = {
        "count": len(xs),
        "mean_us": statistics.mean(xs),
        "stdev_us": statistics.pstdev(xs) if len(xs) > 1 else 0.0,
        "median_us": statistics.median(xs),
        "p95_us": _percentile(sorted_xs, 0.95),
        "p99_us": _percentile(sorted_xs, 0.99),
    }
    for t in thresholds:
        stats[f"gt_{int(t)}_rate"] = sum(1 for x in xs if x > t) / len(xs)
    return stats


def _write_trace_csv(path: str, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "trace_id",
        "root_start_us",
        "root_us",
        "compose_post_us",
        "path1_max_us",
        "target_local_us",
        "text_us",
        "user_us",
        "media_us",
        "unique_us",
        "user_timeline_us",
        "home_timeline_us",
        "post_storage_us",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze SocialNetwork case traces with 4 latency metrics."
    )
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--namespace", default="social")
    parser.add_argument("--jaeger-url", default="")
    parser.add_argument("--lookback", default="2h")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--window-slack-us", type=int, default=200000)
    parser.add_argument("--thresholds-us", default="10000,20000,50000")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    thresholds = _parse_thresholds(args.thresholds_us)
    jaeger_url = args.jaeger_url or _default_jaeger_url(args.namespace)
    windows = _read_case_windows(args.results_dir)
    traces = _fetch_traces(jaeger_url, args.lookback, args.limit)

    case_rows: dict[str, list[dict[str, Any]]] = {w.case_id: [] for w in windows}
    unmatched = 0
    for trace in traces:
        metrics = _extract_trace_metrics(trace)
        if not metrics:
            continue
        t = metrics["root_start_us"]
        matched = False
        for window in windows:
            if (
                window.measure_start_us - args.window_slack_us
                <= t
                <= window.measure_end_us + args.window_slack_us
            ):
                case_rows[window.case_id].append(metrics)
                matched = True
                break
        if not matched:
            unmatched += 1

    metric_keys = [
        ("root_us", "root"),
        ("compose_post_us", "compose_post"),
        ("path1_max_us", "path1_max"),
        ("target_local_us", "target_local"),
    ]
    summary_rows: list[dict[str, Any]] = []
    metrics_by_case: dict[str, dict[str, list[float]]] = {}
    for window in windows:
        rows = sorted(case_rows[window.case_id], key=lambda row: row["root_start_us"])
        trace_csv = os.path.join(args.results_dir, f"trace_metrics_{window.case_id}.csv")
        _write_trace_csv(trace_csv, rows)
        metrics_by_case[window.case_id] = {}
        for metric_key, metric_name in metric_keys:
            xs = [float(row[metric_key]) for row in rows if row.get(metric_key) is not None]
            metrics_by_case[window.case_id][metric_name] = xs
            stats = _quantile_stats(xs, thresholds)
            summary_rows.append(
                {
                    "case_id": window.case_id,
                    "case_label": window.case_label,
                    "metric": metric_name,
                    "trace_count": len(rows),
                    "sample_count": stats["count"],
                    "mean_us": stats["mean_us"],
                    "stdev_us": stats["stdev_us"],
                    "median_us": stats["median_us"],
                    "p95_us": stats["p95_us"],
                    "p99_us": stats["p99_us"],
                    **{f"gt_{int(t)}_rate": stats[f"gt_{int(t)}_rate"] for t in thresholds},
                }
            )

    pair_rows: list[dict[str, Any]] = []
    case_ids = [w.case_id for w in windows]
    for metric_key, metric_name in metric_keys:
        for i in range(len(case_ids)):
            for j in range(i + 1, len(case_ids)):
                a = case_ids[i]
                b = case_ids[j]
                xs = metrics_by_case.get(a, {}).get(metric_name, [])
                ys = metrics_by_case.get(b, {}).get(metric_name, [])
                mean_a = statistics.mean(xs) if xs else None
                mean_b = statistics.mean(ys) if ys else None
                med_a = statistics.median(xs) if xs else None
                med_b = statistics.median(ys) if ys else None
                pair_rows.append(
                    {
                        "metric": metric_name,
                        "case_a": a,
                        "case_b": b,
                        "mean_shift_us": (mean_b - mean_a) if mean_a is not None and mean_b is not None else None,
                        "median_shift_us": (med_b - med_a) if med_a is not None and med_b is not None else None,
                        "wasserstein_us": _wasserstein_1d(xs, ys),
                    }
                )

    summary_csv = os.path.join(args.results_dir, "four_metrics_summary.csv")
    with open(summary_csv, "w", newline="", encoding="utf-8") as handle:
        fieldnames = list(summary_rows[0].keys()) if summary_rows else [
            "case_id",
            "case_label",
            "metric",
            "trace_count",
            "sample_count",
            "mean_us",
            "stdev_us",
            "median_us",
            "p95_us",
            "p99_us",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary_rows)

    pair_csv = os.path.join(args.results_dir, "four_metrics_pairs.csv")
    with open(pair_csv, "w", newline="", encoding="utf-8") as handle:
        fieldnames = ["metric", "case_a", "case_b", "mean_shift_us", "median_shift_us", "wasserstein_us"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(pair_rows)

    summary_json = os.path.join(args.results_dir, "four_metrics_summary.json")
    with open(summary_json, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "generated_at_us": int(time.time() * 1_000_000),
                "results_dir": args.results_dir,
                "jaeger_url": jaeger_url,
                "lookback": args.lookback,
                "limit": args.limit,
                "window_slack_us": args.window_slack_us,
                "thresholds_us": thresholds,
                "unmatched_traces": unmatched,
                "cases": [
                    {
                        "case_id": w.case_id,
                        "case_label": w.case_label,
                        "target_pod": w.target_pod,
                        "measure_start_us": w.measure_start_us,
                        "measure_end_us": w.measure_end_us,
                        "spin_snapshot_json": w.spin_snapshot_json,
                        "trace_count": len(case_rows[w.case_id]),
                    }
                    for w in windows
                ],
                "summary_rows": summary_rows,
                "pair_rows": pair_rows,
            },
            handle,
            indent=2,
        )

    print(f"jaeger_url={jaeger_url}")
    for window in windows:
        print(
            f"{window.case_id}: traces={len(case_rows[window.case_id])} "
            f"window=[{window.measure_start_us}, {window.measure_end_us}]"
        )
    print(f"summary_csv={summary_csv}")
    print(f"pair_csv={pair_csv}")
    print(f"summary_json={summary_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
