"""Auditable one-shot automation for the forward paper portfolio."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from finance_lab.config import get_paths
from finance_lab.paper_models import PaperOperationResult, validate_portfolio_id
from finance_lab.paper_pipeline import PaperPortfolioNotFound, PaperReportWriteError

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


class PaperAutomationStatusWriteError(RuntimeError):
    """The workflow ran, but its final status could not be fully persisted."""

    def __init__(
        self,
        *,
        portfolio_id: str,
        stage: str,
        market_data_committed: bool | None,
        ledger_committed: bool | None,
        processed_dates: list[str],
        archive_confirmed: bool,
        archive_path: Path | None,
    ) -> None:
        self.portfolio_id = portfolio_id
        self.stage = stage
        self.market_data_committed = market_data_committed
        self.ledger_committed = ledger_committed
        self.processed_dates = tuple(processed_dates)
        self.archive_confirmed = archive_confirmed
        self.archive_path = archive_path if archive_confirmed else None
        super().__init__("每日模拟盘状态未完整写入；请先核对账本和状态文件")


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
    failed_sources: int | None = None
    market_data_committed: bool | None = None
    operation: PaperOperationResult | None = None
    report_error: PaperReportWriteError | None = None
    error: dict[str, str] | None = None
    try:
        update_payload = selected_actions.update_market_data(
            start=start_date,
            end=effective_as_of,
            root=root,
        )
        successful_instruments = _successful_instruments(update_payload)
        update_status = update_payload.get("status")
        if update_status in {"success", "partial_sources", "failed"}:
            market_data_committed = successful_instruments > 0
        failed_sources = _failed_sources(update_payload)
        if successful_instruments == 0:
            raise RuntimeError("行情更新没有任何成功标的")
        if update_status != "success" or failed_sources != 0:
            raise RuntimeError("行情更新未明确确认全部来源成功；本次不推进模拟盘")
        steps.append({"name": current_step, "status": "succeeded"})

        current_step = "data_health"
        health_path, health_payload = selected_actions.data_health(
            root=root,
            as_of_date=effective_as_of,
            stale_after_business_days=stale_after_business_days,
        )
        if not isinstance(health_path, Path):
            raise ValueError("数据健康步骤未返回报告路径")
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
            candidate = selected_actions.run_portfolio(
                portfolio_id=safe_portfolio,
                as_of_date=effective_as_of,
                stale_after_business_days=stale_after_business_days,
                root=root,
            )
            _validate_operation(candidate, safe_portfolio, "paper_run", effective_as_of)
            operation = candidate
            steps.append({"name": current_step, "status": "succeeded"})
        except PaperPortfolioNotFound:
            steps.append({"name": current_step, "status": "portfolio_missing"})
            current_step = "paper_init"
            candidate = selected_actions.init_portfolio(
                portfolio_id=safe_portfolio,
                as_of_date=effective_as_of,
                stale_after_business_days=stale_after_business_days,
                root=root,
            )
            _validate_operation(candidate, safe_portfolio, "paper_init", effective_as_of)
            operation = candidate
            steps.append({"name": current_step, "status": "succeeded"})
    except Exception as exc:  # The failure must be persisted before returning nonzero.
        if isinstance(exc, PaperReportWriteError):
            report_error = exc
            steps.append({"name": current_step, "status": "succeeded"})
            current_step = "paper_report"
        if not steps or steps[-1].get("name") != current_step:
            steps.append({"name": current_step, "status": "failed"})
        elif steps[-1].get("status") != "failed":
            steps.append({"name": current_step, "status": "failed"})
        error = {
            "type": type(exc).__name__,
            "code": f"{current_step}_failed",
            "message": _failure_message(current_step),
        }

    completed_at = _aware_utc(selected_clock(), "completed_at")
    if completed_at < started_at:
        raise ValueError("completed_at 不能早于 started_at")
    status = "success" if error is None else "failed"
    if operation is not None:
        operation_status = operation.status
        processed_dates = [item.isoformat() for item in operation.processed_dates]
        ledger_committed: bool | None = operation.status in {"initialized", "processed"} and bool(
            operation.processed_dates
        )
    elif report_error is not None:
        operation_status = report_error.operation_status
        processed_dates = [item.isoformat() for item in report_error.processed_dates]
        ledger_committed = report_error.ledger_committed
    else:
        operation_status = None
        processed_dates = []
        ledger_committed = None
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
        "failed_sources": failed_sources,
        "market_data_committed": market_data_committed,
        "data_health_status": data_health_status,
        "data_health_report_path": data_health_report_path,
        "operation_status": operation_status,
        "processed_dates": processed_dates,
        "ledger_committed": ledger_committed,
        "next_action": _next_action(current_step) if error is not None else None,
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
    status_stage = "prepare_status_paths"
    archive_path: Path | None = None
    archive_confirmed = False
    try:
        archive_path, latest_path = _status_paths(
            paths.outputs,
            safe_portfolio,
            started_at,
            run_id,
        )
        status_stage = "write_status_archive"
        _write_immutable_json(archive_path, payload)
        archive_confirmed = True
        status_stage = "write_latest_status"
        _write_json_atomic(latest_path, payload)
    except Exception as exc:
        raise PaperAutomationStatusWriteError(
            portfolio_id=safe_portfolio,
            stage=status_stage,
            market_data_committed=(
                market_data_committed
            ),
            ledger_committed=ledger_committed,
            processed_dates=processed_dates,
            archive_confirmed=archive_confirmed,
            archive_path=archive_path,
        ) from exc
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
    if type(value) is not int or value < 0:
        raise RuntimeError("行情更新缺少有效的成功标的数")
    return value


def _failed_sources(payload: dict[str, Any]) -> int:
    value = payload.get("failed_sources")
    if type(value) is not int or value < 0:
        raise RuntimeError("行情更新缺少有效的失败来源数")
    return value


def _validate_operation(
    operation: object,
    portfolio_id: str,
    step: str,
    as_of_date: date,
) -> None:
    if not isinstance(operation, PaperOperationResult):
        raise TypeError("模拟盘步骤返回了无效结果")
    if operation.portfolio_id != portfolio_id:
        raise ValueError("模拟盘步骤返回了错误的账户组")
    dates = operation.processed_dates
    if (
        type(dates) is not tuple
        or any(type(item) is not date or item > as_of_date for item in dates)
        or any(left >= right for left, right in zip(dates, dates[1:], strict=False))
    ):
        raise ValueError("模拟盘步骤返回了无效处理日期")
    if step == "paper_run":
        valid = (operation.status == "processed" and bool(dates)) or (
            operation.status == "no-op" and not dates
        )
    else:
        valid = step == "paper_init" and operation.status == "initialized" and len(dates) == 1
    if not valid:
        raise ValueError("模拟盘步骤返回了不一致的状态与处理日期")
    if not isinstance(operation.report_path, Path):
        raise ValueError("模拟盘步骤未返回已生成报告的路径")


def _aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} 必须是带时区的时间")
    return value.astimezone(UTC)


def _failure_message(step: str) -> str:
    messages = {
        "market_data_update": "行情更新未完成或数据源未全部成功；不能推进模拟盘。",
        "data_health": "数据健康检查未通过；不能推进模拟盘。",
        "paper_run": "模拟盘运行失败；不能假定账本未发生变化。",
        "paper_init": "模拟盘初始化失败；不能假定账本未发生变化。",
        "paper_report": "模拟盘报告未完整发布；账本可能已提交。",
    }
    return messages.get(step, "每日模拟盘步骤失败；请核对状态与账本。")


def _next_action(step: str) -> str:
    actions = {
        "market_data_update": (
            "保留行情与批次状态，先运行 data-update-status 核对；确认后再决定是否重试每日任务。"
        ),
        "data_health": "先核对数据健康报告和行情批次状态，确认数据可用后再运行模拟盘。",
        "paper_report": (
            "修复报告写入问题后运行 paper-status 审计并重建报告；不要重置或重复初始化账本。"
        ),
    }
    return actions.get(step, "先运行 paper-status 审计模拟账本；确认是否已有事件提交后再重试。")


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
