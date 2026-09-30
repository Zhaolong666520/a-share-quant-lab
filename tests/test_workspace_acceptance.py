import json
from pathlib import Path

import pytest

from finance_lab.cli import main
from finance_lab.config import get_paths
from finance_lab.market_data_state import (
    MarketDataStateError,
    assert_market_data_readable,
    assert_market_data_writable,
    inspect_market_update_status,
)
from finance_lab.recovery import _hash
from finance_lab.recovery_activation import ActivationError, activate_candidate, verify_activation
from finance_lab.recovery_rebuild import RebuildError, rebuild_candidate, verify_candidate
from finance_lab.research_session import SessionError, create_session, verify_session
from tests.test_paper_store import _accounts_and_initial_steps
from tests.test_recovery_rebuild import _new_bundle
from tests.test_recovery_rebuild import materials as _materials

materials = _materials


def accepted(materials: tuple[Path, Path, Path, Path], tmp_path: Path) -> Path:
    _, bundle, selection, candidate = materials
    report = rebuild_candidate(bundle, selection, candidate)
    root = tmp_path / "accepted"
    activate_candidate(bundle, candidate, root, confirm_candidate_id=report["candidate_id"])
    return root


def test_acceptance_preserves_evidence_and_marks_offline(materials, tmp_path):
    project, bundle, selection, candidate = materials
    before = _hash(project / "data/finance_lab.duckdb")
    report = rebuild_candidate(bundle, selection, candidate)
    original_marker = (candidate / "snapshot/data/market_update_state.json").read_bytes()
    root = tmp_path / "accepted"
    receipt = activate_candidate(
        bundle, candidate, root, confirm_candidate_id=report["candidate_id"]
    )
    assert receipt["ready_for_use"] is True
    assert receipt == verify_activation(root)
    assert verify_candidate(bundle, candidate) == report
    assert _hash(project / "data/finance_lab.duckdb") == before
    assert (
        root / "recovery_evidence/original_market_update_state.json"
    ).read_bytes() == original_marker
    assert_market_data_readable(get_paths(root))
    assert inspect_market_update_status(get_paths(root))["source_outcome"] == "partial_sources"
    (root / "data/curated/sh_000300.parquet").write_bytes(b"changed")
    with pytest.raises(ActivationError):
        verify_activation(root)


def test_explicit_confirmation_before_creating_destination(materials, tmp_path):
    _, bundle, selection, candidate = materials
    rebuild_candidate(bundle, selection, candidate)
    target = tmp_path / "accepted"
    with pytest.raises(ActivationError):
        activate_candidate(bundle, candidate, target, confirm_candidate_id="wrong")
    assert not target.exists()


def test_session_survives_live_changes_and_rejects_tamper(materials, tmp_path):
    root = accepted(materials, tmp_path)
    session = tmp_path / "session"
    receipt = create_session(root, session)
    (root / "data/curated/sh_000300.parquet").write_bytes(b"live update")
    assert verify_session(session) == receipt
    with pytest.raises(MarketDataStateError):
        assert_market_data_writable(get_paths(session / "snapshot"))
    (session / "snapshot/config/instruments.json").write_text("[]")
    with pytest.raises(SessionError):
        verify_session(session)


