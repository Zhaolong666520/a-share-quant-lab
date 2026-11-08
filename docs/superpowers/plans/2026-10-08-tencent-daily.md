# Tencent Daily Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。已确认源组合，连续执行，单写者与任务级独立复核，不在任务间重复询问是否继续。

**Goal:** 自动获取腾讯不复权日线，以BaoStock必需对照，驱动既有固定模型日报并经独立新数据根验收后接入日程。

**Architecture:** 新独立tencent_source模块负责严格取数；root内显式source_profile配置选择两种来源组合，默认legacy兼容。现有paper-daily更新/健康/模型/报告/留证链保持失败关闭；新的来源名与请求窗可追溯。

**Tech Stack:** 现有Python3.11–3.13、requests、AKShare、BaoStock、pandas、DuckDB、PowerShell、pytest。

**执行环境调整（2026-10-08）：** C盘出现空间不足，全量回归attempt02中止，不能算通过。没有删除或移动旧文件；后续新测试临时目录与独立运行候选改为D盘`D:/CodexTaskArtifacts/tencent-daily-20261008/`。r03首轮真实处理证据保留；正式切换候选为该目录内新建`finance-lab-runtime-r04`，重新从accepted复制、配置、捕获基线和完整区间验收。

**完成记录（2026-10-12）：** T1–T5已完成。目录检查和阻塞读取超时各经独立复核发现、修复及再复核；最终完整回归1329通过、9跳过、覆盖率89.41%。10月9日完整日线及重复运行验收通过，实际Windows Task退出0、全区间7次运行/10条新增模型日记录全部验收通过。正式Action只增加DataRoot，不含固定AsOf；原工作日18:30/Principal/其他Settings保持一致。参见[交付记录](2026-10-12-tencent-daily-report.md)。

## Global Constraints

- 仅sh.000300指数和sh.510300 ETF；新组合不会把指数设为可交易资产。
- 主源`tencent`必须实际响应day，不能qfq/hfq回退；股单位volume、元单位amount、不复权none、source/ingested_at完整。
- 新profile=`tencent_baostock`，JSON键只schema_version/profile；缺文件legacy，坏配置失败。新组合内两个来源必需，ETF BaoStock对照明确从2026-01-01开始，不称全史。
- 网络使用正常配置；不改代理/TLS/认证，不重试保护拒绝，不全局patch第三方。取数失败或坏数据不填补、不靠对照源冒充主源成功。
- 原config/data/outputs、accepted和旧故障/事故证据不改。日历只修包资源及新候选，不给休市填K线。
- 固定SMA20/60和120日动量、既有成本/风险/观察规则不变；不自动训练、调参、连接券商或真实资金交易。
- 单写者；主唯一Task作者；测试只用隔离根/fresh basetemp/-B/no缓存。稳定代码后一次全量，修复例外只复测覆盖集。
- 保留feature分支，不push/merge/release。任务未通过就保留候选、恢复原日程并如实标明。

### Task 1: Calendar Resource Correction

**Files:** resources/sse_trading_calendar.json；tests/test_trading_calendar.py。原config文件不改。

**Interface:** `load_sse_trading_calendar(package_resource)`对2025-10-03返回closed，其他日期/来源不变。

- [x] RED：新增真实资源测试`assert date(2025,10,3) in calendar.closed_weekdays`，旧资源失败。
- [x] GREEN：资源2025年closed_weekdays仅补该ISO日期并保持唯一；不改原config/accepted。
- [x] 定向test_trading_calendar及资源bootstrap关联测试，Ruff/diff；单任务提交与Spec+Quality复核。

### Task 2: Tencent Provider

**Files:** 新src/finance_lab/tencent_source.py；新tests/test_tencent_source.py；pyproject.toml明确已有requests依赖。

**Interfaces:** `fetch_tencent(instrument: Instrument, start: date, end: date) -> pd.DataFrame`，返回现有CANONICAL_COLUMNS与source_diagnostics。无磁盘/Task/CLI副作用。

