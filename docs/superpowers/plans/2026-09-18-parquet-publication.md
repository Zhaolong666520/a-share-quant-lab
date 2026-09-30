# P1.1 Parquet 发布边界 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本轮在当前开发分支逐步执行；保留分支，不进行发布操作。

**Goal:** 让写入中和历史中断残留的临时 Parquet 不被当作已发布数据读取，同时保留正式文件的错误暴露与原有原子发布行为。

**Architecture:** 发布协议集中在 `atomic_io.py`。两个写入函数使用非 `.parquet` 后缀的暂存路径；三个目录扫描消费者调用同一枚举函数。仅排除旧的保留临时命名空间，不通过捕获读取错误来跳过正式文件。

**Tech Stack:** Python 3.11–3.13、pathlib、pandas/pyarrow、DuckDB、pytest、现有 Ruff/mypy/coverage。

## Global Constraints

- Python 支持范围维持 `>=3.11,<3.14`。
- 现有 CI 综合覆盖率门槛保持 `80%`；关键新增故障分支必须有针对性测试，不通过扩大排除范围提高数字。
- 原始快照及事件账本保留；迁移不得覆盖原始行情、重置账户、丢弃历史事件或删除亏损实验。
- 本轮仅修改本地开发分支；不自动推送、合并、发布、安装计划任务或连接券商。
- 测试使用隔离临时目录和清楚标注的合成数据；不运行或改写用户现有模拟账户。
- 没有执行的验收标为“未验证”，网络失败、缓存陈旧与真实成功分别呈现。

总设计见 [v1.0 设计方案](../specs/2026-09-18-v1-design.md)。本计划只覆盖 P0 与 P1.1；P1.2–P5 是后续独立交付。本修复不承诺多文件快照一致性、并发写入合并或断电后的持久性。

所有命令在 `C:\Users\zhaolong\Documents\Codex\2026-08-07\ni\finance-lab` 执行；所有文件列表相对于该根目录。当前分支 `codex/v1-quality-sprint`，起点 `0b16aa3`。

## Task 1：证明并修复临时文件可见性

**Files:**

- Create: `tests/test_storage_publication.py`：用真实 Parquet 与数据库验证三个消费者。
- Modify: `src/finance_lab/atomic_io.py`：统一已发布文件枚举与两个临时文件名。
- Modify: `src/finance_lab/storage.py`：数据库同步枚举入口。
- Modify: `src/finance_lab/data_manifest.py`：清单枚举入口。
- Modify: `src/finance_lab/pipeline.py`：校验枚举入口。

**Interfaces:**

- Consumes: `get_paths(root)`、`save_curated(frame, paths)`、`sync_duckdb(paths)`、`generate_dataset_manifest(paths)`、`validate_curated_data(root)`。
- Produces: `published_parquet_files(directory: Path) -> list[Path]`；返回稳定排序的 `.parquet` 候选，排除名称以 `.` 开头且以 `.tmp.parquet` 结尾的保留临时路径。其他候选保持原读取/错误行为。

- [x] **Step 1: 在新测试文件写入以下回归测试。**

