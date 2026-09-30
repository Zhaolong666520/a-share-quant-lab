# P2.2 命令失败语义设计

日期：2026-09-25。状态：分批实施；`validate`、参数预检、`manifest`、八个需要配置的账户/批量研究命令的缺失或格式损坏配置路径、八个单标的命令的缺行情路径、七个批量命令的部分报告诊断、八类历史研究报告逐文件原子替换与完成标记、只读 `report-verify`，以及 `demo`/单标的 `backtest`、`all`、模拟盘、`manifest`、`account`、`experiment`、`walk-forward`、`cost-stress`、`parameter-test`、`execution-test` 与 `compare` 的报告写入失败诊断、`paper-daily` 状态文件写入失败诊断及推进门禁、`fetch`/`all` 行情更新中断诊断、十点故障矩阵与新摘要的来源诊断写入边界已处理。

## 目标

命令退出码必须反映本次操作是否成功。失败输出需要说明失败阶段、数据是否已提交及下一步操作，不能因为生成了一段 JSON 就被视为成功。验证使用临时项目和合成文件，不抓取真实行情、不修改正式数据。

## 第一小批：`validate`

`validate` 是只读命令。无已发布整理 Parquet 时报告 `no_curated_files`，非零退出；只剩临时发布文件也按无正式文件处理，不读取或删除临时文件。无法读取的正式 Parquet 按该文件记录 `unreadable_parquet`，继续检查其他文件；缺少规范字段按现有字段校验记录，不能因 `trade_date` 缺失直接抛出未分类异常。所有失败输出均包含 `status=failed`、`stage`、`data_committed=false` 和安全的 `next_action`；不包含原始数据正文或底层异常字符串。文件正常且无错误时返回 0。警告不直接视为损坏。

## 后续小批

1. CLI 参数与输入：非法日期/参数、缺少配置及文件分别验证退出码和安全错误；不能在解析失败后产生业务输出。
2. 会写入的研究/报告命令：模拟磁盘写入失败与中途异常，区分未提交、数据已提交但报告失败；给出准确的重试/恢复动作，不重复记账。
3. `fetch`、`all`、`paper-daily`：沿用 P2.1 的来源结果，不把部分来源失败或旧数据当作本次完整成功；检查状态持久化和自动化退出码。

每批次独立测试和验收；本设计不授权发布、安装计划任务、联网获取行情或切换恢复候选。

`demo` 与单标的 `backtest` 的报告写入失败已按[实施记录](../plans/2026-09-25-cli-report-write-failure.md)覆盖；`all` 的行情已提交、报告失败分支见[单独实施记录](../plans/2026-09-25-cli-all-report-failure.md)；模拟盘账本提交后报告失败见[实施记录](../plans/2026-09-25-paper-report-failure.md)；自动化状态文件写入失败见[实施记录](../plans/2026-09-25-paper-status-write-failure.md)；`manifest` 报告失败见[实施记录](../plans/2026-09-25-manifest-report-failure.md)；教学账户回测报告失败见[实施记录](../plans/2026-09-25-account-report-failure.md)；样本外实验报告失败见[实施记录](../plans/2026-09-25-experiment-report-failure.md)；滚动检验报告失败见[实施记录](../plans/2026-09-25-walk-forward-report-failure.md)；成本压力报告失败见[实施记录](../plans/2026-09-25-cost-stress-report-failure.md)；参数稳健性报告失败见[实施记录](../plans/2026-09-25-parameter-report-failure.md)；执行可行性报告失败见[实施记录](../plans/2026-09-25-execution-report-failure.md)；多策略对比报告失败见[实施记录](../plans/2026-09-25-compare-report-failure.md)。其他缺文件与来源/状态分支仍需逐类验收。

`fetch` 在行情更新中途异常的安全诊断见[实施记录](../plans/2026-09-26-cli-fetch-interrupted.md)；`all` 复用此诊断且停止报告阶段的分支见[实施记录](../plans/2026-09-26-cli-all-update-interrupted.md)。每日自动化及其他来源/状态分支仍需逐类验收。

`fetch`/`all` 的初始状态、原始快照、整理行情、DuckDB 同步和最终提交十点故障矩阵见[实施记录](../plans/2026-09-26-update-interruption-matrix.md)。

`paper-daily` 的安全失败留证、提交状态不确定性和完整成功推进门禁见[实施记录](../plans/2026-09-26-paper-daily-safe-failure.md)；运行或初始化结果的状态与处理日期契约见[后续实施记录](../plans/2026-09-26-paper-operation-result-contract.md)。

`fetch`/`all` 新生成摘要的来源异常、阶段与计数净化见[实施记录](../plans/2026-09-26-source-diagnostics-boundary.md)。

`validate` 与数据清单对畸形、混合表示和非零时间日线日期的失败分类见[实施记录](../plans/2026-09-26-daily-date-validation.md)。
