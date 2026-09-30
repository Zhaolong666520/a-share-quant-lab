# P2.2o：每日模拟盘状态文件写入失败

## 边界

`paper-daily` 先执行行情更新、数据健康和模拟盘操作，最后依次写不可变运行归档与 latest 状态。归档或 latest 写失败不能倒推出之前业务步骤没有发生；归档成功、latest 失败也不能算整个工作流成功。

## 失败契约

- 状态目录准备、归档写入、latest 写入失败分别标记 `prepare_status_paths`、`write_status_archive`、`write_latest_status`，返回非零及单个安全 JSON。
- 同时暴露已知的 `market_data_committed`、`ledger_committed`、`processed_dates`。未知状态为 `null`，不能擅自标为未提交。
- 只有归档写入函数已成功返回，才设置 `status_archive_confirmed=true` 并提供归档路径；latest 写入失败时 `latest_status_state=unknown`，旧 latest 不应被当作本次状态。
- 下一步先检查归档和 `update_summary.json`，修复写入环境，再运行 `paper-status` 审计账本；确认状态后才决定是否重跑每日任务。底层路径或磁盘异常消息不输出给用户。

## 验证

使用临时目录、合成操作结果和故障注入验证归档写失败与 latest 写失败。前者不谎称归档已确认；后者保留已确认归档，且旧 latest 内容不变。CLI 返回单个结构化失败对象，不泄露内部异常。未调用真实行情，也未修改真实计划任务。

## 后续

若进程被强制终止或操作系统无法打印错误，本命令无法保证错误 JSON 出现；仍需依赖账本审计及运行目录人工核对。其他研究报告写入故障继续分批处理。
