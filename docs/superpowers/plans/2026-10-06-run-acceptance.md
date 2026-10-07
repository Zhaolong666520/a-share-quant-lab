# U5 运行前后验收工具项目计划书

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. 用户要求先计划、直接实施和反复交叉验证；不询问批准菜单，不自动commit/push。

**Goal:** 保存每日模拟运行前的只读账本基线，验收随后一组明确归档与账本新增记录的完整对应，以及末次重复运行无新增提交的事实。

**Architecture:** 新增独立只读命令，与现有写流水线分离。复用Observation、严格归档解析和PaperStore全库审计；用原行指纹保留历史，精确核对新增run与schema2收据集合。整个账本观察在一个read_only事务内，最终文件观察变化则拒绝验收。

**Tech Stack:** 现有Python 3.11–3.13、DuckDB、pytest；本机Windows/Python 3.13.9，不新增依赖。

## Global Constraints

- 原始数据只能新增快照，不能静默覆盖或手工修改。
- 数据源失败时应明确报告，不得编造或补齐真实行情。
- 保留前批U4/身份关联未提交改动、确定性账本ID、数据库schema和原计算。
- 不连接券商、不执行真实资金交易。状态是non_atomic本地观察，不能承诺持续运行或崩溃日志。
- 本批实现U5的验收工具和隔离演练，正式根目录切换、真实联网运行及计划任务变更仍未执行；不把合成数据当真实观察。
- 源读取不写目录、锁、数据库或报告，不导入paper_automation/paper_pipeline/pipeline/paper_report或绘图模块；stdout是唯一产品输出。
- WAL存在时在任何数据库连接之前阻断；路径祖先symlink/Windows重解析点、未知schema、审计失败、输入变化均拒绝通过。
- 不安装依赖、提交、推送、合并或发布。现有功能分支保留，一个产品写入者；审查者只读。原生worktree上批父目录失败不重试。
- 测试仅使用确认不存在的项目外同级合成根；python -B、pytest无缓存；覆盖率/类型/编译缓存限本批.planning/run-acceptance。

## 交付和使用

新增src/finance_lab/paper_acceptance_protocol.py（纯基线协议）和paper_run_acceptance.py（只读capture/verify及独立CLI），tests/test_paper_run_acceptance.py。旧写入API不改；公共文档由主执行者更新。

```powershell
.\.venv\Scripts\python.exe -B -m finance_lab.paper_run_acceptance capture --root C:\quant\recovered --portfolio default
.\.venv\Scripts\python.exe -B -m finance_lab.paper_run_acceptance verify --root C:\quant\recovered --portfolio default --baseline C:\quant\evidence\before.json --attempt-id 第一次完整attempt_id --attempt-id 第二次完整attempt_id
```

capture的JSON可在用户自己的终端保存到独立证据文件，verify直接读该JSON。工具不会调用每日运行；实际运行归档中的32位attempt_id按发生顺序逐条传入。这里路径为示例，本批只在合成根执行完整流程。

API：

- capture_run_acceptance(root: Path, portfolio_id: str = "default") -> dict[str, Any]
- verify_run_acceptance(root: Path, portfolio_id: str, baseline_path: Path, attempt_ids: Sequence[str]) -> dict[str, Any]
- 纯协议validate_baseline(value: Any, root_id: str, portfolio_id: str) -> dict[str, Any]，校验原始字段和baseline_id，不读取文件。
- 参数/基线协议错误为PaperAcceptanceRequestError，CLI退出2；可用证据审计/读取错误为EvidenceError，CLI退出1，仅固定诊断，不泄露底层异常及私有路径。verify的有效结果passed退出0，attention/unavailable退出1。
- 根类型或含`..`为参数错误；不存在/不安全/不可读的根为证据错误。未知计数保持null，真正读到空集合才是0。

## 基线协议（schema_version=1）

精确字段：schema_version、workflow、portfolio_id、root_id、captured_at、ledger、archives、baseline_id。
workflow固定paper_run_acceptance_baseline；root_id为validate_root结果经os.path.normcase后的绝对路径UTF-8 SHA256（JSON不存绝对根路径）；captured_at规范UTC Z时间；baseline_id为去除自身后规范JSON排序紧凑allow_nan=False的完整SHA256。

ledger恰有五键：

