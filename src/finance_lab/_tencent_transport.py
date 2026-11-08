"""Single-response owned worker: deadline covers startup, headers, body and close.

Use an exec-style subprocess, not multiprocessing: library callers need no main
guard and the worker imports Requests only (no pandas or application startup).
The OS owns socket cleanup on forced exit. Only the Popen created here is killed.
"""

import json
import subprocess
import sys
from time import monotonic

import requests

_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
_MAX_BYTES = 1024 * 1024
_CLEANUP_TIMEOUT = 2.0


def _read_body(params: dict[str, str]) -> bytes:
    with requests.Session() as session:
        with session.get(_URL, params=params, timeout=(5, 15),
                         allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                response.raise_for_status()
                raise requests.HTTPError("Unexpected HTTP status")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if len(body) + len(chunk) > _MAX_BYTES:
                    raise requests.RequestException("Tencent response exceeds byte limit")
                body.extend(chunk)
            return bytes(body)


def _worker_main() -> None:
    try:
        params = json.loads(sys.stdin.buffer.read())
        body = _read_body(params)
    except Exception as exc:
        # Only a category crosses the boundary, never provider URLs/body/secrets.
        category = (
            "timeout" if isinstance(exc, requests.Timeout) else
            "proxy" if isinstance(exc, requests.exceptions.ProxyError) else
            "http" if isinstance(exc, requests.HTTPError) else "request"
        )
        sys.stderr.write(category)
        raise SystemExit(1) from None
    sys.stdout.buffer.write(body)


def request_body(params: dict[str, str], deadline: float) -> bytes:
    started = monotonic()
    # Inherit environment, interpreter and installed package resolution normally;
    # do not override routing, trust_env, proxies, TLS or global Requests settings.
    worker = subprocess.Popen(
        [sys.executable, "-B", "-m", "finance_lab._tencent_transport"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        remaining = deadline - (monotonic() - started)
        if remaining <= 0:
            raise requests.Timeout("Tencent response deadline exceeded")
        try:
            body, error = worker.communicate(json.dumps(params).encode(), timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise requests.Timeout("Tencent response deadline exceeded") from exc
        if monotonic() - started >= deadline:
            raise requests.Timeout("Tencent response deadline exceeded")
        if worker.returncode != 0:
            kind = {
                b"timeout": requests.Timeout, b"proxy": requests.exceptions.ProxyError,
                b"http": requests.HTTPError,
            }.get(error, requests.RequestException)
            raise kind("Tencent worker request failed")
        if len(body) > _MAX_BYTES:
            raise requests.RequestException("Tencent response exceeds byte limit")
        return body
    finally:
        # No daemon/thread can retain a market lock or run beyond this call. Kill
        # even when interrupted by KeyboardInterrupt; reap before ordinary return.
        try:
            if worker.poll() is None:
                worker.kill()
            # communicate also joins Windows' pipe reader threads and reaps the
            # child. Waiting only on its PID would leave pipe readers unjoined.
            worker.communicate(timeout=_CLEANUP_TIMEOUT)
        finally:
            for pipe in (worker.stdin, worker.stdout, worker.stderr):
                if pipe is not None:
                    pipe.close()


if __name__ == "__main__":
    _worker_main()
