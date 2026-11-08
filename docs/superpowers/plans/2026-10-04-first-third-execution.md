# P3 第一组三分之一交付项目计划书

**目标：** 完成 U4 严格只读每日状态汇总，从实现、故障测试到独立审查和回归一次执行完。

**授权：** 用户要求“先做一个项目计划书，然后再按照项目计划书直接执行”“不要向我提问，直接通过”“反复和交叉验证”。据此通过此前书面设计确认点并直接实施本地工作。正式目录切换、联网更新、计划任务和发布不纳入本批。

**范围口径：** 当前 P3 剩余三个独立验收包为 U4 状态汇总、运行身份贯通、U5 真实恢复运行。本轮选择 U4，按包数为 1/3，不表示工时、代码量或整个 v1 剩余工作的精确三分之一。P2 残余故障/平台验证、P4 工作台、P5.1 统一发布证据、P5.2 新用户验收、P5.3 候选冻结仍未完成，不因本批更改状态。

**依据：** [U4 详细设计](../specs/2026-10-04-paper-operational-status-design.md)、[恢复计划](2026-10-04-operational-recovery.md)、[v1 总设计](../specs/2026-09-18-v1-design.md)。

## 交付与约束

- 提供 `python -B -m finance_lab.paper_operational_status --root <目录> --portfolio <组合>`。
- 同时显示最近记录、最后有效成功、已审计账本、行情发布与报告生成记录；每项注明来源/未知/限制。
- 归档严格校验 schema、原始键集哈希、文件名、步骤及提交语义；旧版本缺字段不填零；坏的新归档不能被旧成功遮盖。
- 只读连接审计现有库；WAL/权限/结构异常不重试为写连接。不获取项目锁、不生成报告、不创建目标目录。
- 行情用捕获字节和纯解析入口，摘要冲突记为 unknown；非原子观察检测文件变化，不将无变化视为跨文件一致性保证。
- 不导入旧 CLI/自动化/报告/绘图模块，避免导入副作用。JSON 命令退出码 0/1/2 与历史运行退出码分开。
- 保留原业务数据及恢复副本、现有未提交修改；不自动提交、推送、合并、发布。

## 文件与接口

为使单个模块可审阅，将原设计中的读取实现分为三个小模块；这是 U4 内部组织，不增加外部功能。

| 文件 | 职责与接口 |
|---|---|
| `src/finance_lab/operational_status_io.py` | 安全路径、有限 JSON 读取、重复键/非有限数拒绝、前后指纹；仅内部支持接口 |
| `src/finance_lab/paper_automation_history.py` | `inspect_automation_history(root: Path, portfolio_id: str) -> dict[str, Any]`，扫描与选择已验证归档，安全输出 |
| `src/finance_lab/paper_operational_status.py` | `inspect_paper_operational_status(root: Path, portfolio_id: str) -> dict[str, Any]`，行情/账本汇总；`main(argv: list[str] | None = None) -> int` |
| `src/finance_lab/market_data_state.py` | `parse_market_update_state(payload: object) -> MarketUpdateState`，复用既有校验器，不执行 I/O |
| `tests/test_paper_operational_status.py`、`tests/test_paper_automation_history.py` | 合成夹具、读写隔离、错误矩阵、当前/旧归档、命令子进程 |
| `tests/test_market_data_state.py` | 纯解析兼容与现有状态协议回归 |
| README、两份状态设计/恢复计划 | 实际入口、验证范围、仍未做的运行关联和正式恢复 |

## 实施顺序及验收点

### 任务 1：失败测试与只读基础

- [x] 先编写空目录、非法组合、缺 root、严格 JSON、WAL 阻断及导入副作用测试；记录未实现时的预期失败。
- [x] 实现有限安全读取和原始/结束观察指纹，根目录显式且不跟随重解析点。
- [x] 增加纯 `parse_market_update_state`，原状态机/发布流程保持既有契约。

核心外部行为示例（fixture 提供隔离目录）：

```python
result = inspect_paper_operational_status(tmp_path, "study-1")
assert result["command_exit_code"] == 1
assert result["automation"]["latest_attempt"] is None
assert not (tmp_path / "data").exists()
assert not (tmp_path / "outputs").exists()
```

### 任务 2：归档契约与保守选择

- [x] 当前写入器夹具与独立 legacy schema 1 夹具双向检验；校验哈希前不补字段。
- [x] 新失败/坏归档/镜像落后/孤立镜像/相同时间/未知版本/重复键/非有限数/语义冲突均保留诊断。
- [x] 最近报告状态只随最近确定的有效尝试，绝不从 last_success 回填。
- [x] 失败后已提交仅由显式 `ledger_committed=true` 派生；操作状态和处理日期保留。

### 任务 3：行情、账本和命令整合

- [x] marker 与同份摘要字节 SHA-256 交叉核对，来源计数/记录/状态冲突输出 unknown；离线恢复提示保留。
- [x] 单个只读事务先 `audit_recovery_ledger()`、再组合快照/估值查询；只输出请求组合。
- [x] 规范 JSON 与 0/1/2 退出码；未知或失败部分不阻止其他可读事实输出。
- [x] 实现前后输入变化检查和文件集合不变测试，始终声明 non_atomic。
- [x] 提供 README 示例并注明当前健康未重新核验、调度器未知、运行身份尚未贯通。

### 任务 4：独立交叉审查与回归

- [x] 执行者自检并记录定向测试；另一个审查者检查设计符合性及代码质量。
- [x] 独立交叉验证采用补充故障案例/子进程和固定输入重复读取，不只重复同一套断言。
- [x] 有发现就修复并重跑覆盖该问题的测试，复审通过后才记完成。
- [x] 全量 pytest/覆盖率、Ruff、mypy、Markdown 链接及 diff 检查；记录具体平台、未验证项和失败原因。
- [x] 保存验收报告，更新任务状态；到本包结束，不自动进入另外两包。

## 执行管理

起点为 `5810ba38a132a9f36a0d5e452b522ad80c3bd466`，已有功能分支 `codex/v1-quality-sprint`。应用原生 worktree 工具针对聊天上层目录返回 Not a git repository；本批在现有功能分支工作，代码由一位执行者串行写入，其他审查者只读，文档由主执行者维护，避免并行改同一文件。

测试固定使用现有 `.venv`，不安装新依赖。所有 Python 调用带 `-B`。pytest 使用 `-p no:cacheprovider`，每轮使用工作区中已核对不存在的新临时目录；恢复夹具临时根位于真实项目和所有恢复副本之外。覆盖率报告及类型检查缓存定向到本批 `.planning/first-third/`。

推荐定向命令（具体新目录名在运行记录中保存）：

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests/test_paper_operational_status.py tests/test_paper_automation_history.py tests/test_market_data_state.py --basetemp <全新外部目录>
.\.venv\Scripts\python.exe -B -m ruff check src tests
.\.venv\Scripts\python.exe -B -m mypy src --cache-dir .planning/first-third/mypy-cache
.\.venv\Scripts\python.exe -B scripts/check_markdown_links.py
```

## 完成状态

本包本地实现与验收完成。稳定代码全量 1033 passed / 4 skipped、89.20% 综合覆盖率；独立交叉验证 257 条断言通过，独立审查发现修复后复审通过。详细命令、跳过原因、原业务数据保留及未执行事项见[验收报告](2026-10-04-first-third-acceptance.md)。本轮到此停止，不自动开展运行身份贯通或 U5。
