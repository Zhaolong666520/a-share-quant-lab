# 未完成行情批次识别与阻断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 中断后的行情批次持续隔离，提供只读诊断，不修改或回滚用户历史账户。

**Architecture:** 复用项目锁；独立状态模块严格校验 JSON 并绑定摘要 SHA-256。更新上下文持有线程/进程/项目绑定的写入身份，正常消费者不能因同线程持锁而绕过检查。最后发布 completed，失败只保留或加强隔离。

**Tech Stack:** 现有 Python 3.11–3.13、标准库、Parquet、DuckDB、pytest；不增加依赖。

## Global Constraints

- 本步只识别并隔离不完整更新，不宣称已经具备恢复或跨文件事务。
- 本轮不运行真实更新、不修改现有行情与模拟账户。
- 不在 `finally` 中无条件移除标记。
- `completed` 只表示“本次发布结果已完整记录”，不表示数据源全部成功、数据新鲜或策略可用。
- 本步目标为进程中断和普通 I/O 异常，不承诺机器断电、磁盘损坏后的持久性保证。
- 不自动重试、不新增定时任务、不推送/合并/发布。保留已有未提交修改，本批也先留在工作区。

## Task 1: 状态协议与完整更新生命周期

Files: 新建 `src/finance_lab/market_data_state.py`、`tests/test_market_data_state.py`；修改 `pipeline.py`、`storage.py`。

Interfaces:

```python
assert_market_data_readable(paths: ProjectPaths) -> None
assert_market_data_writable(paths: ProjectPaths) -> None
market_update_batch(paths: ProjectPaths, start: date, end: date,
                    symbols: tuple[str, ...]) -> Iterator[MarketUpdateBatch]
MarketUpdateBatch.stage(phase: str) -> None
market_update_status(paths: ProjectPaths) -> dict[str, object]
```

固定 JSON 键：schema_version=1、batch_id（32 位小写十六进制）、status、start/end（ISO 日期）、symbols（不重复的规范标的数组）、started_at（有时区）、phase、completed_at、summary_sha256、error_type。后三项以 null 表示尚无值；completed 需要时间/哈希且无错误，failed 需要错误类型。拒绝未知键、重复 JSON 键、未知版本、错误类型及字段组合。phase 取 preparing/fetch/raw/curated/database/summary/commit。

- [x] 先用真实文件发布加摘要写失败复现旧行为，读取应拒绝而旧代码放行。

```python
with pytest.raises(OSError, match="summary interrupted"):
    update_market_data(start, end, root)
with pytest.raises(RuntimeError, match="行情更新"):
    read_curated("sh.510300", paths)
```

- [x] 实现严格解析和上下文；只在标记成功发布后登记内部写权限。

```python
with market_data_lock(paths.data):
    assert_market_data_readable(paths)
    instruments = load_instruments(paths.root)
    with market_update_batch(paths, start, end, tuple(i.symbol for i in instruments)) as batch:
        return _update_market_data(start, end, paths, instruments, batch)
```

每次抓取、raw、curated、数据库、摘要开始前记录 stage；退出上下文后发布 completed。任何异常尽力记录 failed，失败记录自身失败不得替换原始异常；finally 只清理内存写权限。独立写入必须过状态门禁；批次内部写权限仅匹配当前进程、线程、项目和磁盘 batch_id/updating 状态时有效。

- [x] 用标记写入失败、数据库/摘要/完成发布失败和正常重复更新验证边界。

Run: `.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_market_data_state.py --tb=short`。先确认 red，再 green。

## Task 2: 消费者门禁与只读 CLI

Files: 修改 `data_manifest.py`、`paper_pipeline.py`、`paper_report.py`、`pipeline.py`、`cli.py`、`.gitignore`；扩展 `tests/test_market_data_state.py`。代码审查补充旧报告行情回填分支，不改变现代账本事件独立出报告的能力。

