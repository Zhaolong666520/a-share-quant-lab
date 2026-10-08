# 实际运行阻断恢复与接入计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。用户已对受控暂停、恢复验收和通过后接入方案要求“处理”，直接执行，不推送或发布。

**Goal:** 在保留原故障根与历史账本的前提下，取得不并发写入的静止验收窗口，并将指定每日任务接入通过真实更新/模拟记账验收的稳定新数据根。
**Architecture:** 任务配置先备份、只禁用新触发；原根和accepted-01保留。代码/venv仍在维护仓库，业务数据/日志改由显式DataRoot承载；只在新根完整验证后更新指定TaskAction并恢复原日程。
**Tech Stack:** 既有PowerShell、Python3.11–3.13、AKShare/BaoStock、DuckDB、pytest，不新增产品依赖。

## Global Constraints

- 原始数据只能新增快照，不能静默覆盖或手工修改；不删除原更新标记、重置账本或把updating手改为completed。
- 不连接券商、不执行真实资金交易，所有订单为既有固定策略模拟；不引入新来源/收费/凭证。
- 原目录和accepted恢复副本只读保留；新候选必须为确认不存在、非嵌套的稳定目录。
- 唯一实际任务为A-Share Quant Lab Paper Daily（根TaskPath）；主执行者唯一能禁用/修改/启用任务，其他代理只读。不能停止/修改其他任务。
- 临时暂停只禁止新触发，不强行Stop运行中进程；操作前保存原XML与启用/动作/日程/设置。
- 仅在真实来源全部成功、健康无阻断、历史账本保护/精确提交/no-op检查与代码回归通过后接入；失败不强行启用新根。
- 接入失败或权限/外部源阻塞时，恢复原任务动作及原启用状态并报告仍未恢复；保留失败候选，不留下无说明永久暂停。
- 不修改用户/机器全局FINANCE_LAB_ROOT；仅runner进程作用域覆盖，结束恢复。保持现有无DataRoot调用兼容。
- source失败显式报告，不能把离线或合成数据称联网今日成功；历史补处理与真正新增交易日观察分别记录。
- 保留10月7日静止失败原结果；本轮使用暂停之后的新基准，不能用新窗口覆写旧结果。
- 所有测试仅隔离根，Python -B、pytest无缓存；全量只在稳定代码后一次，必要修复复测例外。不推送/合并/发版。

## 已完成控制起点

2026-10-08，代码分支codex/v1-quality-sprint，HEAD645788f，初始工作区clean。原10月1日updating/fetch在读取guard被确认为incomplete_batch；accepted-01 completed/readable但partial_sources/offline。原任务18:30日程、旧代码目录、启用true、最近exit1，原配置与XML已保存。
用户授权后已成功Disable，State Disabled、Enabled false、Running false，无强行停止进程。暂停后原业务298文件和accepted-01的59文件SHA/size/mtime/属性基准保存，供本轮新静止窗口。原初次中断原因未证明。

## Task 1：受控窗口与独立候选（主操作）

- [x] 保存原任务配置/日程/启用/原XML，再只Disable该任务，并核对无运行中进程。
- [x] 捕获暂停后原config/data/outputs及accepted-01指纹基准。
- [x] 创建C:\Users\zhaolong\Documents\Codex\2026-08-07\ni\finance-lab-runtime-20261008及新的r02候选，由accepted-01独立复制，不硬链接；祖先/内容links/reparse拒绝，原副本前后不变。失败候选不复用为正式根。
- [x] 仅在新候选执行data-recovery-acceptance-verify，复核文件清单、5表/事件/健康；旧凭据在正常更新后会失效，保留作恢复时刻证据，不覆盖。

## Task 2：运行脚本显式数据根（单一代码作者）

**Files:** scripts/run_paper_daily.ps1、scripts/install_paper_daily_task.ps1、新tests/test_paper_script_data_root.py；不改Python计算或数据库schema。

**Interfaces:** 两脚本新增可选[string]$DataRoot。runner选择优先级：显式DataRoot →已有进程FINANCE_LAB_ROOT →维护仓库ProjectRoot；代码/venv仍来自PSScriptRoot父目录。参数指定目录必须已存在且为正常文件系统目录，拒绝..、异常字符、祖先symlink/reparse，失败不创建该根/不写其他根。installer仅显式DataRoot时把绝对已验证路径以quoted参数传给runner，无参保持原调用语义；Uninstall保持原顺序和行为。

