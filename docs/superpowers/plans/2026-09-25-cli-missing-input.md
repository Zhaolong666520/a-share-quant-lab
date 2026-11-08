# P2.2c 缺少输入文件时的 CLI 错误

依据：[P2.2 命令失败语义设计](../specs/2026-09-25-cli-failure-semantics-design.md)。本批只处理输入检查发生在业务写入之前的两条路径：`manifest` 缺整理行情、`account` 缺标的配置。

1. 用临时空项目写失败测试：命令须非零退出，输出机器可读的 `error_code`、`stage`、`data_committed=false` 与 `next_action`，且不输出 Python 原始异常；测试确认没有生成业务报告。
2. 在 CLI 为这两个入口加入最小输入检查，缺失时统一返回安全结构化结果；正常路径保持原行为。避免将整个 CLI 的所有 `FileNotFoundError` 误归类为同一阶段。
3. 跑定向和全量测试、Ruff、mypy、编译、依赖、Markdown 链接及差异检查。记录准确结果；其它命令的缺文件与写入失败留给后续批次。

状态：已完成。本批为 `manifest` 和 `account` 加入缺少输入的预检，返回退出码 3；
未扩展到其他命令或写入失败。定向测试 16 个通过；全量收集 702 个，701 个通过、
1 个因 Windows 符号链接权限跳过。Ruff、mypy、编译、`pip check`、Markdown 链接和
`git diff --check` 均通过。所有缺文件测试仅使用临时项目。
