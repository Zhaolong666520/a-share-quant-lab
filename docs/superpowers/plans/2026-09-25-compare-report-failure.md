# P2.2w：多策略对比报告写入失败

## 边界

`compare` 读取已发布的整理行情和数据清单，在固定切分及成本假设下生成多策略对比报告；不更新行情，也不执行真实交易。本批仅处理报告写入阶段的 `OSError`，不调整策略或实验结论。

## 失败契约

- 写入失败时返回码 1，输出单个安全 JSON：`error_code=report_write_failed`、`stage=write_report`、`data_committed=false`、`report_complete=false`。
- 多文件报告可能留有部分或上次运行的文件，因此 `current_report_state=unknown`；不打印“多策略对比报告已生成”。修复写入问题后重试，再用 `report-verify` 核验完成标记。
- 批量 `compare-all` 仍沿用已有的部分报告诊断，不把单项失败误报为整批成功。

## 验证

在临时项目和合成行情上，对指标 JSON 的原子替换注入磁盘异常。单标的命令返回非零及安全诊断；已发布整理行情字节不变，未产生新的报告完成标记。定向测试和完整 pytest 通过（仅一个 Windows 符号链接权限相关测试跳过）；Ruff、mypy、Markdown 链接及 `git diff --check` 通过。

## 后续

此组单标的历史研究报告的写入失败路径已逐类验收。P2.2 仍需核查其他缺文件与来源/状态持久化分支；测试继续使用临时合成数据。
