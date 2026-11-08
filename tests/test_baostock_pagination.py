"""Synthetic rows through the installed SDK; all network boundaries are replaced."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import baostock as bs
import pandas as pd
import pytest
from baostock.common import contants as constants
from baostock.data import resultset

from finance_lab import pipeline, sources
from finance_lab.config import Instrument
from finance_lab.sources import DataSourceError, fetch_baostock

START, END = date(2018, 1, 1), date(2026, 10, 8)
INDEX = Instrument("000300", "sh", "index", "index")
ETF = Instrument("510300", "sh", "etf", "etf")
FIELDS = "date,code,open,high,low,close,volume,amount,tradestatus"


@pytest.fixture(autouse=True)
def _forbid_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Real network is forbidden in pagination tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(resultset.sock, "send_msg", forbidden)


def _rows(count: int, offset: int = 0, symbol: str = INDEX.symbol) -> list[list[str]]:
    days = pd.bdate_range("2018-01-02", periods=offset + count)[offset:]
    return [[day.date().isoformat(), symbol, "4", "4.1", "3.9", "4", "100", "400", "1"]
            for day in days]


def _result(count: int = 2000, *, page: str = "1") -> Any:
    value = resultset.ResultData()
    value.error_code = "0"
    value.fields = FIELDS.split(",")
    value.data = _rows(count)
    value.cur_page_num = page
    value.msg_type = constants.MESSAGE_TYPE_GETKDATAPLUS_REQUEST
    value.msg_body = constants.MESSAGE_SPLIT.join(["history", "offline", "1"])
    return value


def _response(rows: list[list[str]], code: str = "0", *, page: str = "2") -> str:
    body = constants.MESSAGE_SPLIT.join([
        code, "", "history", "offline", page, "2000", json.dumps({"record": rows}),
    ])
    header = resultset.msgheader.to_message_header(
        constants.MESSAGE_TYPE_GETKDATAPLUS_RESPONSE, len(body),
    )
    assert len(header) == constants.MESSAGE_HEADER_LENGTH
    return header + body + "\n"


def _install(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[str], Any],
    reply: str | None,
    *,
    logout_error: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    calls: list[str] = []
    requests: list[dict[str, Any]] = []

    def login() -> SimpleNamespace:
        calls.append("login")
        return SimpleNamespace(error_code="0")

    def query(code: str, fields: str, **kwargs: Any) -> Any:
        calls.append("query")
        requests.append({"code": code, "fields": fields, **kwargs})
        return factory(code)

    def logout() -> SimpleNamespace:
        calls.append("logout")
        if logout_error:
            raise RuntimeError("private logout detail")
        return SimpleNamespace(error_code="0")

    def transport(message: str) -> str | None:
        calls.append("transport")
        return reply

    monkeypatch.setattr(bs, "login", login)
    monkeypatch.setattr(bs, "query_history_k_data_plus", query)
    monkeypatch.setattr(bs, "logout", logout)
    monkeypatch.setattr(resultset.sock, "send_msg", transport)
    return calls, requests


@pytest.mark.parametrize("reply", [None, "", "   "])
@pytest.mark.parametrize("logout_error", [False, True])
def test_silent_page_disconnect_rejects_all_received_rows(
    monkeypatch: pytest.MonkeyPatch, reply: str | None, logout_error: bool,
) -> None:
    value = _result()
    calls, requests = _install(monkeypatch, lambda code: value, reply, logout_error=logout_error)
    with pytest.raises(DataSourceError, match="pagination_completion_unknown") as raised:
        fetch_baostock(INDEX, START, END)
    error = raised.value
    assert value.error_code == "0"  # The actual installed SDK silently retained success.
    assert error.stage == "iteration"
    assert (error.received_rows, error.accepted_rows, error.rejected_rows) == (2000, 0, 2000)
    assert "page_rows=2000" in str(error) and "cursor=2000" in str(error)
    assert "private" not in str(error)
    assert calls == ["login", "query", "transport", "logout"]
    assert requests == [{"code": INDEX.symbol, "fields": FIELDS, "start_date": "2018-01-01",
                         "end_date": "2026-10-08", "frequency": "d", "adjustflag": "3"}]


def test_invalid_page_number_never_silently_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, _ = _install(monkeypatch, lambda code: _result(page="?"), None)
    with pytest.raises(DataSourceError, match="pagination_state_invalid") as raised:
        fetch_baostock(INDEX, START, END)
    assert raised.value.stage == "iteration" and raised.value.received_rows == 2000
    assert calls == ["login", "query", "logout"]


def test_page_error_code_is_rechecked_after_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, _ = _install(monkeypatch, lambda code: _result(), _response([], "1"))
    with pytest.raises(DataSourceError, match="iteration_error_code") as raised:
        fetch_baostock(INDEX, START, END)
    assert raised.value.stage == "iteration" and raised.value.rejected_rows == 2000
    assert calls == ["login", "query", "transport", "logout"]


@pytest.mark.parametrize("initial,initial_page,tail,reply_page,transports", [
    (140, "2", 0, "2", 0), (2000, "1", 140, "3", 1),
    (2000, "1", 0, "3", 1), (2000, "1", 0, "1", 1),
])
def test_response_page_number_matches_received_rows(
    monkeypatch: pytest.MonkeyPatch, initial: int, initial_page: str,
    tail: int, reply_page: str, transports: int,
) -> None:
    calls, _ = _install(
        monkeypatch, lambda code: _result(initial, page=initial_page),
        _response(_rows(tail, 2000), page=reply_page),
    )
    with pytest.raises(DataSourceError, match="pagination_state_invalid") as raised:
        fetch_baostock(INDEX, START, END)
    received = initial + tail
    assert raised.value.stage == "iteration"
    assert (raised.value.received_rows, raised.value.accepted_rows, raised.value.rejected_rows) == (
        received, 0, received,
    )
    assert calls.count("login") == calls.count("query") == calls.count("logout") == 1
    assert calls.count("transport") == transports


@pytest.mark.parametrize("initial,tail,expected,transports", [
    (2000, 0, 2000, 1), (2000, 140, 2140, 1), (140, 0, 140, 0), (2140, 0, 2140, 0),
])
def test_verified_terminal_states_still_normalize(
    monkeypatch: pytest.MonkeyPatch, initial: int, tail: int, expected: int, transports: int,
) -> None:
    calls, _ = _install(monkeypatch, lambda code: _result(initial), _response(_rows(tail, 2000)))
    frame = fetch_baostock(INDEX, START, END)
    assert len(frame) == expected and not frame.trade_date.duplicated().any()
    assert set(frame.symbol) == {INDEX.symbol}
    assert set(frame.source) == {"baostock"}
    assert set(frame.adjustment) == {"none"} and set(frame.volume_unit) == {"share"}
    assert frame.attrs["source_diagnostics"] == {
        "received_rows": expected, "accepted_rows": expected,
        "rejected_rows": 0, "rejection_reasons": {},
    }
    assert calls.count("login") == calls.count("query") == calls.count("logout") == 1
    assert calls.count("transport") == transports


@pytest.mark.parametrize("changes", [
    {"data": None}, {"cur_row_num": None}, {"cur_row_num": True}, {"cur_row_num": 0},
    {"per_page_count": "unknown"}, {"per_page_count": 1000}, {"cur_page_num": "0"},
    {"cur_page_num": True}, {"cur_page_num": "１"}, {"error_code": "1"},
])
def test_unknown_terminal_state_fails_closed(changes: dict[str, Any]) -> None:
    value = _result(1)
    value.cur_row_num = 1
    for key, content in changes.items():
        setattr(value, key, content)
    with pytest.raises(DataSourceError) as raised:
        sources._assert_baostock_complete(value, 1)
    assert raised.value.stage == "iteration"
    assert (raised.value.received_rows, raised.value.accepted_rows, raised.value.rejected_rows) == (
        1, 0, 1,
    )


def test_empty_initial_response_remains_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, _ = _install(monkeypatch, lambda code: _result(0), None)
    with pytest.raises(DataSourceError) as raised:
        fetch_baostock(INDEX, START, END)
    assert raised.value.stage == "response" and raised.value.received_rows == 0
    assert calls == ["login", "query", "logout"]


@pytest.mark.parametrize("kind", ["unknown_version", "unavailable_metadata"])
def test_unverified_version_stops_before_login(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    calls, _ = _install(monkeypatch, lambda code: _result(1), None)

    def version(name: str) -> str:
        assert name == "baostock"
        if kind == "unavailable_metadata":
            raise RuntimeError("private metadata detail")
        return "0.9.99"

    monkeypatch.setattr(sources, "package_version", version)
    with pytest.raises(DataSourceError) as raised:
        fetch_baostock(INDEX, START, END)
    assert raised.value.stage == "import" and raised.value.received_rows == 0
    assert "private" not in str(raised.value)
    assert calls == []


def test_real_page_disconnect_stops_daily_without_advancing_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import duckdb

    from finance_lab.cli import main
    from finance_lab.data_manifest import read_update_summary
    from finance_lab.paper_pipeline import paper_init_portfolio
    from tests.paper_helpers import make_paper_test_project

    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    paper_init_portfolio(root=tmp_path, as_of_date=prices.trade_date.max().date())

    def ledger_rows() -> dict[str, list[Any]]:
        with duckdb.connect(str(paths.database), read_only=True) as connection:
            tables = [name for (name,) in connection.execute("SHOW TABLES").fetchall()
                      if name.startswith("paper_")]
            assert tables
            return {name: connection.execute(f'SELECT * FROM "{name}"').fetchall()
                    for name in tables}

    previous = ledger_rows()
    (tmp_path / "config/instruments.json").write_text(
        json.dumps([vars(INDEX), vars(ETF)]), encoding="utf-8",
    )
    (tmp_path / "config/source_profile.json").write_text(
        json.dumps({"schema_version": 1, "profile": "tencent_baostock"}), encoding="utf-8",
    )

    def tencent(instrument: Instrument, start: date, end: date) -> pd.DataFrame:
        days = [day for day in (date(2018, 1, 2), date(2026, 1, 5), END) if start <= day <= end]
        return pd.DataFrame({
            "symbol": [instrument.symbol] * len(days), "trade_date": pd.to_datetime(days),
            "open": 4.0, "high": 4.1, "low": 3.9, "close": 4.0,
            "volume": 100.0, "volume_unit": "share", "amount": 400.0,
            "adjustment": "none", "source": "tencent", "ingested_at": "2026-10-08T10:00:00Z",
        })

    def factory(code: str) -> Any:
        if code == INDEX.symbol:
            return _result()
        assert code == ETF.symbol
        value = _result(2)
        value.data = [[day, code, "4", "4.1", "3.9", "4", "100", "400", "1"]
                      for day in ("2026-01-05", "2026-10-08")]
        return value

    calls, requests = _install(monkeypatch, factory, None)
    monkeypatch.setattr(pipeline, "fetch_tencent", tencent)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main(["paper-daily", "--start", START.isoformat(), "--as-of", END.isoformat()]) == 1
    assert ledger_rows() == previous
    assert calls.count("login") == calls.count("query") == calls.count("logout") == 2
    assert calls.count("transport") == 1
    assert [(r["code"], r["start_date"]) for r in requests] == [
        (INDEX.symbol, "2018-01-01"), (ETF.symbol, "2026-01-01"),
    ]
    assert all(r["frequency"] == "d" and r["adjustflag"] == "3" for r in requests)
    assert read_update_summary(paths).parse_error is None
    summary = json.loads((paths.outputs / "update_summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "partial_sources" and summary["failed_sources"] == 1
    index, etf = summary["records"]
    diagnostic = index["source_diagnostics"]["baostock"]
    assert diagnostic["status"] == "failed" and diagnostic["stage"] == "iteration"
    assert (diagnostic["received_rows"], diagnostic["accepted_rows"],
            diagnostic["rejected_rows"]) == (2000, 0, 2000)
    assert etf["source_diagnostics"]["baostock"]["status"] == "success"
    assert not list((paths.raw / "baostock/sh_000300").glob("*.parquet"))
    assert list((paths.raw / "baostock/sh_510300").glob("*.parquet"))
    latest = json.loads((paths.outputs / "default_paper_automation_latest.json").read_text(
        encoding="utf-8",
    ))
    assert latest["failed_step"] == "market_data_update"
    assert latest["processed_dates"] == [] and latest["ledger_committed"] is None
