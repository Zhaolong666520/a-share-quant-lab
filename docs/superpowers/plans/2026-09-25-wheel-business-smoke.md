# P2.3：wheel 工作目录与业务冒烟

## 初始化契约

- `FINANCE_LAB_ROOT` 非空时优先使用其解析后的目录。
- 源码树运行且可识别 `pyproject.toml` 与 `src/finance_lab/config.py` 时，维持原有仓库根目录行为。
- 从 wheel 安装运行且没有显式根目录时，使用当前工作目录；不以 site-packages 作为数据目录。
- `finance-lab init` 仅在配置不存在时创建包内示例 `config/instruments.json`；已有配置，包括损坏配置，都不自动覆盖。
- 交易日历优先使用工作目录的 `config/sse_trading_calendar.json`；缺失时使用 wheel 内置的同一快照。其快照 SHA-256 与源码树配置一致。
- `demo` 只用合成数据，在工作目录的 `outputs/` 发布 HTML、净值 CSV、JSON、PNG 和完整性标记。

## 本轮验证

- Windows / Python 3.13：从本地源码构建 wheel，安装到仓库外临时目录；从该目录执行 `init`、`demo`，检查配置、HTML/CSV/JSON、日历及报告完整性标记，全部通过。
- 包代码实际从临时安装目录导入，未从源码树导入。演示没有接触真实行情或账户。
- 首轮安装仅复用开发虚拟环境依赖，不能代表全新环境；后续验收见下节。
- 单元测试覆盖 wheel 默认工作目录规则、显式根目录优先级和 `init` 不覆盖已有配置。

## 后续验收：全新环境

- Windows / Python 3.13.9：在仓库外新建不继承系统包的虚拟环境，直接安装 wheel 及其完整依赖；`pip install` 成功。
- 使用隔离的 `FINANCE_LAB_ROOT` 执行安装后的 `finance-lab init`、`finance-lab demo`；检查四类产物、示例配置、包内日历、报告完整性标记和包代码来源，全部通过。
- 清除 `FINANCE_LAB_ROOT` 后在另一个仓库外目录运行 `demo`，输出落在该当前目录；site-packages 内没有应用生成的 `data/`、`outputs/`。
- CI 的 Linux / Python 3.12 `package-smoke` 已扩展为同样的业务检查；本地无法替代远程 CI 结果，待提交后观察其执行。Python 3.11 与 macOS wheel 仍未验证。

## 下一步

等待远程 CI 验证 Linux / Python 3.12 的新检查；继续处理 P2.2 剩余写入失败语义。
