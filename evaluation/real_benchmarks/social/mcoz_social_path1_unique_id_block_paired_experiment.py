#!/usr/bin/env python3
"""
Run the SocialNetwork Path 1 request-credit experiment in block-paired form.

Each block executes exactly one measurement for:
- baseline: baseline Path 1 spins + fixedDelayNs=0
- virtual: baseline Path 1 spins + fixedDelayNs=N
- actual: actual unique-id spin + fixedDelayNs=0

The primary output is the paired block delta between:
- predicted actual latency from virtual acceleration
- measured actual latency from changing unique-id work
"""

from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import time

from mcoz_social_paths import default_results_dir
import mcoz_social_path1_unique_id_experiment as base


DEFAULT_RESULTS_DIR = default_results_dir("path1_target_unique-id-block-paired")

PHASE_BASELINE = "baseline"
PHASE_VIRTUAL = "virtual"
PHASE_ACTUAL = "actual"
PHASE_SET = {PHASE_BASELINE, PHASE_VIRTUAL, PHASE_ACTUAL}
PHASE_LABEL = {
    PHASE_BASELINE: "baseline delay0",
    PHASE_VIRTUAL: "virtual delayN",
    PHASE_ACTUAL: "actual unique-id delay0",
}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def _sample_stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    var = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(var)


def _ci95(values: list[float]) -> tuple[float, float]:
    if not values:
        return float("nan"), float("nan")
    mean = _mean(values)
    if len(values) < 2:
        return mean, mean
    std = _sample_stddev(values)
    half = 1.96 * std / math.sqrt(len(values))
    return mean - half, mean + half


def _phase_order(raw: str) -> tuple[str, str, str]:
    parts = tuple(x.strip().lower() for x in raw.split(",") if x.strip())
    if len(parts) != 3 or set(parts) != PHASE_SET:
        raise argparse.ArgumentTypeError(
            "block-order must contain baseline,virtual,actual exactly once"
        )
    return parts


def _phase_spin_map(
    phase: str,
    baseline_spin: dict[str, int],
    actual_spin: dict[str, int],
) -> dict[str, int]:
    if phase == PHASE_ACTUAL:
        return dict(actual_spin)
    return dict(baseline_spin)


def _phase_fixed_delay_ns(phase: str, fixed_delay_ns: int) -> int:
    return fixed_delay_ns if phase == PHASE_VIRTUAL else 0


def _phase_unique_id_spin_us(
    phase: str,
    baseline_spin: dict[str, int],
    actual_spin: dict[str, int],
) -> int:
    if phase == PHASE_ACTUAL:
        return actual_spin[base.TARGET_SERVICE]
    return baseline_spin[base.TARGET_SERVICE]


def _request_stats(rows: list[dict]) -> dict[str, float]:
    vals = [float(row["latency_us"]) for row in rows]
    if not vals:
        return {
            "count": 0.0,
            "mean_us": float("nan"),
            "median_us": float("nan"),
            "t95_us": float("nan"),
            "t99_us": float("nan"),
            "max_us": float("nan"),
            "min_us": float("nan"),
        }
    return {
        "count": float(len(vals)),
        "mean_us": _mean(vals),
        "median_us": base.median(vals),
        "t95_us": base.percentile(vals, 0.95),
        "t99_us": base.percentile(vals, 0.99),
        "max_us": max(vals),
        "min_us": min(vals),
    }


def _result_fields() -> list[str]:
    return [
        "row_type",
        "file_delay_ns",
        "block",
        "phase",
        "phase_order",
        "iter",
        "latency_us",
        "latency_ns",
        "response",
        "target_pod",
        "phase_fixed_delay_ns",
        "baseline_text_spin_us",
        "baseline_user_spin_us",
        "baseline_media_spin_us",
        "baseline_unique_id_spin_us",
        "actual_unique_id_spin_us",
        "phase_unique_id_spin_us",
        "sibling_gate_pods",
        "phase_count",
        "phase_avg_us",
        "phase_mean_us",
        "phase_median_us",
        "phase_t95_us",
        "phase_t99_us",
        "phase_max_us",
        "phase_min_us",
        "block_baseline_avg_us",
        "block_virtual_avg_us",
        "block_actual_avg_us",
        "block_predicted_actual_us",
        "block_virtual_gain_us",
        "block_actual_gain_us",
        "block_paired_gap_us",
        "block_paired_gap_rate_pct",
        "overall_baseline_mean_us",
        "overall_virtual_mean_us",
        "overall_actual_mean_us",
        "overall_virtual_gain_mean_us",
        "overall_actual_gain_mean_us",
        "overall_paired_gap_mean_us",
        "overall_paired_gap_stddev_us",
        "overall_paired_gap_ci95_low_us",
        "overall_paired_gap_ci95_high_us",
    ]


