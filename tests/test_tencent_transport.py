"""Offline real-process regression of blocking Requests/urllib3 transport."""

import subprocess
import sys
from datetime import date
from time import monotonic
from typing import Any

import pytest
import requests

from finance_lab import _tencent_transport as transport
from finance_lab import tencent_source
from finance_lab.config import Instrument
from finance_lab.sources import DataSourceError

_POPEN = subprocess.Popen
# Real Requests Response + real urllib3 pre-yield stream read. The tiny progress
# sleeps represent an internally accumulating/trickling body, not yielded chunks.
# Only Session.send is substituted; URL, parameters, reader and worker are real.
_FIXTURE_WORKER = r'''
import io, json, sys, time
import requests
from urllib3.response import HTTPResponse
from finance_lab._tencent_transport import _worker_main
mode = sys.argv[1]
payload = {"code": 0, "data": {"sh510300": {"day": [
    ["2024-01-02", "10", "10.5", "11", "9", "100", {}, "1.2", "0.105"]
]}}}
class Body(io.BytesIO):
    def read(self, size=-1):
        if mode == "body":
            sys.stderr.write("entered_blocked_body:" + str(size))
            sys.stderr.flush()
            for _ in range(200):
                time.sleep(0.05)
        return super().read(size)
def send(session, request, **kwargs):
    if mode == "headers":
        sys.stderr.write("entered_blocked_headers")
        sys.stderr.flush()
        time.sleep(10)
    if mode in {"timeout", "proxy", "http"}:
        kind = {"timeout": requests.Timeout, "proxy": requests.exceptions.ProxyError,
                "http": requests.HTTPError}[mode]
        raise kind("https://private.invalid/?token=secret")
    response = requests.Response()
    response.status_code = 200
    response.raw = HTTPResponse(body=Body(json.dumps(payload).encode()), preload_content=False)
    response.request = request
    return response
requests.Session.send = send
_worker_main()
'''


def _workers(monkeypatch: pytest.MonkeyPatch, mode: str) -> list[Any]:
    original = _POPEN
    owned: list[Any] = []

    class CapturedWorker(original):
        marker = b""

        def __init__(self, command: Any, **kwargs: Any) -> None:
            assert command == [sys.executable, "-B", "-m", "finance_lab._tencent_transport"]
            super().__init__([sys.executable, "-B", "-c", _FIXTURE_WORKER, mode], **kwargs)
            owned.append(self)

        def communicate(self, *args: Any, **kwargs: Any) -> Any:
            try:
                result = super().communicate(*args, **kwargs)
                self.marker = result[1]
                return result
            except subprocess.TimeoutExpired as exc:
                self.marker = exc.stderr or b""
                raise

    monkeypatch.setattr(transport.subprocess, "Popen", CapturedWorker)
    return owned


@pytest.mark.parametrize("mode", ["body", "headers"])
def test_deadline_interrupts_blocked_request_reaps_worker_and_later_succeeds(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    owned = _workers(monkeypatch, mode)
    monkeypatch.setattr(tencent_source, "_MAX_ELAPSED", 2.0)
    started = monotonic()
    with pytest.raises(DataSourceError) as raised:
        tencent_source.fetch_tencent(Instrument("510300", "sh", "etf", "etf"),
                                    date(2024, 1, 1), date(2024, 1, 3))
    elapsed = monotonic() - started
    assert raised.value.stage == "request" and isinstance(raised.value.__cause__, requests.Timeout)
    assert (raised.value.received_rows, raised.value.accepted_rows,
            raised.value.rejected_rows) == (0, 0, 0)
    assert "private" not in str(raised.value) and "secret" not in str(raised.value)
    assert owned[0].marker == (
        b"entered_blocked_body:65536" if mode == "body" else b"entered_blocked_headers"
    )
    assert elapsed < 2.0 + transport._CLEANUP_TIMEOUT + 0.5
    assert owned[0].returncode is not None and owned[0].poll() is not None
    assert all(pipe.closed for pipe in (owned[0].stdin, owned[0].stdout, owned[0].stderr))
    if sys.platform == "win32":
        assert not owned[0].stdout_thread.is_alive() and not owned[0].stderr_thread.is_alive()
    successful = _workers(monkeypatch, "success")
    frame = tencent_source.fetch_tencent(Instrument("510300", "sh", "etf", "etf"),
                                        date(2024, 1, 1), date(2024, 1, 3))
    assert len(frame) == 1 and frame["volume"].tolist() == [10000]
    assert frame["amount"].tolist() == [1050] and frame["source"].tolist() == ["tencent"]
    assert successful[0].returncode == 0 and successful[0].poll() == 0
    print(f"blocked={mode}; marker={owned[0].marker.decode()}; budget=2.0s; "
          f"elapsed={elapsed:.3f}s; reaped_exit={owned[0].returncode}; later_success=True")


@pytest.mark.parametrize("mode,kind", [
    ("timeout", requests.Timeout), ("proxy", requests.exceptions.ProxyError),
    ("http", requests.HTTPError),
])
def test_worker_error_category_survives_without_private_message(
    monkeypatch: pytest.MonkeyPatch, mode: str, kind: type[Exception],
) -> None:
    owned = _workers(monkeypatch, mode)
    with pytest.raises(kind) as raised:
        transport.request_body({"_var": "kline_day2024"}, 2.0)
    assert "private" not in str(raised.value) and "secret" not in str(raised.value)
    assert owned[0].returncode == 1 and owned[0].poll() == 1


def test_worker_startup_is_included_in_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    owned = _workers(monkeypatch, "success")
    ticks = iter([0.0, 46.0])
    monkeypatch.setattr(transport, "monotonic", lambda: next(ticks))
    with pytest.raises(requests.Timeout):
        transport.request_body({}, 45.0)
    assert owned[0].returncode is not None and owned[0].poll() is not None


def test_completed_worker_cannot_publish_after_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    owned = _workers(monkeypatch, "success")
    ticks = iter([0.0, 0.0, 46.0])
    monkeypatch.setattr(transport, "monotonic", lambda: next(ticks))
    with pytest.raises(requests.Timeout):
        transport.request_body({}, 45.0)
    assert owned[0].returncode == 0 and owned[0].poll() == 0


def test_caller_interruption_also_reaps_only_owned_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    owned = _workers(monkeypatch, "body")
    actual_communicate = transport.subprocess.Popen.communicate
    interrupted = False

    def interrupt_once(worker: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return actual_communicate(worker, *args, **kwargs)

    monkeypatch.setattr(transport.subprocess.Popen, "communicate", interrupt_once)
    with pytest.raises(KeyboardInterrupt):
        transport.request_body({}, 2.0)
    assert owned[0].returncode is not None and owned[0].poll() is not None
    assert all(pipe.closed for pipe in (owned[0].stdin, owned[0].stdout, owned[0].stderr))
