from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path

import pandas as pd


def published_parquet_files(directory: Path) -> list[Path]:
    """List published candidates, excluding the reserved legacy staging namespace."""
    return sorted(
        path
        for path in directory.glob("*.parquet")
        if not (path.name.startswith(".") and path.name.endswith(".tmp.parquet"))
    )


def write_text_atomic(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding=encoding)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_file_atomic(path: Path, writer: Callable[[Path], object]) -> None:
    """Publish a generated file only after its temporary copy is complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        writer(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_parquet_atomic(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.to_parquet(temporary, index=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_parquet_immutable(frame: pd.DataFrame, path: Path) -> None:
    """Publish a completed parquet file without ever replacing an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        frame.to_parquet(temporary, index=False)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise RuntimeError(f"不可覆盖已有原始快照: {path.name}") from exc
    finally:
        temporary.unlink(missing_ok=True)