- [x] 参数化测试 raw/curated/sync/read/manifest/summary/validate/update/paper 拒绝 pending 或坏状态；确认状态、行情、摘要、账本未被改写。
- [x] 在现有行情锁内加入门禁，保持公开入口参数兼容。

```python
with market_data_lock(paths.data):
    assert_market_data_readable(paths)
    return _read_curated(symbol, paths)
```

- [x] 新命令只调用诊断，不进入普通读取门禁；异常 JSON 不回显原始文本。

```python
if args.command == "data-update-status":
    payload = market_update_status(get_paths())
    _print_json(payload)
    return 1 if payload["blocked"] else 0
```

诊断 JSON 包含 status、blocked、reason、state（合法结构或 null）和 message；legacy 只代表兼容，不冒充 completed。繁忙仍由现有 CLI 异常处理非零退出。

- [x] 验证 diagnostic 的 legacy/completed/failed/invalid 返回码与只读性，以及 paper-daily 阻断前置抓取、保留失败归档。

## Task 3: 中断与边界验证、文档

Files: 扩展 `tests/test_market_data_state.py`；更新 `README.md`、`CHANGELOG.md`、`docs/数据说明.md`、总体设计及本记录。

- [x] 子进程在真实 curated 发布、数据库提交、摘要发布之后 `os._exit(7)`，另一进程获取锁后仍拒绝正常读取。
- [x] 验证状态损坏/缺字段/重复键/未知版本、摘要丢失或不匹配；在更新期间同线程普通读取也不能绕过门禁。
- [x] 验证来源全失败仍保留来源错误，不能把完成发布当作行情健康。
- [x] 独立只读代码审查，处理发现；当前分支已有未提交前序工作，因此审核具体文件与工作区，不假设 HEAD 已含实现。
- [x] 运行全套质量检查并核对用户数据指纹。

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -X utf8 -m pytest --cov=finance_lab --cov-branch --cov-report=term-missing --cov-fail-under=80 --tb=short
.\.venv\Scripts\python.exe scripts/check_markdown_links.py
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

## 验收记录

2026-09-20 本地完成。分支 `codex/v1-quality-sprint`，基于 `0b16aa3` 的未提交工作区；Windows / Python 3.13.9。

失败先行证据：首次真实发布回归复现“整理行情已更新、旧摘要仍在、正常读取未拒绝”；38 项隔离/状态用例在旧实现上全部失败。接入后 38 项通过；新增 CLI 用例因命令不存在先失败，再实现命令。独立代码审查发现旧估值报告的行情回填绕过门禁；新增用例先复现，再改为 `read_curated`，复核确认解决。现代账本事件独立生成历史报告的行为保留。

| 检查 | 本地结果 |
| --- | --- |
| 新增状态/中断/诊断/回填回归 | 59 项通过（计入全套） |
| 状态＋模拟报告＋模拟流程定向回归 | 82 项通过 |
| 全套测试 | 566 项通过，60.65 秒 |
| 开启分支测量的综合覆盖率 | 85.30%，通过原有 80% 门槛 |
| 状态模块综合覆盖率 | 95%（不是 100%） |
| Ruff / mypy | 通过；mypy 检查 43 个源文件 |
| 编译 / pip check / diff 空白检查 | 通过 |
| Markdown 本地链接 | 38 份文档通过 |
| 数据保护 | 两份现有整理行情、DuckDB 和更新摘要的 SHA-256 与本轮开始一致 |
| 独立代码审查 | 1 个旧报告回填门禁遗漏，修复后复核通过 |

全部数据更新测试使用临时目录、合成行情和外部来源边界替身。真实子进程中断测试覆盖整理文件、数据库和摘要已经发布后的退出；断电持久性未验证。未联网抓取、未推进用户模拟账户、未修改计划任务、未重新打包，未跑云端或其他 Python/操作系统矩阵，未提交/推送/合并/发布。

下一步是独立副本中的恢复与人工切换协议，本批没有提供解除隔离、自动回滚或强制忽略命令。
