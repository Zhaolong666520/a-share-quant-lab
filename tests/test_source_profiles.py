"""Source-profile integration tests: only provider boundaries are replaced."""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from finance_lab import pipeline
from finance_lab.config import Instrument, get_paths
from finance_lab.data_manifest import read_update_summary
from finance_lab.sources import DataSourceError
from finance_lab.storage import read_curated, save_curated

START, END = date(2018, 1, 1), date(2026, 10, 8)
INDEX = Instrument("000300", "sh", "index", "index")
ETF = Instrument("510300", "sh", "etf", "etf")


def _configure(root: Path, profile: str = "tencent_baostock") -> Path:
    config = root / "config"
    config.mkdir(exist_ok=True)
    (config / "instruments.json").write_text(
        json.dumps([vars(INDEX), vars(ETF)]), encoding="utf-8",
    )
    target = config / "source_profile.json"
    target.write_text(json.dumps({"schema_version": 1, "profile": profile}), encoding="utf-8")
    return target


def _frame(instrument: Instrument, source: str, start: date, end: date) -> pd.DataFrame:
    dates = [day for day in (date(2018, 1, 2), date(2026, 1, 5), END) if start <= day <= end]
    frame = pd.DataFrame({
        "symbol": [instrument.symbol] * len(dates), "trade_date": pd.to_datetime(dates),
        "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0,
        "volume": 1000.0, "volume_unit": "share", "amount": 11000.0,
        "adjustment": "none", "source": source, "ingested_at": "2026-10-08T10:00:00+00:00",
    })
    frame.attrs["source_diagnostics"] = {
        "received_rows": len(frame), "accepted_rows": len(frame),
        "rejected_rows": 0, "rejection_reasons": {},
    }
    if source == "tencent":
        frame.attrs["tencent_transport"] = {
            "protocol": "akshare_stock_zh_a_hist_tx_day_v1",
            "requested_start": start.isoformat(), "requested_end": end.isoformat(),
            "provider_windows": [{"start": "2018-01-01", "end": "2019-12-31"}],
            "raw_received_rows": len(frame) + 2, "unique_valid_rows": len(frame) + 1,
            "exact_overlap_rows": 1, "outside_requested_rows": 1,
        }
    return frame


def _providers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, date, date]]:
    calls: list[tuple[str, str, date, date]] = []
    for source in ("akshare", "baostock", "tencent"):
        def fetch(instrument: Instrument, start: date, end: date, source: str = source):
            calls.append((source, instrument.symbol, start, end))
            return _frame(instrument, source, start, end)
        monkeypatch.setattr(pipeline, f"fetch_{source}", fetch, raising=False)
    return calls


def test_explicit_profile_selects_tencent_and_records_etf_reference_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path)
    calls = _providers(monkeypatch)
    payload = pipeline.update_market_data(START, END, tmp_path)
    assert [item["primary_source"] for item in payload["records"]] == ["tencent", "tencent"]
    assert calls == [
        ("tencent", INDEX.symbol, START, END), ("baostock", INDEX.symbol, START, END),
        ("tencent", ETF.symbol, START, END),
        ("baostock", ETF.symbol, date(2026, 1, 1), END),
    ]
    assert payload["status"] == "success"
    assert payload["source_profile"]["primary_source"] == "tencent"
    record = payload["records"][1]
    assert record["source_request_windows"] == {
        "tencent": {"start_date": "2018-01-01", "end_date": END.isoformat(), "role": "primary"},
        "baostock": {"start_date": "2026-01-01", "end_date": END.isoformat(), "role": "reference"},
    }
    assert record["source_transport"]["tencent"]["raw_received_rows"] == 5
    assert record["source_diagnostics"]["tencent"]["received_rows"] == 3
    assert len(record["raw_files"]) == 2
    assert read_curated(ETF.symbol, get_paths(tmp_path))["source"].unique().tolist() == ["tencent"]
    assert read_update_summary(get_paths(tmp_path)).parse_error is None


@pytest.mark.parametrize("profile", [None, "legacy", "tencent_baostock"])
def test_profile_metadata_snapshot_and_immutability(tmp_path: Path, profile: str | None) -> None:
    from finance_lab.source_profiles import load_source_profile

    if profile is not None:
        target = _configure(tmp_path, profile)
    loaded = load_source_profile(tmp_path)
    assert loaded.profile_id == (profile or "legacy")
    assert loaded.configured == (profile is not None)
    assert len(loaded.config_sha256) == 64
    assert json.loads(json.dumps(loaded.metadata())) == loaded.metadata()
    loaded.assert_unchanged()
    with pytest.raises(FrozenInstanceError):
        loaded.profile_id = "changed"  # type: ignore[misc]
    if profile is not None:
        import hashlib
        assert loaded.config_sha256 == hashlib.sha256(target.read_bytes()).hexdigest()


