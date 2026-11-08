"""A narrow error boundary for decoding published curated Parquet files."""

from pathlib import Path

import pandas as pd
import pyarrow as pa


class CuratedReadError(RuntimeError):
    """A curated Parquet file could not be physically opened or decoded."""


def read_curated_parquet(path: Path) -> pd.DataFrame:
    try:
        return pd.read_parquet(path)
    except FileNotFoundError:
        raise
    except (OSError, pa.ArrowException) as exc:
        raise CuratedReadError("无法读取已发布整理行情。") from exc
