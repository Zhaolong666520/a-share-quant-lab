# v0.9 前向模拟盘设计

状态：已通过口头设计确认，等待用户审阅书面规格

日期：2026-08-25

目标版本：v0.9.0

## 1. 背景与目标

项目已经具备日线数据健康检查、固定策略比较、执行约束分析，以及现金、整数份额和费用账本。下一步不是继续寻找历史上更好看的参数，而是把两个已固定的策略放进一个可持续运行、可恢复、可审计的前向模拟盘。

v0.9 的目标是：每天收盘后，在本地数据更新并通过健康检查的前提下，处理上一交易日留下的待成交订单，再使用当天收盘前可知的数据生成下一交易日订单。系统不连接券商，不发送真实订单，不构成投资建议。

成功标准：用户可以通过一个 Windows 脚本每日运行；重复运行不会重复成交；中断后可以安全恢复；每一笔信号、订单、成交、费用、现金、持仓和权益都能从追加式事件记录中重建并对账。

## 2. 已确定的产品范围

### 2.1 v0.9 包含

- 只处理配置中标记为可交易的 ETF sh.510300。
- 一个账户组包含两个完全隔离的模拟账户：
  - SMA 20/60 双均线账户。
  - 120 日时间序列动量账户。
- 两个账户使用相同的初始资金和费用假设，但现金、持仓、订单和收益互不共享。
- 账户创建时锁定策略参数、初始资金、每手数量、佣金、最低佣金、卖出税费假设和滑点假设。
- 追加式 DuckDB 事件账本、可重建的当前状态缓存和逐日运行记录。
- paper-init、paper-run、paper-status 三个 CLI 命令。
- 面向初学者的 run_paper_trading.cmd 一键入口。
- 中文 HTML 仪表盘，以及 JSON、CSV 审计产物。
- 单元测试、集成测试、故障恢复测试和现有回归测试。
- 一份新的实验记录 experiments/008_forward_paper_trading.md，固定账户与费用假设，并明确起始日期。

### 2.2 v0.9 不包含

- 券商 API、真实订单、真实资金或任何凭证。
- 自动选择近期表现最好的策略。
- 两个策略共享资金或动态资金分配。
- 分钟行情、盘口、成交队列或真实成交概率估计。
- 网页服务器、登录系统、云端同步或手机 App。
- 自动配置 Windows 任务计划程序。
- 对历史日期回填模拟收益。
- 修改或删除已经产生的账户事件。
- 分红、除权现金流和总回报建模。因此累计收益必须标记为基于当前价格口径的模拟账户收益，不得称为长期总回报。

## 3. 方案选择

评估过三种路线：

1. 每天重新运行全历史账户回测并保存快照。实现快，但不是持久化前向状态，重复运行与中断恢复语义较弱。
2. DuckDB 追加式事件账本。实现量适中，能明确表达信号、订单、成交和恢复过程，并复用项目现有 DuckDB 与账户对账能力。
3. 直接建设网页模拟交易平台。展示空间更大，但引入服务端、部署和账户系统，超出当前目标。

采用方案 2。事件表是事实来源；当前状态表只是可以从事件重建的缓存。CSV 和 JSON 只作为导出，不作为可写状态来源。

## 4. 架构与职责边界

新增代码按职责拆分，避免把存储、策略和报告堆进同一个文件：

- paper_models.py：不可变账户配置、订单、事件、运行结果和状态数据类型及基础校验。
- paper_engine.py：纯状态转换。输入账户状态、待成交订单和一根已验证日线，输出事件与新状态，不直接访问数据库或文件系统。
- paper_store.py：DuckDB 表结构、事务、幂等约束、状态重建和审计查询。
- paper_lock.py：使用 Python 标准库文件句柄锁封装单实例运行；Windows 使用 msvcrt，POSIX 使用 fcntl。
- paper_pipeline.py：数据清单检查、行情快照校验、逐根补处理、两个账户的批次协调和报告调用。
- paper_report.py：HTML、JSON 和 CSV 产物，不参与交易状态计算。
- cli.py：新增三个命令并保持现有命令兼容。

