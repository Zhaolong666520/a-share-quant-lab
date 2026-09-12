# 贡献指南

感谢你愿意帮助 A-Share Quant Lab 变得更可靠。这个项目优先接受能够提升**可复现性、可审计性和教学清晰度**的改进。

English-speaking contributors can start with the [`English quick start`](docs/quickstart.en.md). Issues and pull requests are welcome in either Chinese or English.

## 提交前

1. 先搜索现有 Issue，确认问题尚未被讨论。
2. Bug 请附最小复现、Python 版本、操作系统和完整错误信息。
3. 新研究想法请先写清假设、固定参数和验证方式，避免在看完结果后反复调参。
4. 不要提交第三方行情文件、API 密钥、账户信息或券商凭证。

## 本地开发

```powershell
.\scripts\setup.ps1
.\verify.cmd
```

`verify.cmd` 会运行 Ruff、mypy、pytest 和编译检查。提交 PR 前请确保全部通过。

## PR 要求

- 从最新 `main` 创建短生命周期分支。
- 每个 PR 只解决一个明确问题，并说明影响范围。
- 行为变化要补测试；命令、输出或假设变化要更新 README、课程或实验记录。
- 不得引入未来信息、选择性隐藏亏损结果，或把假设费率描述为当前真实标准。
- Conventional Commit 示例：`fix(data): reject mixed adjustment values`。

## 数据与实验纪律

所有贡献都必须遵守 [`AGENTS.md`](AGENTS.md)。尤其注意：原始数据不可静默覆盖、正式数据必须有来源与指纹、信号必须延迟执行、样本外结果不得用于回头修改同一实验。

## 适合新贡献者的问题

可以从带有 `good first issue`、`documentation` 或 `tests` 标签的问题开始。若你不确定方案，先开 Issue 讨论即可。
