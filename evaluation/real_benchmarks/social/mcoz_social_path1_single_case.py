#!/usr/bin/env python3
"""
Measure one SocialNetwork Path 1 case without changing any timing configuration.

This script only:
- warms up by sending requests
- measures request latencies
- records the current live deployment snapshot

It does not:
- start/stop/clear mcoz
- apply syscall_profile
- change fixed delays
- change spin values
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import time
from typing import Any

from mcoz_social_paths import default_results_dir
from mcoz_social_path1_unique_id_experiment import (
    ENTRY_PATH,
    ENTRY_SERVICE,
    TARGET_SERVICE,
    _get_entry_url,
    _get_spin_snapshot,
    _is_valid_compose_post,
    _measure_once,
    _probe_gate_targets,
    _send_compose_post,
    _wait_for_single_ready_pod,
    aggregate,
    compute_case_stats,
    pick_runs,
)


DEFAULT_RESULTS_DIR = default_results_dir("path1_target_unique-id-separate")

CASE_LABELS = {
    "case1": "Case 1 - baseline delay0",
    "case2": "Case 2 - virtual external",
    "case3": "Case 3 - actual external",
}


def _result_fields() -> list[str]:
    return [
        "row_type",
        "case_id",
        "case_label",
        "entry_url",
        "warmup_start_us",
        "warmup_end_us",
        "measure_start_us",
        "measure_end_us",
        "run",
        "iter",
        "run_selected",
        "latency_us",
        "latency_ns",
        "response",
        "target_pod",
        "spin_snapshot_json",
        "sibling_gate_pods",
        "case_count",
        "case_selected_avg_us",
        "case_mean_us",
        "case_variance_us2",
        "case_stddev_us",
        "case_t95_us",
        "case_t99_us",
        "case_max_us",
    ]


def _write_csv(path: str, rows: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_result_fields())
        writer.writeheader()
        writer.writerows(rows)


def _warmup_only(
    entry_url: str,
    warmup_repeat: int,
    timeout_sec: float,
    seq_start: int,
) -> tuple[dict[str, Any], int]:
    if warmup_repeat <= 0:
        return {
            "enabled": False,
            "ok": True,
            "warmup_ok": 0,
            "warmup_errors": 0,
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

    return {
        "enabled": True,
        "ok": err == 0,
        "warmup_ok": ok,
        "warmup_errors": err,
    }, seq


def _build_rows(
    case_id: str,
    case_label: str,
    entry_url: str,
    all_rows: list[dict[str, Any]],
    case_stats: dict[str, dict[str, float]],
    avg_us: float,
    target_pod: str,
    spin_snapshot: dict[str, str],
    sibling_gate_pods: str,
    warmup_start_us: int,
    warmup_end_us: int,
    measure_start_us: int,
    measure_end_us: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    stats = case_stats.get(case_label, {})
    spin_json = json.dumps(spin_snapshot, sort_keys=True)

    rows.append(
        {
            "row_type": "summary_meta",
            "case_id": case_id,
            "case_label": case_label,
            "entry_url": entry_url,
            "warmup_start_us": warmup_start_us,
            "warmup_end_us": warmup_end_us,
            "measure_start_us": measure_start_us,
            "measure_end_us": measure_end_us,
            "run": "",
            "iter": "",
            "run_selected": "",
            "latency_us": "",
            "latency_ns": "",
            "response": "",
            "target_pod": target_pod,
            "spin_snapshot_json": spin_json,
            "sibling_gate_pods": sibling_gate_pods,
            "case_count": "",
            "case_selected_avg_us": "",
            "case_mean_us": "",
            "case_variance_us2": "",
            "case_stddev_us": "",
            "case_t95_us": "",
            "case_t99_us": "",
            "case_max_us": "",
        }
    )

    rows.append(
        {
            "row_type": "summary_case",
            "case_id": case_id,
            "case_label": case_label,
            "entry_url": entry_url,
            "warmup_start_us": warmup_start_us,
            "warmup_end_us": warmup_end_us,
            "measure_start_us": measure_start_us,
            "measure_end_us": measure_end_us,
            "run": "",
            "iter": "",
            "run_selected": "",
            "latency_us": "",
            "latency_ns": "",
            "response": "",
            "target_pod": target_pod,
            "spin_snapshot_json": spin_json,
            "sibling_gate_pods": sibling_gate_pods,
            "case_count": int(stats.get("count", 0)),
            "case_selected_avg_us": avg_us,
            "case_mean_us": stats.get("mean_us", ""),
            "case_variance_us2": stats.get("variance_us2", ""),
            "case_stddev_us": stats.get("stddev_us", ""),
            "case_t95_us": stats.get("t95_us", ""),
            "case_t99_us": stats.get("t99_us", ""),
            "case_max_us": stats.get("max_us", ""),
        }
    )

    for row in all_rows:
        rows.append(
            {
                "row_type": "request",
                "case_id": case_id,
                "case_label": case_label,
                "entry_url": entry_url,
                "warmup_start_us": "",
                "warmup_end_us": "",
                "measure_start_us": "",
                "measure_end_us": "",
                "run": row["run"],
                "iter": row["iter"],
                "run_selected": row.get("run_selected", 1),
                "latency_us": row["latency_us"],
                "latency_ns": row["latency_ns"],
                "response": row["response"],
                "target_pod": target_pod,
                "spin_snapshot_json": spin_json,
                "sibling_gate_pods": sibling_gate_pods,
                "case_count": "",
                "case_selected_avg_us": "",
                "case_mean_us": "",
                "case_variance_us2": "",
                "case_stddev_us": "",
                "case_t95_us": "",
                "case_t99_us": "",
                "case_max_us": "",
            }
        )
    return rows


def regenerate_summary(results_dir: str) -> str:
    rows = []
    for path in sorted(glob.glob(os.path.join(results_dir, "result_case*.csv"))):
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            summary_row = None
            for row in reader:
                if row["row_type"] == "summary_case":
                    summary_row = row
                    break
            if not summary_row:
                continue
            rows.append(
                {
                    "csv_file": os.path.basename(path),
                    "case_id": summary_row["case_id"],
                    "case_label": summary_row["case_label"],
                    "target_pod": summary_row["target_pod"],
                    "spin_snapshot_json": summary_row["spin_snapshot_json"],
                    "case_selected_avg_us": summary_row["case_selected_avg_us"],
                    "case_mean_us": summary_row["case_mean_us"],
                    "case_stddev_us": summary_row["case_stddev_us"],
                    "case_t95_us": summary_row["case_t95_us"],
                    "case_t99_us": summary_row["case_t99_us"],
                    "case_max_us": summary_row["case_max_us"],
                }
            )

    summary_path = os.path.join(results_dir, "summary.csv")
    fieldnames = [
        "csv_file",
        "case_id",
        "case_label",
        "target_pod",
        "spin_snapshot_json",
        "case_selected_avg_us",
        "case_mean_us",
        "case_stddev_us",
        "case_t95_us",
        "case_t99_us",
        "case_max_us",
    ]
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return summary_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure one SocialNetwork Path 1 case without changing timing."
    )
    parser.add_argument("--case-id", choices=sorted(CASE_LABELS.keys()), required=True)
    parser.add_argument("--namespace", default="social")
    parser.add_argument("--entry-service", default=ENTRY_SERVICE)
    parser.add_argument("--entry-path", default=ENTRY_PATH)
    parser.add_argument("--entry-url", default="")
    parser.add_argument("--repeat", type=int, default=200)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmup-repeat", type=int, default=5)
    parser.add_argument("--request-timeout-sec", type=float, default=10.0)
    parser.add_argument("--tail-drop-top-runs", type=int, default=0)
    parser.add_argument("--aggregate-mode", choices=["mean", "median"], default="mean")
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--csv-out", default="")
    args = parser.parse_args(argv)
    if args.repeat <= 0 or args.runs <= 0:
        raise SystemExit("repeat/runs must be > 0")
    if args.tail_drop_top_runs < 0 or args.tail_drop_top_runs >= args.runs:
        raise SystemExit("tail-drop-top-runs must satisfy 0 <= drop < runs")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    case_label = CASE_LABELS[args.case_id]
    entry_url = args.entry_url or _get_entry_url(args.namespace, args.entry_service, args.entry_path)
    spin_snapshot = _get_spin_snapshot(args.namespace)
    target_pod = _wait_for_single_ready_pod(args.namespace, f"service={TARGET_SERVICE}")["metadata"]["name"]
    gate_probe = _probe_gate_targets(args.namespace)

    print(f"entry_url={entry_url}")
    print(f"case_id={args.case_id}")
    print(f"case_label={case_label}")
    print(f"spin_snapshot={spin_snapshot}")

    all_rows: list[dict[str, Any]] = []
    seq = 1

    warmup_start_us = int(time.time() * 1_000_000)
    warmup, seq = _warmup_only(
        entry_url=entry_url,
        warmup_repeat=args.warmup_repeat,
        timeout_sec=args.request_timeout_sec,
        seq_start=seq,
    )
    warmup_end_us = int(time.time() * 1_000_000)
    print(
        f"{case_label} warmup="
        f"{{'enabled': {warmup['enabled']}, 'ok': {warmup['ok']}, "
        f"'warmup_ok': {warmup['warmup_ok']}, 'warmup_errors': {warmup['warmup_errors']}}}"
    )

    run_vals = []
    run_rows_by_idx = {}
    measure_start_us = int(time.time() * 1_000_000)
    for run_idx in range(1, args.runs + 1):
        measured, seq = _measure_once(
            entry_url=entry_url,
            repeat=args.repeat,
            timeout_sec=args.request_timeout_sec,
            case_name=case_label,
            run_idx=run_idx,
            seq_start=seq,
        )
        run_vals.append(measured["avg_us"])
        run_rows_by_idx[run_idx] = measured["rows"]
        print(
            f"{case_label} run={run_idx} avg_latency_us={measured['avg_us']:.3f} "
            f"ok={measured['ok']}/{args.repeat} errors={measured['errors']}"
        )
    measure_end_us = int(time.time() * 1_000_000)

    selected_run_indices, dropped_runs = pick_runs(run_vals, args.tail_drop_top_runs)
    selected_set = set(selected_run_indices)
    for run_idx in range(1, args.runs + 1):
        selected = 1 if run_idx in selected_set else 0
        for row in run_rows_by_idx.get(run_idx, []):
            row["run_selected"] = selected
            all_rows.append(row)
    used_vals = [run_vals[idx - 1] for idx in selected_run_indices]
    avg_us = aggregate(used_vals, args.aggregate_mode)
    print(
        f"{case_label} selected_runs={selected_run_indices} "
        f"dropped_runs={dropped_runs if dropped_runs else '[]'} aggregate={args.aggregate_mode}"
    )

    sibling_gate_pods = ",".join(
        f"{item['service']}:{item['pod']}" for item in sorted(gate_probe, key=lambda x: x["service"])
    )
    case_stats = compute_case_stats(all_rows, selected_only=True)
    rows = _build_rows(
        case_id=args.case_id,
        case_label=case_label,
        entry_url=entry_url,
        all_rows=all_rows,
        case_stats=case_stats,
        avg_us=avg_us,
        target_pod=target_pod,
        spin_snapshot=spin_snapshot,
        sibling_gate_pods=sibling_gate_pods,
        warmup_start_us=warmup_start_us,
        warmup_end_us=warmup_end_us,
        measure_start_us=measure_start_us,
        measure_end_us=measure_end_us,
    )

    os.makedirs(args.results_dir, exist_ok=True)
    csv_out = args.csv_out or os.path.join(args.results_dir, f"result_{args.case_id}.csv")
    _write_csv(csv_out, rows)
    summary_out = regenerate_summary(args.results_dir)

    print("\n=== summary ===")
    print(f"{case_label} avg_ms={avg_us / 1000.0:.3f} runs_us={used_vals}")
    print(f"csv_out={csv_out}")
    print(f"summary_out={summary_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
