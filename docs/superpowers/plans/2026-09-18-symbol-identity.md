# P1.2a 标的与文件身份校验：设计和实施计划

日期：2026-09-18。关联 [v1.0 总设计](../specs/2026-09-18-v1-design.md) 的 P1.2。只实现身份一致性；并发写入与跨文件批次一致性留到 P1.2b，账本迁移不在本次范围。

## 目标与边界

一个日线文件只能包含一个规范标的；整理文件名必须与内容一致；读取和采集请求不能拿到另一个标的而宣称成功。身份失败应发生在发布、合并旧文件或替换数据库表之前。健康检查以结构化错误呈现问题，保留文件供诊断。

现有文件名 `sh_000300.parquet`、`sh_510300.parquet` 和演示文件名 `demo_000300.parquet` 保持不变。拒绝不规范输入，不静默转换大小写、去空格、重命名历史文件或改写历史 payload。

### 规范语法

标的必须是字符串，完整匹配 `[a-z][a-z0-9]{0,15}\.[0-9]{6}`：小写 ASCII 命名空间、一个点、六位 ASCII 数字。例如 `sh.510300`、`sz.000001`、`demo.000300`。命名空间是格式约束，是否可采集或可交易仍由原有配置和业务门禁决定，不新增市场支持。

文件名映射只把该唯一点替换为下划线，再加 `.parquet`。因此 `sh_510300`、`SH.510300`、全角数字、空字符串、数字类型和路径形式一律不作为合法 symbol；不接受隐式别名。文件名也作逐字符比较，旧的大写/别名文件由健康检查指出，不自动修复。

### 模块与接口

新增 `src/finance_lab/data_identity.py`，复用现有 `ValidationIssue` 与 `DataValidationError`：

```python
def canonical_symbol_stem(symbol: str) -> str: ...
def validate_data_identity(
    frame: pd.DataFrame,
    *,
    filename: str | None = None,
    expected_symbol: str | None = None,
) -> list[ValidationIssue]: ...
def assert_data_identity(
    frame: pd.DataFrame,
    *,
    filename: str | None = None,
    expected_symbol: str | None = None,
) -> str: ...
```

以上是接口签名，具体实现由失败测试约束。验证函数无文件写入；严格版本返回唯一合法 symbol 或抛出 `DataValidationError`。不把数据转换为字符串后再检查，防止缺失值、数字等伪装为标的。

| 错误码 | 条件 | 处理 |
| --- | --- | --- |
| missing_symbol | 缺 symbol 列或没有行 | 报告错误，拒绝写入/读取 |
| invalid_symbol | 任意行不是规范字符串，包括空值 | 报告错误，拒绝写入/读取 |
| mixed_symbols | 同一文件有两个以上合法标的 | 报告错误，禁止自动拆分 |
| symbol_filename_mismatch | 整理文件名不等于该标的规范文件名 | 报告文件名和预期名称，不改名 |
| unexpected_symbol | 内容标的不同于请求标的 | 报告实际值和请求值，不返回错标的数据 |

### 接入点

1. `storage.save_raw_snapshot`：原有日线校验后验证单标的，创建路径前失败；原始快照来源的单一性另行处理。
2. `storage.save_curated`：检查新数据；旧文件存在时，在拼接前检查旧文件内容与目标名/请求标的一致；最终合并结果再次检查后才原子发布。
3. `storage.read_curated`：读取请求先验证规范格式；读取内容后验证文件名与请求，失败不返回任何部分数据。
4. `storage.sync_duckdb`：用 DuckDB 对全部候选文件只读取 symbol 列进行身份预检，通过后才执行原有 SQL 同步；失败不能替换上一张有效表。缺列/损坏文件沿用 DuckDB 的解析异常，不隐藏底层错误。此预检与 SQL 读取不是一致快照，不宣称解决并发替换，P1.2b 继续处理。
5. `data_manifest.generate_dataset_manifest`：追加结构化身份错误，移除重复的 mixed_symbols 判断；保留现有清单、SHA、日期与来源检查。
6. `pipeline.validate_curated_data`：追加相同的身份问题，计入 error_count。
7. `pipeline._fetch_instrument`：对每个数据源的结果验证 expected_symbol 等于请求 instrument.symbol，在保存任何原始快照前拒绝错标数据，沿用 source_errors 留证与备用源逻辑。

## 实施步骤

项目根目录：`C:\Users\zhaolong\Documents\Codex\2026-08-07\ni\finance-lab`。使用当前本地分支和 `.venv`，保留上一步尚未提交的修改。

