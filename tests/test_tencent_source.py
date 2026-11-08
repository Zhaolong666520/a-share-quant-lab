"""Tencent daily protocol contracts; only the owned HTTP boundary is replaced."""

from __future__ import annotations

import io
import json
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest
import requests

from finance_lab.config import Instrument
from finance_lab.sources import CANONICAL_COLUMNS, DataSourceError

START, END = date(2024, 1, 1), date(2024, 1, 10)
INDEX = Instrument("000300", "sh", "index", "index")
ETF = Instrument("510300", "sh", "etf", "etf")
URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def _row(day: str = "2024-01-02") -> list[Any]:
    return [day, "10", "10.5", "11", "9", "100", {}, "1.2", "0.105", "0", "0"]


def _payload(rows: list[Any] | None = None, symbol: str = "sh510300") -> dict[str, Any]:
    return {"code": 0, "msg": "", "data": {symbol: {
        "day": [_row()] if rows is None else rows, "qt": {}, "version": "fixture",
        "prec": "3", "mx_price": {}, "fsStartDate": "fixture",
    }}}


class TrackedResponse(requests.Response):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__()
        self.status_code = status
        self.headers["Content-Type"] = "application/json; charset=utf-8"
        self.raw = io.BytesIO(body)
        self.closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


def _http(
    monkeypatch: pytest.MonkeyPatch, payloads: list[Any], *, status: int = 200,
    failure: Exception | None = None,
) -> tuple[list[TrackedResponse], list[dict[str, Any]], list[requests.Session]]:
    responses = [TrackedResponse(
        p if isinstance(p, bytes) else json.dumps(p).encode(), status
    ) for p in payloads]
    calls: list[dict[str, Any]] = []
    closed_sessions: list[requests.Session] = []
    original_close = requests.Session.close

    def send(session: requests.Session, request: requests.PreparedRequest,
             **kwargs: Any) -> requests.Response:
        calls.append({"url": request.url, "kwargs": kwargs, "session": session,
                      "trust_env": session.trust_env, "verify": session.verify,
                      "auth": session.auth, "proxies": dict(session.proxies)})
        if failure is not None:
            raise failure
        response = responses[len(calls) - 1]
        response.request = request
        response.url = request.url or URL
        return response

    def close(session: requests.Session) -> None:
        closed_sessions.append(session)
        original_close(session)

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(requests.Session, "close", close)
    from finance_lab import _tencent_transport, tencent_source

    # Replace only process supervision for detailed in-process HTTP semantics;
    # real deadline/worker tests separately exercise the actual supervised path.
    monkeypatch.setattr(tencent_source, "_request_body", _tencent_transport._read_body)
    return responses, calls, closed_sessions


def _fetch(instrument: Instrument = ETF, start: date = START, end: date = END) -> pd.DataFrame:
    # Import inside behavior tests: a missing feature must fail after test entry.
    from finance_lab.tencent_source import fetch_tencent

    return fetch_tencent(instrument, start, end)


@pytest.mark.parametrize("instrument,multiplier", [(ETF, 100), (INDEX, 1)])
def test_valid_daily_bars_units_metadata_and_owned_transport(
    monkeypatch: pytest.MonkeyPatch, instrument: Instrument, multiplier: int,
) -> None:
    responses, calls, closed = _http(monkeypatch, [_payload(
        [_row(), _row("2024-01-03")], instrument.exchange + instrument.code
    )])
    frame = _fetch(instrument)
    assert list(frame.columns) == CANONICAL_COLUMNS
    assert frame["trade_date"].tolist() == list(pd.to_datetime(["2024-01-02", "2024-01-03"]))
    assert frame[["open", "close", "high", "low", "volume", "amount"]].to_dict("list") == {
        "open": [10, 10], "close": [10.5, 10.5], "high": [11, 11], "low": [9, 9],
        "volume": [100 * multiplier] * 2, "amount": [1050] * 2,
    }
    assert frame["symbol"].tolist() == [instrument.symbol] * 2
    assert frame["source"].tolist() == ["tencent"] * 2
    assert frame["volume_unit"].tolist() == ["share"] * 2
    assert frame["adjustment"].tolist() == ["none"] * 2
    assert all(datetime.fromisoformat(x).utcoffset().total_seconds() == 0
               for x in frame["ingested_at"])
    assert frame.attrs["source_diagnostics"] == {
        "received_rows": 2, "accepted_rows": 2, "rejected_rows": 0, "rejection_reasons": {},
    }
    assert frame.attrs["tencent_transport"] == {
        "protocol": "akshare_stock_zh_a_hist_tx_day_v1", "requested_start": "2024-01-01",
        "requested_end": "2024-01-10",
        "provider_windows": [{"start": "2024-01-01", "end": "2025-12-31"}],
        "raw_received_rows": 2, "unique_valid_rows": 2, "exact_overlap_rows": 0,
        "outside_requested_rows": 0,
    }
    assert len(calls) == 1 and calls[0]["url"].startswith(URL + "?")
    assert calls[0]["kwargs"]["timeout"] == (5, 15)
    assert calls[0]["kwargs"]["stream"] is True
    assert calls[0]["kwargs"]["allow_redirects"] is False
    assert calls[0]["kwargs"]["verify"] is True and calls[0]["kwargs"]["cert"] is None
    assert calls[0]["session"].get_adapter(URL).max_retries.total == 0
    assert calls[0]["trust_env"] is True and calls[0]["verify"] is True
    assert calls[0]["auth"] is None and calls[0]["proxies"] == {}
    assert closed == [calls[0]["session"]] and all(r.closed for r in responses)


