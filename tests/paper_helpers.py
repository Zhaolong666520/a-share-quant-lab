from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from finance_lab.config import ProjectPaths, get_paths
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated


def make_paper_test_project(
    root: Path,
    *,
    periods: int = 140,
    mixed_sources: bool = False,
) -> tuple[ProjectPaths, pd.DataFrame]:
    paths = get_paths(root)
    config_directory = paths.root / "config"
    config_directory.mkdir(exist_ok=True)
    (config_directory / "instruments.json").write_text(
        json.dumps(
            [
                {
                    "code": "510300",
                    "exchange": "sh",
                    "name": "测试沪深300ETF",
                    "kind": "etf",
                }
            ]
        ),
        encoding="utf-8",
    )
    prices = make_synthetic_daily_prices(periods=periods).copy()
    prices["symbol"] = "sh.510300"
    prices["source"] = "akshare"
    prices["volume_unit"] = "share"
    if mixed_sources:
        prices.loc[prices.index[::2], "source"] = "baostock"
    curated_path = save_curated(prices, paths)
    write_update_summary(
        paths,
        end=prices["trade_date"].max().date().isoformat(),
        rows=len(prices),
        curated_file=curated_path.relative_to(paths.root).as_posix(),
    )
    return paths, prices


def write_update_summary(
    paths: ProjectPaths,
    *,
    end: str,
    rows: int,
    curated_file: str | None,
    source_errors: dict[str, str] | None = None,
) -> None:
    (paths.outputs / "update_summary.json").write_text(
        json.dumps(
            {
                "end": end,
                "records": [
                    {
                        "symbol": "sh.510300",
                        "rows": rows,
                        "curated_file": curated_file,
                        "source_errors": source_errors or {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
