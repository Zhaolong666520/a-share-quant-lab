# 单标的研究命令的整理数据读取失败

适用命令：account、backtest、experiment、walk-forward、cost-stress、parameter-test、execution-test、compare。

已有 Parquet 无法物理读取时，命令返回退出码 3，标准输出为单个 JSON：`ok=false`、`error_code=unreadable_curated_data`、`stage=load_curated`、`data_committed=false`，并提示保留文件、检查权限和用 `validate` 核验。标准错误不输出底层异常或路径。已有报告不能证明本次成功。

共享读取边界仅转换 `pandas.read_parquet` 抛出的 `OSError` 或 PyArrow 读取异常，`FileNotFoundError` 保持原类型。无法读取不必然表示文件损坏，也可能是读取权限或 I/O 故障。字段身份、日期、策略参数、隔离状态、锁及报告写入错误不进入这段转换。

account 和 compare 先生成内存数据清单，另外六条直接读取标的文件；两条路径复用相同边界，不增加一次完整预读。`generate_dataset_manifest` 的物理读异常由底层异常变为 `CuratedReadError`，原异常保留在 `__cause__`，函数调用方应按此窄契约处理。其他使用该函数的工作流可能观察到新类型；本批不宣称它们都获得了完整 CLI 验证。批量命令的已有失败回执保持，单标的诊断不推广到已提交数据的其他入口。

离线验收使用独立根中的合成 ETF，保留 `SYNTHETIC_DEMO_NOT_MARKET_DATA` 来源标签。每条命令先真实生成报告，再仅破坏本次新建的 Parquet，用同一固定参数再次运行实际 CLI 子进程。检查文件全集、SHA、大小、业务文件修改时间及旧报告；仅 `data/market_data.lock` 的正常 touch 时间单独处理。网络通过测试进程的 Python 审计钩子阻止，非操作系统级网络沙箱；未连接行情来源。

子进程由 `runpy.run_module("finance_lab.cli", run_name="__main__")` 启动，保留 argv、退出码、stdout/stderr、实际导入路径、源码 SHA 与真实 `pandas.read_parquet` 调用观察。这是源码 CLI 入口验证，不能替代已安装 console script、wheel 或跨平台验收。权限错误采用确定性注入，不冒称实测 NTFS ACL 拒绝。
