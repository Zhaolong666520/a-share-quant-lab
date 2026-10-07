---
title: "U4 Read-Only Paper Operational Status"
date: 2026-10-04
status: implemented-and-locally-verified
---

# U4 每日运行状态汇总设计

后续扩展说明：本文件保留2026-10-04的原U4验收范围；其中运行身份未关联、始终not_available的限制，后续由[2026-10-05运行身份计划](../plans/2026-10-05-run-identity.md)扩展为schema1/2兼容及明确收据核对。原先的非原子、只读和真实恢复授权边界仍适用。实现验证见[后续验收](../plans/2026-10-05-run-identity-acceptance.md)。

## 1. 目标与审批状态

承接 [运行恢复计划 U4](../plans/2026-10-04-operational-recovery.md)，将最近有记录的自动运行、最后一份有效成功归档、已提交账本、行情发布状态和报告生成记录分开说明。不能让历史成功掩盖最近失败，也不能将报告失败误认为账本未提交。

人类在推荐方案 A「新增独立只读命令」之后回复“继续”，本设计据此采用 A。方案 B「给现有 paper-status 增加只读模式」不采用：它与目录初始化、行情锁、研究会话预检和报告发布存在耦合。随后用户明确要求编写计划后直接执行、无需提问并交叉验证，书面设计门槛据此通过，按[本批项目计划](../plans/2026-10-04-first-third-execution.md)实施。不得自动提交或推送设计文件。

### 成功标准

- 原目录处于隔离状态时也能解释故障，不删除标记、不修复数据、不推进模拟账户。
- 最近记录失败与旧成功能同时出现；归档损坏时不能回退成“最近运行成功”。
- 缺失的新字段、归档、账本或并发可读性均明确为未知/不可用，不补零。
- 输出自身运行 ID 的类型与来源明确，不混用自动化运行 ID、账本幂等 ID、行情批次 ID、数据集 ID。
- 读取前后被检查目录的文件集合、内容、大小、修改时间和模式不变；无补建目录、锁、报告、WAL 或临时文件。系统访问时间不属于此承诺。

## 2. 范围与独立入口

第一批提供库函数和模块命令：

```text
python -B -m finance_lab.paper_operational_status --root <项目目录> --portfolio <组合ID>
```

`--root`、`--portfolio` 必填。根目录不从环境变量、当前目录、既有研究会话或其他项目推断。`-B` 禁止 Python 字节码缓存写入，作为严格只读验收的规定调用方式；不承诺操作系统或解释器外部缓存完全没有变化。

入口不导入 `finance_lab.cli`，不经过通用会话包装器，也不修改现有 `paper-status` 的行为。现有 CLI 在解析参数前已经导入流水线/报告依赖，仅在 `main` 中提前分发不足以证明整个启动过程无副作用。首批因此采用独立模块入口，不增加快捷 console-script 或改造整个旧 CLI。也禁止直接或间接导入 paper_automation、paper_pipeline、pipeline、paper_report、report 或 matplotlib；归档版本/哈希是独立读取协议，以写入器生成的隔离夹具做兼容回归，不靠导入写流水线复用私有函数。

拟变更范围：

| 文件 | 职责 |
|---|---|
| `src/finance_lab/paper_operational_status.py` | 无写入的行情/账本汇总、JSON 命令入口 |
| `src/finance_lab/operational_status_io.py`、`src/finance_lab/paper_automation_history.py` | 本批按读取边界和归档协议分离的内部模块，保持同一个 U4 外部契约 |
| `src/finance_lab/market_data_state.py` | 提供纯 payload 校验入口，复用现有状态语义，不更改发布/锁/隔离规则 |
| `tests/test_paper_operational_status.py` | 汇总语义、旧协议、错误及只读回归 |
| `tests/test_market_data_state.py` | 纯解析入口及既有状态协议回归 |
| `README.md`、恢复计划、v1 设计 | 说明入口、只读边界和实际完成范围 |