- paper_schema_metadata：[{"schema_version": int, "row_sha256": 64hex}]，按版本排序。
- paper_accounts：[{"account_id": str, "row_sha256": 64hex}]，按account_id排序。
- paper_runs：[{"run_id": str, "row_sha256": 64hex}]，按run_id排序。
- paper_events：[{"event_id": str, "row_sha256": 64hex}]，按event_id排序。
- paper_state：[{"account_id": str, "row_sha256": 64hex}]，按account_id排序。

行指纹覆盖SELECT *的全列（包括非业务时间），date/datetime转换规范isoformat，浮点必须有限，其他原生标量保持原值；字段/列顺序来自已审计schema，ID唯一。全库五表全部捕获；schema元数据/账户行保持完全相等，旧run/event行必须仍存在且内容相等；有新增run的账户允许state更新，其余账户state整行指纹必须不变。State与账户ID集合必须相等。当前全库新run不得藏在别的组合。

archives为指定portfolio所有既有不可变归档的[{"archive_name": basename, "sha256": 64hex}]，按文件名排序，精确匹配既有portfolio_UTCstamp_16hex.json名。基线保存内容SHA，不复制正文或镜像；旧归档内容即使协议坏也可保留原字节指纹，不能因后续验收删改或跳过它。unsafe/unreadable则拒绝capture。

每张表最多50,000行，SQL LIMIT 50,001后超过即固定诊断；沿用MAX_ARCHIVES=10,000；基线规范JSON最多16MiB，capture和verify都执行同一限制。字符串/数组/字典类型、精确键、规范日期/哈希、列表排序/唯一性严格检查，bool不能冒充int，空/控制字符ID拒绝。基线自哈希是意外变化检测，不是签名或来自可信机器的认证。

## 验收语义

1. 在validate_root/portfolio参数校验后，Observation捕获基线文件（若verify）、数据库/WAL、归档目录和内容；BEGIN只读审计事务、SET temp_directory=''。先用目录元数据确认main BASE TABLE及结构，再限量五表，防止不可信view执行及超限审计；audit_recovery_ledger passed且请求组合存在才读取全行指纹与实际引用。不迁移、不ensure_schema、不导入写入模块。
2. capture必须结束事务并执行observer.changed；变化不得返回看似有效的基线。
3. verify严格校验baseline_id、root_id、portfolio_id。每个基线归档仍必须存在且字节SHA一致；所有该组合新归档须完整读取并由原history._validate校验原始字段/哈希/文件名，schema2必需。没有跳过新坏归档或取last_success的路径。
4. 新归档按started_at严格升序（同刻多份拒绝歧义），attempt_id唯一，started_at不得早于captured_at，completed_at不得晚于本次观察结束。传入attempt_ids必须与**全部新归档的有序attempt_id列表完全相等**，不得重复、漏项、选择旧归档或忽略额外新尝试。
5. 验证旧五表保护规则。每个明确complete/partial非空收据复用store.correlate_run_references并核对历史真实行/账户集合；新收据不能引用任何baseline已有run_id，也不得被两次尝试共同声称。
6. 所有归档已确认run_id的并集必须等于当前全库run_id减baseline run_id（集合相等）。额外真实提交、引用缺行/错误字段/缺账户都为attention及固定诊断；unknown保持未知，不能当complete空。未返回收据的COMMIT通过unclaimed_commits指出，不猜成回滚。
7. 每个选中attempt输出_safe_record的安全记录及correlation；处理报告失败/partial/上游失败时保留实际已知事实，整体attention，不冒充完整闭环通过。
8. repeat_noop_verified只在末次为success/no-op/complete空、前面至少一份success/processed、两者as_of_date相同、全部上述证据完整时为true；单次成功、只有no-op、日期不同、失败或未知不能通过完整验收。不要求两次market/data文件物理SHA相同；no-op业务可能重新写数据库物理文件，但不得新增paper_runs。
9. overall passed仅在历史保护、精确新增收据覆盖、归档顺序/选集完整、全部选中status=success且repeat_noop_verified同时成立。无法读取/审计/观察变化为unavailable；有效证据不符合验收为attention。输入变化优先降级、不得发布之前的passed。
10. 输出固定schema_version=1、workflow=paper_run_acceptance、acceptance_scope=recorded_automation_and_ledger_interval、status、portfolio_id、baseline_id、recorded_attempt_count、new_run_count、checks、attempts、diagnostics、observation及safety。safety包括non_atomic=true、broker_connected=false、authorizes_real_money=false、actual_exit_codes_verified=false、scheduler_status=unknown、current_health_verified=false、report_artifacts_verified=false。归档描述运行结果，不能伪造真实进程退出码/调度次数/实时健康/报告artifact验收。

