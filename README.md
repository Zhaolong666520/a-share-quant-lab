# Finance Lab：A股数据与回测入门项目

这是一个已经替你搭好骨架的学习项目。它能完成：

- 使用 AKShare 获取A股指数和ETF日线；
- 使用 BaoStock 作为第二来源进行抽样核对；
- 保存不可变的原始快照、整理后的 Parquet 和 DuckDB 数据库；
- 检查重复日期、价格关系、空值和异常间隔；
- 运行买入持有及双均线基础回测；
- 强制信号次日执行并扣除成本；
- 生成中文 HTML 报告、净值图和 JSON 指标；
- 将固定参数放进多个互不重叠窗口，生成滚动稳定性报告；
- 使用多组假设单边成本，生成交易成本压力报告；
- 在固定成本和窗口下比较九组均线，生成参数稳健性热力图；
- 比较理想开盘成交、统一延迟与事后日线阻塞代理，生成执行可行性压力报告；
- 为每个整理数据文件生成SHA-256、数据集ID、来源、日期区间与陈旧状态；
- 对可交易ETF运行现金、整手份额、最低佣金、卖出税费和滑点账户账本；
- 在断网时使用明确标记的合成数据演示完整流程。

> 本项目仅用于学习，不连接券商、不自动交易、不构成投资建议。

## 第一次使用

项目已经包含独立环境时，直接双击：

```text
run_demo.cmd
```

运行完成后，打开：

```text
outputs\demo_000300_sma_report.html
```

这份报告使用合成数据，只用于学习指标和确认程序正常。

## 下载真实数据

双击：

```text
update_data.cmd
```

程序会尝试从2018年开始下载两个示例标的，保存数据并生成回测报告。网络数据源随时可能
改变；如果一个来源失败，程序会记录原因并尝试另一个来源，不会伪造行情。

真实数据报告位于 `outputs/`，文件名类似：

```text
sh_000300_sma_report.html
sh_510300_sma_report.html
```

## 运行样本外实验

真实数据已经存在后，双击：

```text
run_experiment.cmd
```

它会固定使用2023年1月1日作为切分点，不自动调参，并生成：

```text
sh_000300_sma_split_<实验ID>_report.html
sh_000300_sma_split_<实验ID>_trades.csv
sh_510300_sma_split_<实验ID>_report.html
sh_510300_sma_split_<实验ID>_trades.csv
```

实验ID包含引擎版本、切分日、均线参数、成本和数据指纹，因此不同代码、参数或数据不会互相
覆盖。

阅读 `docs/第二课.md`，了解开发期、样本外期以及为什么不能根据测试结果反复调参。

## 运行滚动稳定性检验

完成固定切分实验后，双击：

```text
run_walk_forward.cmd
```

程序会从2021年开始，按12个月划分互不重叠的历史窗口。20/60参数保持固定，不根据任何窗口
自动调节。报告文件名类似：

```text
sh_000300_sma_walk_<实验ID>_report.html
sh_510300_sma_walk_<实验ID>_report.html
```

阅读 `docs/第三课.md`。注意：这些历史日期已经被我们看过，因此滚动报告是稳定性诊断，不是
全新的、从未观察过的样本外证据。

## 运行交易成本压力测试

双击：

```text
run_cost_stress.cmd
```

程序会固定信号和窗口，仅把假设单边成本依次设为5、10、20、50个基点。报告文件名类似：

```text
sh_000300_sma_cost_<实验ID>_report.html
sh_510300_sma_cost_<实验ID>_report.html
```

这些数字只用于压力测试，不代表当前券商费率、税率或真实滑点。阅读 `docs/第四课.md`，理解
为什么交易次数越多，成本假设越重要。

## 运行参数稳健性检验

双击：

```text
run_parameter_test.cmd
```

程序会固定数据、滚动窗口和5 bps假设成本，比较短均线10/20/30与长均线40/60/90组成的
九组参数。20/60是预先指定的参照组，程序不会自动挑选“最佳参数”。报告文件名类似：

```text
sh_000300_sma_param_<实验ID>_report.html
sh_510300_sma_param_<实验ID>_report.html
```

阅读 `docs/第五课.md`，重点查看正收益组合数、收益中位数、收益范围以及热力图中好结果是否
只出现在孤立格子。本实验是回顾性诊断，不是参数推荐。

## 运行执行可行性压力测试

双击：

```text
run_execution_test.cmd
```

程序固定20/60双均线和5 bps假设成本，比较理想次日开盘、统一额外延迟一个交易日、以及
零成交量/全天单一价格跳空的事后日线阻塞代理。报告文件名类似：

```text
sh_000300_sma_exec_<实验ID>_report.html
sh_510300_sma_exec_<实验ID>_report.html
```

9.5%跳空阈值只是教学压力代理，不是当前涨跌停规则；它使用收盘后才完整获得的当日K线，
不能当作开盘时可知的条件。阅读 `docs/第六课.md`，重点查看收益变化、仓位不同天数、受阻尝试
和逐行一致性检查。三个场景在切分日前使用相同理想仓位，压力只从样本外边界开始。指数本身
不能直接交易。

## 检查数据身份与新鲜度

双击：

```text
run_data_health.cmd
```

