"""Eight fixed subprocess failure cells, using only fresh synthetic workspaces."""

import hashlib
import json
import os
import shutil
from contextlib import nullcontext
from pathlib import Path

import pytest

from finance_lab.config import get_paths
from finance_lab.market_data_lock import market_data_lock
from finance_lab.paper_store import PaperStore
from finance_lab.recovery import prepare_recovery
from finance_lab.recovery_activation import activate_candidate
from finance_lab.recovery_rebuild import rebuild_candidate
from finance_lab.research_session import create_session
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated
from tests.cli_recovery_helpers import SECRET, assert_preserved, fingerprint, invoke
from tests.test_workspace_lock_paths import directory_link


@pytest.fixture(scope="module")
def templates(tmp_path_factory):
    root = tmp_path_factory.mktemp("recovery-cli-synthetic-templates")
    minimal = root / "minimal"
    (minimal / "data/raw").mkdir(parents=True)
    (minimal / "config").mkdir()
    (minimal / "config/instruments.json").write_text("[]", encoding="utf-8")
    (minimal / "data/raw/synthetic.txt").write_bytes(b"SYNTHETIC ONLY")
    prepare_recovery(minimal, root / "bundle")

    source = root / "session-source"
    paths = get_paths(source)
    (source / "config").mkdir()
    (source / "config/instruments.json").write_text(
        json.dumps([{"code": "510300", "exchange": "sh", "name": "SYNTHETIC ETF", "kind": "etf"}]),
        encoding="utf-8",
    )
    frame = make_synthetic_daily_prices(periods=120)
    frame["symbol"] = "sh.510300"
    frame["ingested_at"] = "2026-11-06T00:00:00+00:00"
    save_curated(frame, paths)
    create_session(source, root / "session")

    project = root / "activation-source"
    project_paths = get_paths(project)
    shutil.copytree(source / "config", project / "config")
    sample = frame.iloc[:2].copy()
    save_curated(sample, project_paths)
    raw = project_paths.raw / "SYNTHETIC_DEMO_NOT_MARKET_DATA/sh_510300/fixture.parquet"
    raw.parent.mkdir(parents=True)
    sample.to_parquet(raw, index=False)
    with PaperStore(project_paths.database) as store:
        store.ensure_schema()
    material = root / "activation-bundle"
    prepare_recovery(project, material)
    selection = root / "selection.json"
    selection.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle_id": json.loads(
                    (material / "recovery_manifest.json").read_text(encoding="utf-8")
                )["bundle_id"],
                "items": [
                    {
                        "symbol": "sh.510300",
                        "raw_path": raw.relative_to(project).as_posix(),
                        "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report = rebuild_candidate(material, selection, root / "candidate")
    activate_candidate(
        material, root / "candidate", root / "accepted", confirm_candidate_id=report["candidate_id"]
    )
    before = fingerprint(root)
    yield root
    assert fingerprint(root) == before, "Template sources must stay immutable during cases"


CASES = (
    "verify_receipt_io",
    "session_wrong_id",
    "session_root_junction",
    "session_busy",
    "session_lock_junction",
    "activate_wrong_id",
    "activate_receipt_io",
    "acceptance_lock_junction",
)


@pytest.mark.parametrize("case", CASES)
def test_recovery_cli_failure_matrix(tmp_path: Path, templates: Path, case: str):
    evidence = Path(os.environ.get("FINANCE_LAB_TEST_EVIDENCE", str(tmp_path / "evidence"))) / case
    evidence.mkdir(parents=True)
    fixture = tmp_path / "fixture"
    fixture.mkdir()

    def copy(name, destination=None, *, skip_market_lock=False):
        target = fixture / (destination or name)
        shutil.copytree(
            templates / name,
            target,
            ignore=shutil.ignore_patterns("market_data.lock") if skip_market_lock else None,
        )
        return target

    current = copy("session-source", "current")
    fault = None
    held_lock = nullcontext()
    protected_names = None
    if case == "verify_receipt_io":
        bundle = copy("bundle")
        argv = ["data-recovery-verify", "--bundle", str(bundle)]
        healthy = invoke(current, evidence, "healthy", argv)
        fault = bundle / "recovery_check.json"
    elif case.startswith("session_"):
        if case in {"session_wrong_id", "session_root_junction"}:
            healthy = invoke(
                current,
                evidence,
                "healthy",
                ["research-session-create", "--destination", str(fixture / "healthy-session")],
            )
            argv = ["research-session-create", "--destination", str(fixture / "rejected-session")]
            if case == "session_wrong_id":
                argv += ["--expected-dataset-id", "0" * 64]
            else:
                alias = fixture / "source-alias"
                directory_link(alias, current)
                current = alias
        else:
            session = copy("session", skip_market_lock=case == "session_lock_junction")
            healthy_session = copy("session", "healthy-session")
            healthy = invoke(
                current,
                evidence,
                "healthy",
                ["research-session-verify", "--session", str(healthy_session)],
            )
            argv = ["research-session-verify", "--session", str(session)]
            if case == "session_busy":
                held_lock = market_data_lock(session / "snapshot/data")
            else:
                external = fixture / "external"
                external.mkdir()
                (external / "sentinel").write_bytes(b"PRESERVE EXTERNAL SYNTHETIC DATA")
                directory_link(session / "snapshot/data/market_data.lock", external)
    elif case.startswith("activate_"):
        bundle, candidate = copy("activation-bundle"), copy("candidate")
        identity = json.loads((candidate / "candidate_report.json").read_text(encoding="utf-8"))[
            "candidate_id"
        ]
        prefix = ["data-recovery-activate", "--bundle", str(bundle), "--candidate", str(candidate)]
        healthy = invoke(
            current,
            evidence,
            "healthy",
            [
                *prefix,
                "--destination",
                str(fixture / "healthy-accepted"),
                "--confirm-candidate-id",
                identity,
            ],
        )
        target = fixture / "attempted-accepted"
        argv = [
            *prefix,
            "--destination",
            str(target),
            "--confirm-candidate-id",
            "0" * 32 if case == "activate_wrong_id" else identity,
        ]
        if case == "activate_receipt_io":
            fault = target / "recovery_acceptance.json"
            protected_names = {p.name for p in fixture.iterdir()}
    else:
        accepted = copy("accepted", skip_market_lock=True)
        healthy_accepted = copy("accepted", "healthy-accepted")
        healthy = invoke(
            current,
            evidence,
            "healthy",
            ["data-recovery-acceptance-verify", "--root", str(healthy_accepted)],
        )
        external = fixture / "external"
        external.mkdir()
        (external / "sentinel").write_bytes(b"PRESERVE EXTERNAL SYNTHETIC DATA")
        directory_link(accepted / "data/market_data.lock", external)
        argv = ["data-recovery-acceptance-verify", "--root", str(accepted)]

    assert healthy["exit_code"] == 0, healthy
    assert json.loads(healthy["stdout"])["ok"] is True
    before = fingerprint(fixture)
    with held_lock:
        failure = invoke(current, evidence, "failure", argv, fault=fault)
    after = fingerprint(fixture)
    (evidence / "preservation.json").write_text(
        json.dumps(
            {
                "before": before,
                "after": after,
                "protected_names": sorted(protected_names) if protected_names else None,
                "lock_mtime_allowed": case in {"session_wrong_id", "session_busy"},
                "fixture_root": str(fixture),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if protected_names:
        assert {p.name for p in fixture.iterdir()} == protected_names | {"attempted-accepted"}
        preserved = {
            name: value for name, value in after.items() if name.split("/")[0] in protected_names
        }
        assert_preserved(before, preserved)
        assert (target / "data/finance_lab.duckdb").is_file()
        assert (target / "data/curated/sh_510300.parquet").is_file()
        assert not (target / "recovery_acceptance.json").exists()
        marker = json.loads((target / "data/market_update_state.json").read_text(encoding="utf-8"))
        assert marker["status"] == "failed"
    else:
        assert_preserved(
            before, after, allow_lock_time=case in {"session_wrong_id", "session_busy"}
        )
    assert failure["exit_code"] == 1
    assert failure["stderr"] == ""
    payload = json.loads(failure["stdout"])
    assert payload["ok"] is False
    assert SECRET not in failure["stdout"] + failure["stderr"]
    assert payload.get("data_committed") is not False  # No invented blanket rollback assertion.
    assert payload["message"]
    if case == "verify_receipt_io":
        assert payload["ready_for_use"] is False
        assert payload["stage"] == "verification"
        assert payload["error_type"] == "RecoveryError"
        assert len(failure["trace"]["replace_failures"]) == 2
    else:
        assert payload["error_code"] == "workspace_acceptance_failed"
    events = failure["trace"]["events"]
    if case == "session_wrong_id":
        assert payload["error_type"] == "SessionError"
        assert any(event.get("dataset_id") for event in events)
        assert not (fixture / "rejected-session").exists()
    if case == "activate_wrong_id":
        assert payload["error_type"] == "ActivationError"
        assert any(event.get("candidate_id") == identity for event in events)
        assert not target.exists()
    if case.endswith("junction"):
        assert payload["error_type"] == "RecoveryError"
    if case == "activate_receipt_io":
        assert len(failure["trace"]["replace_failures"]) == 1
        assert payload["error_type"] == "OSError"
    if case == "session_busy":
        assert payload["error_type"] == "MarketDataLockError"
        released = invoke(current, evidence, "released", argv)
        assert released["exit_code"] == 0
