# P2.2n：模拟盘账本提交后的报告失败

## 提交边界

`paper-init` 在初始化双账户账本后写报告；`paper-run` 逐交易日提交双账户账本，全部处理后写报告。`paper-run` 没有新交易日时是 no-op，`paper-status` 只读审计账本；这两种情况报告失败不代表本次新增账本记录。

## 失败契约

- 报告生成阶段抛出的异常包装为 `PaperReportWriteError`，仅向 CLI 与自动化传递账户组、操作状态、已处理日期和 `ledger_committed`；底层异常保留为内部原因，不输出给用户。
- `paper-init`、`paper-run`、`paper-status` 返回非零及单个失败 JSON，包含 `stage=write_paper_report`、`ledger_committed`、`report_complete=false`、`current_report_state=unknown`。
- 修复写入问题后，运行 `paper-status` 重新审计并生成报告；不得删除或重置账本，不得把报告失败的交易日当成未入账而再次初始化。
- `paper-daily` 把账本步骤记为成功、报告步骤单独记为失败，归档和 latest 状态均保留 `ledger_committed`、已处理日期及安全的下一步提示；返回非零。

## 验证

只用临时项目和合成行情注入报告生成异常。初始化后报告失败仍可读到账本；处理新交易日后报告失败会列出已提交日期，重新运行则为 no-op 且不重复入账；no-op 报告失败标为本次未提交。自动化状态持久化记录 `paper_report` 失败阶段和已提交标记。未连接券商或真实账户。

## 后续

其他研究报告写入错误以及自动化状态文件自身写入失败仍需单独验收；此处不宣称所有故障路径已经覆盖。
