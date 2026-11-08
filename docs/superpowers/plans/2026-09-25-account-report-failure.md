# P2.2q：教学账户回测报告写入失败

## 边界

`account` 读取已发布行情、计算教学账户账本并生成报告，不向前向模拟盘数据库提交账本，也不执行真实交易。输入读取失败与报告写入失败分开处理；本批仅处理后者。

## 失败契约

- 报告写入发生 `OSError` 时，返回码 1，输出单个安全 JSON，含 `error_code=report_write_failed`、`stage=write_report`、`data_committed=false`、`report_complete=false`。
- 多文件报告可能保留部分或上次运行的输出，标记 `current_report_state=unknown`，不打印“账户账本报告已生成”。修复后重试并用 `report-verify` 核验。
- 同一写入异常在 `account-all` 中继续由既有批量部分完成诊断处理，不误报整批成功。

## 验证

临时项目、合成 ETF 行情与配置中，向指标 JSON 原子替换注入磁盘异常。`account` 返回非零及安全诊断；整理行情 Parquet 字节未变化，未创建前向模拟盘数据库，也无新完成标记。定向测试、Ruff 和 mypy 通过。

## 后续

实验、滚动、成本、参数、执行可行性和策略比较等单标的研究命令仍需逐类验证报告写入失败。所有测试继续使用临时合成数据。