def test_multiyear_overlap_numeric_formatting_and_window_statistics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlap = _row("2024-01-02")
    overlap[1] = "10.00"
    responses, calls, closed = _http(monkeypatch, [
        _payload([_row("2022-12-30"), _row("2023-12-29"), _row("2024-01-02")]),
        _payload([overlap, _row("2024-01-03"), _row("2025-01-02")]),
    ])
    frame = _fetch(start=date(2023, 12, 1), end=date(2024, 1, 10))
    assert frame["trade_date"].tolist() == list(pd.to_datetime([
        "2023-12-29", "2024-01-02", "2024-01-03",
    ]))
    meta = frame.attrs["tencent_transport"]
    assert (meta["raw_received_rows"], meta["unique_valid_rows"], meta["exact_overlap_rows"],
            meta["outside_requested_rows"]) == (6, 5, 1, 2)
    assert frame.attrs["source_diagnostics"]["received_rows"] == 3
    assert frame.attrs["source_diagnostics"]["rejected_rows"] == 0
    assert json.loads(json.dumps(meta)) == meta
    for call, year in zip(calls, [2023, 2024], strict=True):
        assert parse_qs(urlsplit(call["url"]).query, keep_blank_values=True) == {
            "_var": [f"kline_day{year}"],
            "param": [f"sh510300,day,{year}-01-01,{year + 1}-12-31,640,"],
            "r": ["0.8205512681390605"],
        }
    assert len({id(c["session"]) for c in calls}) == 2
    assert closed == [call["session"] for call in calls] and all(r.closed for r in responses)


@pytest.mark.parametrize("wrapper", ["json", "jsonp", "jsonp_semicolon"])
def test_exact_json_or_requested_jsonp_and_ignored_metadata(
    monkeypatch: pytest.MonkeyPatch, wrapper: str,
) -> None:
    row = _row()[:9]
    row[6], row[7] = {"untrusted": "do not execute"}, {"unused": True}
    body = json.dumps(_payload([row]))
    if wrapper != "json":
        body = "kline_day2024=" + body + (";" if wrapper.endswith("semicolon") else "")
    _http(monkeypatch, [body.encode()])
    assert _fetch()["amount"].tolist() == [1050]


@pytest.mark.parametrize("body", [
    b"not json", b"[1]", b"null", b"{}", b'{"code":0,"code":0,"data":{}}',
    b'{"code":0,"data":{},"hidden":NaN}', b'{"code":0,"data":{},"hidden":1e999}',
    b'var kline_day2024={"code":0};', b'kline_day2023={"code":0};',
    b'garbage={"code":0};', b'{"code":0};alert(1)', b'kline_day2024={};junk',
    b'kline_day2024({});', b'\xff',
])
def test_bad_json_fails_without_exposing_payload(
    monkeypatch: pytest.MonkeyPatch, body: bytes,
) -> None:
    responses, _, closed = _http(monkeypatch, [body])
    with pytest.raises(DataSourceError) as raised:
        _fetch()
    assert raised.value.stage == "response"
    assert raised.value.accepted_rows == 0
    assert responses[0].closed and len(closed) == 1