不新增策略、收益预测、工作台、报告模板、行情源或调度器；不改变已有归档协议、账本 ID、数据库 schema、快照选择。运行身份贯通属于 U4 后续独立小批次，本设计不推断尚未持久化的关联。

## 3. 输出契约

输出为单个 UTF-8 JSON 对象，`schema_version=1`。JSON 只使用有限数字、字符串、布尔、数组、对象和 null；日期统一 ISO 格式，观察时刻为有时区的 UTC。禁止 NaN/Infinity。正常检查不输出额外的报告横幅。

| 区块 | 事实与限制 |
|---|---|
| `observation` | 显式根目录、组合 ID、观察开始/结束时刻、`consistency=non_atomic`、是否检测到输入变化 |
| `automation` | 最近有记录的尝试、最后一份校验通过的成功记录、latest 镜像状态、本次扫描覆盖/异常；不表示没有后续未留档调用 |
| `ledger` | 只读事务内审计结果、各账户实际最后估值日、现金/份额/权益、末状态哈希、账本运行及输入数据集身份 |
| `market` | 观察到的批次/阶段、隔离原因、摘要指纹匹配、记录的来源结果及离线恢复提示；不重算实时健康 |
| `report` | 最近记录中的报告生成结果；`artifact_verified=false`，不代表当前 HTML/附属文件完整或与当前账本一致 |
| `diagnostics` | 固定错误码、严重程度和安全说明；不输出历史异常原文或跟随归档中的路径 |

顶层 `status` 为 `ok`、`attention` 或 `error`，另有 `command_exit_code`，不得与归档的历史运行退出码混用：

- `0 / ok`：必需事实均可读取并通过本设计的校验；最近归档成功、市场发布匹配且来源未记录失败、账本审计通过、报告生成有成功记录；无输入变化、历史扫描错误或未知的必需字段。
- `1 / attention`：请求有效，已输出可用事实，但出现失败、隔离、缺失、旧字段未知、扫描/镜像异常、WAL、账本不可用或输入变化。保留其他可读区块，不将整体标为正常。
- `2 / error`：组合 ID 无效、根目录不存在/不是目录/不安全，或不能生成可靠 JSON。参数语法错误采用 argparse 的 stderr/退出码 2；参数解析之后的错误输出 JSON 安全错误对象，不创建任何目录。无法序列化时只输出固定最小错误对象，不回调流水线诊断。

`ok` 仅表示记录可解释，不是市场新鲜度、来源今日可用、恢复验收凭据、真实资金准入或调度器成功。始终输出 `broker_connected=false`、`authorizes_real_money=false`、`current_health_verified=false`、`scheduler_status=unknown`。

报告区块的 `generation_state` 取 `generated`、`failed`、`not_reached`、`unknown`：最近尝试已确定、有效、status=success 且有报告路径记录才是 generated；明确失败在 paper_report 才是 failed；失败在行情更新或数据健康时为 not_reached；失败在 paper_run/paper_init、最近归档缺失/损坏/歧义时为 unknown。不得从 automation.last_success 回填最近报告结果。报告路径不被访问，不能将“记录生成成功”称为“当前报告文件验收通过”。

## 4. 归档校验与选择

读取固定目录 `outputs/paper_automation_runs/` 和固定镜像 `<portfolio>_paper_automation_latest.json`。归档是主要事实来源，latest 是完整 payload 镜像，仅用于交叉核对，不是要打开的路径指令。

### 严格版本兼容