def _build_rows(
    fixed_delay_ns: int,
    block_order: tuple[str, str, str],
    baseline_spin: dict[str, int],
    actual_spin: dict[str, int],
    phase_results: list[dict],
    block_results: list[dict],
    overall: dict[str, float],
) -> list[dict]:
    fields = _result_fields()
    rows: list[dict] = []
    phase_order_text = ",".join(block_order)
    sibling_gate_pods = ",".join(
        f"{item['service']}:{item['pod']}" for item in phase_results[-1]["gate_alarm_targets"]
    ) if phase_results else ""

    def blank() -> dict:
        return {key: "" for key in fields}

    meta = blank()
    meta.update(
        {
            "row_type": "summary_meta",
            "file_delay_ns": fixed_delay_ns,
            "phase_order": phase_order_text,
            "baseline_text_spin_us": baseline_spin["text-service"],
            "baseline_user_spin_us": baseline_spin["user-service"],
            "baseline_media_spin_us": baseline_spin["media-service"],
            "baseline_unique_id_spin_us": baseline_spin["unique-id-service"],
            "actual_unique_id_spin_us": actual_spin["unique-id-service"],
            "sibling_gate_pods": sibling_gate_pods,
        }
    )
    rows.append(meta)

    for phase in (PHASE_BASELINE, PHASE_VIRTUAL, PHASE_ACTUAL):
        phase_items = [x for x in phase_results if x["phase"] == phase]
        stats = _request_stats([row for item in phase_items for row in item["rows"]])
        item = blank()
        item.update(
            {
                "row_type": "summary_phase",
                "file_delay_ns": fixed_delay_ns,
                "phase": phase,
                "phase_order": phase_order_text,
                "phase_fixed_delay_ns": _phase_fixed_delay_ns(phase, fixed_delay_ns),
                "baseline_text_spin_us": baseline_spin["text-service"],
                "baseline_user_spin_us": baseline_spin["user-service"],
                "baseline_media_spin_us": baseline_spin["media-service"],
                "baseline_unique_id_spin_us": baseline_spin["unique-id-service"],
                "actual_unique_id_spin_us": actual_spin["unique-id-service"],
                "phase_unique_id_spin_us": _phase_unique_id_spin_us(
                    phase, baseline_spin, actual_spin
                ),
                "sibling_gate_pods": sibling_gate_pods,
                "phase_count": int(stats["count"]),
                "phase_avg_us": _mean([x["avg_us"] for x in phase_items]),
                "phase_mean_us": stats["mean_us"],
                "phase_median_us": stats["median_us"],
                "phase_t95_us": stats["t95_us"],
                "phase_t99_us": stats["t99_us"],
                "phase_max_us": stats["max_us"],
                "phase_min_us": stats["min_us"],
            }
        )
        rows.append(item)

    for block in block_results:
        item = blank()
        item.update(
            {
                "row_type": "summary_block",
                "file_delay_ns": fixed_delay_ns,
                "block": block["block"],
                "phase_order": phase_order_text,
                "target_pod": block["actual_target_pod"],
                "baseline_text_spin_us": baseline_spin["text-service"],
                "baseline_user_spin_us": baseline_spin["user-service"],
                "baseline_media_spin_us": baseline_spin["media-service"],
                "baseline_unique_id_spin_us": baseline_spin["unique-id-service"],
                "actual_unique_id_spin_us": actual_spin["unique-id-service"],
                "sibling_gate_pods": sibling_gate_pods,
                "block_baseline_avg_us": block["baseline_avg_us"],
                "block_virtual_avg_us": block["virtual_avg_us"],
                "block_actual_avg_us": block["actual_avg_us"],
                "block_predicted_actual_us": block["predicted_actual_us"],
                "block_virtual_gain_us": block["virtual_gain_us"],
                "block_actual_gain_us": block["actual_gain_us"],
                "block_paired_gap_us": block["paired_gap_us"],
                "block_paired_gap_rate_pct": block["paired_gap_rate_pct"],
            }
        )
        rows.append(item)

    overall_row = blank()
    overall_row.update(
        {
            "row_type": "summary_overall",
            "file_delay_ns": fixed_delay_ns,
            "phase_order": phase_order_text,
            "baseline_text_spin_us": baseline_spin["text-service"],
            "baseline_user_spin_us": baseline_spin["user-service"],
            "baseline_media_spin_us": baseline_spin["media-service"],
            "baseline_unique_id_spin_us": baseline_spin["unique-id-service"],
            "actual_unique_id_spin_us": actual_spin["unique-id-service"],
            "sibling_gate_pods": sibling_gate_pods,
            "overall_baseline_mean_us": overall["baseline_mean_us"],
            "overall_virtual_mean_us": overall["virtual_mean_us"],
            "overall_actual_mean_us": overall["actual_mean_us"],
            "overall_virtual_gain_mean_us": overall["virtual_gain_mean_us"],
            "overall_actual_gain_mean_us": overall["actual_gain_mean_us"],
            "overall_paired_gap_mean_us": overall["paired_gap_mean_us"],
            "overall_paired_gap_stddev_us": overall["paired_gap_stddev_us"],
            "overall_paired_gap_ci95_low_us": overall["paired_gap_ci95_low_us"],
            "overall_paired_gap_ci95_high_us": overall["paired_gap_ci95_high_us"],
        }
    )
    rows.append(overall_row)

    for phase_item in phase_results:
        block = phase_item["block"]
        phase = phase_item["phase"]
        block_summary = next(x for x in block_results if x["block"] == block)
        for row in phase_item["rows"]:
            item = blank()
            item.update(
                {
                    "row_type": "request",
                    "file_delay_ns": fixed_delay_ns,
                    "block": block,
                    "phase": phase,
                    "phase_order": phase_order_text,
                    "iter": row["iter"],
                    "latency_us": row["latency_us"],
                    "latency_ns": row["latency_ns"],
                    "response": row["response"],
                    "target_pod": phase_item["target_pod"],
                    "phase_fixed_delay_ns": phase_item["fixed_delay_ns"],
                    "baseline_text_spin_us": baseline_spin["text-service"],
                    "baseline_user_spin_us": baseline_spin["user-service"],
                    "baseline_media_spin_us": baseline_spin["media-service"],
                    "baseline_unique_id_spin_us": baseline_spin["unique-id-service"],
                    "actual_unique_id_spin_us": actual_spin["unique-id-service"],
                    "phase_unique_id_spin_us": phase_item["phase_unique_id_spin_us"],
                    "sibling_gate_pods": sibling_gate_pods,
                    "phase_count": phase_item["phase_stats"]["count"],
                    "phase_avg_us": phase_item["avg_us"],
                    "phase_mean_us": phase_item["phase_stats"]["mean_us"],
                    "phase_median_us": phase_item["phase_stats"]["median_us"],
                    "phase_t95_us": phase_item["phase_stats"]["t95_us"],
                    "phase_t99_us": phase_item["phase_stats"]["t99_us"],
                    "phase_max_us": phase_item["phase_stats"]["max_us"],
                    "phase_min_us": phase_item["phase_stats"]["min_us"],
                    "block_baseline_avg_us": block_summary["baseline_avg_us"],
                    "block_virtual_avg_us": block_summary["virtual_avg_us"],
                    "block_actual_avg_us": block_summary["actual_avg_us"],
                    "block_predicted_actual_us": block_summary["predicted_actual_us"],
                    "block_virtual_gain_us": block_summary["virtual_gain_us"],
                    "block_actual_gain_us": block_summary["actual_gain_us"],
                    "block_paired_gap_us": block_summary["paired_gap_us"],
                    "block_paired_gap_rate_pct": block_summary["paired_gap_rate_pct"],
                    "overall_baseline_mean_us": overall["baseline_mean_us"],
                    "overall_virtual_mean_us": overall["virtual_mean_us"],
                    "overall_actual_mean_us": overall["actual_mean_us"],
                    "overall_virtual_gain_mean_us": overall["virtual_gain_mean_us"],
                    "overall_actual_gain_mean_us": overall["actual_gain_mean_us"],
                    "overall_paired_gap_mean_us": overall["paired_gap_mean_us"],
                    "overall_paired_gap_stddev_us": overall["paired_gap_stddev_us"],
                    "overall_paired_gap_ci95_low_us": overall["paired_gap_ci95_low_us"],
                    "overall_paired_gap_ci95_high_us": overall["paired_gap_ci95_high_us"],
                }
            )
            rows.append(item)

    return rows