现有 ledger.py 中经过验证的费用、整数手数和对账规则应复用或提取为小型公共函数。持久模拟盘不能通过每日重跑 run_account_ledger 来伪装状态；paper_engine 必须只处理账户创建后尚未处理的新日线。

## 5. 固定账户配置

paper-init 以账户组为操作单位。默认账户组名为 default，并创建以下两个账户 ID：

- default-sma-20-60-v1
- default-momentum-120-v1

账户组名必须满足小写字母、数字和连字符组成的安全短名规则，禁止路径分隔符、盘符、点号路径和空字符串。用户若想重新开始，必须使用新的账户组名；已有账户组不能用 force 参数覆盖。

默认模拟参数沿用当前项目假设：

- 初始资金：100000 元。
- 每手数量：100 份。
- 佣金：3 bps。
- 最低佣金：5 元。
- 卖出税费：0 bps。
- 滑点：2 bps。

这些数值是可配置的模拟情景，不宣称是用户账户或当前市场的真实收费标准。参数只能在创建账户组时设置，随后与配置哈希一起永久锁定。两个账户必须使用同一份费用配置，保证比较口径一致。

## 6. 数据模型

在现有 finance_lab.duckdb 中新增独立命名空间的四张表，不修改现有整理行情表的语义。

### 6.1 paper_accounts

每个账户一行，创建后不可更新：account_id、portfolio_id、symbol、instrument_kind、strategy、strategy_parameters_json、initial_cash、lot_size、commission_bps、minimum_commission、sell_tax_bps、slippage_bps、config_hash、created_at、created_market_date、engine_version。

account_id 必须唯一且可稳定复现。config_hash 只标识锁定参数，不设全局唯一约束，因为不同账户组可以使用相同参数。created_market_date 是创建时通过健康检查的最新行情日期，用来禁止历史回填。

### 6.2 paper_runs

每个账户、每个实际处理的行情日期一行：run_id、batch_id、account_id、trade_date、status、started_at、completed_at、dataset_id、curated_sha256、manifest_generated_at、data_health_json、event_count、state_hash。

数据库唯一约束为 account_id 加 trade_date。status 只允许 committed；失败运行不写入业务表，而由 CLI 生成独立错误产物。没有新日线属于 no-op CLI 结果，不写伪造的交易日运行记录。

### 6.3 paper_events

事件是账本的权威事实来源：event_id、run_id、account_id、trade_date、sequence_no、event_type、signal_date、order_id、action、quantity、reference_price、execution_price、notional、commission、tax、slippage_cost、cash_after、shares_after、close_price、equity_after、drawdown_after、reason_code、payload_json、previous_event_hash、event_hash、created_at。

同一运行中的 sequence_no 从 1 连续递增。账户首个事件的 previous_event_hash 使用账户配置哈希作为创世值；后续 event_hash 覆盖规范化事件内容与 previous_event_hash，形成每个账户独立的哈希链。审计时还要比较 ACCOUNT_CREATED 事件中的配置与 paper_accounts，防止只修改账户配置而不触发告警。

允许的核心事件类型为 ACCOUNT_CREATED、ORDER_FILLED、ORDER_SKIPPED、VALUATION、SIGNAL_GENERATED 和 ORDER_CREATED。事件字段不适用时使用数据库 NULL，不以空字符串表达缺失。

### 6.4 paper_state

每个账户一行的派生缓存：account_id、last_trade_date、cash、shares、last_close、equity、equity_peak、drawdown、last_target_position、pending_order_json、last_event_hash、updated_at。

该表允许在事务中更新，但不能被视为独立事实。paper-status 和每次 paper-run 在使用状态前，必须从完整事件序列重建状态，并与缓存逐字段比较。事件量在本地双账户范围内足够小，v0.9 不使用只检查尾部的快捷模式。审计不通过时拒绝继续。

