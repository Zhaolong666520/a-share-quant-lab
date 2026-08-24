from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

import finance_lab.pipeline as pipeline_module
from finance_lab.cli import build_parser
from finance_lab.config import get_paths
from finance_lab.ledger import LedgerConfig, run_account_ledger
from finance_lab.ledger_report import write_account_ledger_report
from finance_lab.pipeline import account_backtest_symbol
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated


def test_buy_hold_ledger_uses_cash_and_integer_lots() -> None:
    prices = make_synthetic_daily_prices(periods=120)
    config = LedgerConfig(
        initial_cash=100_000.0,
        lot_size=100,
        commission_bps=0.0,
        minimum_commission=0.0,
        sell_tax_bps=0.0,
        slippage_bps=0.0,
    )

    result = run_account_ledger(
        prices,
        instrument_kind="etf",
        strategy="buy_hold",
        config=config,
    )

    first_trade = result.trades.iloc[0]
    assert first_trade["action"] == "BUY"
    expected_shares = (
        int(config.initial_cash // (float(first_trade["execution_price"]) * 100)) * 100
    )
    assert int(first_trade["shares_after"]) == expected_shares
    assert (result.frame["shares"] % config.lot_size == 0).all()
    assert (result.frame["cash"] >= -1e-9).all()
    assert result.checks.passed


def test_ledger_charges_minimum_commission_and_sell_tax_only_on_sells() -> None:
    prices = make_synthetic_daily_prices(periods=500)
    config = LedgerConfig(
        initial_cash=100_000.0,
        lot_size=100,
        commission_bps=1.0,
        minimum_commission=5.0,
        sell_tax_bps=10.0,
        slippage_bps=2.0,
    )

    result = run_account_ledger(
        prices,
        instrument_kind="etf",
        strategy="sma",
        short_window=5,
        long_window=20,
        config=config,
    )

    assert not result.trades.empty
    assert (result.trades["commission"] >= 5.0).all()
    buys = result.trades[result.trades["action"] == "BUY"]
    sells = result.trades[result.trades["action"] == "SELL"]
    assert (buys["sell_tax"] == 0.0).all()
    assert (sells["sell_tax"] > 0.0).all()
    assert result.checks.passed
    assert result.benchmark_checks.passed


def test_ledger_trade_cash_flows_reconcile_exactly() -> None:
    prices = make_synthetic_daily_prices(periods=500)
    result = run_account_ledger(
        prices,
        instrument_kind="etf",
        strategy="sma",
        short_window=5,
        long_window=20,
    )

    for trade in result.trades.itertuples(index=False):
        fees = float(trade.commission) + float(trade.sell_tax)
        if trade.action == "BUY":
            expected = float(trade.cash_before) - float(trade.notional) - fees
        else:
            expected = float(trade.cash_before) + float(trade.notional) - fees
        assert float(trade.cash_after) == pytest.approx(expected, abs=1e-8)
    assert result.checks.cash_reconciliation_mismatches == 0
    assert result.checks.equity_reconciliation_mismatches == 0


def test_ledger_signal_dates_precede_trade_dates() -> None:
    prices = make_synthetic_daily_prices(periods=500)
    result = run_account_ledger(
        prices,
        instrument_kind="etf",
        strategy="sma",
        short_window=5,
        long_window=20,
    )

    assert (result.trades["signal_date"] < result.trades["trade_date"]).all()
    assert result.checks.invalid_signal_order == 0


def test_ledger_fingerprint_and_id_change_with_exact_config() -> None:
    prices = make_synthetic_daily_prices(periods=200)
    first = run_account_ledger(
        prices,
        instrument_kind="etf",
        config=LedgerConfig(commission_bps=3.0000001),
    )
    second = run_account_ledger(
        prices,
        instrument_kind="etf",
        config=LedgerConfig(commission_bps=3.0000002),
    )

    assert first.experiment_id != second.experiment_id
    assert len(first.experiment_id) < 180
    assert first.data_fingerprint == second.data_fingerprint


def test_ledger_fingerprint_includes_provenance_metadata() -> None:
    prices = make_synthetic_daily_prices(periods=200)
    changed = prices.copy()
    changed["volume_unit"] = "different_unit"
    changed["ingested_at"] = "2026-08-24T00:00:00+00:00"

    first = run_account_ledger(prices, instrument_kind="etf")
    second = run_account_ledger(changed, instrument_kind="etf")

    assert first.data_fingerprint != second.data_fingerprint
    assert first.experiment_id != second.experiment_id


@pytest.mark.parametrize(
    "config",
    [
        LedgerConfig(initial_cash=0),
        LedgerConfig(lot_size=0),
        LedgerConfig(lot_size=True),
        LedgerConfig(commission_bps=-1),
        LedgerConfig(commission_bps=float("nan")),
        LedgerConfig(minimum_commission=-1),
        LedgerConfig(sell_tax_bps=float("inf")),
        LedgerConfig(slippage_bps=10_000),
    ],
)
def test_ledger_rejects_invalid_config(config: LedgerConfig) -> None:
    with pytest.raises(ValueError):
        run_account_ledger(
            make_synthetic_daily_prices(periods=200),
            instrument_kind="etf",
            config=config,
        )


def test_ledger_rejects_non_tradable_index() -> None:
    with pytest.raises(ValueError, match="指数"):
        run_account_ledger(
            make_synthetic_daily_prices(periods=200),
            instrument_kind="index",
        )


def test_ledger_rejects_mixed_symbols() -> None:
    prices = make_synthetic_daily_prices(periods=200)
    prices.loc[50:, "symbol"] = "demo.999999"

    with pytest.raises(ValueError, match="同一个"):
        run_account_ledger(prices, instrument_kind="etf")


def test_buy_hold_requires_a_trade_and_later_valuation_row() -> None:
    prices = make_synthetic_daily_prices(periods=120).head(2)

    with pytest.raises(ValueError, match="至少需要3行"):
        run_account_ledger(prices, instrument_kind="etf", strategy="buy_hold")


def test_three_row_buy_hold_executes_and_values_one_trade() -> None:
    prices = make_synthetic_daily_prices(periods=120).head(3)

    result = run_account_ledger(prices, instrument_kind="etf", strategy="buy_hold")

    assert len(result.trades) == 1
    assert result.trades.iloc[0]["action"] == "BUY"
    assert result.metrics.observations == 2


def test_sma_ledger_requires_enough_rows_for_a_tradable_signal() -> None:
    prices = make_synthetic_daily_prices(periods=120)

    with pytest.raises(ValueError, match="至少需要"):
        run_account_ledger(
            prices,
            instrument_kind="etf",
            strategy="sma",
            short_window=20,
            long_window=119,
        )


def test_ledger_report_writes_html_json_and_trades_csv(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=500)
    result = run_account_ledger(
        prices,
        instrument_kind="etf",
        strategy="sma",
        short_window=5,
        long_window=20,
    )

    html_path, json_path, csv_path, daily_csv_path, chart_path = write_account_ledger_report(
        result,
        get_paths(tmp_path),
    )

    assert all(path.exists() and path.stat().st_size > 0 for path in [
        html_path,
        json_path,
        csv_path,
        daily_csv_path,
        chart_path,
    ])
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["checks"]["passed"]
    assert payload["benchmark_checks"]["passed"]
    assert math.isfinite(payload["metrics"]["total_return"])
    text = html_path.read_text(encoding="utf-8")
    assert "账户账本回测" in text
    assert "假设费用" in text
    assert "尚未处理分红" in text
    trades = pd.read_csv(csv_path)
    assert len(trades) == len(result.trades)
    daily = pd.read_csv(daily_csv_path)
    assert len(daily) == len(result.frame)
    assert {"reason", "cash", "shares", "next_open_equity"}.issubset(daily.columns)


def test_account_pipeline_reads_curated_etf_and_writes_report(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=500)
    save_curated(prices, paths)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "instruments.json").write_text(
        json.dumps(
            [
                {
                    "code": "000300",
                    "exchange": "demo",
                    "name": "Demo ETF",
                    "kind": "etf",
                }
            ]
        ),
        encoding="utf-8",
    )

    report, payload = account_backtest_symbol(
        "demo.000300",
        short_window=5,
        long_window=20,
        root=tmp_path,
    )

    assert report.exists()
    checks = payload["checks"]
    settings = payload["settings"]
    assert isinstance(checks, dict) and checks["passed"]
    assert isinstance(payload["trades"], int) and payload["trades"] > 0
    assert isinstance(settings, dict) and settings["instrument_kind"] == "etf"
    assert settings["dataset_id"]
    assert settings["curated_file_sha256"]
    assert settings["data_health"]