它不会联网，而是给当前 `data/curated/` 中每个Parquet文件计算SHA-256，并生成一个稳定的
数据集ID，同时列出来源、日期区间、复权方式、成交量单位、近似滞后工作日以及上次更新失败
信息。报告文件名类似：

```text
dataset_manifest_<数据集ID前12位>_r<检查上下文哈希>_report.html
```

陈旧天数目前只按周一至周五近似，尚未纳入交易所节假日。正式实验前先看这份报告；出现
“提醒”不等于文件损坏，但必须理解并记录提醒原因。

## 运行真实账户账本

确认数据身份后，双击：

```text
run_account.cmd
```

程序只处理配置中标为ETF或股票的标的，指数会明确跳过。默认使用10万元现金、100份一手、
3 bps假设佣金（最低5元）、0 bps假设卖出税费和2 bps假设单边滑点。报告、逐笔成交CSV、
指标JSON和净值图会写入 `outputs/`。所有费率都只是可修改的教学情景，不代表当前真实标准。

阅读 `docs/第七课.md`，重点查看现金、份额、费用与权益是否逐日对上。当前模型尚未处理分红、
除权现金流、部分成交、排队和市场冲击，因此不能用于真实下单。

## 验证项目

双击 `verify.cmd`，它会依次运行代码检查、类型检查、自动测试和编译检查。

## 关于发布压缩包

`scripts/package_release.ps1` 生成的是源码包 `finance-lab-source-v7.zip`。为避免重新分发第三方
行情，它只保留空的 `data/` 和 `outputs/`，不能单独复现实验006的精确历史结果。实验记录中的
数据集ID和文件SHA-256用于核对；本次实际HTML、JSON、CSV和图表作为单独交付物保存在项目外层
`outputs/`。如要在另一台电脑重跑，必须取得相同哈希且有权使用的数据快照。

## 常用命令

在项目目录打开 PowerShell：

```powershell
.\.venv\Scripts\python.exe -m finance_lab.cli demo
.\.venv\Scripts\python.exe -m finance_lab.cli fetch --start 2018-01-01 --end 2026-08-14
.\.venv\Scripts\python.exe -m finance_lab.cli validate
.\.venv\Scripts\python.exe -m finance_lab.cli backtest --symbol sh.000300
.\.venv\Scripts\python.exe -m finance_lab.cli experiment-all --split-date 2023-01-01
.\.venv\Scripts\python.exe -m finance_lab.cli walk-forward-all --first-oos-date 2021-01-01
.\.venv\Scripts\python.exe -m finance_lab.cli cost-stress-all --costs 5,10,20,50
.\.venv\Scripts\python.exe -m finance_lab.cli parameter-test-all --shorts 10,20,30 --longs 40,60,90
.\.venv\Scripts\python.exe -m finance_lab.cli execution-test-all --delay-days 1 --lock-threshold 0.095
.\.venv\Scripts\python.exe -m finance_lab.cli manifest --as-of 2026-08-24
.\.venv\Scripts\python.exe -m finance_lab.cli account --symbol sh.510300
```

## 目录

```text
finance-lab/
├─ AGENTS.md              所有AI共同遵守的项目规则
├─ config/                研究标的配置
├─ data/raw/              原始数据快照，不进入Git
├─ data/curated/          校验后的规范数据，不进入Git
├─ data/finance_lab.duckdb 本地数据库，不进入Git
├─ docs/                  中文学习资料与数据说明
├─ experiments/           实验参数与结论
├─ outputs/               HTML报告、图表和指标
├─ research/              研究员AI的资料与假设
├─ src/                   程序源码
└─ tests/                 自动测试
```

## 当前限制

- 第一阶段只处理日线，不处理分钟、逐笔和实时行情。
- 停牌和单一价格跳空仅有事后日线压力代理，尚未模拟订单队列、部分成交、申购赎回、分红税和冲击成本。
- ETF使用不复权数据，长期收益不等于含分红再投资的真实总回报。
- 数据陈旧度使用工作日近似，尚未接入交易所交易日历。
- 账户账本尚未处理分红、除权现金流、部分成交、排队和市场冲击。
- 双均线只是教学基准，不能据此直接买卖。
- 数据源属于研究级公共接口，没有生产级稳定性保证。

## 推荐学习顺序

1. 阅读 `docs/第一课.md`。
2. 运行离线演示，先看最大回撤和基准对比。
3. 更新真实数据，阅读 `outputs/update_summary.json` 中的数据源状态。
4. 阅读 `docs/第二课.md` 并运行固定切分实验。
5. 不修改参数，先理解为什么“历史表现好”不代表未来赚钱。
6. 阅读 `docs/第三课.md`，双击 `run_walk_forward.cmd`，比较每个历史窗口。
7. 阅读 `docs/第四课.md`，双击 `run_cost_stress.cmd`，观察成本提高后的收益变化。
8. 阅读 `docs/第五课.md`，双击 `run_parameter_test.cmd`，检查结论是否依赖单一参数。
9. 阅读 `docs/第六课.md`，双击 `run_execution_test.cmd`，检查延迟与成交受阻的影响。
10. 阅读 `docs/第七课.md`，先双击 `run_data_health.cmd`，再双击 `run_account.cmd`。
11. 在进入分钟线前，继续完善分红、除权、真实费用规则、订单簿和部分成交约束。
