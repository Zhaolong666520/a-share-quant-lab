from __future__ import annotations

from pathlib import Path

import pytest


def _snapshot_row(
    *,
    record_date: str = "2026-01-19",
    ex_date: str = "2026-01-20",
    payment_date: str = "2026-01-22",
    source_url: str = "https://example.test/notices/510300-2026-01",
) -> str:
    return (
        "sh.510300,"
        f"{record_date},{ex_date},{payment_date},0.1234,CNY,{source_url},"
        "2026-01-15T08:00:00+08:00,2026-01-15T09:00:00+08:00\n"
    )


def _write_snapshot(path: Path, row: str) -> None:
    path.write_text(
        "symbol,record_date,ex_date,payment_date,cash_per_share,currency,source_url,source_published_at,ingested_at\n"
        + row,
        encoding="utf-8",
    )


def test_load_cash_distributions_creates_deterministic_auditable_actions(
    tmp_path: Path,
) -> None:
    from finance_lab.cash_distributions import load_cash_distributions

    snapshot = tmp_path / "sh_510300.csv"
    _write_snapshot(snapshot, _snapshot_row())

    first = load_cash_distributions(snapshot, symbol="sh.510300")
    second = load_cash_distributions(snapshot, symbol="sh.510300")

    assert len(first) == 1
    action = first[0]
    assert action.action_id == second[0].action_id
    assert len(action.action_id) == 64
    assert action.record_date.isoformat() == "2026-01-19"
    assert action.payment_date.isoformat() == "2026-01-22"
    assert action.cash_per_share == 0.1234
    assert action.source_url == "https://example.test/notices/510300-2026-01"


def test_load_cash_distributions_returns_empty_when_snapshot_is_absent(
    tmp_path: Path,
) -> None:
    from finance_lab.cash_distributions import load_cash_distributions

    assert load_cash_distributions(tmp_path / "absent.csv", symbol="sh.510300") == ()


def test_load_cash_distributions_rejects_payment_before_record_date(tmp_path: Path) -> None:
    from finance_lab.cash_distributions import (
        CashDistributionValidationError,
        load_cash_distributions,
    )

    snapshot = tmp_path / "sh_510300.csv"
    _write_snapshot(snapshot, _snapshot_row(payment_date="2026-01-18"))

    with pytest.raises(CashDistributionValidationError, match="record <= ex <= payment"):
        load_cash_distributions(snapshot, symbol="sh.510300")


def test_load_cash_distributions_rejects_non_https_source(tmp_path: Path) -> None:
    from finance_lab.cash_distributions import (
        CashDistributionValidationError,
        load_cash_distributions,
    )

    snapshot = tmp_path / "sh_510300.csv"
    _write_snapshot(snapshot, _snapshot_row(source_url="http://example.test/notice"))

    with pytest.raises(CashDistributionValidationError, match="HTTPS"):
        load_cash_distributions(snapshot, symbol="sh.510300")


def test_load_cash_distribution_snapshots_combines_append_only_files(
    tmp_path: Path,
) -> None:
    from finance_lab.cash_distributions import load_cash_distribution_snapshots

    snapshots = tmp_path / "cash_distributions"
    snapshots.mkdir()
    _write_snapshot(
        snapshots / "2026-02.csv",
        _snapshot_row(
            record_date="2026-02-18",
            ex_date="2026-02-19",
            payment_date="2026-02-23",
            source_url="https://example.test/notices/2026-02",
        ),
    )
    _write_snapshot(snapshots / "2026-01.csv", _snapshot_row())

    actions = load_cash_distribution_snapshots(snapshots, symbol="sh.510300")

    assert [action.record_date.isoformat() for action in actions] == [
        "2026-01-19",
        "2026-02-18",
    ]
    assert load_cash_distribution_snapshots(
        tmp_path / "absent", symbol="sh.510300"
    ) == ()


def test_load_cash_distributions_rejects_impossible_provenance_order(
    tmp_path: Path,
) -> None:
    from finance_lab.cash_distributions import (
        CashDistributionValidationError,
        load_cash_distributions,
    )

    snapshot = tmp_path / "impossible.csv"
    _write_snapshot(
        snapshot,
        _snapshot_row().replace(
            "2026-01-15T08:00:00+08:00,2026-01-15T09:00:00+08:00",
            "2026-01-15T10:00:00+08:00,2026-01-15T09:00:00+08:00",
        ),
    )

    with pytest.raises(CashDistributionValidationError, match="采集时间"):
        load_cash_distributions(snapshot, symbol="sh.510300")
