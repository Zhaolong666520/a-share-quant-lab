"""Auditable one-shot automation for the forward paper portfolio."""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from finance_lab.config import get_paths
from finance_lab.paper_models import PaperOperationResult, validate_portfolio_id
from finance_lab.paper_pipeline import PaperPortfolioNotFound

PAPER_AUTOMATION_SCHEMA_VERSION = 1

UpdateAction = Callable[..., dict[str, Any]]
HealthAction = Callable[..., tuple[Path, dict[str, object]]]
PaperAction = Callable[..., PaperOperationResult]


@dataclass(frozen=True)
class PaperAutomationActions:
    update_market_data: UpdateAction
    data_health: HealthAction
    run_portfolio: PaperAction
    init_portfolio: PaperAction


@dataclass(frozen=True)
class PaperAutomationResult:
    payload: dict[str, object]
    archive_path: Path
    latest_path: Path

    @property
    def succeeded(self) -> bool:
        return self.payload.get("status") == "success"

    @property
    def exit_code(self) -> int:
        return 0 if self.succeeded else 1


def run_paper_daily_automation(
    *,
    portfolio_id: str = "default",
    start_date: date = date(2018, 1, 1),
    as_of_date: date | None = None,
    stale_after_business_days: int = 3,
    root: Path | None = None,
    actions: PaperAutomationActions | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PaperAutomationResult:
    """Run the daily research workflow and persist success or failure evidence."""
    safe_portfolio = validate_portfolio_id(portfolio_id)
    effective_as_of = as_of_date or date.today()
    _validate_invocation(start_date, effective_as_of, stale_after_business_days)
    paths = get_paths(root)
    selected_actions = actions or _default_actions()
    selected_clock = clock or (lambda: datetime.now(UTC))
    started_at = _aware_utc(selected_clock(), "started_at")

    steps: list[dict[str, str]] = []
    current_step = "market_data_update"
    data_health_status: str | None = None
    data_health_report_path: str | None = None
    successful_instruments: int | None = None
    operation: PaperOperationResult | None = None
    error: dict[str, str] | None = None
    try:
        update_payload = selected_actions.update_market_data(
            start=start_date,
            end=effective_as_of,
            root=root,
        )
        successful_instruments = _successful_instruments(update_payload)
        if successful_instruments == 0:
            raise RuntimeError("行情更新没有任何成功标的")
        steps.append({"name": current_step, "status": "succeeded"})

        current_step = "data_health"
        health_path, health_payload = selected_actions.data_health(
            root=root,
            as_of_date=effective_as_of,
            stale_after_business_days=stale_after_business_days,
        )
        data_health_report_path = str(health_path)
        raw_health_status = health_payload.get("health_status")
        if raw_health_status not in {"pass", "warning", "error"}:
            raise RuntimeError("数据健康状态无效")
        data_health_status = str(raw_health_status)
        if data_health_status == "error":
            raise RuntimeError("数据健康检查为 error，拒绝运行模拟盘")
        steps.append({"name": current_step, "status": "succeeded"})

        current_step = "paper_run"
        try:
            operation = selected_actions.run_portfolio(
                portfolio_id=safe_portfolio,
                as_of_date=effective_as_of,
                stale_after_business_days=stale_after_business_days,
                root=root,
            )
            _validate_operation(operation, safe_portfolio)
            steps.append({"name": current_step, "status": "succeeded"})
        except PaperPortfolioNotFound:
            steps.append({"name": current_step, "status": "portfolio_missing"})
            current_step = "paper_init"
            operation = selected_actions.init_portfolio(
                portfolio_id=safe_portfolio,
                as_of_date=effective_as_of,
                stale_after_business_days=stale_after_business_days,
                root=root,
            )
            _validate_operation(operation, safe_portfolio)
            steps.append({"name": current_step, "status": "succeeded"})
    except Exception as exc:  # The failure must be persisted before returning nonzero.
        if not steps or steps[-1].get("name") != current_step:
            steps.append({"name": current_step, "status": "failed"})
        elif steps[-1].get("status") != "failed":
            steps.append({"name": current_step, "status": "failed"})
        error = {
            "type": type(exc).__name__,
            "message": _single_line_message(exc),
        }

    completed_at = _aware_utc(selected_clock(), "completed_at")
    if completed_at < started_at:
        raise ValueError("completed_at 不能早于 started_at")
    status = "success" if error is None else "failed"
    payload: dict[str, object] = {
        "schema_version": PAPER_AUTOMATION_SCHEMA_VERSION,
        "workflow": "paper_daily",
        "status": status,
        "portfolio_id": safe_portfolio,
        "start_date": start_date.isoformat(),
        "as_of_date": effective_as_of.isoformat(),
        "stale_after_business_days": stale_after_business_days,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "steps": steps,
        "failed_step": current_step if error is not None else None,
        "error": error,
        "successful_instruments": successful_instruments,
        "data_health_status": data_health_status,
        "data_health_report_path": data_health_report_path,
        "operation_status": operation.status if operation is not None else None,
        "processed_dates": (
            [item.isoformat() for item in operation.processed_dates]
            if operation is not None
            else []
        ),
        "paper_report_path": (
            str(operation.report_path)
            if operation is not None and operation.report_path is not None
            else None
        ),
        "broker_connected": False,
        "authorizes_real_money": False,
    }
    run_id = _run_id(payload)
    payload["run_id"] = run_id
    archive_path, latest_path = _status_paths(
        paths.outputs,
        safe_portfolio,
        started_at,
        run_id,
    )
    _write_immutable_json(archive_path, payload)
    _write_json_atomic(latest_path, payload)
    return PaperAutomationResult(
        payload=payload,
        archive_path=archive_path,
        latest_path=latest_path,
    )


def _default_actions() -> PaperAutomationActions:
    from finance_lab.paper_pipeline import paper_init_portfolio, paper_run_portfolio
    from finance_lab.pipeline import data_health, update_market_data

    return PaperAutomationActions(
        update_market_data=update_market_data,
        data_health=data_health,
        run_portfolio=paper_run_portfolio,
        init_portfolio=paper_init_portfolio,
    )


def _validate_invocation(start: date, end: date, stale_days: int) -> None:
    if type(start) is not date or type(end) is not date or start >= end:
        raise ValueError("自动运行的开始日期必须早于检查日期")
    if type(stale_days) is not int or stale_days < 0:
        raise ValueError("允许陈旧交易日数必须是非负整数")


def _successful_instruments(payload: dict[str, Any]) -> int:
    value = payload.get("successful_instruments")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError("行情更新缺少有效的成功标的数")
    number = float(value)
    if not math.isfinite(number) or not number.is_integer() or number < 0:
        raise RuntimeError("行情更新缺少有效的成功标的数")
    return int(number)


def _validate_operation(operation: object, portfolio_id: str) -> None:
    if not isinstance(operation, PaperOperationResult):
        raise TypeError("模拟盘步骤返回了无效结果")
    if operation.portfolio_id != portfolio_id:
        raise ValueError("模拟盘步骤返回了错误的账户组")


def _aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} 必须是带时区的时间")
    return value.astimezone(UTC)


def _single_line_message(exc: Exception) -> str:
    message = " ".join(str(exc).splitlines()).strip()
    return (message or type(exc).__name__)[:2000]


def _run_id(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]


def _status_paths(
    outputs: Path,
    portfolio_id: str,
    started_at: datetime,
    run_id: str,
) -> tuple[Path, Path]:
    output_root = outputs.resolve()
    archive_root = (output_root / "paper_automation_runs").resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    archive = (archive_root / f"{portfolio_id}_{stamp}_{run_id}.json").resolve()
    latest = (output_root / f"{portfolio_id}_paper_automation_latest.json").resolve()
    if not archive.is_relative_to(output_root) or not latest.is_relative_to(output_root):
        raise ValueError("自动运行状态路径越过 outputs 目录")
    return archive, latest


def _write_immutable_json(path: Path, payload: dict[str, object]) -> None:
    serialized = _serialized_payload(payload)
    if path.exists():
        if path.read_text(encoding="utf-8") != serialized:
            raise RuntimeError("自动运行归档已存在且内容不同，拒绝覆盖")
        return
    _write_text_atomic(path, serialized)


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    _write_text_atomic(path, _serialized_payload(payload))


def _serialized_payload(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
