import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from finance_lab import recovery_rebuild
from finance_lab.cli import main
from finance_lab.paper_store import PaperStore
from finance_lab.recovery import prepare_recovery
from finance_lab.recovery_rebuild import RebuildError, rebuild_candidate, verify_candidate
from tests.test_paper_store import _accounts_and_initial_steps


@pytest.fixture
def materials(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    project = tmp_path / "project"
    raw_dir = project / "data/raw/baostock/sh_000300"
    raw_dir.mkdir(parents=True)
    (project / "data/curated").mkdir()
    (project / "config").mkdir()
    (project / "config/instruments.json").write_text(
        json.dumps([{"code": "000300", "exchange": "sh", "name": "index", "kind": "index"}]),
        encoding="utf-8",
    )
    frame = pd.DataFrame(
        {
            "symbol": ["sh.000300", "sh.000300"],
            "trade_date": pd.to_datetime(["2026-09-21", "2026-09-22"]),
            "open": [10.0, 11.0],
            "high": [10.5, 11.5],
            "low": [9.5, 10.5],
            "close": [10.2, 11.2],
            "volume": [100.0, 110.0],
            "volume_unit": ["shares", "shares"],
            "amount": [1000.0, 1100.0],
            "adjustment": ["none", "none"],
            "source": ["baostock", "baostock"],
            "ingested_at": ["2026-09-23T00:00:00Z"] * 2,
        }
    )
    raw = raw_dir / "observed.parquet"
    frame.to_parquet(raw, index=False)
    stale = frame.copy()
    stale["close"] = [999.0, 999.0]
    stale.to_parquet(project / "data/curated/sh_000300.parquet", index=False)
    (project / "data/market_update_state.json").write_bytes(b"broken marker")
    with PaperStore(project / "data/finance_lab.duckdb") as store:
        store.ensure_schema()
    bundle = tmp_path / "bundle"
    assert prepare_recovery(project, bundle)["ok"] is True
    selection = tmp_path / "selection.json"
    selection.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle_id": json.loads((bundle / "recovery_manifest.json").read_text())[
                    "bundle_id"
                ],
                "items": [
                    {
                        "symbol": "sh.000300",
                        "raw_path": raw.relative_to(project).as_posix(),
                        "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return project, bundle, selection, tmp_path / "candidate"


def test_rebuild_replaces_only_candidate_market_data(
    materials: tuple[Path, Path, Path, Path],
) -> None:
    project, bundle, selection, candidate = materials
    before_db = (project / "data/finance_lab.duckdb").read_bytes()
    before_check = (bundle / "recovery_check.json").read_bytes()
    before_marker = (bundle / "snapshot/data/market_update_state.json").read_bytes()
    result = rebuild_candidate(bundle, selection, candidate)
    assert result["status"] == "candidate_validated"
    assert result["ready_for_use"] is False
    assert result["database_rows"] == 2
    assert result["ledger_audit"]["status"] == "passed"
    assert result["items"][0]["first_trade_date"] == "2026-09-21"
    assert pd.read_parquet(candidate / "snapshot/data/curated/sh_000300.parquet")[
        "close"
    ].tolist() == [10.2, 11.2]
    assert (candidate / "snapshot/data/market_update_state.json").read_bytes() == before_marker
    assert (project / "data/finance_lab.duckdb").read_bytes() == before_db
    assert verify_candidate(bundle, candidate)["database_rows"] == 2
    assert (bundle / "recovery_check.json").read_bytes() == before_check


def test_destination_inside_live_project_rejected(
    materials: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, bundle, selection, _ = materials
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(project))
    destination = project / "candidate"
    with pytest.raises(RebuildError):
        rebuild_candidate(bundle, selection, destination)
    assert not destination.exists()


def test_preserved_config_tamper_is_detected(materials: tuple[Path, Path, Path, Path]) -> None:
    _, bundle, selection, candidate = materials
    rebuild_candidate(bundle, selection, candidate)
    (candidate / "snapshot/config/instruments.json").write_bytes(b"corrupted")
    report = json.loads((candidate / "candidate_report.json").read_text(encoding="utf-8"))
    report["snapshot_manifest"] = recovery_rebuild._inventory(
        candidate / "snapshot", source=False
    )
    (candidate / "candidate_report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(RebuildError):
        verify_candidate(bundle, candidate)


def test_final_verification_failure_does_not_publish_success(
    materials: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle, selection, candidate = materials

    def fail_verify(*args: object, **kwargs: object) -> dict[str, object]:
        raise OSError("concurrent change")

    monkeypatch.setattr(recovery_rebuild, "verify_candidate", fail_verify)
    with pytest.raises(RebuildError):
        rebuild_candidate(bundle, selection, candidate)
    report = json.loads((candidate / "candidate_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "candidate_pending_verification"


@pytest.mark.parametrize("mutation", ["bad_hash", "wrong_symbol", "missing_symbol", "wrong_path"])
def test_bad_selection_stops_before_destination(
    materials: tuple[Path, Path, Path, Path], mutation: str
) -> None:
    _, bundle, selection, candidate = materials
    payload = json.loads(selection.read_text())
    if mutation == "bad_hash":
        payload["items"][0]["sha256"] = "0" * 64
    elif mutation == "wrong_symbol":
        payload["items"][0]["symbol"] = "sh.510300"
    elif mutation == "missing_symbol":
        payload["items"] = []
    else:
        payload["items"][0]["raw_path"] = "../elsewhere.parquet"
    selection.write_text(json.dumps(payload))
    with pytest.raises(RebuildError):
        rebuild_candidate(bundle, selection, candidate)
    assert not candidate.exists()


def test_candidate_tamper_is_detected(materials: tuple[Path, Path, Path, Path]) -> None:
    _, bundle, selection, candidate = materials
    rebuild_candidate(bundle, selection, candidate)
    (candidate / "snapshot/data/curated/sh_000300.parquet").write_bytes(b"bad")
    with pytest.raises(RebuildError):
        verify_candidate(bundle, candidate)


@pytest.mark.parametrize("change", ["extra_field", "boolean_version", "missing_report"])
def test_candidate_report_requires_exact_schema(
    materials: tuple[Path, Path, Path, Path], change: str
) -> None:
    _, bundle, selection, candidate = materials
    rebuild_candidate(bundle, selection, candidate)
    report_file = candidate / "candidate_report.json"
    if change == "missing_report":
        report_file.unlink()
    else:
        report = json.loads(report_file.read_text(encoding="utf-8"))
        if change == "extra_field":
            report["unexpected"] = "ignored?"
        else:
            report["schema_version"] = True
        report_file.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(RebuildError):
        verify_candidate(bundle, candidate)


def _new_bundle(project: Path, selection: Path, tmp_path: Path) -> Path:
    bundle = tmp_path / "new-bundle"
    assert prepare_recovery(project, bundle)["ok"] is True
    payload = json.loads(selection.read_text())
    payload["bundle_id"] = json.loads((bundle / "recovery_manifest.json").read_text())["bundle_id"]
    raw = project / payload["items"][0]["raw_path"]
    payload["items"][0]["sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    selection.write_text(json.dumps(payload), encoding="utf-8")
    return bundle


def test_nonempty_paper_ledger_is_identical(materials: tuple[Path, Path, Path, Path]) -> None:
    project, _, selection, candidate = materials
    a, b, first, second, context = _accounts_and_initial_steps()
    with PaperStore(project / "data/finance_lab.duckdb") as store:
        store.initialize_portfolio((a, b), (first, second), context, batch_id="test")
    before = (project / "data/finance_lab.duckdb").read_bytes()
    bundle = _new_bundle(project, selection, project.parent)
    report = rebuild_candidate(bundle, selection, candidate)
    assert report["ledger_audit"]["event_count"] > 0
    assert len(report["ledger_audit"]["accounts"]) == 2
    assert len(report["ledger_table_hashes"]) == 5
    assert (project / "data/finance_lab.duckdb").read_bytes() == before
    assert (
        verify_candidate(bundle, candidate)["ledger_table_hashes"] == report["ledger_table_hashes"]
    )


@pytest.mark.parametrize(
    "change", ["wrong_source", "mixed_adjustment", "bad_date", "empty_metadata", "numeric_string"]
)
def test_bad_raw_content_rejected_before_candidate(
    materials: tuple[Path, Path, Path, Path], change: str
) -> None:
    project, _, selection, candidate = materials
    raw = project / json.loads(selection.read_text())["items"][0]["raw_path"]
    frame = pd.read_parquet(raw)
    if change == "wrong_source":
        frame.loc[1, "source"] = "akshare"
    elif change == "mixed_adjustment":
        frame.loc[1, "adjustment"] = "forward"
    elif change == "bad_date":
        frame.loc[1, "trade_date"] = frame.loc[0, "trade_date"]
    elif change == "numeric_string":
        frame["close"] = frame["close"].astype(str)
    else:
        frame.loc[1, "ingested_at"] = ""
    frame.to_parquet(raw, index=False)
    bundle = _new_bundle(project, selection, project.parent)
    with pytest.raises(RebuildError):
        rebuild_candidate(bundle, selection, candidate)
    assert not candidate.exists()


def test_extra_published_curated_file_rejected(materials: tuple[Path, Path, Path, Path]) -> None:
    project, _, selection, candidate = materials
    (project / "data/curated/sh_510300.parquet").write_bytes(b"old extra")
    bundle = _new_bundle(project, selection, project.parent)
    with pytest.raises(RebuildError):
        rebuild_candidate(bundle, selection, candidate)
    assert not candidate.exists()


def test_interrupted_report_keeps_unfinished_candidate(
    materials: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bundle, selection, candidate = materials
    original = recovery_rebuild._write_json

    def fail_report(path: Path, payload: object) -> None:
        if path.name == "candidate_report.json":
            raise OSError("disk full")
        original(path, payload)

    monkeypatch.setattr(recovery_rebuild, "_write_json", fail_report)
    with pytest.raises(RebuildError):
        rebuild_candidate(bundle, selection, candidate)
    assert candidate.exists()
    assert not (candidate / "candidate_report.json").exists()
    with pytest.raises(RebuildError):
        verify_candidate(bundle, candidate)
    with pytest.raises(RebuildError):
        rebuild_candidate(bundle, selection, candidate)


def test_cli_rebuild_and_verify(
    materials: tuple[Path, Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _, bundle, selection, candidate = materials
    assert (
        main(
            [
                "data-recovery-rebuild",
                "--bundle",
                str(bundle),
                "--selection",
                str(selection),
                "--destination",
                str(candidate),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ready_for_use"] is False
    assert (
        main(
            [
                "data-recovery-candidate-verify",
                "--bundle",
                str(bundle),
                "--candidate",
                str(candidate),
            ]
        )
        == 0
    )
    capsys.readouterr()
    (candidate / "candidate_selection.json").write_bytes(b"bad")
    assert (
        main(
            [
                "data-recovery-candidate-verify",
                "--bundle",
                str(bundle),
                "--candidate",
                str(candidate),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["ok"] is False
