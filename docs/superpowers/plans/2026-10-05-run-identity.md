# P3 运行身份关联项目计划书

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax. 用户已要求按计划直接执行；不再展示审批菜单，不自动提交或推送。

**Goal:** 为每次每日模拟运行分配独立尝试 ID，并把它与实际提交的账本行、行情批次、健康输入和报告记录持久关联；独立只读入口核对明确的账本引用。

**Architecture:** 保留现有确定性账本 ID 和五表 schema；存储层返回已提交事务收据，流水线显式传递，自动化写 schema 2 归档。状态读取器同时支持旧 schema 1 和新 schema 2，在现有只读审计事务中核对历史引用。最终归档是关联的持久载体，不增加后台服务或崩溃日志系统。

**Tech Stack:** 现有 Python 3.11–3.13、DuckDB、pytest；本机执行环境 Windows/Python 3.13.9，不添加依赖。

## Global Constraints

- 项目仅用于金融学习与研究，不连接券商，不自动交易，不构成投资建议。
- 不修改确定性账本 batch_id/run_id、事件哈希、幂等规则、五表 schema 和既有账户计算；不迁移真实数据库。
- 旧 schema 1 归档按原始键集校验哈希，不重写、不补造关联；旧存储 API 仍返回 tuple[PaperState, ...]，既有调用兼容。
- 一次提交的收据只有 COMMIT 成功返回后才允许传播；收据查询失败必须回滚当前批次，不做提交后补查或以最新快照猜测归属。
- 成功 no-op 的新提交引用为明确空集合；缺失引用为未知。逐日提交后又失败只保留明确已返回的收据，不把失败日期猜为提交或回滚成功。
- 自动化尝试 ID 与归档内容哈希、账本 run ID、行情 batch ID 和 dataset ID 分开命名。
- 独立读取器保留显式根目录、路径安全检查、WAL 阻断、无写入/无报告导入、单个只读审计事务及 non_atomic 声明。
- 不运行真实根新命令，不变更原业务数据、恢复材料/候选/验收根、网络更新、计划任务、发布或 Git 状态。
- 不安装依赖。测试使用项目外已核对不存在的新目录，Python -B、pytest -p no:cacheprovider；覆盖率/类型/编译缓存只在本批规划目录。

## 本轮范围和授权

接续[U4 验收](2026-10-04-first-third-acceptance.md)，实施此前明确留下的运行身份关联包。用户授权先计划后直接执行、反复与交叉验证。本轮完成该包后停止，不自动进入 [U5 正式恢复](2026-10-04-operational-recovery.md)；P2/P4/P5 也不因此完成。

原生 worktree 工具上批对聊天上层目录返回 Not a git repository；本轮已核对仍只有现有功能分支 codex/v1-quality-sprint。前批未提交更改完整保留，使用一个代码写入者，审查和行为验证独立只读；不创建漏掉前批代码的新 checkout。

## 数据与兼容契约

### 三种运行身份

1. attempt_id：调用参数通过校验之后、首个业务动作之前生成 uuid.uuid4().hex，32位小写十六进制；本次所有阶段事实在最终归档下属于同一个尝试。
2. automation run_id：仍为最终归档去除 run_id 后，ASCII/排序键/紧凑 JSON/allow_nan=False 的 SHA-256 前16位；不是执行前身份。
3. ledger run_id：仍为 paper-run- 加现有确定性哈希24位，必须来自数据库已写入的实际行，不重新计算来充当收据。

新自动化归档 schema_version=2，继承所有 schema1字段并新增 attempt_id 和 run_identity。文件名/归档不可变/镜像写入顺序不变。run_identity 必须恰含 market、health、ledger、report 四个键：

- market：null 或 {batch_id, summary_sha256}。只由真实更新器在行情锁内、批次上下文成功退出后返回。失败未返回为未知，不事后读取 latest marker 配对。
- health：null 或 {dataset_id, as_of_date}，来自该次返回的健康 manifest；不能称今日健康重新验证。
- ledger：{scope, runs}，scope 为 complete、partial 或 unknown。runs 为按 trade_date/account_id 排序的明确收据数组；unknown 必须空，complete 可空且只用于真实无新增操作，partial 必须非空且只能表示逐日运行后失败。
- report：null 或 {archive_json_name, archive_json_sha256}，记录成功报告的不可变 JSON 文件名和内容指纹，不保存绝对私有路径。只读入口不打开报告文件，artifact_verified 永远为 false。

每条账本收据包含 run_id、batch_id、account_id、trade_date、dataset_id、curated_sha256、state_hash；日期为规范 ISO，字段类型/唯一性/同日账户完整性和操作日期必须相容。历史 dataset_id/输入身份按既有 opaque ID 精确比较，不仅凭日期关联。安全字符/长度限制不能破坏正常已支持的身份；历史schema1没有新字段仍表示not_available。

