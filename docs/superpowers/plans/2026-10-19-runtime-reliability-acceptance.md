# 2026-10-19 运行可靠性与交接验收增补

本文保留10月19日的观察，23日恢复工作时修订交付口径并重新提交接入审核。19日文档未接入：20:54:03的尝试被20:45门禁拒绝。实际23日接入、验证及封存状态见[恢复验收报告](2026-10-23-runtime-reliability-resume-report.md)，不能把本文文件名解释为19日已交付。

本记录补充10月16日的状态诊断交付与早期恢复记录。正文“本轮”均指19日对应批次，任务时间、数据和测试均为当时快照。它记录本地可复核事实，不能解释为正式版本发布、持续调度已稳定或策略有效。旧报告和失败结果保留；后续事实不倒填进历史验收。

19日14:49–16:49和19:16–21:16两轮均未按时完成整体交付。task7与整体终审的旧最终closure不存在；21日是新的迟到补审，结论仍为原显式要求未全部满足。23日恢复不会改变这些历史事实。

10月13–14日D盘迁移的成功次数已按当时用户授权以2/2收尾，其中手动/no-op及自动延迟的标记继续保留。本文待观察的是后续accepted-01新运行根的多个自然窗口，不重开已完成的旧迁移验收，也不为凑次数补跑、重新迁移或清理。旧START-HERE.md中r04为正式根的路径是10月14日历史状态，以本记录的10月19日观察区分。

## 19日代码、数据与任务快照

2026-10-19 20:00左右的只读观察中，正式代码物理目录为 `D:\codex\quest\finance-lab`，数据根为 `D:\FinanceLab\recovery\20261018\accepted-01`。正式计划任务 `A-Share Quant Lab Paper Daily` 为Ready，最近系统运行时间10月19日18:30、结果0，下次计划10月20日18:30。任务动作仍指向主目录 `scripts\run_paper_daily.ps1` 和accepted-01；未指向隔离worktree。完整任务XML与本轮开始基线一致，包括触发器、Principal、Settings和动作。

既有XML条件也继续保留：InteractiveToken、DisallowStartIfOnBatteries=true、StopIfGoingOnBatteries=true、WakeToRun=false、StartWhenAvailable=true、IgnoreNew和两小时执行时限。18:30是计划时间，不能单凭这个时间保证机器当时具备执行条件或应用已经成功；本轮未调整登录、电源、唤醒或执行策略。

隔离开发目录为 `D:\FinanceLab\worktrees\runtime-reliability-20261019`，分支 `codex/runtime-reliability-20261019`；主目录仍为 `codex/v1-quality-sprint`，HEAD `df000ae4e017b07238872c9ef0ee6b206dc06a0f`。既有未提交成果保留；本轮没有提交、推送或安装新环境。D盘虚拟环境复用既有C盘基础Python，不能据此声称所有依赖都在D盘。

既有editable入口的模块路径仍经过 `D:\FinanceLab\workspace\finance-lab` Junction，解析后的物理源码确为主目录。核验同时保存原始和物理路径；这不是允许数据根使用重解析点，也没有因此重装环境。

## 已发生的一次自然运行

10月19日18:30自然执行的应用归档开始18:30:05、完成18:30:56，状态success，处理10月15、16、19日。应用attempt_id为 `8bc642b6c2ad49c5b951f4d19c1e0a26`，run_id为 `6a967a047f60ad29`，行情batch_id为 `b5b8f7c99de64b3a9b0f30012a8571cc`。系统与应用时间接近仍不证明精确实例关联；该字段继续not_verified。

当时的自然运行核验保存行情4262行、两标的各2131行，最新日期和最后账本估值均为10月19日，dataset_id为 `c259cfa882616b39cd6d2002ce5126174f13fb95306dbd04bc1ba43e26e85031`。旧90条事件逐行保留，当前102条；新增12条是三天两账户的信号与估值事件。6份提交收据与账本精确匹配，关联verified/complete。摘要及归档报告SHA匹配。

这次自然成功发生在本轮修复接入之前，不能归功于新代码，也不证明多个窗口持续成功或重复执行均已验收。本轮未手动抓取、运行模拟盘或触发计划任务。原r04仅做文件读取，未用数据库引擎连接；accepted的只读诊断与四根文件清单前后相同。

## 三项修复与实际效果