@pytest.mark.parametrize("content", [
    b'{}', b'[]', b'{"schema_version":true,"profile":"legacy"}',
    b'{"schema_version":1.0,"profile":"legacy"}',
    b'{"schema_version":2,"profile":"legacy"}',
    b'{"schema_version":1,"profile":null}',
    b'{"schema_version":1,"profile":"unknown"}',
    b'{"schema_version":1,"profile":"legacy","extra":1}',
    b'{"schema_version":1,"profile":"legacy","profile":"legacy"}',
    b'{"schema_version":NaN,"profile":"legacy"}',
    b'{"schema_version":Infinity,"profile":"legacy"}', b'\xff', b' ' * 4097,
])
def test_invalid_profile_never_falls_back(tmp_path: Path, content: bytes) -> None:
    from finance_lab.source_profiles import SourceProfileError, load_source_profile

    target = _configure(tmp_path)
    target.write_bytes(content)
    with pytest.raises(SourceProfileError):
        load_source_profile(tmp_path)


@pytest.mark.parametrize("mutation", ["remove", "replace", "rewrite", "touch", "default_appears"])
def test_profile_snapshot_detects_changes(tmp_path: Path, mutation: str) -> None:
    from finance_lab.source_profiles import SourceProfileError, load_source_profile

    target = _configure(tmp_path)
    if mutation == "default_appears":
        target.unlink()
    loaded = load_source_profile(tmp_path)
    _mutate(target, mutation)
    with pytest.raises(SourceProfileError):
        loaded.assert_unchanged()


def _mutate(target: Path, mutation: str) -> None:
    if mutation == "remove":
        target.unlink()
    elif mutation == "replace":
        replacement = target.with_suffix(".replacement")
        replacement.write_bytes(target.read_bytes())
        replacement.replace(target)
    elif mutation == "touch":
        before = target.stat()
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    else:
        target.write_text('{"schema_version":1,"profile":"legacy"}', encoding="utf-8")


@pytest.mark.parametrize("component", ["file", "config", "ancestor", "broken"])
def test_profile_rejects_links(tmp_path: Path, component: str) -> None:
    from finance_lab.source_profiles import SourceProfileError, load_source_profile

    actual = tmp_path / "actual"
    actual.mkdir()
    target = _configure(actual)
    link = tmp_path / "link"
    try:
        if component == "ancestor":
            link.symlink_to(actual, target_is_directory=True)
            root = link
        else:
            root = tmp_path / "root"
            root.mkdir()
            if component == "config":
                (root / "config").symlink_to(actual / "config", target_is_directory=True)
            else:
                (root / "config").mkdir()
                (root / "config" / "source_profile.json").symlink_to(
                    target if component == "file" else actual / "absent",
                )
    except OSError as exc:
        pytest.skip(f"host cannot create symlinks: {exc.winerror}")
    with pytest.raises(SourceProfileError):
        load_source_profile(root)


def test_profile_rejects_directory_and_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_lab.source_profiles import SourceProfileError, load_source_profile

    target = _configure(tmp_path)
    original = Path.open
    def denied(self: Path, *args, **kwargs):
        if self == target:
            raise PermissionError("test")
        return original(self, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", denied)
        with pytest.raises(SourceProfileError):
            load_source_profile(tmp_path)
    target.unlink()
    target.mkdir()
    with pytest.raises(SourceProfileError):
        load_source_profile(tmp_path)


def test_profile_detects_mutation_during_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_lab.source_profiles import SourceProfileError, load_source_profile

    target = _configure(tmp_path)
    original = os.fstat
    mutated = False
    def mutate_on_fstat(fd: int):
        nonlocal mutated
        result = original(fd)
        if not mutated:
            mutated = True
            _mutate(target, "rewrite")
        return result
    monkeypatch.setattr(os, "fstat", mutate_on_fstat)
    with pytest.raises(SourceProfileError):
        load_source_profile(tmp_path)


@pytest.mark.parametrize("failed", [("tencent",), ("baostock",), ("tencent", "baostock")])
def test_required_source_failure_preserves_failure_and_no_reference_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: tuple[str, ...],
) -> None:
    _configure(tmp_path)
    _providers(monkeypatch)
    previous = _frame(ETF, "baostock", START, END)
    old_path = save_curated(previous, get_paths(tmp_path))
    old_bytes = old_path.read_bytes()
    for source in failed:
        def fail(*args):
            raise DataSourceError("mock provider failure", stage="request")
        monkeypatch.setattr(pipeline, f"fetch_{source}", fail)
    payload = pipeline.update_market_data(START, END, tmp_path)
    assert payload["failed_sources"] == len(failed) * 2
    assert payload["status"] == ("failed" if "tencent" in failed else "partial_sources")
    for record in payload["records"]:
        assert set(record["source_errors"]) == set(failed)
        assert all(record["source_diagnostics"][key]["status"] == "failed" for key in failed)
        if "tencent" in failed:
            assert record["rows"] == 0
            assert record["primary_source"] is None
    if "tencent" in failed:
        assert old_path.read_bytes() == old_bytes
    assert read_update_summary(get_paths(tmp_path)).parse_error is None


