#!/usr/bin/env python3
import argparse
import csv
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

def percentile(vals, p):
    vals = sorted(vals)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return vals[int(k)]
    return vals[f] * (c - k) + vals[c] * (k - f)

def send(base_url, uid, timeout=15):
    payload = {
        'username': f'username_{uid}',
        'user_id': str(uid),
        'text': 'hello @username_2 http://a.example.com',
        'media_ids': '[]',
        'media_types': '[]',
        'post_type': '0',
    }
    data = urllib.parse.urlencode(payload).encode('utf-8')
    req = urllib.request.Request(base_url, data=data, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            status = resp.getcode()
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        status = e.code
    dt = (time.perf_counter() - t0) * 1000.0
    return dt, status, body[:200]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base-url', default='http://127.0.0.1:18084/wrk2-api/post/compose')
    ap.add_argument('--count', type=int, default=1000)
    ap.add_argument('--id-start', type=int, default=1)
    ap.add_argument('--id-end', type=int, default=200)
    ap.add_argument('--probe-quantile', type=float, default=0.90)
    ap.add_argument('--out-dir', default=str(Path(__file__).resolve().parent.parent / 'results'))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')
    probe_csv = out_dir / f'compose_probe_{ts}.csv'
    raw_csv = out_dir / f'compose_rr_{args.count}_{ts}.csv'
    summary_json = out_dir / f'compose_rr_{args.count}_{ts}.json'

    for _ in range(30):
        try:
            send(args.base_url, args.id_start)
            break
        except Exception:
            time.sleep(1)

    probe_rows = []
    successes = []
    failures = []
    for uid in range(args.id_start, args.id_end + 1):
        lat, status, body = send(args.base_url, uid)
        ok = status == 200 and 'Successfully upload post' in body
        probe_rows.append([uid, f'{lat:.6f}', status, int(ok), body])
        if ok:
            successes.append((uid, lat))
        else:
            failures.append({'user_id': uid, 'status': status, 'body_head': body})

    success_lats = [lat for _, lat in successes]
    cutoff = percentile(success_lats, args.probe_quantile)
    efficient_ids = [uid for uid, lat in successes if lat <= cutoff]

    with probe_csv.open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['user_id', 'lat_ms', 'status', 'ok', 'body_head'])
        w.writerows(probe_rows)

    for uid in efficient_ids:
        send(args.base_url, uid)

    rows = []
    lats = []
    status_hist = {}
    for i in range(args.count):
        uid = efficient_ids[i % len(efficient_ids)]
        lat, status, body = send(args.base_url, uid)
        ok = status == 200 and 'Successfully upload post' in body
        rows.append([i + 1, uid, f'{lat:.6f}', status, int(ok), body])
        if ok:
            lats.append(lat)
        status_hist[str(status)] = status_hist.get(str(status), 0) + 1

    with raw_csv.open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['idx', 'user_id', 'lat_ms', 'status', 'ok', 'body_head'])
        w.writerows(rows)

    summary = {
        'base_url': args.base_url,
        'count': args.count,
        'probe_success_count': len(successes),
        'probe_fail_count': len(failures),
        'probe_quantile': args.probe_quantile,
        'probe_cutoff_ms': cutoff,
        'efficient_id_count': len(efficient_ids),
        'efficient_ids_first30': efficient_ids[:30],
        'bad_ids': failures,
        'ok': sum(1 for r in rows if r[4] == 1),
        'fail': sum(1 for r in rows if r[4] == 0),
        'status_hist': status_hist,
        'mean_ms': statistics.mean(lats) if lats else None,
        'variance_ms2': statistics.pvariance(lats) if lats else None,
        'p95_ms': percentile(lats, 0.95),
        'p99_ms': percentile(lats, 0.99),
        'min_ms': min(lats) if lats else None,
        'max_ms': max(lats) if lats else None,
        'probe_csv': str(probe_csv),
        'raw_csv': str(raw_csv),
    }
    summary_json.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(summary_json)

if __name__ == '__main__':
    main()
