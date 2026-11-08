# P2.2d 批量研究命令缺标的配置

依据：[P2.2 命令失败语义设计](../specs/2026-09-25-cli-failure-semantics-design.md)。本批仅处理 `account-all`、`experiment-all`、`walk-forward-all`、`cost-stress-all`、`parameter-test-all`、`execution-test-all`、`compare-all` 缺少 `config/instruments.json` 的情况。`all` 含联网更新，不在本批。

1. 扩充临时空项目测试，逐一确认七个命令非零退出，返回 `missing_instruments_config`、`stage=load_config`、`data_committed=false`、`next_action`，不生成业务报告。
2. 在批量研究入口的业务执行之前共用一次配置存在性预检，复用上一批安全错误格式；正常路径保持不变。配置损坏、标的缺行情及报告写入失败另行处理。
3. 跑定向与全量测试、Ruff、mypy、编译、依赖、Markdown 链接及差异检查，记录准确结果。

状态：已完成。七个批量研究命令缺配置时统一在业务执行前返回退出码 3，
不生成部分报告。定向测试 23 个通过；全量收集 709 个，708 个通过、
1 个因 Windows 符号链接权限跳过。Ruff、mypy、编译、`pip check`、
Markdown 链接和 `git diff --check` 均通过。缺文件测试只使用临时项目。