## 7. 策略与订单语义

所有信号均在交易日 D 收盘后计算，只使用截至 D 收盘的 close 序列：

- SMA：20 日均线严格大于 60 日均线时目标仓位为 1，否则为 0。
- 动量：D 日收盘价相对 120 根日线前收盘价的收益严格大于 0 时目标仓位为 1，否则为 0。

指标尚未预热完成时不是空仓信号，而是 NOT_READY；不得创建订单。SMA 至少需要 60 根有效收盘数据，动量至少需要 121 根有效收盘数据。

目标仓位只允许 0 或 1。last_target_position 在账户创建时为 NULL；NOT_READY 不改变它。每个有效信号都记录目标仓位并更新 last_target_position，但只有目标仓位相对上一有效目标发生变化时才允许创建订单。目标从 0 变为 1 时生成全仓买入订单；从 1 变为 0 时，如有持仓则生成全部卖出订单。一个账户最多有一笔待成交订单。

系统不预先猜测 D+1 的日历日期。下一根经过验证、日期严格晚于 D 的日线就是订单的候选成交日。

## 8. 模拟成交与估值

候选成交日存在有限且大于零的 open 与 close 时，按以下透明假设模拟：

- 买入执行价为 open 乘以 1 加滑点率。
- 卖出执行价为 open 乘以 1 减滑点率。
- 买入数量是在支付佣金后现金可承担的最大整数手。实现应先计算候选手数，再向下校正，直到成交金额加佣金不超过现金。
- 如果现金不足一手，记录 ORDER_SKIPPED，原现金和份额不变，待成交订单关闭。last_target_position 仍保留为 1，因此系统不会每天自动重试；只有目标先变回 0、以后再次变为 1 时才产生新的买入订单。
- 卖出数量为账户全部份额；费用为佣金加卖出税费假设。
- 滑点成本单独记录，同时避免在费用中重复扣除。账户现金使用含滑点的执行价结算。
- 成交后按当日 close 估值：equity 等于 cash 加 shares 乘以 close。

v0.9 不使用当日 high、low 或最终 volume 判断开盘能否成交，因为这些是开盘时未知的事后信息。报告必须说明：固定滑点开盘成交只是模拟假设，并不代表真实盘口一定能够成交。

open、close、费用或计算结果出现 NaN、无穷、非正权益、负现金、负份额或非整手份额时，整根日线的处理失败并回滚。

## 9. 初始化和每日数据流

### 9.1 paper-init

paper-init 不联网。它读取现有整理数据和最新数据清单，要求 sh.510300 文件健康、来源上下文明确、文件 SHA-256 在读取前后保持一致，且不存在目标标的的上游错误或陈旧警告。

初始化在一个事务中完成：

1. 校验账户组尚不存在及参数有效。
2. 将当前最新行情日记为 created_market_date。
3. 为两个账户写入 ACCOUNT_CREATED 和当日 VALUATION，现金为初始资金、份额为零。
4. 使用截至 created_market_date 的历史 close 计算初始信号。
5. 指标已预热且目标为持仓时，创建等待下一根真实日线的订单；目标为空仓时只记录信号。
6. 写入账户、事件、运行记录和状态缓存并提交。

初始化不会产生任何历史成交，也不会计算账户创建日前的模拟收益。

### 9.2 paper-run

paper-run 不联网，只消费已经存在的规范 Parquet。运行顺序为：

1. 获取本地独占运行锁，再连接 DuckDB。
2. 生成并验证数据清单、目标文件完整 SHA-256 和目标标的数据健康上下文。
3. 审计账户配置、事件哈希链和状态缓存。
4. 找出 last_trade_date 之后的所有新日线并按日期升序处理。
5. 对每根日线、两个账户执行：处理旧待成交订单、收盘估值、计算新信号、创建新待成交订单。
6. 每根日线作为一个原子批次同时提交两个账户，确保比较日期同步。任何一个账户失败，该日两个账户都不提交；前面已经成功提交的日期不撤销。
7. 处理完所有新日线后生成报告。报告生成位于数据库提交之后；若报告生成失败，已提交账本保持有效，重试命令必须走 no-op 路径并安全地补生成报告。