| 范围 | 修复前的受控失败 | 当前行为 |
| --- | --- | --- |
| 调度快照一致性 | 查询过程中任务状态、动作或运行记录变化仍可能输出available | 两次观察不一致返回unknown/query_changed；第二次读取失败返回unknown/query_unavailable |
| 贯穿系统查询的应用证据检查 | 系统查询期间归档、摘要或账本WAL变化，仍可能保留旧成功/估值 | 查询后复核最初观察集；受影响的候选、计数、来源、关联和估值结论撤回未知，返回attention |
| 已记录来源对照警告 | 摘要显式warning未进入简明关注项 | 仅解析已匹配SHA的摘要，单列安全的对照详情和source_comparison_warning；采集结果不改写 |

所有变化检测仍是非原子观察，不能排除瞬时替换再恢复或最后检查之后变化。工具不重新验证当前健康、报告文件完整性或精确系统应用关联；它不修复数据隔离，也不自动补跑。

20:00的真实只读结果为attention/1，唯一诊断为 `source_comparison_warning`。采集source_outcome仍success、failed_sources仍0，最后估值仍19日，runtime_input_changed=false。ETF `sh.510300` 对照仅189/2131，覆盖率约8.87%；重叠收盘价一致，指数对照pass。这是已有历史对照覆盖不足，不是本轮采集从成功变失败。未修改95%覆盖/0.5%价格差规则、数据来源、策略或每日执行器；自定义“所有来源对照均pass”的严格检查仍false，旧natural-checks.json的passed=false保留。

## 验证证据及读取方式

主目录最终相关回归在19:58:51–19:59:17运行，共229项：228通过、1跳过，退出0，实际26.91秒。跳过是本机Windows符号链接权限不足，不能算已验证通过。这是8个相关模块的集合，不是全项目全量。此前红例、修订失败和中间通过结果分别归档，不能相加或包装成同一次运行。

离线wheel构建退出0，包内63个finance_lab源码/资源与隔离src逐字节匹配；包SHA为 `c9bfb71d9e94d48256e4dabad3470357b1614e312b761f0f2bdb38c05c4b83d7`。对主区的额外比对有42个文件存在LF/CRLF差异，仅行尾归一后相同，不能声称包内63文件与主区逐字节一致；已接入文件的精确身份另行核验。经校验解包后，独立入口在空演示根以--no-scheduler返回attention/1、scheduler未知，且演示根未新增文件。此wheel未安装，包核验不能代替下一次自然运行。build使用既有依赖、--no-isolation与禁用索引，未联网安装。

本地原始材料在主目录 `.planning\optimization-20261019-1916`：task1/2/3的应用记录、备份、独立审查和双向交叉闭环、task3-verification.json、task3-post-task.json、offline-wheel-01、final-pre-review-r2-*。pytest材料在隔离目录同名.planning目录的results-task3-main，保存JUnit及实际模块身份。自然运行核验在主目录 `.planning\optimization-20261019-1449\natural-checks.json` 与natural-runtime-status.json。这些本地材料未随Git提交；仅有文档不能替代原始证据。

第一次汇总脚本遇到中文Git提示的默认GBK解码错误，旧final-pre-review-result.json虽写ok也不作为完整终验。修订r2按原始字节保存输出并重新核验；失败及说明未删除。两轮超时事实均保留，19:16–21:16是第二轮原定窗口，不是已按时完成的证明。19日最后指纹核验也不能代替当时未闭合的整体交叉终审。

以下为10月19日观察对应的只读命令，显式预期日期必须按实际验收目标填写，不能自动把“今天”视为应有数据日：

```powershell
& 'D:\codex\quest\finance-lab\.venv\Scripts\python.exe' -B -m finance_lab.paper_runtime_status --root 'D:\FinanceLab\recovery\20261018\accepted-01' --portfolio default --expected-as-of 2026-10-19
```

需要完全跳过系统调度查询时，加 `--no-scheduler`；此时调度保持unknown，不是系统成功。退出0为该次记录检查通过，1为需关注（包括上述对照警告），2为请求或入口错误。attention不等于需要补跑；先阅读diagnostics及相应来源记录。命令不更新行情、不推进账本、不修改计划任务。后续期望日期与任务时间未到、休市及数据截止日的判断仍需明确业务目标。

## 仍待完成

- 后续多个自然窗口的失败/成功归档、预期日期和收据持续核验；本次单窗口成功不证明长期稳定。
- ETF完整历史来源对照，严格对照检查false仍需保留。现有警告不授权更换来源或放宽规则。
- 精确的系统任务实例到应用attempt关联、检查结束后的变化及报告文件完整性，不在本轮证明范围。
- P4统一工作台、P5发布候选、新用户验收和正式版本发布；当前本地0.9.1未变。

项目用于金融学习和研究，不连接券商、不自动交易；本轮没有产生真实资金准入结论。
