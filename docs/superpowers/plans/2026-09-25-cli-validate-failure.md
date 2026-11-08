# P2.2a `validate` 失败语义实施计划

设计依据：[P2.2 命令失败语义](../specs/2026-09-25-cli-failure-semantics-design.md)。本批只处理只读校验命令。

1. 在临时目录写失败测试：无整理文件、仅临时文件、损坏的正式 Parquet、缺 `trade_date` 的文件；记录现状并确认测试因预期问题失败。
2. 修改 `validate_curated_data`：逐文件给出安全错误，空目录不得报告零错误；返回 `status`、`stage`、`data_committed=false` 和明确下一步。CLI 由报告状态决定退出码，不删除任何数据。
3. 跑定向和全套回归、Ruff、mypy、Markdown 链接与差异检查。记录结果和未覆盖范围，不合并或发布。

状态：本批已通过本地验收。全套收集 690 项：689 项通过、1 项因系统不允许创建符号链接跳过；Ruff、mypy（45 个源文件）、compileall、pip check、46 份 Markdown 链接与 `git diff --check` 通过。只读运行正式项目 `validate` 返回 `passed`：两份整理文件各 2,120 行，0 错误、0 警告。未联网、未执行真实抓取或模拟交易，未合并/推送/发布。其他命令失败语义留在后续独立小批。