如果用户漏跑多天，系统逐根补处理所有未处理日线，使 D 日订单仍在 D 后第一根真实日线开盘成交，而不是错误地使用最新一天开盘价。

同一账户和行情日期的唯一约束加事务共同保证幂等。重复运行时若没有新日线，返回 no-op 并重新生成当前状态报告，不新增事件或费用。no-op 只刷新 latest 入口；已经存在的归档产物保持不变。

### 9.3 一键脚本

run_paper_trading.cmd 面向 Windows 初学者，严格串行执行：

1. 使用现有 fetch 流程更新行情。
2. 运行 manifest 健康检查。
3. 运行 paper-run。
4. 运行 paper-status 并打开最新 HTML。

任一步返回非零退出码，脚本立即停止，不打印成功提示。CLI 的 paper-run 保持离线和确定性，便于测试与复现。

## 10. 数据健康门禁

创建账户或处理新日线前必须满足：

- 目标整理文件包含规范字段，symbol 单一且为 sh.510300。
- adjustment、source、volume_unit 和 ingested_at 有效且一致。
- 价格、日期和主键校验通过。
- 文件 error_codes 为空。
- 文件 warning_codes 为空。
- 目标标的 upstream_errors 为空。
- business_days_stale 不超过命令采用的固定阈值。
- 读取前后的文件 SHA-256 与清单一致。
- 新 trade_date 严格递增，不早于账户 created_market_date。

全局清单中与目标标的无关的警告写入报告，但不阻止模拟盘。目标标的任何警告都采取保守策略：不成交、不生成新信号、不改变状态，并返回非零退出码和结构化错误报告。

## 11. 并发、事务与恢复

- paper-init 和 paper-run 使用 data/paper_trading.lock 的跨进程独占文件句柄锁。paper_lock.py 在 Windows 使用 msvcrt.locking，在 POSIX 使用 fcntl.flock；进程退出或句柄关闭时由操作系统释放，不使用容易遗留的单纯存在性标记。
- 获取锁失败时立即提示已有运行正在进行，不等待、不强行删除锁。
- DuckDB 事务包含同一行情日的两个账户事件、运行记录和状态缓存。
- 异常发生在提交前时全部回滚；异常发生在某日提交后、下一日处理前时，重启后从最后已提交日期继续。
- 唯一约束防止并发或重试导致重复运行；冲突必须转换成清晰的幂等或并发错误，不暴露难懂的数据库堆栈。
- 事件哈希链、事件末值与 paper_state 不一致时进入只读故障状态。paper-status 输出审计失败原因，paper-run 拒绝继续。
- v0.9 不提供自动修复、删除、重写历史或跳过坏事件功能。

## 12. 命令行接口

新增命令的稳定用户语义：

- finance-lab paper-init：创建默认账户组。支持 portfolio、initial-cash、lot-size、commission-bps、minimum-commission、sell-tax-bps、slippage-bps 和 stale-after-business-days 参数。
- finance-lab paper-run：处理指定账户组的全部未处理日线。支持 portfolio 和 stale-after-business-days 参数。
- finance-lab paper-status：只读审计并生成指定账户组的当前报告。支持 portfolio 参数。

所有命令输出 UTF-8 JSON 摘要并使用非零退出码表达失败。路径、账户组名、SHA-256 和数据库枚举值均在进入文件名、SQL 或报告前校验。SQL 只使用参数绑定，不拼接用户输入。

## 13. 报告与产物

每次成功初始化或至少提交一根新日线的 paper-run 后，生成带账户组、最新行情日期和稳定上下文哈希的归档产物。no-op 与 paper-status 只刷新当前 latest 入口，不创建新的归档版本：

