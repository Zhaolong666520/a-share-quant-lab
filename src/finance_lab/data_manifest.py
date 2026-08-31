from __future__ import annotations

import hashlib
import html
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from finance_lab.config import ProjectPaths
from finance_lab.trading_calendar import load_project_sse_trading_calendar
from finance_lab.validation import validate_daily_prices


@dataclass(frozen=True)
class DatasetFileManifest:
    relative_path: str
    symbol: str
    rows: int
    start_date: date | None
    end_date: date | None
    sha256: str
    schema: tuple[str, ...]
    sources: dict[str, int]
    adjustments: tuple[str, ...]
    volume_units: tuple[str, ...]
    business_days_stale: int | None
    warning_codes: tuple[str, ...]
    error_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
        }


@dataclass(frozen=True)
class DatasetManifest:
    dataset_id: str
    generated_at: str
    as_of_date: date
    stale_after_business_days: int
    freshness_method: str
    freshness_calendar_sha256: str
    freshness_calendar_coverage: str
    freshness_calendar_sources: tuple[str, ...]
    total_rows: int
    health_status: str
    files: tuple[DatasetFileManifest, ...]
    upstream_errors: dict[str, dict[str, str]]
    update_summary_end: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "generated_at": self.generated_at,
            "as_of_date": self.as_of_date.isoformat(),
            "stale_after_business_days": self.stale_after_business_days,
            "freshness_method": self.freshness_method,
            "freshness_calendar_sha256": self.freshness_calendar_sha256,
            "freshness_calendar_coverage": self.freshness_calendar_coverage,
            "freshness_calendar_sources": self.freshness_calendar_sources,
            "total_rows": self.total_rows,
            "health_status": self.health_status,
            "files": [item.to_dict() for item in self.files],
            "upstream_errors": self.upstream_errors,
            "update_summary_end": self.update_summary_end,
        }


@dataclass(frozen=True)
class UpdateRecord:
    symbol: str
    rows: int
    curated_file: str | None
    source_errors: dict[str, str]


@dataclass(frozen=True)
class UpdateSummary:
    end: str | None
    records: tuple[UpdateRecord, ...]
    parse_error: str | None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_update_summary(paths: ProjectPaths) -> UpdateSummary:
    summary_path = paths.outputs / "update_summary.json"
    if not summary_path.exists():
        return UpdateSummary(end=None, records=(), parse_error=None)
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("update_summary 根节点必须是对象")
        records = payload.get("records", [])
        if not isinstance(records, list) or not all(
            isinstance(record, dict) for record in records
        ):
            raise TypeError("update_summary.records 必须是对象数组")
        parsed_records: list[UpdateRecord] = []
        for record in records:
            symbol = record.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                raise TypeError("每个 update_summary 记录都必须有非空 symbol")
            rows = record.get("rows", 0)
            if type(rows) is not int or rows < 0:
                raise TypeError("update_summary.rows 必须是大于等于0的整数")
            curated_file = record.get("curated_file")
            if curated_file is not None and not isinstance(curated_file, str):
                raise TypeError("curated_file 必须是字符串或 null")
            source_errors_raw = record.get("source_errors", {})
            if not isinstance(source_errors_raw, dict):
                raise TypeError("source_errors 必须是对象")
            if not all(
                isinstance(source, str) and isinstance(message, str)
                for source, message in source_errors_raw.items()
            ):
                raise TypeError("source_errors 的键和值必须是字符串")
            parsed_records.append(
                UpdateRecord(
                    symbol=symbol.strip(),
                    rows=rows,
                    curated_file=curated_file,
                    source_errors=dict(sorted(source_errors_raw.items())),
                )
            )
        end = payload.get("end")
        if end is not None and not isinstance(end, str):
            raise TypeError("update_summary.end 必须是字符串或 null")
        return UpdateSummary(
            end=end,
            records=tuple(parsed_records),
            parse_error=None,
        )
    except (json.JSONDecodeError, OSError, TypeError, KeyError, ValueError) as exc:
        return UpdateSummary(end=None, records=(), parse_error=str(exc))


def _load_upstream_status(paths: ProjectPaths) -> tuple[dict[str, dict[str, str]], str | None]:
    summary = read_update_summary(paths)
    if summary.parse_error is not None:
        return {"__update_summary__": {"parse": summary.parse_error}}, None
    errors = {
        record.symbol: record.source_errors
        for record in summary.records
        if record.source_errors
    }
    return errors, summary.end


def _string_values(frame: pd.DataFrame, column: str) -> tuple[str, ...]:
    if column not in frame.columns:
        return ()
    values = {
        str(value).strip()
        for value in frame[column].dropna().unique()
        if str(value).strip()
    }
    return tuple(sorted(values))