存储合同核对补充：历史curated_sha256列也接受过opaque文本，收据关联应精确比较原值，不因字段名新增64hex门禁而破坏旧存储API；state_hash及真实行情/报告内容指纹继续校验其SHA-256格式。

正常 init/processed/no-op 的真实流水线返回完整收据。兼容注入动作或旧构造器可缺元数据（None），写 unknown，不把它当空收据。PaperReportWriteError 保留原操作及收据。后续日期失败新增专用 PaperPartialOperationError，携带已确认日期和收据；新归档仅在 schema2 将 operation_status=partial、ledger_committed=true、scope=partial 保存，status仍failed，失败步骤仍paper_run。失败当前批次是否提交不在已确认范围。

PaperAutomationStatusWriteError 增加尝试 ID 和可用收据信息；保留 archive_confirmed 判定。归档未写成功不能宣称关联已经持久化，也不自动重跑。ID在开始分配、在结束归档保存：强制终止或断电发生于COMMIT到归档之间仍可能没有关联。这是明确边界，不声称实现了事务性跨文件日志或崩溃恢复。

### 独立只读关联状态

状态只核对最近可见尝试本身有效且无歧义时的关联；更晚归档无效/无法排序时保持未知，不能跳过它核对较早成功，也不从 last_success 回填。
- not_available：旧归档、未知收据、尚未取得账本事实。
- verified：完整非空引用全部在已审计账本的同一只读事务中匹配。
- partial_verified：仅明确列出的部分提交引用全部匹配，不表示该尝试完整成功或失败日期无提交。
- no_new_commits：完整空收据的 no-op，且请求组合账本可审计；不能把旧latest行分配给该尝试。
- mismatch：结构有效的归档引用与账本出现缺行、跨组合、错误身份/日期/输入/状态哈希或缺账户等不一致。
- unavailable：WAL、账本审计/查询失败，或输入观察变化，不能声称verified。

需查询历史paper_runs并连接paper_accounts，核对请求组合及每个已处理日期的完整账户集合；不能只对比当前latest_run。验证结果在observer.changed之后才能发布；归档/账本变化时降级。顶层automation_ledger_correlation输出上述状态，详细证据放ledger.correlation。market/health/report身份是历史记录，不因账本核对通过升级为当前来源或当前报告验收。

重复收据、非法类型/版本/日期等协议错误先使归档无效：公开结果为not_available和归档诊断，而不是mismatch。底层核对器仍可防御重复输入，但不得用它掩盖上层严格归档拒绝。

账户完整性以已审计数据库中的真实组合 roster 为准，纯归档协议不硬编码账户数。例如单日仅一条且字段合法的收据可通过结构校验，但数据库实际有两个账户时必须输出mismatch；不能把此情况与重复收据等协议错误混为一谈。

## 文件与接口

| 文件 | 变更 |
|---|---|
| src/finance_lab/paper_models.py | 增加不可变 PaperRunReference、PaperBatchCommit；PaperOperationResult追加默认None的ledger_runs和报告身份字段，保留已有构造顺序 |
| src/finance_lab/paper_store.py | 增加initialize_portfolio_with_receipt/commit_batch_with_receipt；旧方法委托并取states；在现有事务COMMIT前查询实际插入行和构造收据；新增只读历史引用核对方法 |
| src/finance_lab/paper_pipeline.py | 真实流水线使用收据API，收集每日已提交引用；专用部分失败异常；报告错误保留收据；成功报告捕获不可变JSON身份 |
| src/finance_lab/pipeline.py | 更新批次上下文成功退出后、仍持行情锁时为返回payload增加market_identity，不更改marker协议或在其后读取latest |
| src/finance_lab/paper_run_identity.py | 新增纯协议校验、序列化/安全投影辅助；不得导入写流水线或报告 |
| src/finance_lab/paper_automation.py | preallocated attempt_id，schema2归档，采集明确阶段身份和partial事实，保留状态写入失败安全语义 |
| src/finance_lab/paper_automation_history.py | schema1/2精确字段、原始哈希、新关联语义；旧字段未知，新partial分支明确且限schema2 |
| src/finance_lab/paper_operational_status.py | 同一只读事务核对归档引用，输入变化后降级，安全JSON输出 |
| tests/test_paper_run_identity.py | 新增真实合成事务/流水线/归档/只读整合测试 |
| 既有automation/pipeline/store/history/operational测试 | 只调整预期schema版本及追加必要回归，不删减旧schema1/安全断言 |
| README、P3设计、恢复计划、验收报告 | 入口/关联含义、兼容、失败和未做事项 |

## 任务1：端到端身份关联（同一个可独立验收实现单元）