- HTML：中文双账户仪表盘。
- JSON：账户配置、数据血缘、当前状态、指标、检查结果和最新事件摘要。
- CSV：逐日权益与全部成交明细。
- PNG：两个账户净值与回撤图。

HTML 至少展示：模拟盘醒目标识、非投资建议、最新行情日期、来源、清单时间、dataset_id、完整 curated SHA-256、健康状态、锁定参数、现金、份额、持仓市值、权益、累计收益、最大回撤、交易次数、累计佣金、累计税费、累计滑点、当天信号、当天成交、待成交订单和原因代码。

报告并排展示两个账户，但不自动宣布赢家、不调整参数，也不删除落后或亏损账户。报告文件名使用白名单清洗并验证最终路径仍位于 outputs。latest 入口可以覆盖为当前指针，带日期和上下文哈希的归档产物不得静默覆盖不同内容。

## 14. 不变量与验收测试

### 14.1 引擎单元测试

- SMA 和动量预热边界，以及 NOT_READY 不被当成空仓。
- 信号日严格早于成交日。
- 下一根真实日线而非自然日用于成交。
- 买入最大整数手、最低佣金、卖出费用和滑点只扣一次。
- 现金不足一手时 ORDER_SKIPPED 且状态保持一致。
- open 或 close 非有限、非正以及极端费用被明确拒绝。
- 每个事件后的现金、份额、权益和回撤计算正确。

### 14.2 存储与恢复测试

- 同一账户与 trade_date 只允许一条 committed run。
- 同日重复 paper-run 不新增事件、不扣第二次费用。
- 两个账户的资金、订单和事件完全隔离。
- 事务中途异常不留下部分事件或部分状态。
- 漏跑多日后逐根重放结果与每日运行结果一致。
- 事件重建状态与 paper_state 完全一致。
- 修改、删除或调换事件后哈希链审计失败并拒绝运行。
- 两个并发进程中只有一个获得运行权。

### 14.3 数据与边界测试

- 数据无更新时 no-op，不伪造成交。
- 目标文件陈旧、来源失败、字段缺失、日期倒退、重复主键或读取中被替换时拒绝运行。
- 初始化不生成 created_market_date 之前的成交或收益。
- 首次待成交订单只在创建日期之后第一根新日线成交。
- 多日补处理时每个信号只使用当时可见的历史切片。
- 两个账户在每个 committed trade_date 上同步，任一失败则该日均不提交。

### 14.4 报告、CLI 和回归测试

- HTML 动态内容转义，安全账户组名不能逃出 outputs。
- JSON 不输出 NaN 或 Infinity，CSV 列与文档一致。
- Windows 脚本在 fetch、manifest、paper-run 或 paper-status 任一步失败时返回非零。
- paper-status 只读，不改变事件、状态或数据库时间戳。
- Python 3.11、3.12、3.13 CI 全部通过。
- 现有 pytest、Ruff 和 mypy 全部通过。

## 15. 兼容性与发布

- 新表以 schema_version 管理；v0.9 首次运行只创建缺失表，不重写现有行情或实验数据。
- 现有 demo、fetch、manifest、account、backtest、experiment、walk-forward、cost-stress、parameter-test、execution-test 和 compare 命令保持兼容。
- 更新 README 功能表、中文第九课、运行脚本、打包必需路径和发布验证。
- 发布包不包含真实行情、DuckDB 模拟账户或用户输出，只包含源码、配置模板、测试和文档。
- v0.9.0 发布前执行干净环境安装、全量测试、CLI 帮助、Windows 脚本语法检查、源码包解压烟测以及 GitHub Actions 验证。

## 16. 后续版本候选

以下内容明确留到 v0.9 稳定之后再单独设计：交易所交易日历、分红和除权现金流、多个 ETF 的共享资金账户、真实盘口或分钟级成交代理、网页仪表盘、自动定时运行、通知，以及任何券商连接。