- 支持既有 `paper_daily` 的 schema 1；拒绝重复 JSON 键、非有限数、非对象、未知版本、不匹配的组合/工作流/安全标记及非预期字段。
- 版本和计数要求真实整数，不能接受 bool；提交状态只能为 bool/null；日期必须真实且为规范 ISO；时间必须是写入器规范的 UTC `Z` 格式，完成不早于开始。
- 校验 `run_id` 为 16 位小写十六进制，并对原始对象仅移除 `run_id` 后，按现有 ASCII、排序键、紧凑 JSON、禁止非有限数的协议重算 SHA-256 前 16 位。先校验原始键集合的哈希，再补充表示未知的输出字段。
- 归档文件名必须与校验后的组合 ID、UTC 开始时间和运行 ID 一致；仅哈希正确不足以证明字段关系正确或来源真实性。
- 旧 schema 1 允许缺少 `failed_sources`、`market_data_committed`、`ledger_committed`、`next_action` 和 `error.code`；缺少时保持未知。旧 `error.message` 可能含路径或敏感异常原文，禁止复制到输出；错误说明由本地安全码生成。
- 验证步骤名称/状态、成功和失败的终态、处理日期以及存在的提交字段相容。失败加明确的 `ledger_committed=true` 才派生 `failed_after_commit`；市场提交成功不代表账本提交成功。成功 no-op 的空日期/`ledger_committed=false` 不当作错误。
- 从 `status` 推导历史退出码时标记 `exit_code_origin=derived`；原归档并未持久化退出码，不能伪称已保存的进程退出证据。

步骤名称限于 market_data_update、data_health、paper_run、paper_init、paper_report；状态限于 succeeded、failed 及仅用于 paper_run 的 portfolio_missing。步骤顺序沿既有流水线前进，paper_init 只能在 paper_run/portfolio_missing 之后；失败终态与 failed_step 对应，paper_report 只能在成功的 paper_run/paper_init 后失败。成功必须以成功的 paper_run 或 paper_init 结束，error/failed_step 为 null。操作成功时状态为 initialized、processed 或 no-op；操作尚未返回可验证结果的失败为 null/空日期/未知提交状态。明确 paper_report 失败保留实际 initialized/processed/no-op 及其处理日期和对应提交状态，不能因总 status=failed 清空已提交记录。不能将报告专用的 status 操作混入每日归档。处理日期严格递增、不晚于 as_of_date；processed 必须非空，initialized 恰好一日，no-op 必须为空。新提交字段存在时依这些事实校验；旧字段缺失不反向填出提交真假。

### 保守选择规则

按归档开始时间而非 mtime、完成时间或哈希字典序选最近尝试。目录中的相关文件名即使内容损坏也不能被静默忽略：较新坏归档使最近结果为未知；不能排序的相关文件名使最近顺序不确定。此时仍可单列旧的有效成功记录，但标明历史扫描不完整。

相同开始时间存在不同运行 ID 时，最近尝试表示歧义并列候选，不按哈希猜顺序；最后有效成功记录有同样并列情况时也表示歧义。镜像落后、损坏、孤立或内容与归档不同均有独立诊断，不覆盖较新的归档事实；比较完整已校验 payload，不能仅比较短 ID。

目录缺失、空目录、扫描无成功记录分别描述实际观察结果；均不声称“历史从未运行”。恢复材料只复制配置、数据和更新摘要，新根目录缺归档尤其不能推断为零次历史成功。扫描得到的记录数只能称本目录可见归档数量，不计算连续自动运行天数。

## 5. 行情与账本读取

### 无锁观察行情

禁止调用 `get_paths/ensure`、`market_update_status`、`inspect_market_update_status`、`generate_dataset_manifest`、报告写入函数、恢复/验收或初始化函数。`inspect_market_update_status` 需要不可变输入或调用者持锁，本入口不满足该前提。

直接捕获 marker/summary 字节，在内存严格解析，使用新增纯 payload 校验入口复用 `MarketUpdateState` 现有规则。当前状态为 updating/failed 时明确观察到隔离；completed 还需摘要字节 SHA-256 与 marker 相符。标记缺失表示旧协议/未知完整性；状态无效、摘要不可读/不符分别给出安全诊断。

纯解析入口只接收已捕获的 payload，返回已验证的 `MarketUpdateState` 或抛出解析/校验异常，不接收路径、不读取文件。U4 的严格 JSON 解码器负责 UTF-8、重复键和非有限数拒绝。既有基于路径的函数保留锁与 I/O 约束，无需把整个状态模块重构为新框架。