**Interfaces:** PaperRunReference记录七个上述字段；trade_date在Python为date，JSON为ISO。PaperBatchCommit(states: tuple[PaperState,...], run_refs: tuple[PaperRunReference,...])。两个with_receipt方法接受与旧方法完全相同参数；旧API取result.states。PaperOperationResult.ledger_runs为tuple[PaperRunReference,...] | None，None与()不同。

- [x] 先写失败测试，覆盖实际提交、第二账户回滚、重复日期、no-op、后续日期失败、报告失败、归档失败、旧新格式及只读精确引用。
- [x] 运行新测试并保留RED证据，确认因缺少目标功能失败，不以fixture拼错充当RED。
- [x] 按下列事务结构实现，不改_append_step、_append_batch、_run_id或数据库schema；收据查询/校验失败发生在COMMIT之前。

```python
connection.execute("BEGIN TRANSACTION")
try:
    # 现有验证/初始化及 _append_batch 保持在同一事务
    states = self._append_batch(accounts, steps, data_context, batch_id)
    refs = self._read_batch_references(accounts, steps, data_context, batch_id)
    result = PaperBatchCommit(states=states, run_refs=refs)
    connection.execute("COMMIT")
    return result
except BaseException:
    connection.execute("ROLLBACK")
    raise
```

精确查询以batch_id和此次账户/日期限定，返回实际行；验证其status=committed、账户集合/日期/输入一致后构造PaperRunReference。不要把新attemptID写入确定性batch_id。

- [x] 流水线每次with_receipt返回后才追加refs和日期；后续异常只包装此前返回的非空收据；无此前收据保持原错误语义。报告生成失败从完成operation保留收据。
- [x] 自动化执行前生成attempt_id，完成时按上述schema2序列化；状态写入失败保留其原stage/archive_confirmed和新增身份。不要向注入动作强行添加新keyword。
- [x] 纯reader对schema1保持原键集规则；schema2校验新字段和partial分支；通过以下核心断言验证含义：

```python
assert first.payload["attempt_id"] != repeated.payload["attempt_id"]
assert repeated.payload["processed_dates"] == []
assert repeated.payload["run_identity"]["ledger"] == {"scope": "complete", "runs": []}
assert partial.payload["status"] == "failed"
assert partial.payload["operation_status"] == "partial"
assert partial.payload["run_identity"]["ledger"]["scope"] == "partial"
assert old_status["automation_ledger_correlation"] == "not_available"
assert linked_status["automation_ledger_correlation"] == "verified"
assert partial_status["automation_ledger_correlation"] == "partial_verified"
```

- [x] 在_ledger原只读事务中核对历史准确引用，不重新打开写连接；最终观察变化、WAL或审计失败不能输出verified。无新增引用不是首次运行。
- [x] 定向GREEN并自审，保存精确命令、结果、当前差异包和已知边界；禁止自动commit。

## 任务2：独立复核、反例交叉与完整回归

- [x] 独立代码审查读取本批差异（基准为本轮开始的工作文件，不是空HEAD..HEAD），检查上述约束、回滚/部分提交及导入边界。
- [x] 独立行为验收使用与实现测试不同的夹具构造：真实离线init、processed、第二次no-op、后续日失败、报告失败、旧schema1、新schema2篡改、旧引用在新增账本后仍可核对。
- [x] 用真实子进程运行只读CLI，重复同一根，比对文件/目录集合、SHA256、大小、mtime和模式；导入/写入拦截及WAL前不连接检查继续保留。
- [x] 发现就交原执行者补RED修复，再由独立审查者复验。不以首轮绿色代替审查。
- [x] 稳定代码后运行全套pytest+分支覆盖、Ruff、mypy、编译、pip check、Markdown和diff检查；最后比较原业务文件基准。本轮基准296文件（上批295是上一日快照），排除既有锁，mtime用精确字符串保存。
- [x] 更新设计/说明和验收报告，列明跳过和未测平台，保留所有失败证据；到此停止。

统一测试命令模板（fresh_root必须先确认不存在，位于项目及恢复副本之外）：

```powershell
if (Test-Path -LiteralPath $fresh_root) { throw 'Refusing to reuse test root' }
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests/test_paper_run_identity.py tests/test_paper_automation.py tests/test_paper_pipeline.py tests/test_paper_store.py tests/test_paper_automation_history.py tests/test_paper_operational_status.py --basetemp $fresh_root
```

全套采用同样隔离规则，增加 --cov=finance_lab --cov-branch --cov-report=term-missing --cov-fail-under=80 -ra。不执行网络审计/安装/新平台CI来暗示P5已完成。

## 执行状态

已按计划实施并完成修复后的独立复验。稳定代码全量1062 passed / 4权限skip、综合覆盖率89.08%；独立代码复审53 passed，独立行为25场景/75子进程通过，296个原业务文件内容及元数据不变。仅完成本批身份关联，不执行U5正式恢复；完整失败、修复及边界见[本批验收](2026-10-05-run-identity-acceptance.md)。
