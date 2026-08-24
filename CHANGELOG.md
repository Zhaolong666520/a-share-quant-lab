# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的结构。

## [Unreleased]

### Planned

- 交易所交易日历、分红/除权现金流和更丰富的稳健性基准。

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

[Unreleased]: https://github.com/Zhaolong666520/a-share-quant-lab/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/Zhaolong666520/a-share-quant-lab/releases/tag/v0.7.0