来源状态与发布状态分离：`partial_sources`、`failed` 不能转为成功；旧摘要通过规范记录推导结果并标记 derived，信息不足为 unknown。只有匹配 marker 的摘要才可作为该批次来源结果。摘要中的 `source_errors.offline_recovery` 单列离线恢复提示，不能称今日联网成功。孤立摘要可以报告“存在”，但不用于确认该批次完整或健康。

摘要记录需为非空对象数组，rows 为真实非负整数、source_errors 为字符串键值对象；计数 successful_instruments/failed_sources 如存在也须为真实非负整数，并分别等于正行数记录数/记录错误项总数。依据正行数和错误项推导 success、partial_sources 或 failed，与存在的顶层 status 交叉核对；record.status 或 source_diagnostics 如存在失败证据也不能被顶层 success 掩盖。顶层结果、计数、记录结果相互矛盾时，保留合法 marker 的发布观察，但 source_outcome=unknown、添加 summary_conflict 并返回 attention。离线恢复错误项参与部分失败判定。旧摘要缺可选计数或顶层 status 可依据完整记录派生并注明来源，不能将缺失字段声称为已保存的值；记录不全、非法类型或无法校验的诊断信息为 unknown。摘要只在捕获字节中解析一次，哈希绑定不代替语义验证。

观察前后重新核对已读文件的字节指纹、存在状态与相关目录文件集合；变化时相关区块标为 changed/unknown。至多做一次完整观察，不自动循环等写入结束、不触碰锁文件。没有检测到变化也始终 `consistency=non_atomic`：不能排除 ABA、最后核对后的变化，不能授权后续写入。

### 只读审计账本

数据库必须是已存在的安全普通文件。观察到 `<database>.wal` 就不连接数据库，避免隐式恢复；记录 `ledger_wal_present`，不删除或修复。数据库缺失、持有者导致的读取失败、schema 不兼容或事件审计失败都不能触发初始化、迁移、写模式重试。

使用 `PaperStore(..., read_only=True)`；在单个只读事务中先执行既有 `audit_recovery_ledger()`，确认账本表集合、版本元数据、字段/约束与事件审计为 passed，再完成请求组合的 `portfolio_snapshot` 和最后 VALUATION 查询，确保账本区块来自同一数据库事务。not_present、unsupported 或异常均停止账户信息输出，不调用 ensure_schema。现有 schema 比较使用内存数据库生成预期结构，允许此类不落盘的内存操作，禁止文件数据库写连接。既有全库审计会读取库内其他模拟组合，但只输出请求组合；不冒称仅扫描一个组合或全库审计没有成本。审计不通过时不输出貌似正常的账户余额。账户不存在不创建账户，记为 unavailable。

该复用选择的代价是其他组合的损坏也可能使请求组合的账本区块不可用。本批采用全库审计的保守结果，不绕过其他组合的异常；未来若需要仅组合级可信视图，须单独设计结构与关联审计范围。

最后估值日从 VALUATION 事件取值，各账户分别列出；仅所有账户日期相同才有组合共同最后估值日，否则共同日期为 null 并提示不一致。账本 latest_run 的 dataset ID/文件 SHA 明确名为账本输入身份，不冒充当前整理文件身份或当前健康。自动化 run ID 与账本 run ID 尚无持久化关联，`automation_ledger_correlation=not_available`，不得按日期猜测。

## 6. 路径、安全与资源界限

在根目录及每个待访问目标的 lstat/祖先检查后才做解析和读取，拒绝符号链接、Windows reparse、非常规文件；缺失的可选文件保持缺失。读取只在显式根目录的固定位置及匹配组合的归档文件进行。归档中所有报告路径只作历史元数据，不打开、不转换为待操作目标，也不输出其中的个人绝对路径。

本地路径检查不是恶意并发替换的强 TOCTOU 防护；输出明确非原子，本命令不作为跨租户安全边界。打开后核对普通文件身份，遇到替换/不确定路径时不信任该证据。原 root、候选和验收 root 不跨目录补读或拼接信息。

单份 JSON 读取上限 2 MiB；相关归档上限 10,000 份。超出读取或扫描上限时标为不可用/不完整，不悄悄截断后宣称最近/最后成功已确定。账本复用已有只读结构/事件审计，不渲染图表、读取全部逐行行情或运行回测；只输出请求组合。