```python
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pytest

from finance_lab import atomic_io
from finance_lab.config import get_paths
from finance_lab.data_manifest import generate_dataset_manifest
from finance_lab.pipeline import validate_curated_data
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated, sync_duckdb


def _read_rows(root: Path, reader: str) -> int:
    paths = get_paths(root)
    if reader == "database":
        return sync_duckdb(paths)
    if reader == "manifest":
        manifest = generate_dataset_manifest(paths)
        assert len(manifest.files) == 1
        return manifest.total_rows
    assert reader == "validation"
    report = validate_curated_data(root)
    assert len(report["files"]) == 1
    assert report["error_count"] == 0
    return int(report["files"][0]["rows"])


@pytest.mark.parametrize("reader", ["database", "manifest", "validation"])
@pytest.mark.parametrize("complete", [False, True], ids=["partial", "complete-unpublished"])
def test_readers_ignore_in_progress_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str, complete: bool,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=150)
    save_curated(prices.head(120).copy(), paths)
    to_parquet = pd.DataFrame.to_parquet

    def paused_write(self: pd.DataFrame, path: object, **kwargs: object) -> None:
        temporary = Path(str(path))
        if complete:
            to_parquet(self, temporary, index=False)
        else:
            temporary.write_bytes(b"partial parquet")
        assert _read_rows(tmp_path, reader) == 120
        if not complete:
            to_parquet(self, temporary, index=False)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", paused_write)
    save_curated(prices, paths)
    assert _read_rows(tmp_path, reader) == 150
    assert not list(paths.curated.glob(".*.tmp*"))


@pytest.mark.parametrize("reader", ["database", "manifest", "validation"])
@pytest.mark.parametrize("complete", [False, True], ids=["partial", "complete-unpublished"])
def test_readers_ignore_legacy_temporary_file(
    tmp_path: Path, reader: str, complete: bool,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=120)
    save_curated(prices, paths)
    temporary = paths.curated / ".demo_000300.parquet.0123456789abcdef.tmp.parquet"
    if complete:
        prices.to_parquet(temporary, index=False)
    else:
        temporary.write_bytes(b"partial parquet")
    before = temporary.read_bytes()
    assert _read_rows(tmp_path, reader) == 120
    assert temporary.read_bytes() == before


@pytest.mark.parametrize("reader", ["database", "manifest", "validation"])
def test_corrupted_published_file_is_not_silently_skipped(tmp_path: Path, reader: str) -> None:
    paths = get_paths(tmp_path)
    save_curated(make_synthetic_daily_prices(periods=120), paths)
    (paths.curated / "broken.parquet").write_bytes(b"not parquet")
    error_type = duckdb.InvalidInputException if reader == "database" else pa.ArrowInvalid
    with pytest.raises(error_type):
        _read_rows(tmp_path, reader)


@pytest.mark.parametrize("writer_name", ["write_parquet_atomic", "write_parquet_immutable"])
def test_staged_parquet_has_no_published_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer_name: str,
) -> None:
    prices = make_synthetic_daily_prices(periods=120)
    to_parquet = pd.DataFrame.to_parquet

    def paused_write(self: pd.DataFrame, path: object, **kwargs: object) -> None:
        to_parquet(self, Path(str(path)), index=False)
        assert list(tmp_path.glob("*.parquet")) == []

    monkeypatch.setattr(pd.DataFrame, "to_parquet", paused_write)
    target = tmp_path / "prices.parquet"
    getattr(atomic_io, writer_name)(prices, target)
    pd.testing.assert_frame_equal(pd.read_parquet(target), prices)
    assert list(tmp_path.iterdir()) == [target]
```

该测试只暂停底层写入时机，仍执行真实 Parquet 写入和真实消费者；不用线程 sleep，不替换消费者返回值。

- [x] **Step 2: 运行失败测试并确认原因。**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_storage_publication.py --tb=short
```

预期：写入中/历史残留测试因无效 Parquet 或行数/文件数增加而失败；暂存后缀测试失败。正式文件损坏测试应当已通过，是现有行为保护。

- [x] **Step 3: 实施以下最小修改。**

在 `atomic_io.py` 的两个 Parquet 写入函数中，将暂存路径语句均改为：

```python
temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
```

在该模块新增完整函数：

```python
def published_parquet_files(directory: Path) -> list[Path]:
    """List published candidates, excluding the reserved legacy staging namespace."""
    return sorted(
        path
        for path in directory.glob("*.parquet")
        if not (path.name.startswith(".") and path.name.endswith(".tmp.parquet"))
    )
```

三个消费者从 `finance_lab.atomic_io` 导入 `published_parquet_files`，保留原有其他导入。`storage.sync_duckdb` 和 `data_manifest.generate_dataset_manifest` 中替换为：

```python
parquet_files = published_parquet_files(paths.curated)
```

`pipeline.validate_curated_data` 中替换为：

```python
files = published_parquet_files(paths.curated)
```

不添加删除残留文件的逻辑，不捕获正式文件损坏异常，不改变空目录既有返回语义。

- [x] **Step 4: 运行定向回归。**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_storage_publication.py tests/test_storage.py tests/test_data_manifest.py tests/test_pipeline.py --tb=short
```

预期全部通过。确认写入前为 120 行，发布后为 150 行；保留测试中旧临时文件字节。

## Task 2：保护失败语义与收尾验收

**Files:** 在 `tests/test_storage_publication.py` 追加测试；更新 `CHANGELOG.md`、本计划与总设计的进度；在 `README.md` 增加总设计入口。

**Interfaces:** 沿用 Task 1 的函数；无新业务 API。

- [x] **Step 1: 追加可直接运行的失败保护测试。**

