# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的结构。

## [Unreleased]

### Added

- 上交所 2022–2026 年休市快照：数据健康检查在覆盖期内按交易日计算陈旧度，并记录快照 SHA-256 与公告来源。
- 日历覆盖期外改为明确的周一至周五保守估算提醒；前向模拟盘会将该提醒视为阻断条件。
- 追加式 ETF 现金分红快照、登记日权益、支付日现金入账、迟到快照拒绝和分红审计 CSV。
- 同代码 ETF 份额放缩快照、生效日前持仓调整、零碎份额拒绝、迟到检查和份额调整审计 CSV。
- 可重放的订单生命周期：资金不足延期、最多三次开盘尝试、信号反转取消、超限过期及订单事件 CSV。

### Planned

- 跨代码派送、登记机构零碎份额分配和跨平台运行脚本。

## [0.9.0] - 2026-08-31

### Added

- 仅用于本地学习的前向模拟盘：固定 sh.510300 ETF、SMA 20/60 与 120 日动量两个隔离账户。
- 追加式 DuckDB 事件账本、哈希链审计、可重建状态缓存、跨进程运行锁和同日双账户原子提交。
- `paper-init`、`paper-run`、`paper-status` 命令，以及 `run_paper_trading.cmd` Windows 日常入口。
- 确定性 HTML/JSON/CSV/PNG 审计报告，报告中的费用和滑点均明确为假设情景。

### Security

- 模拟盘只读取通过清单、更新摘要、SHA-256 与陈旧度检查的本地行情；数据异常时不产生订单或状态变更。
- 不连接券商、不保存凭证、不发送真实订单；报告失败不会回滚已提交账本。

## [0.8.0] - 2026-08-25

### Added

- 120 日时间序列动量教学基准，信号在收盘后生成并延迟到下一交易日执行。
- 固定三策略对比实验，共用数据、切分、成本和买入持有基准。
- 中文 HTML、样本外净值图、JSON、CSV，以及 Windows 一键运行脚本和第八课文档。

### Changed

- 基础回测命令支持 `momentum`，并在报告中保存策略参数。
- 正式多策略对比绑定数据集 ID、整理文件 SHA-256 和数据健康状态。

### Security

- 拒绝非有限成本、非正净值因子和未在首个样本外交易日前完成预热的指标。

## [0.7.0] - 2026-08-24

### Added

- 数据集清单：SHA-256、稳定数据集 ID、来源、日期区间、复权、成交量单位和陈旧状态。
- 现金/整手/最低佣金/卖出费用/滑点账户账本，以及逐日余额与权益一致性检查。
- 执行可行性、成本敏感性、参数敏感性和 walk-forward 报告。
- 七节中文学习文档、Windows 一键脚本和经过解压验证的源码发布包。

### Changed

- GitHub 仓库品牌升级为 A-Share Quant Lab。
- README 改为面向学习者和审查者的可导航入口，并公开展示负收益实验。

### Security

- 正式实验继续强制信号次日执行、数据指纹、边界检查和报告前验证。

### Fixed

- 锁定兼容 Python 3.11–3.13 的 NumPy 版本，并让 CI 在依赖安装失败时立即中止。

[Unreleased]: https://github.com/Zhaolong666520/a-share-quant-lab/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/Zhaolong666520/a-share-quant-lab/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Zhaolong666520/a-share-quant-lab/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/Zhaolong666520/a-share-quant-lab/releases/tag/v0.7.0
