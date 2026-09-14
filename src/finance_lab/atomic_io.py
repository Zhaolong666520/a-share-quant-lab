from __future__ import annotations

import os
import uuid
from pathlib import Path

import pandas as pd


def write_text_atomic(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding=encoding)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_parquet_atomic(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp.parquet")
    try:
        frame.to_parquet(temporary, index=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_parquet_immutable(frame: pd.DataFrame, path: Path) -> None:
    """Publish a completed parquet file without ever replacing an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp.parquet")
    try:
        frame.to_parquet(temporary, index=False)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise RuntimeError(f"不可覆盖已有原始快照: {path.name}") from exc
    finally:
        temporary.unlink(missing_ok=True)
