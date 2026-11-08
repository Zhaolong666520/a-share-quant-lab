# P2.2t：成本压力报告写入失败

## 边界

`cost-stress` 读取已发布的整理行情，对预设的假设成本情景计算回顾性敏感性诊断并生成多文件报告；不更新行情，也不执行真实交易。本批仅处理报告写入阶段的 `OSError`。

## 失败契约

- 写入失败时返回码 1，输出单个安全 JSON：`error_code=report_write_failed`、`stage=write_report`、`data_committed=false`、`report_complete=false`。
- 多文件报告可能留有部分或上次运行的文件，因此 `current_report_state=unknown`；不打印“成本压力报告已生成”。修复写入问题后重试，再用 `report-verify` 核验完成标记。
- 批量 `cost-stress-all` 仍沿用已有的部分报告诊断，不把单项失败误报为整批成功。

## 验证

在临时项目和合成行情上，对指标 JSON 的原子替换注入磁盘异常。单标的命令返回非零及安全诊断；已发布整理行情字节不变，未产生新的报告完成标记。定向测试和完整 pytest 通过（仅一个 Windows 符号链接权限相关测试跳过）；Ruff、mypy、Markdown 链接及 `git diff --check` 通过。

## 后续

参数、执行可行性与策略比较等单标的研究命令仍需逐类验证报告写入失败。测试继续使用临时合成数据。