def _write_csv(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_result_fields())
        writer.writeheader()
        writer.writerows(rows)


def regenerate_summary(results_dir: str) -> str:
    rows = []
    for path in sorted(glob.glob(os.path.join(results_dir, "result_delay-*.csv"))):
        meta = None
        overall = None
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if row["row_type"] == "summary_meta":
                    meta = row
                elif row["row_type"] == "summary_overall":
                    overall = row
        if not meta or not overall:
            continue
        rows.append(
            {
                "csv_file": os.path.basename(path),
                "fixed_delay_ns": meta["file_delay_ns"],
                "phase_order": meta["phase_order"],
                "baseline_text_spin_us": meta["baseline_text_spin_us"],
                "baseline_user_spin_us": meta["baseline_user_spin_us"],
                "baseline_media_spin_us": meta["baseline_media_spin_us"],
                "baseline_unique_id_spin_us": meta["baseline_unique_id_spin_us"],
                "actual_unique_id_spin_us": meta["actual_unique_id_spin_us"],
                "overall_baseline_mean_us": overall["overall_baseline_mean_us"],
                "overall_virtual_mean_us": overall["overall_virtual_mean_us"],
                "overall_actual_mean_us": overall["overall_actual_mean_us"],
                "overall_virtual_gain_mean_us": overall["overall_virtual_gain_mean_us"],
                "overall_actual_gain_mean_us": overall["overall_actual_gain_mean_us"],
                "overall_paired_gap_mean_us": overall["overall_paired_gap_mean_us"],
                "overall_paired_gap_stddev_us": overall["overall_paired_gap_stddev_us"],
                "overall_paired_gap_ci95_low_us": overall["overall_paired_gap_ci95_low_us"],
                "overall_paired_gap_ci95_high_us": overall["overall_paired_gap_ci95_high_us"],
            }
        )
    summary_path = os.path.join(results_dir, "summary.csv")
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "csv_file",
            "fixed_delay_ns",
            "phase_order",
            "baseline_text_spin_us",
            "baseline_user_spin_us",
            "baseline_media_spin_us",
            "baseline_unique_id_spin_us",
            "actual_unique_id_spin_us",
            "overall_baseline_mean_us",
            "overall_virtual_mean_us",
            "overall_actual_mean_us",
            "overall_virtual_gain_mean_us",
            "overall_actual_gain_mean_us",
            "overall_paired_gap_mean_us",
            "overall_paired_gap_stddev_us",
            "overall_paired_gap_ci95_low_us",
            "overall_paired_gap_ci95_high_us",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return summary_path


