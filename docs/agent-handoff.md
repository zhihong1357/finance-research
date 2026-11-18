# Agent 交接

这里记录 Claude Code 与 Codex 的共享工作状态。仓库文件不会实时通知另一个 agent；每次开始任务都要重新读取本页。项目阶段和验收标准以 `docs/roadmap.md` 为准，本页只记录交接与下一步。

## 当前交接

- **负责 agent：** Claude Code（2026-11-18 起，所有者决定由 Claude Code 恢复主责）。Codex 在 Claude Code 额度用完时临时顶替；顶替前先读下面"Codex 顶替时"一节。
- **当前任务：** 无进行中的任务。Phase 2 等待期间能做的 Phase 3 步骤（3.1a、3.2、3.5、3.6a、3.8、3.9）已全部合并（[PR #86](https://github.com/zhihong1357/finance-research/pull/86) 至 [PR #91](https://github.com/zhihong1357/finance-research/pull/91)）。
- **下一步：** 等 Phase 2 两项验收。其余 Phase 3 步骤要 numpy（3.1b）或要读数据集，按决策 0017 等两项都通过之后再做，期间没有可做的开发。11-30 01:00 UTC 核对 2.3，12-04 10:00 UTC 核对 2.10（Claude Code 会话里的一次性提醒）；两项都通过后先做 3.1b。3.1b 的草稿（numpy、随机流）只在 Claude Code 的会话里，不在仓库；重做或取回都可以，做法见路线图 3.1b。
- **当前路线图关卡：** Phase 2 剩余两项被动验收，Phase 3 按决策 0017 并行。
  - **2.3 观察窗口：** 2026-11-16 00:00 UTC 至 2026-11-30 00:00 UTC（马来西亚时间 11-30 08:00 结束）。窗口内不手动启动采集；补跑由系统自己启动，不算人工干预。11-16 08:39 UTC 有一次 DVOL 采集失败在"采集并推送"一步，当天更早和次日的运行都成功、没有开出中断问题；原因未查，11-30 验收时确认当天没有缺数据。
  - **2.10：** 工作流"录制类规范层"2026-12-04 06:19 UTC 写入 11 月；之后做重建核对，内容指纹一致才算通过。
- **决策 0017 的五条约束**（两项验收都通过前有效）：不启动 Phase 2 的工作流；只新增文件，不改 `src/`、`config/`、`.github/workflows/` 里已有的文件；不改 `uv.lock` 里已有包的版本；不在 GitHub Actions 上跑读写数据集的任务；Phase 2 任何一项验收没通过时先修 Phase 2，Phase 3 暂停。
- **Phase 3 门槛：** Phase 2 两项都通过之前，Phase 3 不能验收，也不能据它定稿规程第 5 节的阈值。
- **定时核对：** Claude Code 的会话设了两个一次性提醒：2026-11-30 01:00 UTC 核对 2.3，2026-12-04 10:00 UTC 核对 2.10。

### Codex 顶替时

1. 先读本页、`CLAUDE.md` 和决策 0017；检查有没有未合并的拉取请求，Claude Code 开着的拉取请求不要并行接手，除非所有者明确交给你。
2. 接手时把"负责 agent"改成 Codex（临时顶替），在活动记录末尾追加一行；交回时改回 Claude Code，再追加一行。
3. Phase 3 的工作照样遵守上面五条约束。

## 交接约定

- 接手任务前，先检查本页、路线图、未合并拉取请求和相关工作流；同一时间只由一个 agent 负责一项任务。
- 接手时填写当前 agent、任务、基准提交、分支、拉取请求和下一步。暂停、完成或转交时更新当前状态。
- 每项工作通过一个独立分支和一个拉取请求。拉取请求说明和活动记录明确标注 Agent: Codex 或 Agent: Claude Code；提交信息能由 agent 控制时，再追加对应标记。保留 GitHub 实际提交账号，不伪造作者。
- 活动记录只追加，不改写既有记录。除非所有者明确批准，不读取封存数据或金库报告；不把第三方原始数据、衍生数字、密钥、账户信息或私有数据集内容写进公开仓库，也不记录私人会话链接。

## 活动记录

| 日期（UTC） | Agent | 工作 | 基准提交／PR | 验证 | 下一步 |
|---|---|---|---|---|---|
| 2026-11-17 | Codex | 建立 Codex 与 Claude Code 共用的交接说明。 | 基准 `51eed29988990ce43c019d2c09c8e1965dd55a36`；[PR #79](https://github.com/zhihong1357/finance-research/pull/79) | 文档检查；等待 GitHub 检查 | 合并后等待 Phase 2.3 观察期结束并核验自动运行记录；12 月 3 日后核验 2.10 快照重建。两项通过前不开始 Phase 3。 |
| 2026-11-16 | Codex | 给质量检查增加失败提醒，后续成功运行时自动关闭。 | 基准 `93d94a488e2af1cfb541a78bf0c6d0edcd9179fa`；分支 `phase-2/quality-check-failure-alert`；[PR #80](https://github.com/zhihong1357/finance-research/pull/80) | 未在本地运行检查；等待 PR 自动检查 | 检查 PR 结果；不手动启动采集；Phase 2 两项剩余验收通过前不开始 Phase 3。 |
| 2026-11-16 | Codex | PR #80 的首次 GitHub CI 检查通过。 | PR #80；提交 `2f2d83d233fe437a5b95750c7f5d677da0810d69` | 检查 #171 成功；未手动启动采集或质量检查 | 等当前 PR 检查完成，再由所有者以 Create a merge commit 合并。 |
| 2026-11-16 | Codex | 质量检查失败提醒通过 PR #80 合并。 | 基准 `93d94a488e2af1cfb541a78bf0c6d0edcd9179fa`；merge commit `27bd71cfb2e896de1d18411cac1e67655289d7d5` | PR 检查 #173 通过；未手动启动质量检查或采集 | 等待 Phase 2.3 观察期结束与 Phase 2.10 首个完整月份重建核验；两项通过前不开始 Phase 3。 |
| 2026-11-16 | Codex | 准备 Phase 2 验收总结框架，只保留结论和待验收项。 | 基准 `5ce59b6164799ca18d5121ddd51b6000d41e9f32`；分支 `phase-2/acceptance-report-skeleton`；PR 待创建 | 文档审阅；未运行本地检查或数据任务 | 完成 PR；Phase 2 验收继续按路线图门槛推进。 |
| 2026-11-16 | Codex | 为 Phase 2 验收总结框架开 PR #82。 | 基准 `5ce59b6164799ca18d5121ddd51b6000d41e9f32`；[PR #82](https://github.com/zhihong1357/finance-research/pull/82) | 文档审阅；未运行本地检查或数据任务；等待 PR CI | 所有者审核并合并后，等待 Phase 2.3 与 2.10 验收；通过前不开始 Phase 3。 |
| 2026-11-18 | Claude Code | 所有者决定由 Claude Code 恢复主责；起草并采纳决策 0017：Phase 3 在 Phase 2 等待验收期间开工，附五条不影响验收的约束。核对了 11-16 以来的采集记录：窗口内没有人手动启动采集，补跑都由系统启动；11-16 有一次 DVOL 失败后自行恢复。 | 基准 `01e91198caae8d0a2b9ac1100c485c75a8f3b01b`；分支 `claude/blissful-goodall-b5rewq`；拉取请求待创建 | 本地跑了 ruff、pyright、pytest；未启动任何工作流 | 所有者合并后开始 Phase 3 的开工核实与拆分；11-30 核对 2.3，12-04 核对 2.10。 |
| 2026-11-18 | Claude Code | 为决策 0017 开 PR #83（草稿）。 | 基准 `01e91198caae8d0a2b9ac1100c485c75a8f3b01b`；[PR #83](https://github.com/zhihong1357/finance-research/pull/83) | 本地检查已过；等待 PR 的 CI | 所有者审核后以合并提交合并；之后开始 Phase 3 的开工核实与拆分。 |
| 2026-11-18 | Claude Code | 决策 0017 随 PR #83 合并。开始 Phase 3 的 3.0：核对本阶段要用的文献出版信息，把 Phase 3 拆成 3.0 至 3.17，标出哪些在 Phase 2 等待期间做；路线图新增两项待所有者决定的事；术语表补了元研究用到的词。 | 基准 `d0b16f90a532517248ec17780c8f40121b4781e2`；分支 `claude/blissful-goodall-b5rewq`；拉取请求待创建 | 本地跑了 ruff、pyright、pytest；只改文档；未启动任何工作流 | 合并后做 3.1 数值底座。 |
| 2026-11-18 | Claude Code | 为 3.0 开 PR #84（草稿）。 | 基准 `d0b16f90a532517248ec17780c8f40121b4781e2`；[PR #84](https://github.com/zhihong1357/finance-research/pull/84) | 本地检查已过；等待 PR 的 CI | 所有者审核后以合并提交合并；之后做 3.1 数值底座。 |
| 2026-11-18 | Claude Code | 3.0 随 PR #84 合并。试做 3.1 时发现：装上 numpy 后数据指纹和细线结果逐位不变，但类型检查要求改 `data/bars.py`、`data/open_interest.py`、`quality.py`；这三个文件会被所有工作流共用的命令行入口加载。所有者决定 numpy 等 Phase 2 验收之后再加；路线图把 3.1、3.6 各拆成两步，标出要 numpy 的步骤。 | 基准 `1ad08e32389252bd1100b333476138336603d556`；分支 `claude/blissful-goodall-b5rewq`；拉取请求待创建 | 本地跑了 ruff、pyright、pytest；只改文档；未启动任何工作流 | 合并后做 3.1a。 |
| 2026-11-18 | Claude Code | 为 Phase 3 顺序调整开 PR #85（草稿）。 | 基准 `1ad08e32389252bd1100b333476138336603d556`；[PR #85](https://github.com/zhihong1357/finance-research/pull/85) | 本地检查已过；等待 PR 的 CI | 所有者审核后以合并提交合并；之后做 3.1a。 |
| 2026-11-18 | Claude Code | Phase 3 顺序调整随 PR #85 合并。做 3.1a：蒙特卡洛估计的比例、Wilson 区间、均值和所需次数，每个函数一个手算的例子。 | 基准 `c081758a7897c3b18ffdccaca2191167e9090355`；分支 `claude/blissful-goodall-b5rewq`；拉取请求待创建 | 本地跑了 ruff、pyright、pytest（781 个通过）；只新增文件和改文档；未启动任何工作流 | 合并后做 3.2。 |
| 2026-11-18 | Claude Code | 为 3.1a 开 PR #86（草稿）。 | 基准 `c081758a7897c3b18ffdccaca2191167e9090355`；[PR #86](https://github.com/zhihong1357/finance-research/pull/86) | 本地检查已过；等待 PR 的 CI | 所有者审核后以合并提交合并；之后做 3.2。 |
| 2026-11-18 | Claude Code | 3.1a 随 PR #86 合并。做 3.2：Holm、Benjamini–Hochberg、Benjamini–Yekutieli 的调整后 p 值；手算例子、性质测试，以及已知真假的合成检验族上的错误率检查（独立和正相关两种情形），另有一个"不校正时检查会报警"的对照。核对了三篇原文的出版信息。 | 基准 `b9e7e9e54c77689e4ad471750f02746ac441d45d`；分支 `claude/blissful-goodall-b5rewq`；拉取请求待创建 | 本地跑了 ruff、pyright、pytest（800 个通过）；只新增文件和改文档；未启动任何工作流 | 合并后做 3.5。 |
| 2026-11-18 | Claude Code | 为 3.2 开 PR #87（草稿）。 | 基准 `b9e7e9e54c77689e4ad471750f02746ac441d45d`；[PR #87](https://github.com/zhihong1357/finance-research/pull/87) | 本地检查已过；等待 PR 的 CI | 所有者审核后以合并提交合并；之后做 3.5。 |
| 2026-11-18 | Claude Code | 3.2 随 PR #87 合并。做 3.5：k 折和组合式交叉验证，训练集按标签区间清洗、测试段之后留禁区，组合式的完整回测路径。手算例子之外，在纯噪声、标签重叠的合成数据上验证：不清洗时"预测"显著为正，清洗后与 0 一致。途中发现按单条序列算 t 统计量会因标签重叠而夸大，改为多次独立重复、按重复之间的离散度判断。 | 基准 `1fb2dc893e91939d4c8d44875901c977b243ea8d`；分支 `claude/blissful-goodall-b5rewq`；拉取请求待创建 | 本地跑了 ruff、pyright、pytest（809 个通过）；只新增文件和改文档；未启动任何工作流 | 合并后做 3.6a。 |
| 2026-11-18 | Claude Code | 为 3.5 开 PR #88（草稿）。 | 基准 `1fb2dc893e91939d4c8d44875901c977b243ea8d`；[PR #88](https://github.com/zhihong1357/finance-research/pull/88) | 本地检查已过；等待 PR 的 CI | 所有者审核后以合并提交合并；之后做 3.6a。 |
| 2026-11-18 | Claude Code | 3.5 随 PR #88 合并。做 3.6a：夏普的标准误（非正态）、概率夏普、最短记录长度、N 次试验的期望最大夏普、折损夏普。重现了原文的数值例子（DSR 0.9004，46 次时 0.9505）。 | 基准 `68264cdf475cc980da27087a411cc8b88495cd19`；分支 `claude/blissful-goodall-b5rewq`；拉取请求待创建 | 本地跑了 ruff、pyright、pytest（819 个通过）；只新增文件和改文档；未启动任何工作流 | 合并后做 3.8。 |
| 2026-11-18 | Claude Code | 为 3.6a 开 PR #89（草稿）。 | 基准 `68264cdf475cc980da27087a411cc8b88495cd19`；[PR #89](https://github.com/zhihong1357/finance-research/pull/89) | 本地检查已过；等待 PR 的 CI | 所有者审核后以合并提交合并；之后做 3.8。 |
| 2026-11-18 | Claude Code | 3.6a 随 PR #89 合并。做 3.8：夏普和两组比较的功效与所需数据量，相依数据按长期方差放大系数折算（AR(1)、重叠标签）。核对了规程第 2 节假说卡示例的"约需 240 天"（算出每组 119、共 238）。三个蒙特卡洛对照都在 99% 区间内；忽略相依性时的功效被判为不符。 | 基准 `e38b4ea80b9dab40fa20bfd4a89150adba21d177`；分支 `claude/blissful-goodall-b5rewq`；拉取请求待创建 | 本地跑了 ruff、pyright、pytest（830 个通过）；只新增文件和改文档；未启动任何工作流 | 合并后做 3.9。 |
| 2026-11-18 | Claude Code | 为 3.8 开 PR #90（草稿）。 | 基准 `e38b4ea80b9dab40fa20bfd4a89150adba21d177`；[PR #90](https://github.com/zhihong1357/finance-research/pull/90) | 本地检查已过；等待 PR 的 CI | 所有者审核后以合并提交合并；之后做 3.9。 |
| 2026-11-18 | Claude Code | 3.8 随 PR #90 合并。做 3.9：把规程第 5 节的七项检验写成一个判定函数（t、折损夏普、回测过拟合概率、成本加倍、去掉最好的 2 笔、参数扰动、分段），阈值取自假说卡；不适用的检验明确标出，不当作通过。 | 基准 `3660228264032484d9b1672d141669a7275a7a72`；分支 `claude/blissful-goodall-b5rewq`；拉取请求待创建 | 本地跑了 ruff、pyright、pytest（846 个通过）；只新增文件和改文档；未启动任何工作流 | 合并后等 11-30 和 12-04 的定时核对。 |
| 2026-11-18 | Claude Code | 为 3.9 开 PR #91（草稿）。 | 基准 `3660228264032484d9b1672d141669a7275a7a72`；[PR #91](https://github.com/zhihong1357/finance-research/pull/91) | 本地检查已过；等待 PR 的 CI | 所有者审核后以合并提交合并；之后等 11-30 和 12-04 的定时核对。 |
| 2026-11-18 | Claude Code | 3.9 随 PR #91 合并。等待期间能做的 Phase 3 步骤全部完成，进入等待：下一次工作是 11-30 的 2.3 核对。 | 基准 `7618e2370d10fe16a7d5a64c5954353e34aeb3bf` | 只改交接页；未启动任何工作流 | 11-30 核对 2.3，12-04 核对 2.10。 |