@pytest.mark.parametrize("mutation", ["remove", "replace", "rewrite", "touch", "default_appears"])
def test_config_changes_during_fetch_quarantine_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    from finance_lab.market_data_state import MarketDataStateError, assert_market_data_readable
    from finance_lab.source_profiles import SourceProfileError

    target = _configure(tmp_path)
    if mutation == "default_appears":
        target.unlink()
    _providers(monkeypatch)
    source = "akshare" if mutation == "default_appears" else "tencent"
    changed = False
    def fetch(instrument: Instrument, start: date, end: date):
        nonlocal changed
        if not changed:
            changed = True
            _mutate(target, mutation)
        return _frame(instrument, source, start, end)
    monkeypatch.setattr(pipeline, f"fetch_{source}", fetch)
    with pytest.raises(SourceProfileError):
        pipeline.update_market_data(START, END, tmp_path)
    assert not (tmp_path / "outputs" / "update_summary.json").exists()
    state = json.loads((tmp_path / "data" / "market_update_state.json").read_text())
    assert state["status"] == "failed"
    with pytest.raises(MarketDataStateError):
        assert_market_data_readable(get_paths(tmp_path))


@pytest.mark.parametrize("invalid", ["unknown_profile", "instrument", "kind", "old_etf_window"])
def test_invalid_selection_rejects_before_any_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    target = _configure(tmp_path)
    calls = _providers(monkeypatch)
    if invalid == "unknown_profile":
        target.write_text('{"schema_version":1,"profile":"unknown"}')
    elif invalid in {"instrument", "kind"}:
        (target.parent / "instruments.json").write_text(json.dumps([
            vars(INDEX), vars(Instrument("510301" if invalid == "instrument" else "510300",
                                        "sh", "invalid", "index")),
        ]))
    with pytest.raises(ValueError):
        pipeline.update_market_data(START, date(2025, 12, 31) if invalid == "old_etf_window"
                                    else END, tmp_path)
    assert calls == []


def test_source_label_must_match_selected_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure(tmp_path)
    _providers(monkeypatch)
    monkeypatch.setattr(pipeline, "fetch_tencent", lambda instrument, start, end:
                        _frame(instrument, "baostock", start, end))
    payload = pipeline.update_market_data(START, END, tmp_path)
    assert payload["status"] == "failed"
    assert payload["failed_sources"] == 2
    assert not list((tmp_path / "data" / "raw" / "tencent").glob("*/*.parquet"))


@pytest.mark.parametrize("component", ["root", "config", "file", "parent"])
def test_profile_rejects_windows_reparse_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str,
) -> None:
    import stat

    from finance_lab.source_profiles import SourceProfileError, load_source_profile

    target = _configure(tmp_path)
    selected = {"root": tmp_path, "config": target.parent, "file": target,
                "parent": tmp_path.parent}[component]
    original = Path.lstat
    def reparse(self: Path):
        result = original(self)
        if self == selected:
            return SimpleNamespace(st_mode=result.st_mode,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return result
    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(SourceProfileError):
        load_source_profile(tmp_path)


@pytest.mark.parametrize("failure", [False, True])
def test_fetch_cli_uses_real_update_and_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool,
) -> None:
    from finance_lab.cli import main

    _configure(tmp_path)
    _providers(monkeypatch)
    if failure:
        def fail(*args):
            raise DataSourceError("mock", stage="request")
        monkeypatch.setattr(pipeline, "fetch_baostock", fail)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main(["fetch", "--start", START.isoformat(), "--end", END.isoformat()]) == (
        3 if failure else 0
    )


