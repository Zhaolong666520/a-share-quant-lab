from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import finance_lab.paper_process_evidence as evidence
from finance_lab.paper_automation import PaperAutomationActions, run_paper_daily_automation


def _blocked(*args, **kwargs):
    raise AssertionError("Business action must not run")


def test_started_receipt_exists_before_first_business_action(tmp_path: Path) -> None:
    def update(**kwargs):
        records = list(
            (tmp_path / "outputs/paper_process_runs/default").glob("*/worker-000-started.json")
        )
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding="utf-8"))
        assert record["origin"] == "direct"
        assert record["worker"]["pid"] == os.getpid()
        assert record["worker"]["parent_pid"] == os.getppid()
        raise RuntimeError("Synthetic source error")

    result = run_paper_daily_automation(
        root=tmp_path,
        as_of_date=date(2026, 11, 4),
        actions=PaperAutomationActions(update, _blocked, _blocked, _blocked),
    )
    assert result.exit_code == 1
    assert result.payload["schema_version"] == 2
    assert "launch_id" not in result.payload
    directory = next((tmp_path / "outputs/paper_process_runs/default").iterdir())
    receipt = json.loads((directory / "worker-002-archived.json").read_text(encoding="utf-8"))
    assert receipt["attempt_id"] == result.payload["attempt_id"]
    assert receipt["archive_sha256"] == evidence._sha(result.archive_path)