def _has_missing_string(frame: pd.DataFrame, column: str) -> bool:
    if column not in frame.columns or frame.empty:
        return True
    values = frame[column]
    return bool(values.isna().any() or values.astype(str).str.strip().eq("").any())


def _dataset_id(files: tuple[DatasetFileManifest, ...]) -> str:
    canonical = json.dumps(
        [
            {
                "relative_path": item.relative_path,
                "sha256": item.sha256,
                "rows": item.rows,
                "symbol": item.symbol,
            }
            for item in files
        ],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def generate_dataset_manifest(
    paths: ProjectPaths,
    as_of_date: date | None = None,
    stale_after_business_days: int = 3,
) -> DatasetManifest:
    if (
        type(stale_after_business_days) is not int
        or stale_after_business_days < 0
    ):
        raise ValueError("stale_after_business_days 必须是大于等于0的整数")
    effective_as_of = as_of_date or date.today()
    parquet_files = sorted(paths.curated.glob("*.parquet"))
    if not parquet_files:
        raise FileNotFoundError("data/curated 中没有可生成清单的Parquet文件")
    calendar = load_project_sse_trading_calendar(paths.root)

    items: list[DatasetFileManifest] = []
    used_calendar_fallback = False
    for path in parquet_files:
        hash_before = file_sha256(path)
        frame = pd.read_parquet(path)
        hash_after = file_sha256(path)
        if hash_before != hash_after:
            raise RuntimeError(f"{path.name} 在生成清单期间发生变化，请重试")
        if "trade_date" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        issues = validate_daily_prices(frame)
        issue_warnings = [issue.code for issue in issues if issue.severity == "warning"]
        issue_errors = [issue.code for issue in issues if issue.severity == "error"]

        valid_dates = (
            frame["trade_date"].dropna()
            if "trade_date" in frame.columns
            else pd.Series(dtype="datetime64[ns]")
        )
        start_date = pd.Timestamp(valid_dates.min()).date() if not valid_dates.empty else None
        end_date = pd.Timestamp(valid_dates.max()).date() if not valid_dates.empty else None
        sources = _string_values(frame, "source")
        source_counts = (
            Counter(
                str(value).strip()
                for value in frame["source"].dropna()
                if str(value).strip()
            )
            if "source" in frame.columns
            else Counter()
        )
        symbols = _string_values(frame, "symbol")
        adjustments = _string_values(frame, "adjustment")
        volume_units = _string_values(frame, "volume_unit")
        freshness = calendar.days_after(end_date, effective_as_of) if end_date else None
        stale_days = freshness.trading_days if freshness else None
        custom_warnings: list[str] = []
        for column in ("symbol", "source", "adjustment", "volume_unit", "ingested_at"):
            if _has_missing_string(frame, column):
                issue_errors.append(f"missing_{column}")
        if not _has_missing_string(frame, "ingested_at"):
            ingested = pd.to_datetime(frame["ingested_at"], errors="coerce", utc=True)
            if ingested.isna().any():
                issue_errors.append("invalid_ingested_at")
        if len(symbols) > 1:
            issue_errors.append("mixed_symbols")
        if len(adjustments) > 1:
            issue_errors.append("mixed_adjustments")
        if len(volume_units) > 1:
            issue_errors.append("mixed_volume_units")
        if end_date and end_date > effective_as_of:
            issue_errors.append("data_after_as_of")
        if freshness and freshness.used_weekday_fallback:
            custom_warnings.append("calendar_snapshot_out_of_range")
            used_calendar_fallback = True
        if stale_days is not None and stale_days > stale_after_business_days:
            custom_warnings.append("stale_data")
        if len(sources) > 1:
            custom_warnings.append("mixed_sources")
        if "none" in adjustments:
            custom_warnings.append("unadjusted_prices")
        schema = tuple(f"{column}:{frame[column].dtype}" for column in frame.columns)
        items.append(
            DatasetFileManifest(
                relative_path=path.relative_to(paths.root).as_posix(),
                symbol=symbols[0] if len(symbols) == 1 else path.stem,
                rows=int(len(frame)),
                start_date=start_date,
                end_date=end_date,
                sha256=hash_after,
                schema=schema,
                sources=dict(sorted(source_counts.items())),
                adjustments=adjustments,
                volume_units=volume_units,
                business_days_stale=stale_days,
                warning_codes=tuple(sorted(set(issue_warnings + custom_warnings))),
                error_codes=tuple(sorted(set(issue_errors))),
            )
        )

    files = tuple(items)
    upstream_errors, update_summary_end = _load_upstream_status(paths)
    has_errors = any(item.error_codes for item in files)
    has_warnings = any(item.warning_codes for item in files) or bool(upstream_errors)
    health_status = "error" if has_errors else "warning" if has_warnings else "pass"
    freshness_method = calendar.calendar_id
    if used_calendar_fallback:
        freshness_method += "_with_weekday_fallback"
    return DatasetManifest(
        dataset_id=_dataset_id(files),
        generated_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        as_of_date=effective_as_of,
        stale_after_business_days=stale_after_business_days,
        freshness_method=freshness_method,
        freshness_calendar_sha256=calendar.sha256,
        freshness_calendar_coverage=(
            f"{calendar.coverage_start.isoformat()} 至 {calendar.coverage_end.isoformat()}"
        ),
        freshness_calendar_sources=calendar.source_urls,
        total_rows=sum(item.rows for item in files),
        health_status=health_status,
        files=files,
        upstream_errors=upstream_errors,
        update_summary_end=update_summary_end,
    )


def _status_text(item: DatasetFileManifest) -> str:
    if item.error_codes:
        return "错误：" + ", ".join(item.error_codes)
    if item.warning_codes:
        labels = {
            "stale_data": "数据陈旧",
            "mixed_sources": "混合来源",
            "unadjusted_prices": "不复权",
            "large_date_gap": "日期间隔异常",
            "calendar_snapshot_out_of_range": "交易日历快照覆盖范围外",
        }
        return "提醒：" + ", ".join(labels.get(code, code) for code in item.warning_codes)
    return "通过"


def _report_context_id(manifest: DatasetManifest) -> str:
    payload = manifest.to_dict()
    payload.pop("generated_at")
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:8]