@pytest.mark.parametrize("failure", ["tencent", "baostock"])
def test_paper_daily_required_failure_exits_one_without_advancing_existing_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    import duckdb

    from finance_lab.cli import main
    from finance_lab.paper_pipeline import paper_init_portfolio
    from tests.paper_helpers import make_paper_test_project

    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    paper_init_portfolio(root=tmp_path, as_of_date=prices["trade_date"].max().date())
    def ledger_rows():
        with duckdb.connect(str(paths.database), read_only=True) as connection:
            tables = [name for (name,) in connection.execute("SHOW TABLES").fetchall()
                      if name.startswith("paper_")]
            assert tables
            return {name: connection.execute(f'SELECT * FROM "{name}"').fetchall()
                    for name in tables}
    previous = ledger_rows()
    _configure(tmp_path)
    _providers(monkeypatch)
    def fail(*args):
        raise DataSourceError("mock", stage="request")
    monkeypatch.setattr(pipeline, f"fetch_{failure}", fail)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main(["paper-daily", "--start", START.isoformat(), "--as-of", END.isoformat()]) == 1
    assert ledger_rows() == previous
    latest = json.loads((tmp_path / "outputs" / "default_paper_automation_latest.json").read_text(
        encoding="utf-8",
    ))
    assert latest["failed_step"] == "market_data_update"
    assert latest["processed_dates"] == []
    assert latest["ledger_committed"] is None  # Existing receipt has not entered the ledger phase.


@pytest.mark.parametrize("previous_source", ["akshare", "baostock"])
def test_new_profile_replaces_previous_source_on_overlapping_curated_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous_source: str,
) -> None:
    _configure(tmp_path)
    _providers(monkeypatch)
    save_curated(_frame(ETF, previous_source, START, END), get_paths(tmp_path))
    payload = pipeline.update_market_data(START, END, tmp_path)
    assert payload["status"] == "success"
    assert read_curated(ETF.symbol, get_paths(tmp_path))["source"].unique().tolist() == ["tencent"]


def test_profile_missing_to_present_during_load_is_not_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_lab.source_profiles import SourceProfileError, load_source_profile

    target = _configure(tmp_path)
    target.unlink()
    original = Path.lstat
    changed = False
    def appear(self: Path):
        nonlocal changed
        if self == target and not changed:
            changed = True
            _mutate(target, "default_appears")
            raise FileNotFoundError("captured before creation")
        return original(self)
    monkeypatch.setattr(Path, "lstat", appear)
    with pytest.raises(SourceProfileError):
        load_source_profile(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction behavior")
@pytest.mark.parametrize("component", ["root", "config"])
def test_profile_rejects_native_windows_junction(tmp_path: Path, component: str) -> None:
    import subprocess

    from finance_lab.source_profiles import SourceProfileError, load_source_profile

    actual = tmp_path / "actual"
    actual.mkdir()
    _configure(actual)
    root = tmp_path / "root"
    if component == "config":
        root.mkdir()
    link = root if component == "root" else root / "config"
    destination = actual if component == "root" else actual / "config"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(destination)],
        capture_output=True, check=False,
    )
    assert result.returncode == 0, "junction fixture creation failed"
    with pytest.raises(SourceProfileError):
        load_source_profile(root)


def test_switch_back_to_legacy_uses_original_curated_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(tmp_path)
    _providers(monkeypatch)
    pipeline.update_market_data(START, END, tmp_path)
    _configure(tmp_path, "legacy")
    payload = pipeline.update_market_data(START, END, tmp_path)
    assert [record["primary_source"] for record in payload["records"]] == ["akshare"] * 2
    assert read_curated(ETF.symbol, get_paths(tmp_path))["source"].unique().tolist() == ["akshare"]


@pytest.mark.parametrize("preferred,source", [("unknown", "tencent"), ("tencent", "baostock")])
def test_storage_explicit_preference_rejects_invalid_selection_before_save(
    tmp_path: Path, preferred: str, source: str,
) -> None:
    paths = get_paths(tmp_path)
    with pytest.raises(ValueError):
        save_curated(_frame(ETF, source, START, END), paths, preferred_source=preferred)
    assert not list(paths.curated.glob("*.parquet"))


@pytest.mark.parametrize("profile", [None, "legacy"])
def test_legacy_default_retains_windows_and_baostock_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str | None,
) -> None:
    target = _configure(tmp_path, "legacy")
    if profile is None:
        target.unlink()
    calls = _providers(monkeypatch)
    def fail(*args):
        raise DataSourceError("mock", stage="request")
    monkeypatch.setattr(pipeline, "fetch_akshare", fail)
    payload = pipeline.update_market_data(START, END, tmp_path)
    assert payload["status"] == "partial_sources"
    assert [record["primary_source"] for record in payload["records"]] == ["baostock"] * 2
    assert calls == [("baostock", INDEX.symbol, START, END), ("baostock", ETF.symbol, START, END)]