def test_session_cli_binds_reports_and_restores_environment(
    materials, tmp_path, monkeypatch, capsys
):
    root = accepted(materials, tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(root))
    session = tmp_path / "session"
    assert main(["research-session-create", "--destination", str(session)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert main(["research-session-verify", "--session", str(session)]) == 0
    capsys.readouterr()
    from finance_lab import cli

    seen = []
    monkeypatch.setattr(
        cli,
        "_run_command",
        lambda args: seen.append((get_paths().root, args.expected_dataset_id)) or 0,
    )
    for command in ("backtest", "experiment"):
        assert main([command, "--symbol", "sh.000300", "--session", str(session)]) == 0
    assert seen == [(session / "snapshot", receipt["dataset_id"])] * 2
    assert get_paths().root == root
    assert (
        main(
            [
                "backtest",
                "--symbol",
                "sh.000300",
                "--session",
                str(session),
                "--expected-dataset-id",
                "0" * 64,
            ]
        )
        == 1
    )
    assert get_paths().root == root


def test_creation_expected_id_and_path_safety(materials, tmp_path):
    root = accepted(materials, tmp_path)
    target = tmp_path / "session"
    with pytest.raises(SessionError):
        create_session(root, target, expected_dataset_id="0" * 64)
    assert not target.exists()
    with pytest.raises(SessionError):
        create_session(root, root / "nested")
    with pytest.raises(SessionError):
        create_session(root, root)


def test_activation_interruption_leaves_quarantine(materials, tmp_path, monkeypatch):
    from finance_lab import recovery_activation

    _, bundle, selection, candidate = materials
    report = rebuild_candidate(bundle, selection, candidate)

    def fail(*args):
        raise OSError("interrupted")

    monkeypatch.setattr(recovery_activation, "copy_inventory", fail)
    root = tmp_path / "failed"
    with pytest.raises(OSError):
        activate_candidate(bundle, candidate, root, confirm_candidate_id=report["candidate_id"])
    assert inspect_market_update_status(get_paths(root))["blocked"] is True
    assert not (root / "recovery_acceptance.json").exists()


def test_session_blocks_mutating_cli(materials, tmp_path, monkeypatch):
    root = accepted(materials, tmp_path)
    session = tmp_path / "session"
    create_session(root, session)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(session / "snapshot"))
    assert main(["init"]) == 1
    assert main(["fetch"]) == 1
    verify_session(session)


def test_real_cli_report_uses_frozen_dataset(materials, tmp_path, monkeypatch, capsys):
    root = accepted(materials, tmp_path)
    session = tmp_path / "session"
    receipt = create_session(root, session)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(root))
    (root / "data/curated/sh_000300.parquet").write_bytes(b"live source no longer readable")
    assert (
        main(
            [
                "backtest",
                "--symbol",
                "sh.000300",
                "--strategy",
                "buy_hold",
                "--session",
                str(session),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert verify_session(session) == receipt
    assert list((session / "snapshot/outputs").rglob("*.html"))
    assert get_paths().root == root


def test_direct_snapshot_root_cannot_bypass_session_check(materials, tmp_path, monkeypatch):
    root = accepted(materials, tmp_path)
    session = tmp_path / "session"
    create_session(root, session)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(session / "snapshot"))
    (session / "snapshot/config/instruments.json").write_text("[]")
    assert main(["backtest", "--symbol", "sh.000300"]) == 1


def test_missing_verification_is_non_mutating(tmp_path):
    target = tmp_path / "missing"
    with pytest.raises(ActivationError):
        verify_activation(target)
    assert not target.exists()
    with pytest.raises(FileNotFoundError):
        verify_session(target)
    assert not target.exists()


def test_paper_api_and_update_batch_blocked_in_session(materials, tmp_path):
    from datetime import date

    from finance_lab.market_data_state import market_update_batch
    from finance_lab.paper_pipeline import paper_init_portfolio, paper_run_portfolio

    root = accepted(materials, tmp_path)
    session = tmp_path / "session"
    create_session(root, session)
    paths = get_paths(session / "snapshot")
    for operation in (paper_init_portfolio, paper_run_portfolio):
        with pytest.raises(MarketDataStateError):
            operation(root=paths.root)
    with pytest.raises(MarketDataStateError):
        with market_update_batch(paths, date(2026, 9, 21), date(2026, 9, 23), ("sh.000300",)):
            pytest.fail("frozen update entered")
    verify_session(session)


def test_acceptance_is_blocked_until_receipt_published(materials, tmp_path, monkeypatch):
    from finance_lab import recovery_activation

    _, bundle, selection, candidate = materials
    report = rebuild_candidate(bundle, selection, candidate)
    root = tmp_path / "failed"
    original = recovery_activation._write_json

    def fail_receipt(path, payload):
        if path.name == "recovery_acceptance.json":
            assert inspect_market_update_status(get_paths(root))["blocked"] is True
            raise OSError("receipt failure")
        original(path, payload)

    monkeypatch.setattr(recovery_activation, "_write_json", fail_receipt)
    with pytest.raises(OSError):
        activate_candidate(bundle, candidate, root, confirm_candidate_id=report["candidate_id"])
    assert inspect_market_update_status(get_paths(root))["blocked"] is True
    assert not (root / "recovery_acceptance.json").exists()


def test_session_failed_copy_is_not_a_valid_session(materials, tmp_path, monkeypatch):
    from finance_lab import research_session

    root = accepted(materials, tmp_path)
    target = tmp_path / "failed_session"

    def fail(*args):
        raise OSError("copy failure")

    monkeypatch.setattr(research_session, "copy_inventory", fail)
    with pytest.raises(OSError):
        create_session(root, target)
    assert (target / "snapshot/data/research_session.json").is_file()
    assert not (target / "session.json").exists()
    with pytest.raises(SessionError):
        verify_session(target)


@pytest.mark.parametrize("location", ["bundle", "candidate"])
def test_activation_rejects_tampered_inputs(materials, tmp_path, location):
    _, bundle, selection, candidate = materials
    report = rebuild_candidate(bundle, selection, candidate)
    source = bundle if location == "bundle" else candidate
    (source / "snapshot/config/instruments.json").write_text("[]")
    root = tmp_path / "rejected"
    with pytest.raises(RebuildError):
        activate_candidate(bundle, candidate, root, confirm_candidate_id=report["candidate_id"])
    assert not root.exists()


def test_activation_cli_and_safe_failure(materials, tmp_path, capsys):
    _, bundle, selection, candidate = materials
    report = rebuild_candidate(bundle, selection, candidate)
    root = tmp_path / "cli_accepted"
    assert (
        main(
            [
                "data-recovery-activate",
                "--bundle",
                str(bundle),
                "--candidate",
                str(candidate),
                "--destination",
                str(root),
                "--confirm-candidate-id",
                report["candidate_id"],
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ready_for_use"] is True
    assert main(["data-recovery-acceptance-verify", "--root", str(root)]) == 0
    capsys.readouterr()
    assert main(["research-session-verify", "--session", str(tmp_path / "missing")]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_session_refuses_busy_market_lock(materials, tmp_path):
    import threading

    from finance_lab.market_data_lock import market_data_lock

    root = accepted(materials, tmp_path)
    session = tmp_path / "session"
    create_session(root, session)
    results = []

    def verify():
        try:
            verify_session(session)
        except Exception as exc:
            results.append(type(exc).__name__)

    with market_data_lock(session / "snapshot/data"):
        worker = threading.Thread(target=verify)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert results == ["MarketDataLockError"]


def test_acceptance_preserves_nonempty_paper_ledger(materials, tmp_path):
    from finance_lab.paper_store import PaperStore

    project, _, selection, candidate = materials
    a, b, first, second, context = _accounts_and_initial_steps()
    with PaperStore(project / "data/finance_lab.duckdb") as store:
        store.initialize_portfolio((a, b), (first, second), context, batch_id="acceptance")
    bundle = _new_bundle(project, selection, tmp_path)
    report = rebuild_candidate(bundle, selection, candidate)
    before = _hash(project / "data/finance_lab.duckdb")
    receipt = activate_candidate(
        bundle, candidate, tmp_path / "nonempty", confirm_candidate_id=report["candidate_id"]
    )
    assert report["ledger_audit"]["event_count"] > 0
    assert receipt["ledger_table_hashes"] == report["ledger_table_hashes"]
    assert len(receipt["ledger_table_hashes"]) == 5
    assert _hash(project / "data/finance_lab.duckdb") == before


def test_session_supports_curated_only_legacy_project(materials, tmp_path, monkeypatch):
    root = accepted(materials, tmp_path)
    source = tmp_path / "curated_only"
    (source / "data/curated").mkdir(parents=True)
    (source / "config").mkdir()
    (source / "data/curated/sh_000300.parquet").write_bytes(
        (root / "data/curated/sh_000300.parquet").read_bytes()
    )
    (source / "config/instruments.json").write_bytes(
        (root / "config/instruments.json").read_bytes()
    )
    session = tmp_path / "legacy_session"
    receipt = create_session(source, session)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(source))
    assert main(["backtest", "--strategy", "buy_hold", "--session", str(session)]) == 0
    assert verify_session(session) == receipt
    assert not (source / "data/raw").exists()
    assert not (source / "outputs").exists()


def test_session_receipt_cannot_change_during_command(materials, tmp_path, monkeypatch):
    from finance_lab import cli

    root = accepted(materials, tmp_path)
    session = tmp_path / "changed_receipt"
    create_session(root, session)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(root))

    def replace_receipt(args):
        path = session / "session.json"
        payload = json.loads(path.read_text())
        payload["created_at"] = "2026-09-01T00:00:00Z"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return 0

    monkeypatch.setattr(cli, "_run_command", replace_receipt)
    assert main(["backtest", "--session", str(session)]) == 1
    assert get_paths().root == root


def test_single_day_recovery_does_not_invent_summary_date(materials, tmp_path):
    import pandas as pd

    project, _, selection, candidate = materials
    raw = project / "data/raw/baostock/sh_000300/observed.parquet"
    pd.read_parquet(raw).iloc[:1].to_parquet(raw, index=False)
    bundle = _new_bundle(project, selection, tmp_path)
    report = rebuild_candidate(bundle, selection, candidate)
    root = tmp_path / "single_day"
    activate_candidate(bundle, candidate, root, confirm_candidate_id=report["candidate_id"])
    summary = json.loads((root / "outputs/update_summary.json").read_text(encoding="utf-8"))
    assert summary["end"] == report["items"][0]["last_trade_date"]