- [x] 先真实RED：实际PowerShell调用显式DataRoot能把日志及Python业务导向该根，原脚本缺参数失败；保存输出原因。
- [x] 最小实现，在主try/finally中保存并恢复旧FINANCE_LAB_ROOT与工作目录，根选择/校验错误退出非零。日志归DataRoot，Python使用-B，金融根仅为该进程设置。
```powershell
# 意图接口示例（具体安全路径检查需测试覆盖）
$PreviousFinanceRoot = $env:FINANCE_LAB_ROOT
try {
    $env:FINANCE_LAB_ROOT = $ResolvedDataRoot
    # 调用维护仓库.venv Python -B，日志进入ResolvedDataRoot/outputs
} finally {
    $env:FINANCE_LAB_ROOT = $PreviousFinanceRoot
}
```
- [x] 测试真实路由/环境恢复/日志归属、无参数兼容、env回退、空/不存在/文件/..与links/reparse拒绝、带空格/中文目录、原任务参数/触发节奏不变。通知UI和任务注册API可在边界mock，不mock数据根路由/业务写入。
- [x] 定向GREEN、自审与独立Spec+Quality审查；主稳定版本全量+Ruff/mypy/编译/链接，不运行实际installer去覆盖未验证任务。

## Task 3：真实来源与模拟闭环（主操作）

- [x] Task 3a最小同源指数接口修复：provider边界RED、English/prefix/严格契约GREEN及独立审查通过；不等于真实来源可用。

- [ ] 在新候选之前保存只读账本区间基线；按既有来源真实更新和健康检查。处理日期2026-10-08；盘中不能把当日未完成日线当收盘，本轮受控验证先截止最近完整交易日2026-09-30，正式18:30日程不固定历史as-of。进程/请求设有界时间，超时保留候选并报告，不能反复重试或移除隔离。
- [ ] 真实processed+同日期重复调用，保存全部schema2归档，verify完整区间、原历史、实际新增与no-op对应；有历史补处理时明示，不当作新连续观察。
- [ ] 可读健康不等于来源全成功，partial/failed/未知/报告失败不能接入。检测到外部或源契约问题仅最小有证据修复；不伪造数据/隐藏坏来源。
- [ ] 报告预计真实模拟首次回补历史日期；不预先承诺新增run数或今日行情已可用。表/event/hash/资金与份额一致性由既有审计证明。

## Task 4：条件接入、恢复日程、收尾

- [x] 失败分支：r02真实门禁失败后不接入候选，实际恢复原完整任务配置及Enabled=true；核对Ready、原18:30日程及原Action。不Start新的实际任务。

- [ ] 独立最终复核恢复/真实运行/代码与原根静止证据，关闭阻碍后更新TaskAction：同powershell/维护仓库runner、原StartDate/stale/portfolio参数+显式DataRoot新根；触发器/Principal/其他Settings保留。
- [ ] 先验证Action与新根正确，再恢复原Enabled状态并记录新TaskXML；必要一次Start该指定任务的实际运行，取得退出码/新归档/no-op和Task状态，不把命令接受当执行成功。
- [ ] 原目录298、accepted59历史前后不变，新窗口静止结果单独记录；无法达成接入时按原配置回退、不删除任何新候选或失败记录。
- [x] 文档提供临时终端FINANCE_LAB_ROOT和-DataRoot用法，明确正式根未接入、回退范围、来源日期与未完成观察；保留本地分支，不推送/发布。最终本地提交由Git日志追溯。

所有完成项必须有实际证据；如果来源或运行阻断未解决，任务恢复原配置并说明剩余问题，不宣称真实恢复成功。

执行结果：Task 1完成；Task 2代码与独立审查通过，全量1163passed/5权限skip。
Task 3真实门禁未通过：r02的AKShare两请求失败，未处理模拟日期、五表行指纹完全不变，区间验收正确拒绝。
Task 4未接入新根，已按失败分支恢复原任务完整配置与Enabled=true，NextRun为2026-10-08 18:30。
一次脚本RED测试隔离失误曾写原latest和一份失败归档，已恢复latest并移存测试档留证；不能把整轮称为从未写原目录。
本轮收尾及未完成范围见[处理记录](2026-10-08-runtime-recovery-report.md)。

调查追加：第1候选daily1为partial_sources，未推进账本。AKShare指数generic接口全市场映射pz100请求配置代理/直连都失败、pz1成功，不能只认定代理故障。同AKShare/同东方财富的官方stock_zh_index_daily_em接受明确市场前缀，本机真实请求成功，增加sources.py和相关source-contract tests的最小修复任务，TDD/独立复核后在新候选验证；不增来源/依赖、不patch第三方/修改代理、不降失败门禁。旧失败候选及盘中raw保留，不直接作为已验收正式根。
