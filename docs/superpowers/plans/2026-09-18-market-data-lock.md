# P1.2b.1 行情并发访问保护

关联 [v1.0 设计](../specs/2026-09-18-v1-design.md)。延续 P1.2a 的身份校验，不改数据内容、策略或账本 schema。

## 设计决策

现有原子替换只能保证单次文件发布完整。两个任务仍可能同时读取旧文件，各自合并后依次发布，后发布者会丢掉另一个任务的新行；更新期间的健康扫描也可能看到尚未结束的多文件更新。

本批次为每个项目的 `data/market_data.lock` 增加统一的非阻塞互斥锁。一个任务从开始抓取到保存更新摘要持锁，其他协作任务立即报错；调用者稍后显式重试，不新增后台排队或无限等待。

操作系统文件锁沿用已有 `paper_run_lock` 的机制，使用独立锁文件，转换为行情专用的 `MarketDataLockError(RuntimeError)`。进程内登记持有线程，防止部分平台的进程级锁语义放行其他线程；同一线程嵌套操作可重入。注册表只记录活动锁，退出后清理，PID 加入身份键，路径先 resolve/normcase。

锁文件保留，不用“文件存在”判断繁忙，不根据 PID 或时间擅自删锁。正常退出、异常和进程退出后由上下文或操作系统释放持锁状态。不同项目的锁互不影响。

## 受保护入口

| 位置 | 持锁范围 |
| --- | --- |
| `pipeline.update_market_data` | 配置读取、两个来源获取、快照/整理数据保存、数据库同步、摘要发布 |
| `storage.save_raw_snapshot` | 校验、路径生成、快照发布 |
| `storage.save_curated` | 校验、读取旧文件、合并、再次校验、原子替换 |
| `storage.sync_duckdb` | 文件枚举、身份预检、SQL 读取与替换表 |
| `storage.read_curated` | 路径/身份校验与完整读取 |
| `data_manifest.generate_dataset_manifest` | 枚举、指纹、内容与更新摘要读取 |
| `data_manifest.read_update_summary` | 摘要读取与解析 |
| `pipeline.validate_curated_data` | 整批校验扫描 |
| `paper_pipeline.load_paper_market_snapshot` | 同一次清单、摘要、价格与指纹检查 |

对同一线程中的内部调用允许重入。因此旧的文件发布回归测试可继续在受控写入回调中检查旧文件；独立线程/进程则不能进入正在更新的批次。CLI 沿用现有异常非零退出机制，每日自动化沿用失败留证流程。

## 本批次明确不解决

- 锁是协作式的，无法拦截手工改文件或不使用本项目接口的程序。
- 进程在发布多个文件的中途崩溃后，锁会释放，但不能撤销已经发布的文件。跨文件事务、失败批次标记和恢复属于 P1.2b.2。
- 分开的多次 API 调用之间仍可能发生更新；本次只保证列出的单次调用及模拟快照入口内部的排他访问。其他研究流程的整段输入快照绑定仍需下一批核验。
- 模拟账本原有锁与行情锁独立，本次不修改账户提交协议；共享 DuckDB 的其他写入冲突仍由现有数据库错误与事务处理。
- 跨平台已存在文件名别名审计继续单列，不自动重命名历史文件。
- 本次验证以本机 Windows 为准；复用的 POSIX 文件锁分支保留，未跑的平台不宣称通过。

## 文件与实现步骤

新增 `src/finance_lab/market_data_lock.py`：提供 `market_data_lock(data_directory: Path)` 上下文及专用异常，复用现有 OS 锁。`storage.py`、`pipeline.py`、`data_manifest.py`、`paper_pipeline.py` 的公开接口签名保持不变；长函数通过薄包装调用原实现，避免大段逻辑重写。`.gitignore` 排除运行时锁文件。

新增 `tests/test_market_data_lock.py`。使用真实子进程、线程和文件；抓取边界替换为合成行情，在真实发布函数之前的回调中启动竞争任务，用限时 join/子进程完成控制顺序，不靠固定睡眠碰运气。子进程调用有超时，增量保存测试的线程在 finally 中再次 join；测试不会启动真实数据更新或修改用户账户。

- [x] 记录两个行情文件、数据库与现有更新摘要的 SHA-256。
- [x] 用旧代码复现两次增量保存交错导致丢行，确认新增测试失败。
- [x] 实现项目锁、重入与异常清理，接入上述入口。
- [x] 验证竞争者被拒绝且旧文件不变；持有者结束后重试包含两次更新，不丢行。
- [x] 验证完整更新在抓取和摘要发布阶段均持锁；常用读取入口也拒绝并发进入。
- [x] 验证独立进程、独立线程、嵌套异常、不同目录、已有锁文件及进程异常退出后的恢复。
- [x] 验证 CLI 非零退出，以及 paper-daily 将锁冲突记录为失败，不继续模拟。
- [x] 执行定向/全套回归、Ruff、mypy、编译、文档链接、依赖检查，核对原数据指纹未变。

## 验收命令

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_market_data_lock.py --tb=short
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_storage.py tests/test_storage_publication.py tests/test_data_identity.py tests/test_data_manifest.py tests/test_pipeline.py tests/test_paper_pipeline.py tests/test_paper_lock.py tests/test_paper_automation.py tests/test_market_data_lock.py --tb=short
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -X utf8 -m pytest --cov=finance_lab --cov-branch --cov-report=term-missing --cov-fail-under=80
.\.venv\Scripts\python.exe scripts/check_markdown_links.py
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

## 验收记录

状态：已完成本批次本地实现与验证。环境：Windows、Python 3.13.9，分支 `codex/v1-quality-sprint`，基于 `0b16aa3` 的未提交工作区。

先复现再修复的证据：

1. 旧代码中，两个真实线程/进程分别向已有 120 行数据新增 10 行，后发布者覆盖了另一个任务的增量，最终只有 130 行；两项回归测试均失败。
2. 接入 `save_curated` 锁后，两项测试通过：竞争者得到 `MarketDataLockError`，持有者完成后重试得到完整 140 行。
3. 扩展到全部入口时，18 项测试中有 12 项失败、6 项通过，暴露了抓取/摘要阶段以及读取入口缺少整体锁；接入全部入口后 18 项通过。
4. CLI 锁冲突返回 1；每日自动化不启动来源抓取、不创建数据库，记录 `market_data_update` 步骤失败及 `MarketDataLockError`，不可变归档与返回状态一致。

最终结果：

| 检查 | 本地结果 |
| --- | --- |
| 新增并发回归 | 18 项通过 |
| 存储/清单/更新/模拟/自动化定向回归 | 185 项通过 |
| 全套回归 | 507 项通过，54.10 秒 |
| 开启分支测量的综合覆盖率 | 84.38%，达到现有 80% 门槛 |
| Ruff / mypy | 通过；mypy 检查 42 个源文件 |
| 编译 / pip check / git diff --check | 通过 |
| Markdown 本地链接 | 36 份文档通过 |
| 原数据保护 | 两份现有整理行情、DuckDB 与更新摘要的 SHA-256 前后一致 |

跨进程异常转换在真实子进程中验证；本次覆盖率只汇总主测试进程，没有将子进程计入，因此不宣称所有新增代码达到 100% 覆盖率。

未运行真实行情抓取、现有模拟账户或云端 CI；未验证其他 Python/操作系统矩阵，未重新打包。未推送、合并、发布或修改计划任务。下一批先设计失败批次标记与跨文件恢复，不把互斥锁视作崩溃回滚协议。
