# P2.2l：离线演示与单标的回测报告写入失败

## 边界

仅处理 `demo` 和单标的 `backtest` 在报告写入过程中发生的 `OSError`。这两个命令本次运行都不提交真实行情或账户账本，因此 `data_committed=false`。读取行情、参数错误和其他研究命令不归入此错误码。

## 失败契约

- 返回码为 1；标准输出只有结构化 JSON，没有“报告已生成”的成功信息。
- `status=failed`、`error_code=report_write_failed`、`stage=write_report`、`report_complete=false`。
- 多文件报告可能残留部分文件或上一轮文件，因此 `current_report_state=unknown`；修复磁盘问题后重试，并使用 `report-verify` 核验。
- 不在输出中暴露底层异常字符串。已发布的整理行情不因报告写入失败而变化。

## 验证

使用临时目录和合成数据，在 JSON 文件原子替换处注入 `OSError`；两条命令均返回上述失败结构，不发布新完成标记，不泄露故障字符串。单标的回测输入 Parquet 字节不变。定向测试、Ruff 与 mypy 通过。

## 后续

批量研究报告、`all` 的数据提交后报告失败、模拟盘账本提交后报告失败需要分别定义并测试提交状态，不复用本条 `data_committed=false` 语义。