def write_dataset_manifest_report(
    manifest: DatasetManifest,
    paths: ProjectPaths,
) -> tuple[Path, Path]:
    stem = f"dataset_manifest_{manifest.dataset_id[:12]}_r{_report_context_id(manifest)}"
    json_path = paths.outputs / f"{stem}.json"
    html_path = paths.outputs / f"{stem}_report.html"
    json_path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(item.symbol)}</td>"
        f"<td>{item.rows}</td>"
        f"<td>{item.start_date or '未知'} 至 {item.end_date or '未知'}</td>"
        f"<td>{item.business_days_stale if item.business_days_stale is not None else '未知'}</td>"
        f"<td>{html.escape(str(item.sources))}</td>"
        f"<td>{html.escape(', '.join(item.adjustments))}</td>"
        f"<td>{html.escape(_status_text(item))}</td>"
        "</tr>"
        for item in manifest.files
    )
    upstream = (
        "<p class=\"warning\"><strong>上游数据源失败：</strong>"
        f"{html.escape(json.dumps(manifest.upstream_errors, ensure_ascii=False))}</p>"
        if manifest.upstream_errors
        else "<p class=\"ok\">最近一次更新记录未报告上游错误。</p>"
    )
    calendar_uses_fallback = "weekday_fallback" in manifest.freshness_method
    calendar_class = "warning" if calendar_uses_fallback else "ok"
    calendar_note = (
        "覆盖范围外日期按周一至周五保守估算，且已标出提醒。"
        if calendar_uses_fallback
        else "覆盖范围内已扣除上交所公告的休市日。"
    )
    calendar_sources = "<br>".join(
        html.escape(source) for source in manifest.freshness_calendar_sources
    )
    calendar_block = f"""<p class="{calendar_class}">交易日历：
<code>{html.escape(manifest.freshness_method)}</code>；覆盖期
{html.escape(manifest.freshness_calendar_coverage)}；SHA-256
<code>{manifest.freshness_calendar_sha256}</code>。{calendar_note}<br>
来源：{calendar_sources}</p>"""
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>数据集健康报告</title><style>
body {{ font-family: system-ui, "Microsoft YaHei", sans-serif; max-width: 1100px; margin: 36px auto;
padding: 0 20px; color: #172033; line-height: 1.6; }}
.warning {{ background: #fff4d6; border-left: 5px solid #ef9f27; padding: 12px 16px; }}
.ok {{ background: #edf8ef; border-left: 5px solid #4a9b5f; padding: 12px 16px; }}
table {{ width: 100%; border-collapse: collapse; }}
th,td {{ padding: 9px; border-bottom: 1px solid #ddd; }}
code {{ background: #f2f4f7; padding: 2px 5px; }}
</style></head><body>
<h1>数据集健康报告</h1>
<p>数据集ID：<code>{manifest.dataset_id}</code>；状态：<strong>{manifest.health_status}</strong>；
总行数：<strong>{manifest.total_rows}</strong>；检查日：<strong>{manifest.as_of_date}</strong>。</p>
{calendar_block}
<p class="warning">“不复权”意味着当前价格不能直接代表含分红再投资的总收益。</p>
{upstream}
<table><thead><tr><th>标的</th><th>行数</th><th>区间</th><th>滞后交易日</th>
<th>来源</th><th>复权</th><th>状态</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""
    html_path.write_text(document, encoding="utf-8")
    return json_path, html_path