## 7. 失败测试与验收

先写失败测试，再完成纯读取实现；测试只使用隔离合成夹具，不修改真实账本或数据。关键场景如下：

1. 当前成功、初始化、成功 no-op；旧成功后新失败；报告失败但账本已提交；仅行情已提交不得误报账本提交。
2. 旧 schema 1 缺新字段/错误码，哈希仍按原对象验证，未知保持 null，不泄露旧异常原文。
3. 重复键、NaN/Infinity、bool 版本/计数、错误工作流/组合/安全标记、未知版本、字段关系矛盾、哈希篡改、文件名与 payload 不符。
4. 时间无时区、非法日期、完成早于开始；并列时间；坏的新归档和可用的旧成功；无法排序的相关坏文件名。
5. latest 落后/损坏/孤立/同 ID 内容不同；目录缺失/空目录不能写作从未运行；归档超过上限不猜最近运行。
6. updating/failed/invalid/legacy/completed；摘要缺失/不匹配/部分失败/离线恢复；顶层 success 与错误计数、记录错误、失败诊断或离线证据冲突；观察途中替换或新增归档造成 unknown。
7. 有效账本只读审计；WAL、缺库、账户缺失、读取冲突、schema/事件篡改；版本元数据缺失/重复/未来版本、视图替换/未知表/约束变更；事务内估值查询；不初始化、不迁移、不写模式重试。
8. 不同账户估值日、自动化 ID 与账本 ID 不同、不把历史补处理日期数包装为连续自动运行天数。
9. 缺 root/非法 ID、链接/reparse/越界归档报告路径、输入权限错误、序列化错误；双调用后内容/文件集合/大小/mtime/mode 不变。
10. 独立入口子进程验收 JSON 与 0/1/2 退出码；禁止锁、mkdir、报告、文件数据库写连接及任何旧 CLI/写流水线/绘图模块直接或间接导入；`-B` 调用不新增字节码；原 `paper-status`/自动化行为不变。

实施验证包含定向测试、既有相关回归、全量测试及质量检查。临时测试根必须是工作区中全新且已核对不存在的目录；涉及恢复夹具时位于真实项目及所有恢复副本之外，不复用会被 pytest 清理的已有目录。Windows 无链接权限的跳过须单列，不能冒充所有平台均已验收。

设计批准不等于实现完成或全量测试通过。真实 root 的只读验收若涉及环境权限限制，停止受阻操作，不绕过权限；正常输出“未知”不作为修复成功证明。

## 8. 后续与完成门槛

本子批次完成门槛：书面设计获批、详细任务计划、失败测试、实现、独立审查及回归证据齐全；所有实现限制和剩余未知清楚记录。仅按实际验收范围更新 P3/U4，运行身份贯通尚未做时不得把整个 P3 宣称完成。

正式 root 切换、联网恢复运行、计划任务变更及 U5 仍需独立批准；本设计不提供这些操作的隐式授权。设计文件保持本地，不自动 commit/push/merge/release，不上传私人数据或恢复证据。

## 9. 设计复核记录（不是功能验收）

2026-10-04 早期设计阶段：主审完成占位、矛盾、范围及歧义自检；独立文档审阅提出的导入副作用、账本结构校验、报告失败提交语义、摘要矛盾和最近报告回退问题已修订，复审通过。当时尚未编写功能代码或进行功能验收，此记录只说明设计复核。

## 10. 本地实现验收

随后用户明确要求按计划直接实施并交叉验证，已完成[本批项目计划](../plans/2026-10-04-first-third-execution.md)。稳定代码全量 1033 passed / 4 skipped，综合覆盖率 89.20%；独立交叉验证 257 条断言通过，独立代码复审无未解决发现。跳过均与本机符号链接权限有关；完整范围与未执行事项见[验收报告](../plans/2026-10-04-first-third-acceptance.md)。运行身份贯通、正式切换、真实持续运行及全平台验证不属于本次完成范围。