def test_started_write_failure_prevents_every_business_action(tmp_path: Path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise evidence.ProcessEvidenceWriteError("a" * 32, "b" * 32, "started")

    monkeypatch.setattr(evidence.ProcessJournal, "record", fail)
    with pytest.raises(evidence.ProcessEvidenceWriteError):
        run_paper_daily_automation(
            root=tmp_path,
            as_of_date=date(2026, 11, 4),
            actions=PaperAutomationActions(_blocked, _blocked, _blocked, _blocked),
        )


def test_receipt_failure_is_not_recorded_as_a_market_failure(tmp_path: Path, monkeypatch) -> None:
    original = evidence.ProcessJournal.record

    def fail_phase(self, kind, **detail):
        if kind == "phase":
            raise evidence.ProcessEvidenceWriteError(self.launch_id, self.attempt_id, kind)
        return original(self, kind, **detail)

    monkeypatch.setattr(evidence.ProcessJournal, "record", fail_phase)
    with pytest.raises(evidence.ProcessEvidenceWriteError):
        run_paper_daily_automation(
            root=tmp_path,
            as_of_date=date(2026, 11, 4),
            actions=PaperAutomationActions(_blocked, _blocked, _blocked, _blocked),
        )
    assert not (tmp_path / "outputs/paper_automation_runs").exists()


def test_wrapped_call_requires_a_matching_live_launcher(tmp_path: Path, monkeypatch) -> None:
    launch_id = "a" * 32
    directory = tmp_path / "outputs/paper_process_runs/default" / launch_id
    directory.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "kind": "launcher_started",
        "launch_id": launch_id,
        "portfolio_id": "default",
        "as_of_date": "2026-11-04",
        "root": str(tmp_path),
        "wrapper": {"pid": 100, "creation_filetime": "123"},
    }
    (directory / "launcher.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(evidence, "observe_identity", lambda _: "exited")
    with pytest.raises(evidence.ProcessEvidenceWriteError):
        evidence.ProcessJournal(tmp_path, "default", date(2026, 11, 4), "b" * 32, launch_id)
    assert not (directory / "worker-000-started.json").exists()
    monkeypatch.setattr(evidence, "observe_identity", lambda _: "running")
    identity = {"pid": os.getpid(), "creation_filetime": "123", "state": "running"}
    monkeypatch.setattr(evidence, "process_identity", lambda _: identity.copy())
    (directory / "child.json").write_text(
        json.dumps({**payload, "kind": "child_started", "child": identity})
    )
    evidence.ProcessJournal(tmp_path, "default", date(2026, 11, 4), "b" * 32, launch_id)
    with pytest.raises(evidence.ProcessEvidenceWriteError):
        evidence.ProcessJournal(tmp_path, "default", date(2026, 11, 4), "c" * 32, launch_id)
    assert json.loads((directory / "worker-000-started.json").read_text())["attempt_id"] == "b" * 32


def test_pid_reuse_is_not_a_running_original_process(monkeypatch) -> None:
    monkeypatch.setattr(
        evidence,
        "process_identity",
        lambda pid: {"pid": pid, "creation_filetime": "456", "state": "running"},
    )
    assert (
        evidence.observe_identity({"pid": 123, "creation_filetime": "123"}) == "exited_pid_reused"
    )
    assert evidence.observe_identity({"pid": 123, "creation_filetime": None}) == "unknown"


@pytest.mark.skipif(os.name != "nt", reason="Windows process identity")
def test_actual_self_process_identity() -> None:
    current = evidence.process_identity(os.getpid())
    assert current["state"] == "running"
    assert int(current["creation_filetime"]) > 0
    assert Path(current["image_path"]).name.lower() == "python.exe"
    assert evidence.observe_identity(current) == "running"


def test_deeply_nested_bounded_json_stays_unknown(tmp_path):
    from finance_lab.paper_process_status import inspect_process_evidence

    directory = tmp_path / "outputs/paper_process_runs/default" / ("f" * 32)
    directory.mkdir(parents=True)
    (directory / "launcher.json").write_text("[" * 2000 + "0" + "]" * 2000)
    assert inspect_process_evidence(tmp_path, "default")["state"] == "unknown"


def test_archived_hash_failure_keeps_original_archive(tmp_path, monkeypatch) -> None:
    journal = evidence.ProcessJournal(tmp_path, "default", date(2026, 11, 4), "b" * 32)
    archive = tmp_path / "outputs/business-archive.json"
    archive.write_bytes(b'{"business_evidence":"retained"}')
    original = archive.read_bytes()

    def fail(path):
        raise OSError("synthetic disk read error")

    monkeypatch.setattr(evidence, "_sha", fail)
    with pytest.raises(evidence.ProcessEvidenceWriteError) as caught:
        journal.archived(archive)
    assert caught.value.stage == "archived"
    assert archive.read_bytes() == original


@pytest.mark.skipif(os.name != "nt", reason="Windows editable-import junction")
def test_imported_code_identity_accepts_resolved_junction_but_data_does_not(tmp_path, monkeypatch):
    from tests.test_paper_runtime_windows_junction import junction

    actual = Path(evidence.__file__).resolve()
    alias = tmp_path / "retained-code-alias"
    junction(alias, actual.parent)
    monkeypatch.setattr(evidence, "__file__", str(alias / actual.name))
    journal = evidence.ProcessJournal(
        tmp_path / "ordinary-root", "default", date(2026, 11, 4), "c" * 32
    )
    record = json.loads((journal.directory / "worker-000-started.json").read_text(encoding="utf-8"))
    assert record["module_original_path"] == str(alias / actual.name)
    assert record["module_path"] == str(actual)
    assert record["module_sha256"] == evidence._sha(actual)
    with pytest.raises(ValueError, match="reparse"):
        evidence.ProcessJournal(alias, "default", date(2026, 11, 4), "d" * 32)


def test_read_only_diagnosis_keeps_permission_unknown_and_rejects_corruption(tmp_path, monkeypatch):
    import finance_lab.paper_process_status as status

    journal = evidence.ProcessJournal(tmp_path, "default", date(2026, 11, 4), "e" * 32)
    journal.phase("market_data_update")
    monkeypatch.setattr(status, "observe_identity", lambda _: "unknown")
    assert status.inspect_process_evidence(tmp_path, "default")["state"] == "unknown"
    monkeypatch.setattr(status, "observe_identity", lambda _: "exited_pid_reused")
    assert status.inspect_process_evidence(tmp_path, "default")["state"] == "interrupted"
    (journal.directory / "worker-001-phase.json").write_text("{broken", encoding="utf-8")
    assert status.inspect_process_evidence(tmp_path, "default")["state"] == "unknown"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("none", "completed"),
        ("missing-child-exit", "unknown"),
        ("conflicting-codes", "unknown"),
        ("nonzero-after-success", "failed"),
        ("reversed-time", "unknown"),
        ("growth-during-read", "unknown"),
    ],
)
def test_success_archive_cannot_override_incomplete_or_conflicting_process_evidence(
    tmp_path, monkeypatch, mutation, expected
):
    import finance_lab.paper_process_status as status
    from finance_lab.paper_models import PaperOperationResult

    def update(**kw):
        return {"successful_instruments": 1, "failed_sources": 0, "status": "success"}

    def health(**kw):
        return tmp_path / "health.html", {"health_status": "warning"}

    def paper(**kw):
        return PaperOperationResult("no-op", "default", (), (), None, tmp_path / "report.html")

    run_paper_daily_automation(
        root=tmp_path,
        as_of_date=date(2026, 11, 4),
        actions=PaperAutomationActions(update, health, paper, paper),
    )
    directory = next((tmp_path / "outputs/paper_process_runs/default").iterdir())
    start_path = directory / "worker-000-started.json"
    start = json.loads(start_path.read_text(encoding="utf-8"))
    start["origin"] = "wrapper"
    start_path.write_text(json.dumps(start), encoding="utf-8")
    stamp = datetime.fromisoformat(start["recorded_at"])
    base = {
        key: start[key]
        for key in ("schema_version", "launch_id", "portfolio_id", "as_of_date", "root")
    }

    def write(name, kind, seconds, **details):
        (directory / name).write_text(
            json.dumps(
                {
                    **base,
                    "kind": kind,
                    "recorded_at": (stamp + timedelta(seconds=seconds)).isoformat(),
                    **details,
                }
            ),
            encoding="utf-8",
        )

    write("launcher.json", "launcher_started", -2, wrapper=start["worker"])
    write("child.json", "child_started", -1, child=start["worker"])
    code = 23 if mutation in {"conflicting-codes", "nonzero-after-success"} else 0
    if mutation != "missing-child-exit":
        write(
            "child-exit.json",
            "child_exited",
            -3 if mutation == "reversed-time" else 10,
            child=start["worker"],
            exit_code=code,
            exit_code_uint32=code,
            exit_code_hex=f"0x{code:08X}",
        )
    wrapper_code = 23 if mutation == "nonzero-after-success" else 0
    write(
        "wrapper-exit.json",
        "wrapper_exited",
        11,
        exit_code=wrapper_code,
        child_exit_code=wrapper_code,
        status="failed" if wrapper_code else "succeeded",
    )
    monkeypatch.setattr(status, "observe_identity", lambda _: "exited")
    if mutation == "growth-during-read":
        original = status._bounded
        calls = 0

        def grow(path, limit):
            nonlocal calls
            if path.name == "wrapper-exit.json":
                calls += 1
                if calls == 2:
                    with path.open("ab") as stream:
                        stream.write(b" " * 65537)
            return original(path, limit)

        monkeypatch.setattr(status, "_bounded", grow)
    outcome = status.inspect_process_evidence(tmp_path, "default", now=stamp + timedelta(minutes=1))
    assert outcome["state"] == expected, outcome
