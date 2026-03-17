#!/usr/bin/env python3
"""MSA sibling/fan-out analyzer based on Jaeger trace timing overlap."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

import requests


DEFAULT_JAEGER_URL = "http://jaeger-query.istio-system:16686"
DEFAULT_POLL_TIMEOUT_S = 15.0
DEFAULT_POLL_INTERVAL_S = 0.5
DEFAULT_MIN_OVERLAP_MS = 0.5
DEFAULT_TRACE_SETTLE_S = 2.0


@dataclass
class SpanRecord:
    span_id: str
    service_name: str
    operation_name: str
    start_us: int
    end_us: int
    duration_us: int
    kind: str
    parent_span_id: Optional[str]
    peer: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Send one traced request, poll Jaeger for the same trace ID, "
            "then infer sibling services and fan-out candidates."
        )
    )
    parser.add_argument("--entry-url", required=True, help="Entry URL to call once.")
    parser.add_argument(
        "--request-path",
        default=None,
        help=(
            "Optional request path (e.g. /entry, /api/v1). "
            "If set, this path overrides the path part in --entry-url."
        ),
    )
    parser.add_argument(
        "--jaeger-url",
        default=DEFAULT_JAEGER_URL,
        help=f"Jaeger query base URL (default: {DEFAULT_JAEGER_URL})",
    )
    parser.add_argument(
        "--trace-id",
        default=None,
        help="32-hex trace ID. If omitted, auto-generated.",
    )
    parser.add_argument(
        "--span-id",
        default=None,
        help="16-hex root span ID. If omitted, auto-generated.",
    )
    parser.add_argument(
        "--method",
        default="GET",
        help="HTTP method for entry call (default: GET).",
    )
    parser.add_argument(
        "--request-timeout-s",
        type=float,
        default=10.0,
        help="Timeout for entry request in seconds (default: 10).",
    )
    parser.add_argument(
        "--poll-timeout-s",
        type=float,
        default=DEFAULT_POLL_TIMEOUT_S,
        help=f"Trace polling timeout in seconds (default: {DEFAULT_POLL_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
        help=f"Trace polling interval in seconds (default: {DEFAULT_POLL_INTERVAL_S}).",
    )
    parser.add_argument(
        "--trace-settle-s",
        type=float,
        default=DEFAULT_TRACE_SETTLE_S,
        help=(
            "After spans appear, wait until span count stops increasing for this many "
            f"seconds before finalizing trace (default: {DEFAULT_TRACE_SETTLE_S})."
        ),
    )
    parser.add_argument(
        "--min-overlap-ms",
        type=float,
        default=DEFAULT_MIN_OVERLAP_MS,
        help=(
            "Minimum overlap in milliseconds for sibling match "
            f"(default: {DEFAULT_MIN_OVERLAP_MS})."
        ),
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Optional path to save analysis result as JSON.",
    )
    return parser.parse_args()


def normalize_hex(raw: Optional[str], expected_len: int, field_name: str) -> str:
    if raw is None:
        if field_name == "trace_id":
            return secrets.token_hex(16)
        return secrets.token_hex(8)

    value = raw.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    if not value:
        raise ValueError(f"{field_name} is empty.")
    if not re.fullmatch(r"[0-9a-f]+", value):
        raise ValueError(f"{field_name} must be hex: {raw}")

    if field_name == "trace_id" and len(value) == 16:
        value = value.rjust(32, "0")
    if len(value) != expected_len:
        raise ValueError(
            f"{field_name} must be {expected_len} hex chars (got {len(value)}): {raw}"
        )

    if int(value, 16) == 0:
        raise ValueError(f"{field_name} must not be all zeros.")
    return value


def build_propagation_headers(trace_id: str, span_id: str) -> Dict[str, str]:
    b3_single = f"{trace_id}-{span_id}-1"
    traceparent = f"00-{trace_id}-{span_id}-01"
    return {
        "x-b3-traceid": trace_id,
        "x-b3-spanid": span_id,
        "x-b3-sampled": "1",
        "b3": b3_single,
        "traceparent": traceparent,
    }


def send_entry_request(
    entry_url: str,
    method: str,
    headers: Dict[str, str],
    timeout_s: float,
) -> Tuple[int, float]:
    started = time.perf_counter()
    resp = requests.request(method=method.upper(), url=entry_url, headers=headers, timeout=timeout_s)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return resp.status_code, elapsed_ms


def build_entry_request_url(entry_url: str, request_path: Optional[str]) -> str:
    if not request_path:
        return entry_url

    parsed = urlparse(entry_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            f"--entry-url must include scheme and host when --request-path is used: {entry_url}"
        )

    normalized_path = request_path.strip()
    if not normalized_path:
        return entry_url
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"

    return urlunparse((parsed.scheme, parsed.netloc, normalized_path, "", "", ""))


def normalize_jaeger_url(url: str) -> str:
    return url.rstrip("/")


def detect_jaeger_base_path(jaeger_url: str, timeout_s: float = 3.0) -> str:
    base = normalize_jaeger_url(jaeger_url)
    checks = ("", "/jaeger")
    errors: List[str] = []

    for prefix in checks:
        endpoint = f"{base}{prefix}/api/services"
        try:
            resp = requests.get(endpoint, timeout=timeout_s)
            if resp.status_code != 200:
                errors.append(f"{endpoint} -> HTTP {resp.status_code}")
                continue

            # Some setups return HTML for /api/services with 200.
            # Treat as success only when body looks like Jaeger JSON payload.
            try:
                payload = resp.json()
            except ValueError:
                body_head = resp.text[:80].replace("\n", " ").strip()
                errors.append(f"{endpoint} -> HTTP 200 non-JSON body='{body_head}'")
                continue

            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, list):
                return prefix

            errors.append(f"{endpoint} -> HTTP 200 JSON but unexpected schema")
        except requests.RequestException as exc:
            errors.append(f"{endpoint} -> {exc}")

    joined = "; ".join(errors)
    raise RuntimeError(
        "Failed to detect Jaeger base path using /api/services and /jaeger/api/services. "
        f"Details: {joined}"
    )


def fetch_trace_once(jaeger_url: str, base_path: str, trace_id: str, timeout_s: float = 4.0) -> Optional[Dict[str, Any]]:
    endpoint = f"{normalize_jaeger_url(jaeger_url)}{base_path}/api/traces/{trace_id}"
    resp = requests.get(endpoint, timeout=timeout_s)

    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"Jaeger trace API returned HTTP {resp.status_code} for {endpoint}")

    try:
        payload = resp.json()
    except ValueError as exc:
        body_head = resp.text[:120].replace("\n", " ").strip()
        raise RuntimeError(
            f"Jaeger trace API returned non-JSON for {endpoint}. body_head='{body_head}'"
        ) from exc
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if isinstance(data, list):
        if not data:
            return None
        first = data[0]
        if isinstance(first, dict):
            return first
        return None
    if isinstance(data, dict):
        return data
    return None


def poll_trace(
    jaeger_url: str,
    base_path: str,
    trace_id: str,
    timeout_s: float,
    interval_s: float,
    settle_s: float,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_error: Optional[Exception] = None
    best_trace: Optional[Dict[str, Any]] = None
    best_span_count = 0
    last_growth_at: Optional[float] = None

    while time.monotonic() < deadline:
        try:
            trace = fetch_trace_once(jaeger_url, base_path, trace_id)
            if trace is not None:
                spans = trace.get("spans")
                span_count = len(spans) if isinstance(spans, list) else 0
                if span_count > 0:
                    now = time.monotonic()
                    if span_count > best_span_count:
                        best_span_count = span_count
                        best_trace = trace
                        last_growth_at = now
                    elif span_count == best_span_count and best_trace is None:
                        best_trace = trace
                        last_growth_at = now

                    if settle_s <= 0:
                        return best_trace or trace

                    if (
                        best_trace is not None
                        and last_growth_at is not None
                        and (now - last_growth_at) >= settle_s
                    ):
                        return best_trace
        except Exception as exc:  # pylint: disable=broad-except
            last_error = exc
        time.sleep(interval_s)

    if best_trace is not None and best_span_count > 0:
        return best_trace
    if last_error:
        raise TimeoutError(f"Trace polling timed out after {timeout_s}s. Last error: {last_error}") from last_error
    raise TimeoutError(f"Trace polling timed out after {timeout_s}s without finding spans.")


def tags_to_map(tags: Optional[Iterable[Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not tags:
        return out
    for item in tags:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str) or key in out:
            continue
        out[key] = item.get("value")
    return out


def get_span_kind(tags: Dict[str, Any]) -> str:
    value = tags.get("span.kind")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return "unknown"


def get_parent_span_id(references: Optional[Iterable[Any]]) -> Optional[str]:
    if not references:
        return None
    for ref in references:
        if not isinstance(ref, dict):
            continue
        ref_type = str(ref.get("refType", "")).upper()
        if ref_type != "CHILD_OF":
            continue
        span_id = ref.get("spanID")
        if isinstance(span_id, str) and span_id:
            return span_id
    return None


def extract_service_from_upstream_cluster(value: str) -> Optional[str]:
    # e.g. outbound|5000||d.default.svc.cluster.local
    match = re.search(r"outbound\|[^|]*\|\|([^|]+)", value)
    if match:
        return match.group(1)
    return None


def extract_peer(tags: Dict[str, Any], operation_name: str) -> str:
    for key in ("peer.service", "net.peer.name", "rpc.service"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    host = tags.get("http.host")
    if isinstance(host, str) and host.strip():
        return host.strip()

    http_url = tags.get("http.url")
    if isinstance(http_url, str) and http_url.strip():
        parsed = urlparse(http_url)
        if parsed.netloc:
            return parsed.netloc
        if parsed.path:
            return parsed.path
        return http_url.strip()

    upstream = tags.get("upstream_cluster")
    if isinstance(upstream, str) and upstream.strip():
        extracted = extract_service_from_upstream_cluster(upstream)
        return extracted or upstream.strip()

    return operation_name or "unknown-peer"


def parse_trace(trace: Dict[str, Any]) -> Tuple[List[SpanRecord], Dict[str, SpanRecord]]:
    processes = trace.get("processes", {})
    if not isinstance(processes, dict):
        processes = {}

    spans_raw = trace.get("spans", [])
    spans: List[SpanRecord] = []
    by_id: Dict[str, SpanRecord] = {}

    for item in spans_raw:
        if not isinstance(item, dict):
            continue

        process = processes.get(item.get("processID"), {})
        if not isinstance(process, dict):
            process = {}
        service_name = process.get("serviceName")
        if not isinstance(service_name, str) or not service_name:
            service_name = "unknown-service"

        tags = tags_to_map(item.get("tags", []))
        kind = get_span_kind(tags)
        parent_span_id = get_parent_span_id(item.get("references", []))
        operation_name = str(item.get("operationName", ""))
        peer = extract_peer(tags, operation_name)

        try:
            start_us = int(item.get("startTime", 0))
            duration_us = int(item.get("duration", 0))
        except (TypeError, ValueError):
            continue
        if duration_us < 0:
            duration_us = 0
        end_us = start_us + duration_us

        span_id = str(item.get("spanID", ""))
        if not span_id:
            continue

        record = SpanRecord(
            span_id=span_id,
            service_name=service_name,
            operation_name=operation_name,
            start_us=start_us,
            end_us=end_us,
            duration_us=duration_us,
            kind=kind,
            parent_span_id=parent_span_id,
            peer=peer,
        )
        spans.append(record)
        by_id[span_id] = record

    return spans, by_id


def merge_intervals(intervals: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    sorted_intervals = sorted((s, e) for s, e in intervals if e > s)
    if not sorted_intervals:
        return []

    merged: List[Tuple[int, int]] = [sorted_intervals[0]]
    for start, end in sorted_intervals[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def total_interval_us(intervals: Iterable[Tuple[int, int]]) -> int:
    return sum((end - start) for start, end in intervals if end > start)


def overlap_us(a: List[Tuple[int, int]], b: List[Tuple[int, int]]) -> int:
    i = 0
    j = 0
    total = 0
    while i < len(a) and j < len(b):
        sa, ea = a[i]
        sb, eb = b[j]
        start = max(sa, sb)
        end = min(ea, eb)
        if end > start:
            total += end - start
        if ea <= eb:
            i += 1
        else:
            j += 1
    return total


def build_service_intervals(spans: List[SpanRecord]) -> Dict[str, Dict[str, Any]]:
    per_service: Dict[str, Dict[str, Any]] = {}
    for span in spans:
        state = per_service.setdefault(
            span.service_name,
            {"all_intervals": [], "server_intervals": []},
        )
        state["all_intervals"].append((span.start_us, span.end_us))
        if span.kind == "server":
            state["server_intervals"].append((span.start_us, span.end_us))

    out: Dict[str, Dict[str, Any]] = {}
    for service, raw in per_service.items():
        server_merged = merge_intervals(raw["server_intervals"])
        if server_merged:
            basis = "server"
            merged = server_merged
        else:
            basis = "all"
            merged = merge_intervals(raw["all_intervals"])

        out[service] = {
            "basis": basis,
            "intervals": merged,
            "total_us": total_interval_us(merged),
        }
    return out


def qualifies_overlap(overlap: int, min_overlap_us: int) -> bool:
    if min_overlap_us <= 0:
        return overlap > 0
    return overlap >= min_overlap_us


def build_children_by_parent(spans: List[SpanRecord]) -> Dict[str, List[SpanRecord]]:
    children_by_parent: Dict[str, List[SpanRecord]] = {}
    for span in spans:
        if not span.parent_span_id:
            continue
        children_by_parent.setdefault(span.parent_span_id, []).append(span)
    return children_by_parent


def normalize_peer_service_name(peer: str) -> Optional[str]:
    value = peer.strip().lower()
    if not value:
        return None

    if "://" in value:
        parsed = urlparse(value)
        if parsed.hostname:
            value = parsed.hostname
        else:
            value = parsed.path or value

    if "/" in value and not value.startswith("/"):
        value = value.split("/", 1)[0]

    if ":" in value:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            value = host

    if value.endswith(".svc.cluster.local"):
        value = value[: -len(".svc.cluster.local")]

    value = value.strip()
    return value or None


def resolve_downstream_service_name(
    client_span: SpanRecord,
    children_by_parent: Dict[str, List[SpanRecord]],
) -> str:
    direct_server_children = [
        child
        for child in children_by_parent.get(client_span.span_id, [])
        if child.kind == "server"
    ]
    if direct_server_children:
        direct_server_children.sort(key=lambda s: (-s.duration_us, s.start_us, s.service_name))
        return direct_server_children[0].service_name

    peer_name = normalize_peer_service_name(client_span.peer)
    if peer_name:
        return peer_name
    return "unknown-service"


def span_pair_overlap_us(left: SpanRecord, right: SpanRecord) -> int:
    start = max(left.start_us, right.start_us)
    end = min(left.end_us, right.end_us)
    if end <= start:
        return 0
    return end - start


def add_sibling_relation(
    matrix: Dict[str, Dict[str, Dict[str, Any]]],
    source_service: str,
    target_service: str,
    overlap_us: int,
    parent_span_id: str,
    parent_service: str,
    parent_op: str,
    source_span_id: str,
    target_span_id: str,
) -> None:
    partner_map = matrix.setdefault(source_service, {})
    record = partner_map.setdefault(
        target_service,
        {"service": target_service, "overlap_us": 0, "contexts": []},
    )
    record["overlap_us"] += overlap_us
    record["contexts"].append(
        {
            "parent_span_id": parent_span_id,
            "parent_service": parent_service,
            "parent_op": parent_op,
            "source_span_id": source_span_id,
            "target_span_id": target_span_id,
        }
    )


def compute_siblings(
    spans: List[SpanRecord],
    span_by_id: Dict[str, SpanRecord],
    service_intervals: Dict[str, Dict[str, Any]],
    min_overlap_ms: float,
) -> Dict[str, List[Dict[str, Any]]]:
    min_overlap_us = int(max(min_overlap_ms, 0.0) * 1000.0)
    children_by_parent = build_children_by_parent(spans)
    matrix: Dict[str, Dict[str, Dict[str, Any]]] = {}
    resolved_service_by_span_id: Dict[str, str] = {}
    fallback_intervals_by_service: Dict[str, List[Tuple[int, int]]] = {}

    for span in spans:
        if span.kind != "client":
            continue
        resolved = resolve_downstream_service_name(span, children_by_parent)
        resolved_service_by_span_id[span.span_id] = resolved
        fallback_intervals_by_service.setdefault(resolved, []).append((span.start_us, span.end_us))

    fallback_total_us_by_service = {
        service: total_interval_us(merge_intervals(intervals))
        for service, intervals in fallback_intervals_by_service.items()
    }

    for parent_span_id, children in children_by_parent.items():
        client_children = [child for child in children if child.kind == "client"]
        if len(client_children) < 2:
            continue

        parent = span_by_id.get(parent_span_id)
        parent_service = parent.service_name if parent else "unknown-service"
        parent_op = parent.operation_name if parent else f"parent:{parent_span_id}"

        for i, left in enumerate(client_children):
            left_service = resolved_service_by_span_id.get(
                left.span_id,
                resolve_downstream_service_name(left, children_by_parent),
            )
            for right in client_children[i + 1 :]:
                right_service = resolved_service_by_span_id.get(
                    right.span_id,
                    resolve_downstream_service_name(right, children_by_parent),
                )
                if left_service == right_service:
                    continue

                overlap = span_pair_overlap_us(left, right)
                if not qualifies_overlap(overlap, min_overlap_us):
                    continue

                add_sibling_relation(
                    matrix=matrix,
                    source_service=left_service,
                    target_service=right_service,
                    overlap_us=overlap,
                    parent_span_id=parent_span_id,
                    parent_service=parent_service,
                    parent_op=parent_op,
                    source_span_id=left.span_id,
                    target_span_id=right.span_id,
                )
                add_sibling_relation(
                    matrix=matrix,
                    source_service=right_service,
                    target_service=left_service,
                    overlap_us=overlap,
                    parent_span_id=parent_span_id,
                    parent_service=parent_service,
                    parent_op=parent_op,
                    source_span_id=right.span_id,
                    target_span_id=left.span_id,
                )

    all_services = set(service_intervals.keys())
    all_services.update(matrix.keys())
    for partners in matrix.values():
        all_services.update(partners.keys())

    out: Dict[str, List[Dict[str, Any]]] = {}
    for service in sorted(all_services):
        rows: List[Dict[str, Any]] = []
        for partner in matrix.get(service, {}).values():
            other = partner["service"]
            dur_service = service_intervals.get(service, {}).get("total_us", 0)
            if dur_service <= 0:
                dur_service = fallback_total_us_by_service.get(service, 0)
            dur_other = service_intervals.get(other, {}).get("total_us", 0)
            if dur_other <= 0:
                dur_other = fallback_total_us_by_service.get(other, 0)
            denom = min(dur_service, dur_other)
            ratio = (partner["overlap_us"] / denom) if denom > 0 else 0.0

            contexts = sorted(
                partner["contexts"],
                key=lambda c: (
                    c["parent_service"],
                    c["parent_op"],
                    c["parent_span_id"],
                    c["source_span_id"],
                ),
            )
            rows.append(
                {
                    "service": other,
                    "overlap_us": partner["overlap_us"],
                    "ratio": ratio,
                    "contexts": contexts,
                }
            )
        rows.sort(key=lambda x: (-x["overlap_us"], x["service"]))
        out[service] = rows
    return out


def has_any_overlap(spans: List[SpanRecord]) -> bool:
    if len(spans) < 2:
        return False
    sorted_spans = sorted(spans, key=lambda s: (s.start_us, s.end_us))
    active_end = sorted_spans[0].end_us
    for span in sorted_spans[1:]:
        if span.start_us < active_end:
            return True
        active_end = max(active_end, span.end_us)
    return False


def detect_fanout_candidates(
    spans: List[SpanRecord],
    span_by_id: Dict[str, SpanRecord],
) -> Dict[str, List[Dict[str, Any]]]:
    children_by_parent = build_children_by_parent(spans)
    grouped: Dict[Tuple[str, str], List[SpanRecord]] = {}
    for span in spans:
        if span.kind != "client":
            continue
        if not span.parent_span_id:
            continue
        key = (span.service_name, span.parent_span_id)
        grouped.setdefault(key, []).append(span)

    per_service: Dict[str, List[Dict[str, Any]]] = {}
    for (service, parent_id), children in grouped.items():
        if len(children) < 2:
            continue
        if not has_any_overlap(children):
            continue

        parent = span_by_id.get(parent_id)
        parent_kind = parent.kind if parent else "unknown"
        parent_op = parent.operation_name if parent else f"parent:{parent_id}"
        peers = sorted(
            {
                resolved
                for child in children
                for resolved in [resolve_downstream_service_name(child, children_by_parent)]
                if resolved
            }
        )
        window_start = min(child.start_us for child in children)
        window_end = max(child.end_us for child in children)

        candidate = {
            "parent_span_id": parent_id,
            "parent_kind": parent_kind,
            "parent_op": parent_op,
            "peers": peers,
            "group_dur_us": max(window_end - window_start, 0),
            "span_count": len(children),
            "span_ids": [child.span_id for child in sorted(children, key=lambda x: x.start_us)],
        }
        per_service.setdefault(service, []).append(candidate)

    filtered: Dict[str, List[Dict[str, Any]]] = {}
    for service, cands in per_service.items():
        server_parent_cands = [c for c in cands if c["parent_kind"] == "server"]
        chosen = server_parent_cands if server_parent_cands else cands
        chosen.sort(key=lambda c: (-c["group_dur_us"], c["parent_op"]))
        filtered[service] = chosen
    return filtered


def format_ms_from_us(value_us: int) -> str:
    return f"{(value_us / 1000.0):.3f}"


def print_human_output(
    service_intervals: Dict[str, Dict[str, Any]],
    siblings: Dict[str, List[Dict[str, Any]]],
    fanout: Dict[str, List[Dict[str, Any]]],
) -> None:
    print("\n=== Sibling services (same parent + overlap) ===")
    all_services = sorted(
        set(service_intervals.keys()) | set(siblings.keys()) | set(fanout.keys())
    )
    for service in all_services:
        total_us = service_intervals.get(service, {}).get("total_us", 0)
        basis = service_intervals.get(service, {}).get("basis", "peer-only")
        print(f"[Service: {service}] dur_ms={format_ms_from_us(total_us)} basis={basis}")

        partners = siblings.get(service, [])
        if partners:
            for item in partners:
                contexts = item.get("contexts", [])
                context = contexts[0] if contexts else None
                parent_text = ""
                if context:
                    parent_text = (
                        f" parent={context['parent_service']} "
                        f"parent_op='{context['parent_op']}'"
                    )
                print(
                    "  - "
                    f"{item['service']}: overlap={format_ms_from_us(item['overlap_us'])}ms "
                    f"ratio={item['ratio']:.2f}"
                    f"{parent_text}"
                )
        else:
            print("  - (no sibling services over threshold)")

        service_fanout = fanout.get(service, [])
        if service_fanout:
            print("  Async fan-out candidates:")
            for cand in service_fanout:
                peers = ", ".join(cand["peers"]) if cand["peers"] else "unknown-peer"
                print(
                    "    * "
                    f"parent_op='{cand['parent_op']}' "
                    f"group_dur={format_ms_from_us(cand['group_dur_us'])}ms "
                    f"peers=[{peers}] spans={cand['span_count']}"
                )


def build_json_output(
    args: argparse.Namespace,
    trace_id: str,
    span_id: str,
    request_status: int,
    request_elapsed_ms: float,
    entry_request_url: str,
    detected_base_path: str,
    service_intervals: Dict[str, Dict[str, Any]],
    siblings: Dict[str, List[Dict[str, Any]]],
    fanout: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    services = []
    all_services = sorted(
        set(service_intervals.keys()) | set(siblings.keys()) | set(fanout.keys())
    )
    service_to_id = {service: idx for idx, service in enumerate(all_services, start=1)}
    id_to_service = {idx: service for service, idx in service_to_id.items()}

    edge_pairs = set()
    for left_service, partners in siblings.items():
        for item in partners:
            right_service = item["service"]
            if left_service == right_service:
                continue
            if left_service not in service_to_id or right_service not in service_to_id:
                continue
            left_id = service_to_id[left_service]
            right_id = service_to_id[right_service]
            edge_pairs.add((min(left_id, right_id), max(left_id, right_id)))

    adjacency: Dict[int, set] = {}
    for left_id, right_id in edge_pairs:
        adjacency.setdefault(left_id, set()).add(right_id)
        adjacency.setdefault(right_id, set()).add(left_id)

    sibling_sets: List[str] = []
    visited = set()
    for start in sorted(adjacency.keys()):
        if start in visited:
            continue
        stack = [start]
        component = set()
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            component.add(node)
            for nxt in adjacency.get(node, set()):
                if nxt not in visited:
                    stack.append(nxt)
        if len(component) >= 2:
            ordered = sorted(component)
            sibling_sets.append("{" + ",".join(str(x) for x in ordered) + "}")

    for service in all_services:
        total_us = service_intervals.get(service, {}).get("total_us", 0)
        sibling_ids = sorted(
            {
                service_to_id[item["service"]]
                for item in siblings.get(service, [])
                if item["service"] in service_to_id
            }
        )
        fanout_candidates = [
            {
                "parent_span_id": cand["parent_span_id"],
                "parent_kind": cand["parent_kind"],
                "parent_op": cand["parent_op"],
                "group_dur_ms": round(cand["group_dur_us"] / 1000.0, 3),
                "peers": cand["peers"],
                "span_count": cand["span_count"],
                "span_ids": cand["span_ids"],
            }
            for cand in fanout.get(service, [])
        ]
        services.append(
            {
                "service_id": service_to_id[service],
                "service": service,
                "interval_basis": service_intervals.get(service, {}).get("basis", "peer-only"),
                "total_duration_ms": round(total_us / 1000.0, 3),
                "sibling_ids": sibling_ids,
                "fanout_candidates": fanout_candidates,
            }
        )

    return {
        "entry_url": entry_request_url,
        "entry_url_input": args.entry_url,
        "request_path": args.request_path,
        "jaeger_url": args.jaeger_url,
        "jaeger_base_path": detected_base_path,
        "trace_id": trace_id,
        "span_id": span_id,
        "request": {
            "method": args.method.upper(),
            "status_code": request_status,
            "elapsed_ms": round(request_elapsed_ms, 3),
        },
        "analysis": {
            "min_overlap_ms": args.min_overlap_ms,
            "service_index": {str(idx): id_to_service[idx] for idx in sorted(id_to_service)},
            "sibling_sets": sibling_sets,
            "services": services,
        },
    }


def save_json(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)


def main() -> int:
    args = parse_args()
    try:
        trace_id = normalize_hex(args.trace_id, 32, "trace_id")
        span_id = normalize_hex(args.span_id, 16, "span_id")
    except ValueError as exc:
        print(f"[ERROR] Invalid tracing IDs: {exc}", file=sys.stderr)
        return 2

    headers = build_propagation_headers(trace_id, span_id)
    try:
        request_url = build_entry_request_url(args.entry_url, args.request_path)
    except ValueError as exc:
        print(f"[ERROR] Invalid entry URL/path: {exc}", file=sys.stderr)
        return 2

    try:
        status_code, elapsed_ms = send_entry_request(
            entry_url=request_url,
            method=args.method,
            headers=headers,
            timeout_s=args.request_timeout_s,
        )
    except requests.RequestException as exc:
        print(f"[ERROR] Entry request failed: {exc}", file=sys.stderr)
        return 3

    print(f"Request sent: {args.method.upper()} {request_url}")
    print(
        f"trace_id={trace_id} span_id={span_id} status={status_code} "
        f"elapsed_ms={elapsed_ms:.3f}"
    )

    try:
        base_path = detect_jaeger_base_path(args.jaeger_url)
        print(f"Jaeger base path detected: '{base_path or '/'}'")
        trace = poll_trace(
            jaeger_url=args.jaeger_url,
            base_path=base_path,
            trace_id=trace_id,
            timeout_s=args.poll_timeout_s,
            interval_s=args.poll_interval_s,
            settle_s=args.trace_settle_s,
        )
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[ERROR] Jaeger trace retrieval failed: {exc}", file=sys.stderr)
        return 4

    spans, span_by_id = parse_trace(trace)
    if not spans:
        print("[ERROR] Trace found but has no parsable spans.", file=sys.stderr)
        return 5

    service_intervals = build_service_intervals(spans)
    siblings = compute_siblings(spans, span_by_id, service_intervals, args.min_overlap_ms)
    fanout = detect_fanout_candidates(spans, span_by_id)

    print_human_output(service_intervals, siblings, fanout)

    output_json = build_json_output(
        args=args,
        trace_id=trace_id,
        span_id=span_id,
        request_status=status_code,
        request_elapsed_ms=elapsed_ms,
        entry_request_url=request_url,
        detected_base_path=base_path,
        service_intervals=service_intervals,
        siblings=siblings,
        fanout=fanout,
    )

    if args.json_out:
        try:
            save_json(args.json_out, output_json)
            print(f"\nJSON saved: {args.json_out}")
        except OSError as exc:
            print(f"[ERROR] Failed to write JSON output: {exc}", file=sys.stderr)
            return 6

    return 0


if __name__ == "__main__":
    sys.exit(main())
