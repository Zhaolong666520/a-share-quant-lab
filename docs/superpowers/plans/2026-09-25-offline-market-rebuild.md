# Offline Market Rebuild Implementation Plan

> **For agentic workers:** Use sequential execution with focused tests. Preserve earlier uncommitted changes and do not operate on user data.

**Goal:** 从明确选择的原始快照生成一个可复核的独立行情候选。

**Architecture:** 先严格复核恢复材料与选择文件，再复制快照到全新目标；在目标直接发布验证过的整理文件并事务重建 `daily_prices`，只读核对全部模拟账本表与账户状态。候选报告最后发布，复核入口每次重新计算。

**Tech Stack:** Python 3.11–3.13、pandas、pyarrow、DuckDB、SHA-256、pytest、现有恢复材料协议。

## Global Constraints

- 设计见[离线行情候选重建设计](../specs/2026-09-25-offline-market-rebuild-design.md)。
- 原始材料包和正式项目保持原字节；候选新建，不覆盖。
- 每个配置标的恰好选择一份清单内原始 Parquet，必须指定正确 SHA-256。
- 候选继续保留原隔离状态，`ready_for_use=false`；不自动清除标记或推进账户。

## Task 1：选择协议与数据验证

Files: `src/finance_lab/recovery_rebuild.py`, `tests/test_recovery_rebuild.py`。

- [x] 编写合成材料包测试，覆盖合法快照与错误选择，记录身份、来源、日期与行数。
- [x] 实现严格 JSON 选择解析，包 ID/路径/哈希/标的集合与清单一致；验证 Parquet 内容、日期主键、数值类型、元数据和价格。
- [x] 运行 `.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_recovery_rebuild.py -q`。

## Task 2：候选发布与账本核验

Files: `src/finance_lab/recovery_rebuild.py`, `tests/test_recovery_rebuild.py`。

- [x] 编写非空账本、旧整理文件、失败中断与候选复核测试。
- [x] 实现目标路径检查、独立复制、原始快照保留、整理 Parquet 原子发布及 `daily_prices` 事务重建。
- [x] 实现模拟表指纹和只读审计前后比较、候选报告通过完整复核后原子发布、独立复核。
- [x] 运行定向及账本回归测试。

## Task 3：CLI、文档与验收

Files: `src/finance_lab/cli.py`, `README.md`, `docs/数据说明.md`, `CHANGELOG.md`, v1 设计进度。

- [x] 接入重建与复核 CLI，结构化报告、非零错误码及操作说明。
- [x] 运行全套 pytest、Ruff、mypy、compileall、pip check 与 diff 检查，记录结果。
- [x] 只读审查关键数据与账本边界；修复重要缺陷并复测。

## 验收记录

2026-09-25：合成数据定向测试 21 项通过；全套回归 651 项通过、1 项因系统符号链接权限跳过。Ruff、mypy（45 个源文件）、compileall、pip check 与 `git diff --check` 通过。独立审查发现并修复候选目录边界、原材料复核副作用、复制文件与原清单未绑定、数值字符串类型、过早成功标记五项问题；增加相应回归测试。仅验证合成候选，未对用户真实数据执行重建或切换。
