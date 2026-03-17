#!/usr/bin/env python3

import http.client
import json
import os
import socket
import struct
import threading
import time
from socketserver import BaseRequestHandler, ThreadingTCPServer


LISTEN_HOST = os.getenv("MCOZ_THRIFT_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("MCOZ_THRIFT_LISTEN_PORT", "19094"))
UPSTREAM_HOST = os.getenv("MCOZ_THRIFT_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.getenv("MCOZ_THRIFT_UPSTREAM_PORT", "9090"))
GATE_HOST = os.getenv("MCOZ_GATE_HOST", "127.0.0.1")
GATE_PORT = int(os.getenv("MCOZ_GATE_PORT", "19093"))
GATE_TRIGGER_PATH = os.getenv("MCOZ_GATE_TRIGGER_PATH", "/trigger")
GATE_TIMEOUT_SEC = float(os.getenv("MCOZ_GATE_TIMEOUT_SEC", "0.2"))
CONNECT_TIMEOUT_SEC = float(os.getenv("MCOZ_THRIFT_CONNECT_TIMEOUT_SEC", "2.0"))
BUFFER_SIZE = int(os.getenv("MCOZ_THRIFT_BUFFER_SIZE", "65536"))
MAX_FRAME_SIZE = int(os.getenv("MCOZ_THRIFT_MAX_FRAME_SIZE", str(16 * 1024 * 1024)))
SERVICE_NAME = os.getenv("MCOZ_THRIFT_SERVICE", "").strip()
VERBOSE = os.getenv("MCOZ_THRIFT_VERBOSE", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

_CONN_SEQ = 0
_SEQ_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()


def _next_conn_id():
    global _CONN_SEQ
    with _SEQ_LOCK:
        _CONN_SEQ += 1
        return _CONN_SEQ


def _read_exact(sock, size):
    if size <= 0:
        return b""
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


def _thread_http_conn():
    conn = getattr(_THREAD_LOCAL, "conn", None)
    if conn is None:
        conn = http.client.HTTPConnection(GATE_HOST, GATE_PORT, timeout=GATE_TIMEOUT_SEC)
        _THREAD_LOCAL.conn = conn
    return conn


def _reset_thread_http_conn():
    conn = getattr(_THREAD_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _THREAD_LOCAL.conn = None


def _parse_method_name(frame):
    if len(frame) < 4:
        return ""
    first = struct.unpack("!I", frame[:4])[0]
    # Strict TBinaryProtocol: version/type then method string.
    if (first & 0xFFFF0000) == 0x80010000:
        if len(frame) < 8:
            return ""
        name_len = struct.unpack("!I", frame[4:8])[0]
        if name_len < 0 or 8 + name_len > len(frame):
            return ""
        raw = frame[8 : 8 + name_len]
        return raw.decode("utf-8", errors="replace")
    # Old-style TBinaryProtocol: first field is method string length.
    name_len = first
    if name_len < 0 or 4 + name_len > len(frame):
        return ""
    raw = frame[4 : 4 + name_len]
    return raw.decode("utf-8", errors="replace")


def _trigger_path(method):
    method = method or "unknown"
    if SERVICE_NAME:
        return f"/thrift/{SERVICE_NAME}/{method}"
    return f"/thrift/{method}"


def _trigger_gate(request_id, method):
    payload = {
        "path": _trigger_path(method),
        "request_id": request_id,
        "headers": {
            "x-mcoz-protocol": "thrift",
            "x-mcoz-method": method or "unknown",
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        conn = _thread_http_conn()
        conn.request("POST", GATE_TRIGGER_PATH, body=raw, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        if VERBOSE:
            print(
                f"[mcoz-thrift-adapter] trigger request_id={request_id} method={method or 'unknown'} "
                f"status={resp.status} body={body[:120]!r}",
                flush=True,
            )
    except Exception as exc:
        _reset_thread_http_conn()
        if VERBOSE:
            print(
                f"[mcoz-thrift-adapter] trigger-failed request_id={request_id} "
                f"method={method or 'unknown'} err={exc}",
                flush=True,
            )


def _close_sock(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass


class _RelayThread(threading.Thread):
    def __init__(self, src, dst):
        super().__init__(daemon=True)
        self._src = src
        self._dst = dst

    def run(self):
        try:
            while True:
                chunk = self._src.recv(BUFFER_SIZE)
                if not chunk:
                    break
                self._dst.sendall(chunk)
        except Exception:
            pass
        finally:
            try:
                self._dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass


class Handler(BaseRequestHandler):
    def handle(self):
        conn_id = _next_conn_id()
        downstream = self.request
        upstream = None
        try:
            upstream = socket.create_connection(
                (UPSTREAM_HOST, UPSTREAM_PORT), timeout=CONNECT_TIMEOUT_SEC
            )
            upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            relay = _RelayThread(upstream, downstream)
            relay.start()

            req_seq = 0
            while True:
                header = _read_exact(downstream, 4)
                if header is None:
                    break
                frame_size = struct.unpack("!I", header)[0]
                if frame_size <= 0 or frame_size > MAX_FRAME_SIZE:
                    raise ValueError(f"invalid thrift frame size: {frame_size}")
                frame = _read_exact(downstream, frame_size)
                if frame is None:
                    break
                req_seq += 1
                method = _parse_method_name(frame)
                request_id = f"thrift-{conn_id}-{req_seq}-{int(time.time() * 1e6)}"
                _trigger_gate(request_id, method)
                upstream.sendall(header)
                upstream.sendall(frame)
        finally:
            _close_sock(upstream)
            _close_sock(downstream)
            _reset_thread_http_conn()


class Server(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    print(
        f"[mcoz-thrift-adapter] listen={LISTEN_HOST}:{LISTEN_PORT} "
        f"upstream={UPSTREAM_HOST}:{UPSTREAM_PORT} gate={GATE_HOST}:{GATE_PORT}{GATE_TRIGGER_PATH} "
        f"service={SERVICE_NAME or '-'}",
        flush=True,
    )
    with Server((LISTEN_HOST, LISTEN_PORT), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