- [x] 只读核对现有两个整理文件的标的和文件 SHA-256，保存数据库文件指纹，用于完工后的不变性核对。
- [x] 新增 `tests/test_data_identity.py`：先通过现有公共存取接口测试，不依赖尚未创建模块导致整套无法收集。
- [x] 在未改生产代码时运行新测试，确认失败来自错误数据被接受、错标读取或缺少健康告警。
- [x] 实现上述唯一身份模块并接入七处路径，保留来源优先级、原子写入和临时文件隔离逻辑。
- [x] 运行定向测试；新增模块接口的空文件/缺字段/文件名大小写边界纳入同一批测试。
- [x] 运行全套质量检查，复核无行情/账户副作用，更新总设计、数据说明与 Changelog。

### 测试矩阵

| 场景 | 必须证明 |
| --- | --- |
| 规范 sh/sz/demo 标的 | 存取、DB 同步和健康结果兼容；两个不同文件可共同入库 |
| 非规范字符串、大小写、空白、下划线、Unicode 数字、非字符串 | 原始/整理入口拒绝；不新增文件，不覆盖已有有效文件 |
| 一份文件混入另一标的 | 原始/整理入口都拒绝；读取旧坏文件也拒绝 |
| 文件内合法标的与名称不符 | 直接读取拒绝；后续增量合并不得悄悄修复或覆盖旧文件 |
| 旧文件身份错误后同步 | DB 原表仍是之前正确的行数和标的 |
| 名称错误、混合标的、非规范标的 | manifest 与 validate 都给出相应错误码 |
| 抓取请求与响应不符 | 两个来源分别留下错误，原始/整理目录都不增加错标数据 |
| 合法混合来源 | 仍允许整理数据合并，来源优先级测试保持通过 |
| 空文件或缺少 symbol | 身份报告明确错误，读取/合并拒绝 |

测试写入错误文件仅用于临时目录模拟历史坏文件，不绕开生产门禁来保存正常业务数据。

### 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_data_identity.py --tb=short
.\.venv\Scripts\python.exe -m pytest tests/test_data_identity.py tests/test_storage.py tests/test_storage_publication.py tests/test_data_manifest.py tests/test_pipeline.py tests/test_paper_pipeline.py --tb=short
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest --cov=finance_lab --cov-branch --cov-report=term-missing --cov-fail-under=80
.\.venv\Scripts\python.exe scripts/check_markdown_links.py
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

首次新测试应暴露缺陷，修复后上述验证均须退出码 0。综合覆盖率口径沿用上一批次，不改门槛，不增加依赖，不改 Python 版本范围。

## 兼容与恢复

正式存在的文件若不符合契约，健康检查报错，原文件保留；先对照不可变原始快照及来源确认身份，再另行设计恢复步骤。不自动转换、拆分、删除数据。现有 canonical 文件不需要迁移；实际日线数值、策略参数、账本 schema、计划任务和发布版本均不改变。

## 验收记录

状态：P1.2a 已完成，修改保留在本地 `codex/v1-quality-sprint` 工作区，未提交、合并、推送或发布。Windows、Python 3.13.9；未执行其他操作系统/Python 版本或云端 CI，也没有运行真实行情更新或模拟账户。

| 检查 | 结果 |
| --- | --- |
| 生产代码修改前的新测试 | 71 项中 56 失败、15 通过；复现别名/混合标的被接受、错标内容被返回、错误旧文件被合并和缺少健康告警。保留缺字段时已有的 DuckDB BinderException 行为 |
| 新增测试总数 | 73 项；另补充两个已有文件名大小写的读取保护用例 |
| 定向回归 | 首轮 152 项通过；补充两个用例后最终 154 项通过 |
| 全套回归 | 489 项通过，41.07 秒；开启分支测量的综合覆盖率 84.17% |
| 身份校验模块 | 测量到的 33 条语句、14 个分支均覆盖；不是所有可能输入均被穷尽的声明 |
| 静态检查 | Ruff 通过；mypy 检查 41 个源文件通过；compileall 通过 |
| 文档与依赖 | 35 份 Markdown 链接通过；pip check 无依赖冲突 |
| 数据保护 | 两个整理行情文件与 `data/finance_lab.duckdb` 的 SHA-256 和修改前完全一致 |

既有数据身份核对：`sh_000300.parquet` 只含 `sh.000300`，`sh_510300.parquet` 只含 `sh.510300`，无需迁移。未修改原始快照、模拟账户、配置、依赖或策略参数。

后续 P1.2b 处理写锁、跨文件一致性和不同文件系统上的已有名称别名审计；本次单标的预检不能代替这些保护。保留历史坏文件，不自动做恢复或重命名。
