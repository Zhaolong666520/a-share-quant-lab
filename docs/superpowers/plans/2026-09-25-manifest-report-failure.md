# P2.2p：数据健康报告写入失败

## 边界

`manifest` 只读取已发布整理行情，并生成 JSON/HTML 健康报告；本次命令不提交行情或账本。读取输入失败与报告写入失败是不同阶段，本次仅处理后者。

## 实施

- JSON 与 HTML 分别通过临时文件原子替换，完成标记在两者写入后记录哈希；`report-verify` 可核对报告是否完整。
- 写入阶段发生 `OSError` 时，CLI 返回非零、单个结构化失败 JSON，含 `stage=write_report`、`data_committed=false`、`report_complete=false`、`current_report_state=unknown`，不打印“已生成”或底层异常。
- 不删除、覆盖或修复输入行情；部分报告文件可能存在，不能仅凭文件存在判断本次成功。

## 验证

用临时目录保存合成 Parquet，向 HTML 原子替换注入 `OSError`：CLI 返回失败、无新完成标记，输入 Parquet 字节不变；正常路径的完成标记可被 `report-verify` 核验。未抓取真实行情。

## 后续

其他单标的研究报告命令的写入失败仍需逐类区分报告阶段与输入阶段；不将读取失败误标为报告写入失败。
