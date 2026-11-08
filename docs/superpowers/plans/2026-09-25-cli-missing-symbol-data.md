# P2.2f 单标的研究缺行情的 CLI 错误

依据：[P2.2 命令失败语义设计](../specs/2026-09-25-cli-failure-semantics-design.md)。本批覆盖 `account`、`backtest`、`experiment`、`walk-forward`、`cost-stress`、`parameter-test`、`execution-test`、`compare` 请求标的没有已发布整理行情文件的情况。批量命令的部分报告与写入失败另行处理。

1. 用临时空项目测试八个命令：请求标的缺行情时退出码 3，返回 `missing_symbol_data`、`stage=load_curated`、`data_committed=false` 和补救建议，不生成报告；账户命令提供有效的临时标的配置。
2. 在现有行情锁与隔离状态检查之后，按规范标的文件名确认请求文件存在。保留隔离状态优先级，不将非法标的、权限或损坏文件误归为缺文件。正常路径继续由原读取逻辑校验。
3. 运行定向、全量及静态检查，记录准确结果。所有测试只用临时项目，不下载或修改真实行情。

状态：已完成。八个单标的命令缺少请求标的行情时统一返回退出码 3 和
`missing_symbol_data`；隔离状态在缺文件判断前被核验。定向测试 42 个通过；
全量收集 742 个，741 个通过、1 个因 Windows 符号链接权限跳过。
Ruff、mypy、编译、`pip check`、Markdown 链接及 `git diff --check` 均通过。
所有缺行情和隔离测试只使用临时项目。