@pytest.mark.parametrize("change", [
    "wrong_symbol", "missing_data", "data_list", "quote_list", "missing_day", "empty_day",
    "day_dict", "qfqday", "hfqday", "string_code", "bool_code", "nonzero_code",
    "short_row", "row_dict", "container_price", "none_volume", "bool_amount",
])
def test_wrong_shape_symbol_or_adjusted_only_response_is_rejected(
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    payload = _payload()
    quote = payload["data"]["sh510300"]
    if change == "wrong_symbol":
        payload["data"] = {"sh000300": quote}
    elif change == "missing_data":
        del payload["data"]
    elif change == "data_list":
        payload["data"] = []
    elif change == "quote_list":
        payload["data"]["sh510300"] = []
    elif change == "missing_day":
        del quote["day"]
    elif change == "empty_day":
        quote["day"] = []
    elif change == "day_dict":
        quote["day"] = {"private": "secret"}
    elif change in {"qfqday", "hfqday"}:
        quote[change] = quote.pop("day")
    elif change.endswith("code"):
        payload["code"] = {"string_code": "0", "bool_code": False, "nonzero_code": 1}[change]
    elif change == "short_row":
        quote["day"] = [_row()[:8]]
    elif change == "row_dict":
        quote["day"] = [{"private": "secret"}]
    else:
        index, value = {"container_price": (1, {}), "none_volume": (5, None),
                        "bool_amount": (8, True)}[change]
        quote["day"][0][index] = value
    responses, _, closed = _http(monkeypatch, [payload])
    with pytest.raises(DataSourceError) as raised:
        _fetch()
    assert raised.value.stage == "response"
    assert "secret" not in str(raised.value)
    assert raised.value.received_rows == raised.value.rejected_rows
    assert responses[0].closed and len(closed) == 1


@pytest.mark.parametrize("index,value", [
    (0, "2024-01-02T00:00:00"), (0, "2024-1-02"), (0, "2024-02-30"),
    (0, "2026-01-01"), (0, "9999-01-01"), (0, "private token=secret"),
    (1, "NaN"), (2, "inf"), (3, "bad"), (4, "0"), (5, "-1"), (8, "-0.1"),
    (8, "0"), (1, "12"), (2, "8"), (3, "8"), (4, "12"),
    (8, "1e308"), (5, "1e308"),
])
def test_all_consumed_values_are_validated_before_trimming(
    monkeypatch: pytest.MonkeyPatch, index: int, value: Any,
) -> None:
    bad = _row("2023-12-29")  # Outside user window; must still be validated.
    bad[index] = value
    _http(monkeypatch, [_payload([_row(), bad])])
    with pytest.raises(DataSourceError) as raised:
        _fetch()
    assert raised.value.stage == "validation"
    assert (raised.value.received_rows, raised.value.accepted_rows,
            raised.value.rejected_rows) == (2, 0, 2)
    assert "secret" not in str(raised.value)


def test_zero_volume_and_amount_are_real_values_not_filled(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()
    row[5], row[8] = "0", "0"
    _http(monkeypatch, [_payload([row])])
    assert _fetch()[["volume", "amount"]].to_dict("list") == {"volume": [0], "amount": [0]}


@pytest.mark.parametrize("different", [False, True])
def test_duplicates_within_one_response_are_not_annual_overlap(
    monkeypatch: pytest.MonkeyPatch, different: bool,
) -> None:
    other = _row()
    other[6], other[7], other[9] = {"not": "consumed"}, "unused", "changed"
    if different:
        other[8] = "0.106"
    _http(monkeypatch, [_payload([_row(), other])])
    with pytest.raises(DataSourceError) as raised:
        _fetch()
    assert raised.value.stage == "validation" and raised.value.rejected_rows == 2


def test_conflict_across_annual_windows_rejects_entire_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = _row()
    other[2] = "10.6"
    _http(monkeypatch, [_payload(), _payload([other])])
    with pytest.raises(DataSourceError) as raised:
        _fetch(start=date(2023, 1, 1))
    assert raised.value.stage == "validation" and raised.value.rejected_rows == 2


def test_empty_final_window_is_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _http(monkeypatch, [_payload([_row("2023-12-29")])])
    with pytest.raises(DataSourceError) as raised:
        _fetch()
    assert raised.value.stage == "validation" and raised.value.accepted_rows == 0


@pytest.mark.parametrize("status", [301, 401, 403, 407, 429, 500])
def test_failed_http_status_never_retries_and_closes(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    responses, calls, closed = _http(monkeypatch, [_payload()], status=status)
    with pytest.raises(DataSourceError) as raised:
        _fetch()
    assert raised.value.stage == "request" and len(calls) == 1
    assert raised.value.received_rows == raised.value.rejected_rows == 0
    assert responses[0].closed and len(closed) == 1


@pytest.mark.parametrize("failure", [
    requests.Timeout("https://private.invalid/?token=secret"),
    requests.exceptions.ProxyError("private token=secret"),
])
def test_request_failure_preserves_cause_but_surfaces_generic_message(
    monkeypatch: pytest.MonkeyPatch, failure: Exception,
) -> None:
    _, calls, closed = _http(monkeypatch, [], failure=failure)
    with pytest.raises(DataSourceError) as raised:
        _fetch()
    assert raised.value.stage == "request" and raised.value.__cause__ is failure
    assert "private" not in str(raised.value) and "secret" not in str(raised.value)
    assert len(calls) == 1 and len(closed) == 1


def test_stream_failure_closes_response_and_session(monkeypatch: pytest.MonkeyPatch) -> None:
    responses, calls, closed = _http(monkeypatch, [_payload()])
    failure = requests.Timeout("private token=secret")

    def chunks(*args: Any, **kwargs: Any) -> Any:
        raise failure

    responses[0].iter_content = chunks
    with pytest.raises(DataSourceError) as raised:
        _fetch()
    assert raised.value.stage == "request" and raised.value.__cause__ is failure
    assert "private" not in str(raised.value)
    assert responses[0].closed and len(calls) == len(closed) == 1


def test_decoded_response_byte_limit_closes_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    responses, calls, closed = _http(monkeypatch, [b" " * (1024 * 1024 + 1)])
    with pytest.raises(DataSourceError) as raised:
        _fetch()
    assert raised.value.stage == "request" and raised.value.received_rows == 0
    assert responses[0].closed and len(calls) == len(closed) == 1


@pytest.mark.parametrize("instrument,start,end", [
    (Instrument("399001", "sz", "index", "index"), START, END),
    (Instrument("510300", "sh", "stock", "stock"), START, END),
    (Instrument("000300", "sz", "index", "index"), START, END),
    (Instrument("000300", "sh", "etf", "etf"), START, END),
    (ETF, END, START), (ETF, "2024-01-01", END),
    (ETF, datetime(2024, 1, 1), END), (ETF, START, datetime(2024, 1, 10)),
    (ETF, date(1999, 12, 31), date(2024, 1, 1)),
    (ETF, date(9999, 1, 1), date(9999, 1, 2)),
])
def test_invalid_instrument_or_date_window_fails_before_network(
    monkeypatch: pytest.MonkeyPatch, instrument: Instrument, start: Any, end: Any,
) -> None:
    _, calls, closed = _http(monkeypatch, [])
    with pytest.raises(DataSourceError) as raised:
        _fetch(instrument, start, end)
    assert raised.value.stage == "validation"
    assert raised.value.received_rows == raised.value.rejected_rows == 0
    assert calls == closed == []


def test_future_row_rejected_even_with_wide_provider_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _http(monkeypatch, [_payload([_row("9998-01-02")])])
    with pytest.raises(DataSourceError) as raised:
        _fetch(start=date(9998, 1, 1), end=date(9998, 1, 3))
    assert raised.value.stage == "validation" and raised.value.rejected_rows == 1


@pytest.mark.parametrize("prefix,suffix", [("\u00a0", ""), ("", "\u00a0")])
def test_non_json_whitespace_is_not_removed_heuristically(
    monkeypatch: pytest.MonkeyPatch, prefix: str, suffix: str,
) -> None:
    _http(monkeypatch, [(prefix + json.dumps(_payload()) + suffix).encode()])
    with pytest.raises(DataSourceError) as raised:
        _fetch()
    assert raised.value.stage == "response"


@pytest.mark.parametrize("invalid", [
    '{"repeated":1,"repeated":2}', 'NaN', 'Infinity', '-Infinity', '1e999',
])
def test_invalid_json_inside_unused_metadata_cannot_hide_in_valid_envelope(
    monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    body = json.dumps(_payload()).replace('"qt": {}', '"qt": ' + invalid)
    _http(monkeypatch, [body.encode()])
    with pytest.raises(DataSourceError) as raised:
        _fetch()
    assert raised.value.stage == "response" and raised.value.__cause__ is not None


def test_exact_byte_limit_is_per_response_not_cumulative(monkeypatch: pytest.MonkeyPatch) -> None:
    first = json.dumps(_payload([_row("2023-12-29")])).encode()
    second = json.dumps(_payload()).encode()
    _http(monkeypatch, [body + b" " * (1024 * 1024 - len(body)) for body in [first, second]])
    frame = _fetch(start=date(2023, 12, 1))
    assert len(frame) == 2 and frame.attrs["tencent_transport"]["raw_received_rows"] == 2


def test_later_request_failure_counts_only_received_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    responses, calls, closed = _http(monkeypatch, [_payload([_row("2023-12-29")]), _payload()])
    responses[1].status_code = 429
    with pytest.raises(DataSourceError) as raised:
        _fetch(start=date(2023, 12, 1))
    error = raised.value
    assert error.stage == "request"
    assert (error.received_rows, error.accepted_rows, error.rejected_rows) == (1, 0, 1)
    assert len(calls) == len(closed) == 2 and all(r.closed for r in responses)


def test_at_most_25_calendar_years_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = [_payload([_row(f"{year}-01-02")]) for year in range(2000, 2025)]
    _http(monkeypatch, payloads)
    frame = _fetch(start=date(2000, 1, 1), end=date(2024, 12, 31))
    assert len(frame) == 25 and len(frame.attrs["tencent_transport"]["provider_windows"]) == 25
