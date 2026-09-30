"""Offline source contracts: mock only the external library boundary."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from finance_lab import pipeline
from finance_lab.cli import main
from finance_lab.config import Instrument, get_paths
from finance_lab.data_manifest import read_update_summary
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.sources import DataSourceError, fetch_akshare, fetch_baostock
from finance_lab.storage import save_curated

START, END = date(2024, 1, 1), date(2024, 1, 10)
INDEX = Instrument("000300", "sh", "index", "index")
ETF = Instrument("510300", "sh", "etf", "etf")


def _ak_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": ["2024-01-02", "2024-01-03"],
            "开盘": ["10", "11"],
            "最高": ["11", "12"],
            "最低": ["9", "10"],
            "收盘": ["10.5", "11.5"],
            "成交量": ["100", "110"],
            "成交额": ["1000", "1100"],
        }
    )


def _install_ak(monkeypatch: pytest.MonkeyPatch, rows: pd.DataFrame) -> None:
    module = SimpleNamespace(
        index_zh_a_hist=lambda **kwargs: rows.copy(),
        fund_etf_hist_em=lambda **kwargs: rows.copy(),
    )
    monkeypatch.setitem(sys.modules, "akshare", module)


@pytest.mark.parametrize("instrument", [INDEX, ETF])
def test_akshare_accepts_all_valid_rows_with_counts(
    monkeypatch: pytest.MonkeyPatch, instrument: Instrument
) -> None:
    _install_ak(monkeypatch, _ak_rows())
    frame = fetch_akshare(instrument, START, END)
    assert len(frame) == 2
    assert frame.attrs["source_diagnostics"] == {
        "received_rows": 2,
        "accepted_rows": 2,
        "rejected_rows": 0,
        "rejection_reasons": {},
    }


@pytest.mark.parametrize(
    "change",
    [
        "empty", "missing", "bad_date", "bad_price", "bad_volume", "infinite_price",
        "duplicate", "same_day_different_time", "outside",
    ],
)
def test_akshare_bad_response_is_not_silently_published(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    rows = _ak_rows()
    if change == "empty":
        rows = rows.iloc[0:0]
    elif change == "missing":
        rows = rows.drop(columns=["收盘"])
    elif change == "bad_date":
        rows.loc[1, "日期"] = "not-a-date"
    elif change == "bad_price":
        rows.loc[1, "收盘"] = "not-a-price"
    elif change == "bad_volume":
        rows.loc[1, "成交量"] = "not-a-volume"
    elif change == "infinite_price":
        rows.loc[1, "收盘"] = "inf"
    elif change == "duplicate":
        rows.loc[1, "日期"] = rows.loc[0, "日期"]
    elif change == "same_day_different_time":
        rows.loc[0, "日期"] = "2024-01-02 09:30"
        rows.loc[1, "日期"] = "2024-01-02 15:00"
    else:
        rows.loc[1, "日期"] = "2024-02-01"
    _install_ak(monkeypatch, rows)
    with pytest.raises(DataSourceError) as raised:
        fetch_akshare(INDEX, START, END)
    assert raised.value.stage in {"response", "validation"}
    assert raised.value.received_rows == len(rows)


def test_akshare_exception_does_not_expose_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**kwargs: Any) -> pd.DataFrame:
        raise TimeoutError("https://example.invalid/private?token=secret")

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(index_zh_a_hist=fail))
    with pytest.raises(DataSourceError) as raised:
        fetch_akshare(INDEX, START, END)
    assert raised.value.stage == "request"
    assert "secret" not in str(raised.value)


class _BaoResult:
    fields = ["date", "code", "open", "high", "low", "close", "volume", "amount", "tradestatus"]

    def __init__(self, rows: list[list[str]], *, fail_iteration: bool = False) -> None:
        self.error_code = "0"
        self.error_msg = ""
        self.rows = rows
        self.index = 0
        self.fail_iteration = fail_iteration

    def next(self) -> bool:
        if self.fail_iteration:
            raise RuntimeError("iteration failed")
        if self.index >= len(self.rows):
            return False
        self.index += 1
        return True

    def get_row_data(self) -> list[str]:
        return self.rows[self.index - 1]


def _bao_rows() -> list[list[str]]:
    return [
        ["2024-01-02", "sh.000300", "10", "11", "9", "10.5", "100", "1000", "1"],
        ["2024-01-03", "sh.000300", "11", "12", "10", "11.5", "110", "1100", "1"],
    ]


def _install_bao(
    monkeypatch: pytest.MonkeyPatch,
    result: _BaoResult,
    *, login_code: str = "0",
    logout_error: bool = False,
) -> list[str]:
    calls: list[str] = []

    def login() -> SimpleNamespace:
        calls.append("login")
        return SimpleNamespace(error_code=login_code, error_msg="bad credentials")

    def query(*args: Any, **kwargs: Any) -> _BaoResult:
        calls.append("query")
        return result

    def logout() -> None:
        calls.append("logout")
        if logout_error:
            raise RuntimeError("private logout error")

    monkeypatch.setitem(
        sys.modules,
        "baostock",
        SimpleNamespace(login=login, query_history_k_data_plus=query, logout=logout),
    )
    return calls


def test_baostock_nontrading_rows_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _bao_rows()
    rows[1][-1] = "0"
    calls = _install_bao(monkeypatch, _BaoResult(rows))
    frame = fetch_baostock(INDEX, START, END)
    assert len(frame) == 1
    assert frame.attrs["source_diagnostics"] == {
        "received_rows": 2,
        "accepted_rows": 1,
        "rejected_rows": 1,
        "rejection_reasons": {"nontrading": 1},
    }
    assert calls == ["login", "query", "logout"]


@pytest.mark.parametrize("change", ["login", "query", "iteration", "bad_number", "logout"])
def test_baostock_failures_have_stage_and_logout_once(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    rows = _bao_rows()
    result = _BaoResult(rows, fail_iteration=change == "iteration")
    if change == "query":
        result.error_code = "1"
    if change == "bad_number":
        rows[1][6] = "bad-volume"
    calls = _install_bao(
        monkeypatch,
        result,
        login_code="1" if change == "login" else "0",
        logout_error=change == "logout",
    )
    with pytest.raises(DataSourceError) as raised:
        fetch_baostock(INDEX, START, END)
    assert raised.value.stage == {
        "login": "login", "query": "query", "iteration": "iteration",
        "bad_number": "validation", "logout": "logout",
    }[change]
    assert calls.count("logout") == (0 if change == "login" else 1)


@pytest.mark.parametrize(
    "change",
    [
        "empty", "missing", "bad_date", "duplicate", "outside", "unknown_status",
        "wrong_code", "all_nontrading",
    ],
)
def test_baostock_invalid_response_rejected_without_silent_row_loss(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    rows = _bao_rows()
    if change == "empty":
        rows = []
    elif change == "missing":
        rows = [row[:-1] for row in rows]
    elif change == "bad_date":
        rows[1][0] = "bad-date"
    elif change == "duplicate":
        rows[1][0] = rows[0][0]
    elif change == "outside":
        rows[1][0] = "2024-02-01"
    elif change == "unknown_status":
        rows[1][-1] = "?"
    elif change == "wrong_code":
        rows[1][1] = "sh.510300"
    else:
        for row in rows:
            row[-1] = "0"
    result = _BaoResult(rows)
    if change == "missing":
        result.fields = result.fields[:-1]
    calls = _install_bao(monkeypatch, result)
    with pytest.raises(DataSourceError) as raised:
        fetch_baostock(INDEX, START, END)
    assert raised.value.received_rows == len(rows)
    assert raised.value.received_rows == raised.value.accepted_rows + raised.value.rejected_rows
    assert calls.count("logout") == 1


def test_baostock_query_error_survives_logout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _BaoResult(_bao_rows())
    result.error_code = "1"
    calls = _install_bao(monkeypatch, result, logout_error=True)
    with pytest.raises(DataSourceError) as raised:
        fetch_baostock(INDEX, START, END)
    assert raised.value.stage == "query"
    assert calls.count("logout") == 1


@pytest.mark.parametrize("stage", ["login", "query"])
def test_baostock_call_exceptions_keep_stage_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    calls: list[str] = []

    def login() -> SimpleNamespace:
        calls.append("login")
        if stage == "login":
            raise TimeoutError("private login URL")
        return SimpleNamespace(error_code="0")

    def query(*args: Any, **kwargs: Any) -> _BaoResult:
        calls.append("query")
        raise TimeoutError("private query URL")

    def logout() -> None:
        calls.append("logout")

    monkeypatch.setitem(
        sys.modules,
        "baostock",
        SimpleNamespace(login=login, query_history_k_data_plus=query, logout=logout),
    )
    with pytest.raises(DataSourceError) as raised:
        fetch_baostock(INDEX, START, END)
    assert raised.value.stage == stage
    assert "private" not in str(raised.value)
    assert calls.count("logout") == (0 if stage == "login" else 1)


def test_pipeline_records_failed_primary_and_successful_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(**kwargs: Any) -> pd.DataFrame:
        raise TimeoutError("private URL token=secret")

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(index_zh_a_hist=fail))
    _install_bao(monkeypatch, _BaoResult(_bao_rows()))
    record = pipeline._fetch_instrument(INDEX, START, END, get_paths(tmp_path))
    assert record.status == "partial_sources"
    assert record.primary_source == "baostock"
    assert record.source_diagnostics["akshare"]["stage"] == "request"
    assert record.source_diagnostics["baostock"]["received_rows"] == 2
    assert record.source_diagnostics["baostock"]["accepted_rows"] == 2
    assert record.source_diagnostics["baostock"]["first_trade_date"] == "2024-01-02"
    assert "secret" not in str(record)


def test_partial_source_update_has_nonzero_cli_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = get_paths(tmp_path)
    (paths.root / "config").mkdir()
    (paths.root / "config/instruments.json").write_text(
        json.dumps([{"code": "000300", "exchange": "sh", "name": "index", "kind": "index"}]),
        encoding="utf-8",
    )

    def fail(**kwargs: Any) -> pd.DataFrame:
        raise TimeoutError("private token")

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(index_zh_a_hist=fail))
    _install_bao(monkeypatch, _BaoResult(_bao_rows()))
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main(["fetch", "--start", START.isoformat(), "--end", END.isoformat()]) == 3
    saved = read_update_summary(paths)
    assert saved.parse_error is None
    assert saved.records[0].rows == 2
    assert main(["data-update-status"]) == 2


def test_pipeline_both_fail_preserves_old_curated_but_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = get_paths(tmp_path)
    (paths.root / "config").mkdir()
    (paths.root / "config/instruments.json").write_text(
        json.dumps([{"code": "000300", "exchange": "sh", "name": "index", "kind": "index"}]),
        encoding="utf-8",
    )
    old = pd.DataFrame(
        {
            "symbol": [INDEX.symbol], "trade_date": pd.to_datetime(["2023-01-03"]),
            "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.0],
            "volume": [100.0], "volume_unit": ["share"], "amount": [1000.0],
            "adjustment": ["none"], "source": ["baostock"],
            "ingested_at": ["2023-01-04T00:00:00Z"],
        }
    )
    old_file = save_curated(old, paths)
    original = old_file.read_bytes()

    def fail(**kwargs: Any) -> pd.DataFrame:
        raise TimeoutError("upstream timeout")

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(index_zh_a_hist=fail))
    _install_bao(monkeypatch, _BaoResult([]), login_code="1")
    result = pipeline.update_market_data(START, END, tmp_path)
    record = result["records"][0]
    assert result["successful_instruments"] == 0
    assert record["status"] == "failed"
    assert result["status"] == "failed"
    assert set(record["source_errors"]) == {"akshare", "baostock"}
    saved = read_update_summary(paths).records[0]
    assert saved.source_diagnostics["akshare"]["stage"] == "request"
    assert saved.source_diagnostics["baostock"]["stage"] == "login"
    assert old_file.read_bytes() == original
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main(["data-update-status"]) == 2


def test_pipeline_valueerror_before_frame_keeps_summary_parseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = get_paths(tmp_path)
    (paths.root / "config").mkdir()
    (paths.root / "config/instruments.json").write_text(
        json.dumps([{"code": "000300", "exchange": "sh", "name": "index", "kind": "index"}]),
        encoding="utf-8",
    )

    def fail(*args: Any) -> pd.DataFrame:
        raise ValueError("external parse failed")

    monkeypatch.setattr(pipeline, "fetch_akshare", fail)
    monkeypatch.setattr(pipeline, "fetch_baostock", fail)
    result = pipeline.update_market_data(START, END, tmp_path)
    assert result["status"] == "failed"
    summary = read_update_summary(paths)
    assert summary.parse_error is None
    assert summary.records[0].source_diagnostics["akshare"]["received_rows"] == 0


@pytest.mark.parametrize(
    ("source_error", "expected_stage"),
    [
        (ValueError("private URL token=secret"), "request"),
        (DataSourceError("private URL token=secret", stage="query"), "query"),
        (DataSourceError("private URL token=secret", stage="private-stage"), "unknown"),
        (
            DataSourceError(
                "private URL token=secret",
                stage="query",
                received_rows=2,
                rejected_rows="private rejected token",  # type: ignore[arg-type]
            ),
            "query",
        ),
    ],
)
def test_fetch_summary_and_cli_redact_source_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_error: Exception,
    expected_stage: str,
) -> None:
    paths = get_paths(tmp_path)
    config = paths.root / "config" / "instruments.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps([{"code": "000300", "exchange": "sh", "name": "index", "kind": "index"}]),
        encoding="utf-8",
    )

    def fail(*args: Any) -> pd.DataFrame:
        raise source_error

    monkeypatch.setattr(pipeline, "fetch_akshare", fail)
    monkeypatch.setattr(pipeline, "fetch_baostock", fail)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main(["fetch", "--start", START.isoformat(), "--end", END.isoformat()]) == 2
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["status"] == "failed"
    assert "private" not in output.out + output.err
    assert output.err == ""
    summary_path = paths.outputs / "update_summary.json"
    assert "private" not in summary_path.read_text(encoding="utf-8")
    saved = read_update_summary(paths).records[0]
    assert saved.source_diagnostics["akshare"]["stage"] == expected_stage
    assert saved.source_diagnostics["baostock"]["stage"] == expected_stage
    assert saved.source_diagnostics["akshare"]["rejected_rows"] == 0


def test_untrusted_source_counts_cannot_pollute_update_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = get_paths(tmp_path)
    config = paths.root / "config" / "instruments.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps([{"code": "000300", "exchange": "sh", "name": "index", "kind": "index"}]),
        encoding="utf-8",
    )
    frame = make_synthetic_daily_prices(periods=120)
    frame["symbol"] = "sh.000300"
    frame.attrs["source_diagnostics"] = {
        "received_rows": 120,
        "accepted_rows": 120,
        "rejected_rows": 0,
        "rejection_reasons": {"private token=C:/secret": 0},
    }
    monkeypatch.setattr(pipeline, "fetch_akshare", lambda *args: frame.copy())
    monkeypatch.setattr(pipeline, "fetch_baostock", lambda *args: frame.copy())
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    assert main(["fetch", "--start", START.isoformat(), "--end", END.isoformat()]) == 2
    output = capsys.readouterr()
    assert "private token" not in output.out + output.err
    saved_text = (paths.outputs / "update_summary.json").read_text(encoding="utf-8")
    assert "private token" not in saved_text
    saved = read_update_summary(paths).records[0]
    assert saved.source_diagnostics["akshare"]["stage"] == "validation"
    assert saved.source_diagnostics["baostock"]["stage"] == "validation"
    assert saved.source_errors
    assert not list(paths.raw.rglob("*.parquet"))


def test_validation_exception_text_does_not_reach_update_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = get_paths(tmp_path)
    config = paths.root / "config" / "instruments.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps([{"code": "000300", "exchange": "sh", "name": "index", "kind": "index"}]),
        encoding="utf-8",
    )
    frame = make_synthetic_daily_prices(periods=120)
    frame["symbol"] = "sh.000300"
    monkeypatch.setattr(pipeline, "fetch_akshare", lambda *args: frame.copy())
    monkeypatch.setattr(pipeline, "fetch_baostock", lambda *args: frame.copy())

    def fail_validation(_frame: pd.DataFrame) -> None:
        raise ValueError("private value=C:/secret/validation")

    monkeypatch.setattr(pipeline, "assert_valid_daily_prices", fail_validation)
    result = pipeline.update_market_data(START, END, tmp_path)
    assert result["status"] == "failed"
    assert result["records"][0]["status"] == "failed"
    record = read_update_summary(paths).records[0]
    assert record.source_diagnostics["akshare"]["stage"] == "validation"
    assert record.source_diagnostics["baostock"]["stage"] == "validation"
    assert "private" not in json.dumps(record.source_errors)
    assert "private" not in (paths.outputs / "update_summary.json").read_text(encoding="utf-8")
    assert not list(paths.raw.rglob("*.parquet"))
