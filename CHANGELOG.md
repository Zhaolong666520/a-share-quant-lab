# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的结构。

## [Unreleased]

### Planned

- 交易所交易日历、分红/除权现金流和跨平台运行脚本。

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

[Unreleased]: https://github.com/Zhaolong666520/a-share-quant-lab/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/Zhaolong666520/a-share-quant-lab/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/Zhaolong666520/a-share-quant-lab/releases/tag/v0.7.0