def _run_phase(
    *,
    block: int,
    phase: str,
    namespace: str,
    spin_map: dict[str, int],
    fixed_delay_ns: int,
    force_start: bool,
    mcoz_url: str,
    entry_url: str,
    repeat: int,
    timeout_sec: float,
    warmup_repeat: int,
    apply_policy_duration_ms: int,
    target_container: str,
    policy_state: dict[str, object],
    seq_start: int,
    settle_sec_after_spin_change: float,
    settle_sec_after_target_change: float,
    baseline_spin: dict[str, int],
    actual_spin: dict[str, int],
) -> tuple[dict, int]:
    label = f"Block {block} {PHASE_LABEL[phase]}"
    before_snapshot = base._get_spin_snapshot(namespace)
    snapshot = base._set_spin_env(namespace, spin_map)
    spin_changed = any(snapshot.get(svc) != before_snapshot.get(svc) for svc in spin_map)
    print(f"{label} spin_snapshot={snapshot}")
    if spin_changed and settle_sec_after_spin_change > 0:
        print(f"{label} settle_after_spin_change_sec={settle_sec_after_spin_change}")
        time.sleep(settle_sec_after_spin_change)

    target_pod = base._wait_for_single_ready_pod(
        namespace, f"service={base.TARGET_SERVICE}"
    )["metadata"]["name"]
    if policy_state.get("last_target_pod") and policy_state["last_target_pod"] != target_pod:
        if settle_sec_after_target_change > 0:
            print(
                f"{label} settle_after_target_change_sec={settle_sec_after_target_change} "
                f"from={policy_state['last_target_pod']} to={target_pod}"
            )
            time.sleep(settle_sec_after_target_change)
    policy_state["last_target_pod"] = target_pod

    gate_probe = base._probe_gate_targets(namespace)
    all_self_target = bool(gate_probe) and all(
        str(item.get("target_mode", "")).strip().lower() == "self" for item in gate_probe
    )
    if all_self_target:
        start_resp = base._start_mcoz(mcoz_url, fixed_delay_ns, force_start, namespace)
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
        print(
            f"{label} gate_target_update="
            f"{{'target_pod': '{target_pod}', 'gates': {gate_targets}, 'start_first': True}}"
        )
    else:
        gate_targets = base._set_gate_targets(namespace, target_pod, target_container)
        print(f"{label} gate_target_update={{'target_pod': '{target_pod}', 'gates': {gate_targets}}}")
        start_resp = base._start_mcoz(mcoz_url, fixed_delay_ns, force_start, namespace)

    gate_alarm_targets = base._validate_gate_alarm(start_resp, namespace, target_pod)
    print(f"{label} gate_alarm={gate_alarm_targets}")

    current_daemon_pids = base._extract_pids(start_resp)
    need_policy = not bool(policy_state.get("applied")) or policy_state.get("pod") != target_pod
    if need_policy:
        profile_obj, seq_start = base._apply_syscall_profile_once(
            mcoz_url=mcoz_url,
            namespace=namespace,
            pod=target_pod,
            container=target_container,
            entry_url=entry_url,
            request_timeout_sec=timeout_sec,
            seq_start=seq_start,
            duration_ms=apply_policy_duration_ms,
            top_k=12,
        )
        local = profile_obj.get("local") if isinstance(profile_obj.get("local"), dict) else {}
        local_payload = local.get("payload") if isinstance(local.get("payload"), dict) else {}
        traffic = profile_obj.get("_traffic", {})
        policy_state["applied"] = True
        policy_state["pod"] = target_pod
        policy_state["container"] = target_container
        policy_state["daemon_pids"] = list(current_daemon_pids)
        policy_state["applied_paths"] = local_payload.get("applied_consume_paths") or []
        print(
            f"{label} syscall_profile="
            f"{{'pod': '{target_pod}', "
            f"'apply_policy_ok': {local_payload.get('apply_policy_ok')}, "
            f"'applied_paths': {local_payload.get('applied_consume_paths')}, "
            f"'traffic_ok': {traffic.get('ok', 0)}, "
            f"'traffic_errors': {traffic.get('errors', 0)}}}"
        )
    else:
        print(
            f"{label} policy_check="
            f"{{'ok': True, 'pod': '{target_pod}', 'daemon_pids': {list(current_daemon_pids)}}}"
        )

    warmup, seq_start = base._warmup_and_clear(
        mcoz_url=mcoz_url,
        entry_url=entry_url,
        warmup_repeat=warmup_repeat,
        timeout_sec=timeout_sec,
        seq_start=seq_start,
    )
    print(
        f"{label} warmup="
        f"{{'enabled': {warmup['enabled']}, 'ok': {warmup['ok']}, "
        f"'warmup_ok': {warmup['warmup_ok']}, 'warmup_errors': {warmup['warmup_errors']}}}"
    )

    measured, seq_start = base._measure_once(
        entry_url=entry_url,
        repeat=repeat,
        timeout_sec=timeout_sec,
        case_name=phase,
        run_idx=block,
        seq_start=seq_start,
    )
    phase_stats = _request_stats(measured["rows"])
    print(
        f"{label} avg_latency_us={measured['avg_us']:.3f} "
        f"median_us={phase_stats['median_us']:.3f} "
        f"ok={measured['ok']}/{repeat} errors={measured['errors']}"
    )

    return {
        "block": block,
        "phase": phase,
        "avg_us": measured["avg_us"],
        "rows": measured["rows"],
        "target_pod": target_pod,
        "gate_alarm_targets": gate_alarm_targets,
        "snapshot": snapshot,
        "fixed_delay_ns": fixed_delay_ns,
        "phase_unique_id_spin_us": _phase_unique_id_spin_us(phase, baseline_spin, actual_spin),
        "phase_stats": phase_stats,
    }, seq_start


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run SocialNetwork Path 1 request-credit experiment for target unique-id-service "
            "using block-paired mean comparison."
        )
    )
    parser.add_argument("--namespace", default="social")
    parser.add_argument("--entry-service", default=base.ENTRY_SERVICE)
    parser.add_argument("--entry-path", default=base.ENTRY_PATH)
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
        help="explicit unique-id spin for actual phases; default -1 means reuse baseline value",
    )
    parser.add_argument("--blocks", type=int, default=5)
    parser.add_argument("--repeat", type=int, default=100)
    parser.add_argument("--warmup-repeat", type=int, default=10)
    parser.add_argument(
        "--block-order",
        type=_phase_order,
        default=(PHASE_BASELINE, PHASE_VIRTUAL, PHASE_ACTUAL),
        help="comma-separated order of baseline,virtual,actual inside each block",
    )
    parser.add_argument("--request-timeout-sec", type=float, default=10.0)
    parser.add_argument("--force-start", action="store_true")
    parser.add_argument("--target-container", default=base.TARGET_SERVICE)
    parser.add_argument("--apply-policy-duration-ms", type=int, default=2000)
    parser.add_argument("--settle-sec-after-spin-change", type=float, default=10.0)
    parser.add_argument("--settle-sec-after-target-change", type=float, default=3.0)
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--csv-out", default="")
    parser.add_argument(
        "--no-restore-baseline-after",
        action="store_true",
        help="leave the final live *_SPIN_US values at the last experiment phase",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.fixed_delay_ns < 0:
        raise SystemExit("fixed-delay-ns must be >= 0")
    if args.blocks <= 0 or args.repeat <= 0:
        raise SystemExit("blocks/repeat must be > 0")

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

    entry_url = args.entry_url or base._get_entry_url(
        args.namespace, args.entry_service, args.entry_path
    )
    print(f"entry_url={entry_url}")
    print(f"baseline_spin={baseline_spin}")
    print(f"actual_spin={actual_spin}")
    print(f"fixed_delay_ns={args.fixed_delay_ns}")
    print(f"block_order={args.block_order}")
    print(f"blocks={args.blocks}")

    phase_results: list[dict] = []
    block_results: list[dict] = []
    policy_state: dict[str, object] = {}
    seq = 1
    if args.csv_out:
        csv_out = args.csv_out
    else:
        os.makedirs(args.results_dir, exist_ok=True)
        csv_out = os.path.join(args.results_dir, f"result_delay-{args.fixed_delay_ns}.csv")

    try:
        first_phase = True
        for block in range(1, args.blocks + 1):
            per_block: dict[str, dict] = {}
            print(f"\n=== block {block}/{args.blocks} ===")
            for phase in args.block_order:
                spin_map = _phase_spin_map(phase, baseline_spin, actual_spin)
                phase_delay_ns = _phase_fixed_delay_ns(phase, args.fixed_delay_ns)
                result, seq = _run_phase(
                    block=block,
                    phase=phase,
                    namespace=args.namespace,
                    spin_map=spin_map,
                    fixed_delay_ns=phase_delay_ns,
                    force_start=bool(args.force_start and first_phase),
                    mcoz_url=args.mcoz_url,
                    entry_url=entry_url,
                    repeat=args.repeat,
                    timeout_sec=args.request_timeout_sec,
                    warmup_repeat=args.warmup_repeat,
                    apply_policy_duration_ms=args.apply_policy_duration_ms,
                    target_container=args.target_container,
                    policy_state=policy_state,
                    seq_start=seq,
                    settle_sec_after_spin_change=args.settle_sec_after_spin_change,
                    settle_sec_after_target_change=args.settle_sec_after_target_change,
                    baseline_spin=baseline_spin,
                    actual_spin=actual_spin,
                )
                per_block[phase] = result
                phase_results.append(result)
                first_phase = False
                is_final_phase = block == args.blocks and phase == args.block_order[-1]
                if not is_final_phase:
                    base._cleanup_between_cases(
                        args.mcoz_url,
                        policy_state,
                        f"between_block{block}_{phase}",
                    )

            baseline_us = per_block[PHASE_BASELINE]["avg_us"]
            virtual_us = per_block[PHASE_VIRTUAL]["avg_us"]
            actual_us = per_block[PHASE_ACTUAL]["avg_us"]
            n_us = args.fixed_delay_ns / 1000.0
            predicted_actual_us = virtual_us - n_us
            virtual_gain_us = (baseline_us + n_us) - virtual_us
            actual_gain_us = baseline_us - actual_us
            paired_gap_us = predicted_actual_us - actual_us
            paired_gap_rate_pct = (paired_gap_us / actual_us * 100.0) if actual_us else float("inf")
            block_summary = {
                "block": block,
                "baseline_avg_us": baseline_us,
                "virtual_avg_us": virtual_us,
                "actual_avg_us": actual_us,
                "predicted_actual_us": predicted_actual_us,
                "virtual_gain_us": virtual_gain_us,
                "actual_gain_us": actual_gain_us,
                "paired_gap_us": paired_gap_us,
                "paired_gap_rate_pct": paired_gap_rate_pct,
                "baseline_target_pod": per_block[PHASE_BASELINE]["target_pod"],
                "virtual_target_pod": per_block[PHASE_VIRTUAL]["target_pod"],
                "actual_target_pod": per_block[PHASE_ACTUAL]["target_pod"],
            }
            block_results.append(block_summary)
            print(
                f"block {block} summary="
                f"{{'baseline_ms': {baseline_us / 1000.0:.3f}, "
                f"'virtual_ms': {virtual_us / 1000.0:.3f}, "
                f"'actual_ms': {actual_us / 1000.0:.3f}, "
                f"'predicted_actual_ms': {predicted_actual_us / 1000.0:.3f}, "
                f"'paired_gap_ms': {paired_gap_us / 1000.0:+.3f}}}"
            )

        baseline_vals = [x["baseline_avg_us"] for x in block_results]
        virtual_vals = [x["virtual_avg_us"] for x in block_results]
        actual_vals = [x["actual_avg_us"] for x in block_results]
        virtual_gain_vals = [x["virtual_gain_us"] for x in block_results]
        actual_gain_vals = [x["actual_gain_us"] for x in block_results]
        paired_gap_vals = [x["paired_gap_us"] for x in block_results]
        ci_low, ci_high = _ci95(paired_gap_vals)

        overall = {
            "baseline_mean_us": _mean(baseline_vals),
            "virtual_mean_us": _mean(virtual_vals),
            "actual_mean_us": _mean(actual_vals),
            "virtual_gain_mean_us": _mean(virtual_gain_vals),
            "actual_gain_mean_us": _mean(actual_gain_vals),
            "paired_gap_mean_us": _mean(paired_gap_vals),
            "paired_gap_stddev_us": _sample_stddev(paired_gap_vals),
            "paired_gap_ci95_low_us": ci_low,
            "paired_gap_ci95_high_us": ci_high,
        }

        rows = _build_rows(
            fixed_delay_ns=args.fixed_delay_ns,
            block_order=args.block_order,
            baseline_spin=baseline_spin,
            actual_spin=actual_spin,
            phase_results=phase_results,
            block_results=block_results,
            overall=overall,
        )
        _write_csv(csv_out, rows)
        summary_path = regenerate_summary(args.results_dir)

        print("\n=== overall summary ===")
        print(f"baseline_mean_ms={overall['baseline_mean_us'] / 1000.0:.3f}")
        print(f"virtual_mean_ms={overall['virtual_mean_us'] / 1000.0:.3f}")
        print(f"actual_mean_ms={overall['actual_mean_us'] / 1000.0:.3f}")
        print(f"virtual_gain_mean_ms={overall['virtual_gain_mean_us'] / 1000.0:.3f}")
        print(f"actual_gain_mean_ms={overall['actual_gain_mean_us'] / 1000.0:.3f}")
        print(f"paired_gap_mean_ms={overall['paired_gap_mean_us'] / 1000.0:+.3f}")
        print(f"paired_gap_stddev_ms={overall['paired_gap_stddev_us'] / 1000.0:.3f}")
        print(
            f"paired_gap_ci95_ms=[{overall['paired_gap_ci95_low_us'] / 1000.0:+.3f}, "
            f"{overall['paired_gap_ci95_high_us'] / 1000.0:+.3f}]"
        )
        print(f"policy_applied_paths={policy_state.get('applied_paths', [])}")
        print(f"csv_out={csv_out}")
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
                base._set_gate_targets(args.namespace, restored_target_pod, args.target_container)
                restore["target_pod"] = restored_target_pod
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