## Task 1：可执行只读区间验收（一个完整实施单元）

**Files:** 新建两个上述产品模块和tests/test_paper_run_acceptance.py；不修改旧产品模块。测试可复用tests.paper_helpers真实交易夹具和既有自动化动作注入；原业务目录只做指纹检查。

- [x] 先写核心集成RED：真实init建立原账本，capture，再追加实际合成价格两日、真实自动化processed，重复真实no-op；verify新增4行且历史不变、末次无新增。
```python
baseline = capture_run_acceptance(root)
# 测试把baseline保存到root外证据文件；实际init在capture之前
first = automation(root, day, paper_run_portfolio)
repeat = automation(root, day, paper_run_portfolio)
result = verify_run_acceptance(root, "default", evidence_path,
                               [first.payload["attempt_id"], repeat.payload["attempt_id"]])
assert result["status"] == "passed"
assert result["new_run_count"] == 4
assert result["checks"]["repeat_noop_verified"] is True
```
- [x] 保留缺模块/行为不符的真实RED，不把fixture错误当功能证据；按协议实现pure validator、受限五表快照、capture/verify和CLI。
```python
connection.execute("SET temp_directory = ''")
connection.execute("BEGIN TRANSACTION")
try:
    audit = store.audit_recovery_ledger()
    # audit passed + existing portfolio; 同事务读取并核对五表及精确引用
finally:
    connection.execute("ROLLBACK")
# 然后Observation.changed，变化拒绝通过
```
- [x] 必要反例：旧行非业务时间篡改且账本审计仍可通过、旧行删除、跨组合/缺账户、run未归档、老引用复用、两归档重复引用、选择漏新/旧/重复ID、同刻归档、较新坏归档/schema1新归档、篡改/错根基线、dupkey/非有限数、WAL前零连接、观察变化、报告失败完整refs、逐日partial、成功COMMIT后未返回收据、上游失败/unknown、单次/只有no-op/不同as_of日期、空/超限表或文件、链接和只读导入/写入围栏。按实际路径选最小真实构造，非所有输入排列逐项机械测试。
- [x] 定向GREEN；Ruff/mypy；实施自审并保存精确RED/GREEN与边界，给稳定信号。不能自动commit。

## Task 2：独立复核与交付

- [x] 独立代码审查本轮差异包（新文件NUL diff，旧文档以本轮工作内容为基准），检查实际事务/历史保护/选集完整和末次无新增；发现交唯一实施者修复再复验。
- [x] 独立行为脚本用不同合成夹具生成实际五表、归档、报告；真实子进程capture/verify，重复读取并核对全根文件/目录、SHA、size、精确mtime和属性；进程文件写/联网/写流水线/绘图/report读取拦截，WAL前零连接。
- [x] 稳定代码后一次全量pytest+分支覆盖、Ruff、mypy、pipcheck、编译、Markdown/diff及原业务指纹；修复后的必要复测优先，避免无理由重复全套。
- [x] 更新README、恢复说明、P3/U5进度及本批验收；记录权限skip、实际剩余真实运行与平台验证，不把工具交付写成U5真实验收完成。当前工作分支保留。

2026-10-06 完成记录见[工具验收](2026-10-06-run-acceptance-report.md)。checkbox表示本批工具交付验收已执行；权限skip和真实未执行范围仍明确单列。

定向：
```powershell
# 每次root先确认不存在，不重用basetemp
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests/test_paper_run_acceptance.py tests/test_paper_run_identity.py tests/test_paper_operational_status.py tests/test_paper_automation_history.py --basetemp 新的项目外绝对目录
```
全量同样隔离并增加-ra --cov=finance_lab --cov-branch --cov-report=term-missing --cov-fail-under=80。原业务基准本轮296文件，排除既有两个锁，精确mtime tick字符串；最终核对集合和内容/元数据。
