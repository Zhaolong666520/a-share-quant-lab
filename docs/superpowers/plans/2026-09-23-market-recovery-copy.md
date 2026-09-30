# 恢复材料准备 Implementation Plan

> **For agentic workers:** Use executing-plans for sequential implementation and review checkpoints. Preserve the existing uncommitted changes; do not publish or operate on user data.

**Goal:** 实现已确认的恢复材料准备与复核，不解除行情隔离。

**Architecture:** 文件材料协议独立于策略；数据库只在副本上只读审计；CLI 将完整性、账本与行情状态分别输出。

**Tech Stack:** Python 3.11–3.13、pathlib、SHA-256、DuckDB、pytest、现有项目锁与原子发布。

## Global Constraints

- 依据[已确认设计](../specs/2026-09-22-market-recovery-copy-design.md)，本步 `ready_for_use` 恒为 `false`。
- 原行情、账本、配置、摘要和状态的字节不得改变；仅现有两个锁文件允许运行时副作用。
- 不抓行情、不迁移账本、不覆盖目标、不清理残留、不切换目录、不提交或推送。
- 合成数据与临时目录测试；保留前序未提交成果。按依赖顺序在当前非主分支实施。

## Task 1：只读账本入口

Files: `src/finance_lab/paper_store.py`, `tests/test_recovery_ledger.py`。

接口：`PaperStore.audit_recovery_ledger() -> dict[str, object]`，要求只读连接；返回 `status`、账户组与事件计数。版本不支持返回 `unsupported`；结构/事件错误抛出现有异常，由恢复入口转为失败结果。

- [x] 写缺少接口的失败测试，覆盖无账本、空账本、版本、部分表和只读约束。
- [x] 运行 `.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_recovery_ledger.py`，确认因缺少接口失败。
- [x] 实现只读结构前置检查、孤立记录检查，再调用既有 `audit_portfolio`；不得调用 `ensure_schema`。
- [x] 增加非空账本及指纹不变测试，运行账本定向回归。

## Task 2：材料复制与严格清单

Files: `src/finance_lab/recovery.py`, `tests/test_recovery.py`。

接口：`prepare_recovery(source: Path, destination: Path) -> dict[str, object]`；`verify_recovery(bundle: Path) -> dict[str, object]`。无副作用路径构造 `ProjectPaths`；清单记录文件、目录与缺失根项。失败抛 `RecoveryError` 或返回明确失败的审计结果，CLI 保持非零。

- [x] 先写失败测试：证据复制、失败标记原样保留、额外文件/损坏拒绝、已有目标拒绝。
- [x] 拒绝链接、特殊文件、路径重叠、大小写别名、WAL；按模拟锁→行情锁执行源清点、复制和源/副本复核。
- [x] 完整清单最后原子发布；复核严格解析版本、路径、类型、重复项，忽略旧检查结论并重新计算。
- [x] 副本账本只读审计，复用行情状态解析；检查前后核对负载，结果绑定清单指纹和程序版本。
- [x] 覆盖源中途变化、I/O 失败、锁竞争、进程中断、路径穿越、空目录、缺失项、WAL 和重复 JSON 键。

## Task 3：CLI 与验收

Files: `src/finance_lab/cli.py`, `README.md`, `CHANGELOG.md`, `docs/数据说明.md`，本计划和设计进度。

- [x] 接入 `data-recovery-prepare --destination` 和 `data-recovery-verify --bundle`；准备使用项目根但不调用有创建副作用的 `get_paths`。
- [x] CLI 测试检查返回码、结构化 JSON、错误不泄露数据正文；帮助强调材料包不代表恢复完成。
- [x] 更新文档，不把尚未完成的实际重建/切换描述为可用。
- [x] 执行全套 pytest 覆盖率、Ruff、mypy、compileall、pip check、Markdown 链接和 git diff --check；记录实际结果。
- [x] 独立只读审查本批改动，处理重要问题并复测；不提交、不合并、不发布。

## 验收记录

2026-09-23：用户“继续”确认设计，完成本地实现与验收；没有提交、推送、合并或发布。

| 验收 | 实际结果 |
| --- | --- |
| TDD | 首批 6 项账本测试因缺少接口失败；副本测试因缺少模块失败；实现后通过 |
| 新增回归 | 64 通过、1 跳过；跳过项为本机无权限创建符号链接，Windows 目录联接测试通过 |
| 全套测试 | 630 通过、1 跳过；Windows / Python 3.13.9，综合覆盖率 85.76%，恢复模块约 90% |
| 静态与环境检查 | Ruff、mypy（44 源文件）、compileall、pip check 通过 |
| 文档 | 40 份 Markdown 链接检查及 git diff --check 通过 |
| 独立审查 | 修复大写账本表误判、CLI 提前解析源目录链接；补充失败阶段和目标位置；复核通过 |

验证了真实子进程复制后强制退出、锁竞争后重试、源文件新增/删除/变化、WAL 出现、磁盘/元数据写入失败、严格清单解析、非空账本完整复制和只读审计。均使用临时目录与合成数据，不将这些结果描述为真实行情恢复成功。

实施边界补充：源项目和 `data` 必须存在，否则在创建锁或目标前拒绝；这是避免锁实现初始化源目录。允许缺少 config、outputs、数据库或摘要并在清单中记录。材料 API 返回 JSON 兼容字典，嵌套字段随结果类型存在；失败诊断仅给出类型、阶段和目标，不回显数据库或行情正文。

没有对用户数据执行更新、复制、迁移、恢复或模拟账户推进。收尾只读核对的四个现有行情/数据库/摘要文件修改时间均为 2026-09-22 18:30（早于本轮）；本轮未采集开始指纹，不将 2026-09-20 的旧指纹当作今日前后对比证据。正式数据恢复演练、行情重建、解除隔离和人工切换仍未实施。