```python
def test_replace_failure_preserves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=150)
    target = save_curated(prices.head(120).copy(), paths)
    before = target.read_bytes()

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated publish failure")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated publish failure"):
        save_curated(prices, paths)
    assert target.read_bytes() == before
    assert list(paths.curated.iterdir()) == [target]


def test_immutable_collision_preserves_original(tmp_path: Path) -> None:
    target = tmp_path / "raw.parquet"
    prices = make_synthetic_daily_prices(periods=120)
    atomic_io.write_parquet_immutable(prices, target)
    before = target.read_bytes()
    with pytest.raises(RuntimeError, match="不可覆盖"):
        atomic_io.write_parquet_immutable(prices.tail(2), target)
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


def test_published_enumeration_only_excludes_reserved_staging(tmp_path: Path) -> None:
    names = [
        "z.parquet", "a.parquet", ".hidden.parquet", "notes.tmp.parquet",
        ".prices.parquet.deadbeef.tmp.parquet", ".prices.parquet.deadbeef.tmp",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"candidate")
    assert [path.name for path in atomic_io.published_parquet_files(tmp_path)] == [
        ".hidden.parquet", "a.parquet", "notes.tmp.parquet", "z.parquet",
    ]


@pytest.mark.parametrize("reader", ["database", "manifest", "validation"])
def test_only_legacy_staging_is_treated_as_empty(tmp_path: Path, reader: str) -> None:
    paths = get_paths(tmp_path)
    temporary = paths.curated / ".prices.parquet.deadbeef.tmp.parquet"
    temporary.write_bytes(b"partial parquet")
    if reader == "database":
        assert sync_duckdb(paths) == 0
    elif reader == "manifest":
        with pytest.raises(FileNotFoundError, match="没有"):
            generate_dataset_manifest(paths)
    else:
        assert validate_curated_data(tmp_path)["files"] == []
    assert temporary.read_bytes() == b"partial parquet"
```

原子替换和不可变冲突测试应保护已有功能，不能为追求先失败而破坏旧实现。枚举测试保留普通隐藏文件与非隐藏 `.tmp.parquet` 文件，避免掩盖其他异常。

- [x] **Step 2: 定向测试、静态检查与全套回归。**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_storage_publication.py tests/test_storage.py tests/test_data_manifest.py tests/test_pipeline.py --tb=short
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest --cov=finance_lab --cov-branch --cov-report=term-missing --cov-fail-under=80
.\.venv\Scripts\python.exe scripts/check_markdown_links.py
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

逐项检查退出码，预期均为 0。代码检查与文档检查可独立运行；测试以当前 `.venv` 为准，不声称运行了三种 Python 或云端 CI。本步没有依赖变化，不为本地文件命名修复重复安装环境或访问行情源。

- [x] **Step 3: 更新文档与进度。**

`CHANGELOG.md` 的 Unreleased/Fixed 增加“临时 Parquet 不再进入数据库、数据清单和数据校验，旧残留保留且不参与读取”。将现有“80% 分支覆盖率”准确改为“开启分支测量的 80% 综合覆盖率”；表结构检查表述限定为实际已有的表/字段核验，完整类型/约束验收仍在 P1.3。

`README.md` 添加本地设计和实施计划链接；本计划记录实际测试数、覆盖率、日期及检查结果。通过后仅把总设计的 P1.1 更新为完成，其余维持未实施。

- [x] **Step 4: 差异复核与保留开发分支。**

```powershell
git diff --stat
git diff -- src/finance_lab/atomic_io.py src/finance_lab/storage.py src/finance_lab/data_manifest.py src/finance_lab/pipeline.py
git status --short --branch
```

确认无用户数据、账户、密钥、依赖锁文件变更。所有成果保留在当前本地分支；本轮不合并、不推送、不发布。

## 自审与验收记录

- 设计覆盖：本批次实现总设计 P1.1 的文件可见性、旧残留兼容与失败保护；多文件一致性、数据身份、账本与工作台不在本批次。
- 命名与类型：三个消费者使用同一 `published_parquet_files(Path) -> list[Path]`；两个写入函数的发布接口保持不变。
- 测试证据：见下表；Task 2 的失败保护用例一并提前加入首次失败测试，生产代码修改仍在复现问题之后。
- 下个批次：P1.2 先做规范标的/文件身份检查的独立小设计；不直接改动现有数据文件。

### 2026-09-18 本地验收

环境：Windows、Python 3.13.9，项目 `.venv`。起点提交 `0b16aa3`，本批次文件修改保留在当前开发分支工作区。

| 检查 | 结果 |
| --- | --- |
| 修改前新增 23 项测试 | 18 失败、5 通过；临时文件被读取，未完成文件触发格式错误，完成未发布的数据将 120 行误计为 270 行；新枚举函数尚不存在 |
| 修改后定向回归 | 64 通过，3.38 秒；写入前读 120 行，发布后读 150 行 |
| 完整测试及综合覆盖率 | 416 通过，40.20 秒；83.58%，超过现有 80% 门槛 |
| Ruff | 通过 |
| mypy | 40 个源文件通过 |
| Markdown 链接 | 34 份文档通过 |
| compileall / pip check | 通过 / 无依赖冲突 |
| git diff --check | 通过；Git 提示后续检出时进行 LF/CRLF 转换，没有空白错误 |
| 数据保护 | 测试仅使用临时目录；原始快照冲突和替换失败均保留原文件字节 |

新增枚举协议及其消费者共用测试已检查；正式损坏文件仍报错，旧暂存文件没有被删除。不运行或修改现有模拟账户，不改依赖、不改版本。本轮没有验证云端 CI、Python 3.11/3.12、Linux/macOS、联网数据源、真实计划任务或安装包。