def test_account_report_preserves_staleness_and_upstream_failure(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=500)
    save_curated(prices, paths)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "instruments.json").write_text(
        json.dumps(
            [
                {
                    "code": "000300",
                    "exchange": "demo",
                    "name": "Demo ETF",
                    "kind": "etf",
                }
            ]
        ),
        encoding="utf-8",
    )
    (paths.outputs / "update_summary.json").write_text(
        json.dumps(
            {
                "end": "2026-08-24",
                "records": [
                    {
                        "symbol": "demo.000300",
                        "source_errors": {"akshare": "upstream unavailable"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report, payload = account_backtest_symbol(
        "demo.000300",
        short_window=5,
        long_window=20,
        root=tmp_path,
    )

    settings = payload["settings"]
    assert isinstance(settings, dict)
    health = settings["data_health"]
    assert isinstance(health, dict)
    assert health["file_health_status"] == "warning"
    stale_days = health["business_days_stale"]
    assert isinstance(stale_days, int) and stale_days > 3
    assert health["upstream_errors"] == {"akshare": "upstream unavailable"}
    text = report.read_text(encoding="utf-8")
    assert "数据健康状态" in text
    assert "upstream unavailable" in text


def test_account_pipeline_detects_change_after_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=500)
    save_curated(prices, paths)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "instruments.json").write_text(
        json.dumps(
            [
                {
                    "code": "000300",
                    "exchange": "demo",
                    "name": "Demo ETF",
                    "kind": "etf",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pipeline_module, "file_sha256", lambda _path: "0" * 64)

    with pytest.raises(RuntimeError, match="发生变化"):
        account_backtest_symbol("demo.000300", root=tmp_path)


def test_account_cli_accepts_ledger_settings() -> None:
    args = build_parser().parse_args(
        [
            "account",
            "--symbol",
            "sh.510300",
            "--initial-cash",
            "200000",
            "--lot-size",
            "100",
            "--commission-bps",
            "2.5",
            "--minimum-commission",
            "5",
            "--sell-tax-bps",
            "0",
            "--slippage-bps",
            "1.5",
        ]
    )

    assert args.symbol == "sh.510300"
    assert args.initial_cash == 200_000.0
    assert args.lot_size == 100
    assert args.commission_bps == 2.5
    assert args.slippage_bps == 1.5
