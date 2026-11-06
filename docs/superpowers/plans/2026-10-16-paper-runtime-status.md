# Paper Runtime Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 避免旧成功掩盖未完成运行，并把系统调度与应用/账本证据分开展示。

**Architecture:** 新独立 Python 入口复用已有严格只读状态检查；证据模块捕获日志、标记及 Windows 查询快照。
固定 PowerShell 资源只读取任务与可用的启动事件，查询不可用时安全退化为未知。

**Tech Stack:** Python 3.11–3.13、既有 DuckDB 只读审计、Windows PowerShell、pytest、Ruff、mypy。

## Global Constraints

- 根目录、组合、预期已完成交易日必须显式指定；原命令与正式调度定义不变。
- 不写正式根、不联网、不修改行情/账本/报告/锁，不根据未知系统状态推断运行成功。
- 全量测试、恢复夹具和打包材料在新建的 `D:\FinanceLab\scratch` 子目录中，必须位于仓库外；真实根只读。
- 本轮保留未提交改动供用户审阅，不自动提交或推送。

## Task 1: 证据捕获及纯判定

**Files:** 新建 `src/finance_lab/paper_runtime_evidence.py`、`src/finance_lab/paper_runtime_status.py`、
`tests/test_paper_runtime_status.py`。

**Interfaces:**

```python
def read_runtime_evidence(root: Path, portfolio: str, observer: Observation) -> dict[str, Any]: ...
def inspect_paper_runtime_status(root: Path, portfolio_id: str, expected_as_of: str, *,
                               now: datetime | None = None) -> dict[str, Any]: ...
```

- [x] 写失败测试：较新启动不被旧成功覆盖；预期日期必填/规范/不晚于上海观察日；
  缺完成归档、行情未完成、日志失败、超限/损坏/未来输入均 attention；规范终态不复制异常原文。
- [x] 运行 `python -B -m pytest tests/test_paper_runtime_status.py -p no:cacheprovider`，确认缺少新入口而失败。
- [x] 实现有界 Observation 捕获和分类：保留 base、日志最新启动/终态、标记起始时间和时长；
  在最终 changed 检查后清除变化证据，不用日志终态替代归档。
- [x] 加入真实隔离归档/账本及 CLI 测试，核对前后文件内容、mtime 和目录集合不变。

## Task 2: Windows 查询边界

**Files:** 新建 `src/finance_lab/resources/paper_scheduler_status.ps1`；
修改 `pyproject.toml` 的 package-data；扩展同一测试文件。

**Interfaces:**

```python
def read_scheduler_status(task_name: str) -> dict[str, Any]: ...
```

- [x] 写失败测试：权限错误/超时/不存在的工具/非法 JSON/旧快照/未来快照保留 unknown；
  Running 与 267009 不记失败；Ready 与非零完成码提示失败；next_run_time 不算已运行。
- [x] 固定资源脚本输出 schema1/plain JSON；仅查询根路径任务、任务信息和时间窗内的 107/110 事件。
  不调用 Start/Set/Register/Unregister，不输出 CIM 内部对象或异常原文。
- [x] Python 用 shell=False 参数数组与 15 秒 timeout，只接收有界严格 JSON；
  包含 `resources/*.ps1`，离线/非 Windows/无权限输出 unknown，不重试写操作。
- [x] 在真实 PowerShell 进程里仅隔离任务 API，核对查询失败仍为单 JSON，返回检查退出码 1。

## Task 3: 文档与验收

**Files:** 修改 `README.md`；新增本批交付记录，更新本计划复选框。

- [x] 文档写出源码/Windows 入口、预期日期语义、未知与非原子边界，不将快照当作实时保证。
- [x] 定向新测试及原只读/归档/运行身份回归；`ruff check --no-cache` 与 mypy 使用 D 工作区缓存。
- [x] 新建仓库外独立 basetemp 运行全量 pytest，保留真实跳过及失败，不触发正式运行。
- [x] 自审差异，真实根只读执行新入口，确认旧成功和 10 月 15 日未完成证据可见；
  对无法访问的系统状态保持未知，不以此宣称调度恢复。
