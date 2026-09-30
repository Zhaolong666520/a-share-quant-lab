# P2.2e 标的配置损坏时的 CLI 错误

依据：[P2.2 命令失败语义设计](../specs/2026-09-25-cli-failure-semantics-design.md)。本批覆盖 `account` 和七个批量研究命令读取 `config/instruments.json` 前的格式错误，不改变配置文件内容或交易数据。

1. 在临时项目写失败测试：坏 JSON、空列表和缺少必需字段均返回非零、`invalid_instruments_config`、`stage=load_config`、`data_committed=false` 和可执行的 `next_action`；不泄露原始配置或底层异常，不生成报告。
2. 复用已有配置预检，在业务处理之前尝试读取并解析配置，只捕获确定属于格式/结构错误的异常；缺文件仍使用原错误码，其他 I/O 错误不误归类。
3. 运行定向与全量测试、Ruff、mypy、编译、依赖、Markdown 链接和差异检查，记录准确结果。其他命令、行情缺失及写入失败留待后续批次。

状态：已完成。账户与七个批量研究命令预检坏 JSON、空列表和缺必需字段，
返回退出码 3、`invalid_instruments_config`，不输出底层异常或生成报告。
定向测试 47 个通过；全量收集 733 个，732 个通过、1 个因 Windows 符号链接
权限跳过。Ruff、mypy、编译、`pip check`、Markdown 链接及 `git diff --check`
均通过。所有配置测试只使用临时项目。
