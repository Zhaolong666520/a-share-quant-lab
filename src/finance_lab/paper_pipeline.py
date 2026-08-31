"""Market-data gate and orchestration for forward paper trading."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from finance_lab.config import ProjectPaths, load_instruments
from finance_lab.data_manifest import (
    DatasetFileManifest,
    file_sha256,
    generate_dataset_manifest,
    read_update_summary,
)
from finance_lab.ledger import LedgerConfig
from finance_lab.paper_engine import advance_one_bar, initialize_account
from finance_lab.paper_lock import paper_run_lock
from finance_lab.paper_models import (
    PaperDataContext,
    PaperOperationResult,
    make_default_accounts,
)
from finance_lab.paper_store import PaperStore
from finance_lab.validation import DataValidationError, assert_valid_daily_prices


class PaperDataGateError(RuntimeError):
    """Raised when local market-data lineage is unsafe for paper processing."""


class PaperPortfolioNotFound(RuntimeError):
    """Raised when a requested local paper portfolio has not been initialized."""

    exit_code = 4


@dataclass(frozen=True)
class PaperMarketSnapshot:
    prices: pd.DataFrame
    data_context: PaperDataContext


def load_paper_market_snapshot(
    paths: ProjectPaths,
    as_of_date: date,
    stale_after_business_days: int,
) -> PaperMarketSnapshot:
    """Load the one approved ETF only when its complete local lineage is healthy."""
    target_symbol = "sh.510300"
    configured = next(
        (item for item in load_instruments(paths.root) if item.symbol == target_symbol),
        None,
    )
    if configured is None or configured.kind != "etf":
        raise PaperDataGateError("config/instruments.json 必须把 sh.510300 配置为 etf")

    manifest = generate_dataset_manifest(
        paths,
        as_of_date=as_of_date,
        stale_after_business_days=stale_after_business_days,
    )
    item = next((entry for entry in manifest.files if entry.symbol == target_symbol), None)
    if item is None:
        raise PaperDataGateError("数据清单中没有 sh.510300")
    _validate_manifest_item(item)

    summary = read_update_summary(paths)
    if summary.parse_error is not None:
        raise PaperDataGateError(f"update_summary.json 无法解析：{summary.parse_error}")
    if summary.end != as_of_date.isoformat():
        raise PaperDataGateError("update_summary.json 的 end 必须等于本次检查日期")
    records = [record for record in summary.records if record.symbol == target_symbol]
    if len(records) != 1:
        raise PaperDataGateError("update_summary.json 必须恰好包含一条 sh.510300 记录")
    update_record = records[0]
    if update_record.rows <= 0:
        raise PaperDataGateError("sh.510300 最近更新没有有效数据行")
    if update_record.curated_file is None or not update_record.curated_file.strip():
        raise PaperDataGateError("sh.510300 最近更新缺少整理数据路径")

    curated_path = _validated_curated_path(paths, update_record.curated_file)
    manifest_path = (paths.root / item.relative_path).resolve()
    if curated_path != manifest_path:
        raise PaperDataGateError("更新摘要中的整理数据路径与清单不一致")
    before_hash = file_sha256(curated_path)
    if before_hash != item.sha256:
        raise PaperDataGateError("整理数据文件与清单哈希不一致，请重新生成清单")
    try:
        prices = pd.read_parquet(curated_path)
        prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce")
        assert_valid_daily_prices(prices)
    except (OSError, KeyError, DataValidationError, ValueError) as exc:
        raise PaperDataGateError(f"无法读取或验证 sh.510300 整理数据：{exc}") from exc
    after_hash = file_sha256(curated_path)
    if after_hash != before_hash or after_hash != item.sha256:
        raise PaperDataGateError("整理数据文件在读取期间发生变化，请重试")
    if prices.empty or set(prices["symbol"].astype(str)) != {target_symbol}:
        raise PaperDataGateError("整理数据必须只包含 sh.510300")
    if item.start_date is None or item.end_date is None:
        raise PaperDataGateError("清单缺少 sh.510300 的有效日期范围")

    context = PaperDataContext(
        dataset_id=manifest.dataset_id,
        curated_sha256=item.sha256,
        manifest_generated_at=manifest.generated_at,
        as_of_date=manifest.as_of_date,
        data_start_date=item.start_date,
        data_end_date=item.end_date,
        sources=item.sources,
        warning_codes=item.warning_codes,
        upstream_errors=update_record.source_errors,
        business_days_stale=item.business_days_stale or 0,
        update_summary_end=summary.end,
    )
    return PaperMarketSnapshot(
        prices=prices.sort_values("trade_date").reset_index(drop=True),
        data_context=context,
    )


def paper_init_portfolio(
    portfolio_id: str = "default",
    ledger_config: LedgerConfig | None = None,
    as_of_date: date | None = None,
    stale_after_business_days: int = 3,
    root: Path | None = None,
) -> PaperOperationResult:
    """Create the fixed account pair at the latest validated local market bar."""
    paths = _project_paths(root)
    effective_as_of_date = as_of_date or date.today()
    with paper_run_lock(paths.data / "paper_trading.lock"):
        snapshot = load_paper_market_snapshot(
            paths,
            effective_as_of_date,
            stale_after_business_days,
        )
        accounts = make_default_accounts(
            portfolio_id,
            snapshot.data_context.data_end_date,
            ledger_config or LedgerConfig(),
        )
        steps = tuple(initialize_account(account, snapshot.prices) for account in accounts)
        with PaperStore(paths.database) as store:
            store.ensure_schema()
            states = store.initialize_portfolio(
                accounts,
                steps,
                snapshot.data_context,
                batch_id=_batch_id("init", portfolio_id, snapshot.data_context.data_end_date),
            )
    return PaperOperationResult(
        status="initialized",
        portfolio_id=portfolio_id,
        processed_dates=(snapshot.data_context.data_end_date,),
        states=states,
        data_context=snapshot.data_context,
    )


def paper_run_portfolio(
    portfolio_id: str = "default",
    as_of_date: date | None = None,
    stale_after_business_days: int = 3,
    root: Path | None = None,
) -> PaperOperationResult:
    """Replay every unseen validated bar in one atomic two-account batch per date."""
    paths = _project_paths(root)
    effective_as_of_date = as_of_date or date.today()
    with paper_run_lock(paths.data / "paper_trading.lock"):
        snapshot = load_paper_market_snapshot(
            paths,
            effective_as_of_date,
            stale_after_business_days,
        )
        with PaperStore(paths.database) as store:
            store.ensure_schema()
            if not store.portfolio_exists(portfolio_id):
                raise PaperPortfolioNotFound(f"账户组 {portfolio_id} 不存在，请先运行 paper-init")
            states = store.audit_portfolio(portfolio_id)
            accounts = store.load_accounts(portfolio_id)
            if len(accounts) != 2 or len(states) != len(accounts):
                raise RuntimeError("模拟账户组必须包含两个经过审计的账户")
            last_dates = {state.last_trade_date for state in states}
            if len(last_dates) != 1:
                raise RuntimeError("两个模拟账户的最后处理日期不一致，拒绝继续")
            last_trade_date = next(iter(last_dates))
            prices = snapshot.prices.copy()
            prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="raise")
            unseen_dates = tuple(
                pd.Timestamp(value).date()
                for value in prices.loc[
                    prices["trade_date"].dt.date > last_trade_date, "trade_date"
                ]
            )
            processed_dates: list[date] = []
            current_states = states
            for trade_date in unseen_dates:
                history = prices.loc[prices["trade_date"].dt.date <= trade_date].copy()
                steps = tuple(
                    advance_one_bar(account, state, history)
                    for account, state in zip(accounts, current_states, strict=True)
                )
                current_states = store.commit_batch(
                    accounts,
                    steps,
                    snapshot.data_context,
                    batch_id=_batch_id("run", portfolio_id, trade_date),
                )
                processed_dates.append(trade_date)
    return PaperOperationResult(
        status="processed" if processed_dates else "no-op",
        portfolio_id=portfolio_id,
        processed_dates=tuple(processed_dates),
        states=current_states,
        data_context=snapshot.data_context,
    )


def paper_status_portfolio(
    portfolio_id: str = "default",
    root: Path | None = None,
) -> PaperOperationResult:
    """Audit current account state without taking the write lock or mutating DuckDB."""
    paths = _project_paths(root)
    if not paths.database.exists():
        raise PaperPortfolioNotFound(f"账户组 {portfolio_id} 不存在，请先运行 paper-init")
    with PaperStore(paths.database, read_only=True) as store:
        if not store.portfolio_exists(portfolio_id):
            raise PaperPortfolioNotFound(f"账户组 {portfolio_id} 不存在，请先运行 paper-init")
        states = store.audit_portfolio(portfolio_id)
    return PaperOperationResult(
        status="status",
        portfolio_id=portfolio_id,
        processed_dates=(),
        states=states,
        data_context=None,
    )


def _validate_manifest_item(item: DatasetFileManifest) -> None:
    if item.error_codes:
        raise PaperDataGateError(f"sh.510300 数据健康检查失败：{', '.join(item.error_codes)}")
    allowed_warnings = {"unadjusted_prices", "mixed_sources"}
    blocked_warnings = set(item.warning_codes).difference(allowed_warnings)
    if blocked_warnings:
        raise PaperDataGateError(
            "sh.510300 存在阻断数据提醒：" + ", ".join(sorted(blocked_warnings))
        )
    if item.business_days_stale is None:
        raise PaperDataGateError("sh.510300 的数据陈旧状态未知")


def _validated_curated_path(paths: ProjectPaths, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise PaperDataGateError("更新摘要中的整理数据路径必须是项目内相对路径")
    resolved = (paths.root / candidate).resolve()
    try:
        resolved.relative_to(paths.root.resolve())
    except ValueError as exc:
        raise PaperDataGateError("更新摘要中的整理数据路径不能离开项目目录") from exc
    if not resolved.is_file():
        raise PaperDataGateError("更新摘要指向的整理数据文件不存在")
    return resolved


def _project_paths(root: Path | None) -> ProjectPaths:
    from finance_lab.config import get_paths

    return get_paths(root)


def _batch_id(operation: str, portfolio_id: str, trade_date: date) -> str:
    return f"paper-{operation}-{portfolio_id}-{trade_date.isoformat()}"
