# U5运行验收工具交付记录

对应[项目计划书](2026-10-06-run-acceptance.md)。结论：本批区间验收工具、隔离演练及独立复核完成。未将工具交付计作真实正式恢复或整个 P3 完成。

## 验收能力

运行前capture只读保存五张账本表的行指纹及既有归档指纹；运行后verify完整核对全部新归档、实际新增提交和原历史。只有正常processed后、相同as_of_date的末次真实no-op及全部账本/归档检查通过，才返回本工具范围的passed。未知、部分失败或缺少关联提交会保持attention/unavailable。

归档记录不提供真实进程退出码、计划任务状态、实时行情健康或报告文件验收。本批交付U5的区间验收工具与隔离演练；正式切换和真实持续运行观察仍单列。

## 验证过程

相关基线111 passed / 1本机符号链接权限skip，16.62秒；本批原业务基准296文件，保存SHA256、大小、mtime精确tick字符串、属性及文件集合，排除既有两把锁。实施先真实RED，再最小实现、定向GREEN，之后独立代码审查和不同夹具的真实子进程行为验证。

| 验证层 | 本次实际结果 |
|---|---|
| 新功能专项 | 58 passed / 1 权限 skip，60.75 秒；59 个新增参数化场景 |
| 独立代码/规范审查 | PASS，无 Critical/Important/Minor 修复项；2 个独立实际探针通过，已修边界复跑 5 passed |
| 不同夹具的真实 CLI | 42 个主场景 + 7 个补充场景，各重复 3 次；147 个子进程（57 capture、90 verify），另 1 个链接创建权限场景未运行 |
| 稳定代码全量回归 | 1120 passed / 5 skipped，204.54 秒；语句与分支综合覆盖率 89.16%，门槛 80% |
| 质量检查 | 全项目 Ruff、mypy 55 个源文件、pip check、编译及差异空白检查通过；Markdown 链接收尾复核通过 |
| 原输入与代码保留 | 原 config/data/outputs 的 296 文件集合、SHA256、大小、精确 mtime、属性相同；128 个源/测试相关文件的测试前后指纹相同 |

各层存在重叠，不把专项、独立复跑和全量数量相加。5 个全量跳过均为本机 Windows 的符号链接创建权限限制；Windows 重解析点模拟检查已执行，但不代替原生链接或其他平台验收。综合覆盖率不是单独的分支覆盖率。

实施先确认缺 API 的真实 RED，再加入反例修复相对基线路径祖先检查、审计前 view 拒绝、参数错误隐私、坏新归档分类和未知计数误报零值。一个跨组合日期夹具错误、两个独立驱动设置错误均定位并单独记录，未冒充产品 RED。最终稳定代码通过后没有继续修改源文件。

独立夹具使用不同的 154 日合成价格、真实引擎、存储、报告与自动化归档。两份补充闭环均记录 runs 从基线 2 行增至 6 行，实际重复 no-op 后仍为 6 行；五表的变化均逐表记录。只读子进程的整根文件/目录集合、SHA、大小、mtime、属性保持不变，文件写、写流水线/绘图导入、报告文件读取和联网均受围栏约束。六次观察末尾由外部夹具进程实际改变基线，工具均返回 unavailable/input_changed，而不是通过。

异常复核保留真实事实：逐日部分失败保留 2 个新 run 与 partial correlation；报告失败保留 4 个新 run 与完整对应；提交成功但未返回收据保留 4 个新 run 和 unclaimed_commits；未匹配额外真实提交检出 6 个新增对 4 个收据。旧账户、结构元数据、run 或未推进组合的 state 时间字段被改动时，即使旧账本审计仍通过，区间验收也拒绝通过。50,002 行元数据的独立探针确认 LIMIT 50001 后拒绝，没有在此之前全表读取。

全量命令使用新建且事先确认不存在的外部根：

```powershell
$env:COVERAGE_FILE = '.planning/run-acceptance/.coverage-final'
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -ra --cov=finance_lab --cov-branch --cov-report=term-missing --cov-fail-under=80 --basetemp 新的项目外绝对目录
```

不要复用已有 basetemp（pytest 会清理该目录）。详细实施、独立审查、驱动 stdout/退出码和文件指纹保存在本地 `.planning/run-acceptance/` 与独立演练目录；公共记录不分发用户行情或账本内容。

## 使用时的证据保存

先在项目外准备证据目录，按实际根和账户组运行capture。PowerShell中可用UTF-8无BOM保存stdout；保存前检查退出码：

```powershell
$baselineJson = & .\.venv\Scripts\python.exe -B -m finance_lab.paper_run_acceptance capture --root C:\quant\recovered --portfolio default
if ($LASTEXITCODE -ne 0) { throw '账本基线捕获失败，先检查输出诊断' }
[System.IO.File]::WriteAllText('C:\quant\evidence\before.json', ($baselineJson -join "`n"), [System.Text.UTF8Encoding]::new($false))
```

路径为示例，父目录需先存在。成功捕获之后保留这份文件；基线的自哈希用于检测意外变化，不是签名认证。

## 剩余范围

本轮仅本机 Windows / Python 3.13.9 验证。工具是非原子观察，不能排除检查之后变化或恶意 ABA；没有执行本工具其他操作系统/Python 的实际矩阵。正式根目录切换、联网每日运行、真实连续观察和计划任务状态/修改仍未进行；没有连接券商、真实资金交易、提交、推送、合并或发布。P4 统一只读工作台和 P5 发布候选仍按总设计分阶段实施。