- [x] RED：当前没有fetch_tencent时，用完整HTTP响应fixture验证day、identity、字段和单位结果；缺功能失败后实现。
- [x] 使用拥有的Session，HTTPS腾讯新fqkline/get公开协议，年度param=`symbol,day,Y-01-01,(Y+1)-12-31,640,`，_var=`kline_dayY`，无复权后缀。明确连接/读取超时；不跟随未知重定向/保护响应、不改trust_env。最终采用每响应独立监督子进程，45秒请求预算加最多2秒正常回收及OS调度容差。
- [x] 验证code=0、data目标symbol、day数组、日期与所有数值。按SDK列位置date/open/close/high/low/volume/未用字段/turnover/amount，执行明确单位转换；只支持本次两个symbol/kind组合。
- [x] 全部接收行先校验；精确跨年重叠可去重，冲突同日拒绝；在用户请求窗选择后调用现有严格_normalize。规范化计数received=accepted=len(result)、rejected=0，另留JSON可序列化transport计数/窗口说明，不冒充坏行过滤成功。
- [x] 测试合法两标的、单位、多年度重叠、窗口裁剪统计、qfq-only/错误symbol/返回码/缺列/空/bad date/NaN/inf/负量额/OHLC/冲突重复/请求异常、TLS/默认路由未改与无业务写入。只mockHTTP边界，规范化真实。
- [x] 定向GREEN、Ruff/mypy及自审；任务提交与独立Spec+Quality复核。不得联网或全量，主负责真数据。

### Task 3: Explicit Source Profile and Pipeline Integration

**Files:** 新src/finance_lab/source_profiles.py；src/finance_lab/pipeline.py；新tests/test_source_profiles.py；README.md和数据说明。

**Interfaces:** `load_source_profile(root: Path) -> SourceProfile`提供profile ID/配置fingerprint、source角色和请求窗；`assert_unchanged()`在批次完成前复核。fetch_tencent来自Task2。public update_market_data签名不变。

- [x] RED：隔离root写JSON`{"schema_version":1,"profile":"tencent_baostock"}`，mock两provider边界但真实pipeline存储/摘要，验证tencent主源与ETF Bao2026请求窗，旧pipeline应失败。
- [x] loader拒绝unknown/extra/duplicate JSON键/非法类型/version/NaN/危险路径，不存在默认legacy。锁内在batch前读取，网络期间变更/移除/替换/危险路径失败，不提交成功摘要。普通CLI/env入口保留未resolve的输入路径以拒绝junction及链接祖先。
- [x] 显式新profile仅允许本次两标的。UpdateRecord新增source请求窗/角色字段，source_diagnostics维持现有严格字段契约；摘要增加profile标识/指纹，不改schema2账本或自动化收据协议。
- [x] 新组合tencent主源；原legacy逻辑兼容。任一选定源失败计入failed_sources/partial，不推进模拟。新组合tencent失败不写对照源为主的curated。无profile不要求新API。
- [x] 测试两组合、全部成功、任一失败、主源失败旧curated保留、未知配置/网络期间配置变更、identity、source标签、摘要来源日期/字段、原默认tests及paper-daily失败关闭。定向GREEN及独立复核。

### Task 4: Regression, Fixed-Model Reports and Candidate Acceptance

**Files:** 主私有证据目录与新的独立r04根（D盘环境调整）；公开处理记录。现有paper_automation/paper_models/账本schema不变。

- [x] 稳定代码完整pytest分支覆盖≥80%；Ruff/mypy/compile/pip/公开Markdown链接/diff。
- [x] 保存实际Task XML与指纹；仅在需要静止窗口时Disable指定Task新触发，不Stop进程。失败恢复原Action/Enabled。
- [x] fresh r04独立复制accepted，全文件hash/复制验收0；只在新root明确配置profile和修日历，保存复制时刻旧凭据与变更证据。
- [x] 捕获五表/旧归档基线；真实paper-daily截止完整日期执行首次处理和重复，同两固定模型HTML/CSV/JSON风险与基准报告验证；不称训练新模型。
- [x] 区间选择全部新attempt IDs验证历史保护/精确提交/同日no-op，实际退出码与报告hash单独证明；原根和accepted指纹重复核对。

### Task 5: Conditional Schedule Cutover and Handoff

- [x] 仅Task4及独立最终审核通过后改指定Task Action加新DataRoot，Trigger/Principal/Settings不变。
- [x] 控制实际Task执行、退出码/新归档/no-op与完整区间证据；受控日期可临时固定，结束必须移除，正式工作日18:30照旧。
- [x] 原始数据/旧结果/失败候选全保留；恢复或接入失败如实报告，不留下无说明的Disabled。
- [x] 本地提交与交付说明已完成/未完成/固定模型范围；不push/merge/release、不保证盈利。
