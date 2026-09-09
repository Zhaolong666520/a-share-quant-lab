from __future__ import annotations

from pathlib import Path

import pytest

_HEADER = (
    "symbol,effective_date,ratio_numerator,ratio_denominator,source_url,"
    "source_published_at,ingested_at\n"
)


def _write_snapshot(
    path: Path,
    *,
    effective_date: str = "2026-01-20",
    numerator: str = "3",
    denominator: str = "2",
    source_url: str = "https://example.test/notices/510300-adjustment",
    published_at: str = "2026-01-15T08:00:00+08:00",
    ingested_at: str = "2026-01-15T09:00:00+08:00",
) -> None:
    path.write_text(
        _HEADER
        + f"sh.510300,{effective_date},{numerator},{denominator},{source_url},"
        f"{published_at},{ingested_at}\n",
        encoding="utf-8",
    )


def test_load_share_adjustments_normalizes_ratio_and_identity(tmp_path: Path) -> None:
    from finance_lab.share_adjustments import load_share_adjustments

    snapshot = tmp_path / "adjustment.csv"
    _write_snapshot(snapshot, numerator="150", denominator="100")

    first = load_share_adjustments(snapshot, symbol="sh.510300")
    second = load_share_adjustments(snapshot, symbol="sh.510300")

    assert len(first) == 1
    assert first == second
    assert len(first[0].action_id) == 64
    assert first[0].ratio_numerator == 3
    assert first[0].ratio_denominator == 2
    assert first[0].effective_date.isoformat() == "2026-01-20"


@pytest.mark.parametrize(
    ("numerator", "denominator"),
    [("1", "1"), ("0", "1"), ("3", "0"), ("1.5", "1")],
)
def test_load_share_adjustments_rejects_unsafe_ratio(
    tmp_path: Path,
    numerator: str,
    denominator: str,
) -> None:
    from finance_lab.share_adjustments import (
        ShareAdjustmentValidationError,
        load_share_adjustments,
    )

    snapshot = tmp_path / "adjustment.csv"
    _write_snapshot(snapshot, numerator=numerator, denominator=denominator)

    with pytest.raises(ShareAdjustmentValidationError, match="比例"):
        load_share_adjustments(snapshot, symbol="sh.510300")


def test_load_share_adjustments_rejects_impossible_provenance_time(
    tmp_path: Path,
) -> None:
    from finance_lab.share_adjustments import (
        ShareAdjustmentValidationError,
        load_share_adjustments,
    )

    snapshot = tmp_path / "adjustment.csv"
    _write_snapshot(
        snapshot,
        published_at="2026-01-21T08:00:00+08:00",
        ingested_at="2026-01-20T09:00:00+08:00",
    )

    with pytest.raises(ShareAdjustmentValidationError, match="公告|采集"):
        load_share_adjustments(snapshot, symbol="sh.510300")


def test_load_share_adjustment_snapshots_combines_append_only_files(
    tmp_path: Path,
) -> None:
    from finance_lab.share_adjustments import load_share_adjustment_snapshots

    directory = tmp_path / "share_adjustments"
    directory.mkdir()
    _write_snapshot(directory / "b.csv", numerator="4", denominator="3")
    _write_snapshot(
        directory / "a.csv",
        effective_date="2026-02-20",
        numerator="3",
        denominator="2",
        source_url="https://example.test/notices/510300-adjustment-a",
    )

    actions = load_share_adjustment_snapshots(directory, symbol="sh.510300")

    assert len(actions) == 2
    assert sorted(action.ratio_numerator for action in actions) == [3, 4]
    assert load_share_adjustment_snapshots(
        tmp_path / "absent", symbol="sh.510300"
    ) == ()


def test_load_share_adjustment_snapshots_rejects_same_effective_date_conflict(
    tmp_path: Path,
) -> None:
    from finance_lab.share_adjustments import (
        ShareAdjustmentValidationError,
        load_share_adjustment_snapshots,
    )

    directory = tmp_path / "share_adjustments"
    directory.mkdir()
    _write_snapshot(directory / "first.csv")
    _write_snapshot(
        directory / "second.csv",
        numerator="4",
        denominator="3",
        source_url="https://example.test/notices/conflicting-adjustment",
    )

    with pytest.raises(ShareAdjustmentValidationError, match="同一生效日"):
        load_share_adjustment_snapshots(directory, symbol="sh.510300")
