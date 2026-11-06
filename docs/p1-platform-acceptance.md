# P1 平台与权限验收范围

日期：2026-11-06。本说明区分配置、实际执行和继承的历史证据；配置清单完整不代表各平台已执行。P1整体尚未宣布完成，本候选未部署。

## 固定平台与测试清单

现有 `storage-portability` 任务列 Windows、Ubuntu、macOS，均使用 Python 3.13。Windows Python 3.11/3.12/3.13 的完整质量矩阵与安装测试属于P2，本批没有扩大为所有平台和解释器的笛卡尔积。

存储任务保留原六文件：`test_filename_audit.py`、`test_storage_portability.py`、`test_paper_schema_contract.py`、`test_paper_store.py`、`test_recovery_ledger.py`、`test_workspace_acceptance.py`；加入三个P1专项：

- `test_workspace_lock_paths.py`：锁写入前路径检查。
- `test_storage_validation_boundary.py`：发布/入库内容校验与异常保全。
- `test_paper_legacy_shape.py`：旧账本结构识别及拒绝不支持的缺表组合。

本地在Windows/Python 3.13.9对该清单执行`--collect-only`，九文件共191节点，其中三新专项为15、54、35节点。收集未执行测试函数或夹具，不能计为通过191项，也不是跨平台执行。

CI保留既有平台、Python版本、依赖、触发器及失败继续取证设置。新增元数据步骤记录实际解释器、平台、依赖版本、提交环境字段和源码/测试/配置SHA；与JUnit一并保存，pytest增加`-ra`显示跳过原因。元数据命令已在本地运行核对，GitHub托管执行器尚未运行本候选。缺失提交/执行器环境字段保留null，不伪造CI身份。

## 已知环境与权限事实

| 环境或场景 | 本批实际事实 | 验收边界 |
|---|---|---|
| Windows x64 / Python 3.13.9项目venv | 已安装所需测试依赖；配置解析、191节点收集、元数据写入/身份核对通过 | 沿用三专项原始执行证据，不重跑同环境已过用例；配置验证不是业务测试通过 |
| Windows / Python 3.12.13、3.13.14 | 解释器存在，pytest/duckdb/pandas/pyarrow等必要依赖缺失 | 未执行专项，不借用其他ABI环境依赖、不安装凑通过 |
| Windows / Python 3.11 | 注册及已知解释器位置中未发现 | 有限库存结论，不是全盘不存在证明；相关P2组合未验 |
| Windows / Python 3.14.0 | 注册默认解释器存在 | 超出项目`>=3.11,<3.14`声明，未用于验收 |
| Ubuntu、macOS / Python 3.13 | 当前没有建立可用验收环境 | 本候选未验；仅发现两个停止中的Podman WSL分发，未启动，不能当Ubuntu验收 |
| 文件、目录symbolic link | 本批D盘实际创建均返回`WinError 1314` | 未提权、不改变系统设置；五个文件链接专项继续未验 |
| 目录Junction | 本批D盘创建与目标身份检查成功 | 只是能力探测；不能替代文件symbolic link或POSIX执行 |

三专项原始执行分别为：锁路径10通过/5权限跳过，写入校验54通过，账本结构35通过。测试文件与相应旧候选字节一致；中间P2改动包括共享整理文件读取器，因此不能声称所有生产源码与旧批完全一致。历史证据按各自代码身份与变更影响引用，不改名为本次执行。

五个权限缺口是`test_lock_link_rejected_before_touching_any_lock`的文件链接分支：create_session的paper/market锁、verify_activation的paper/market锁、verify_session的market锁。已通过的目录Junction路径不足以证明这些文件链接场景。旧回归中的recovery/source-profile链接跳过另行保留，不与这五格混算。

## 退出和后续

本次有限包完成CI遗漏修正和本机配置证据，不改生产源码、正式任务、来源窗口、阈值、策略或正式账本。原有三平台旧版本执行有效，但不能代替新候选的三专项。

后续需在具备条件、范围获准的环境下对同一候选补齐实际平台/权限证据，并保留失败与skip；然后单列正式接入评估。没有环境的格保持未验，不能降为通过或自动缩小已列明平台。正式账本迁移不作为凑验收的必做动作。P3固定自然观察保持冻结版本。

需求来源：[账本结构与跨平台验收](账本结构与跨平台验收.md)、[项目总纲](项目总纲.md)。
