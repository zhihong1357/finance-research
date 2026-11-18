# Agent 交接

这里记录 Claude Code 与 Codex 的共享工作状态。仓库文件不会实时通知另一个 agent；每次开始任务都要重新读取本页。项目阶段和验收标准以 `docs/roadmap.md` 为准，本页只记录交接与下一步。

## 当前交接

- **负责 agent：** Claude Code（2026-11-18 起，所有者决定由 Claude Code 恢复主责）。Codex 在 Claude Code 额度用完时临时顶替；顶替前先读下面"Codex 顶替时"一节。
- **当前任务：** [决策 0017](decisions/0017-phase-3-during-phase-2-acceptance.md)：Phase 3 在 Phase 2 等待验收期间开工。分支 `claude/blissful-goodall-b5rewq`，基准 `01e91198caae8d0a2b9ac1100c485c75a8f3b01b`，拉取请求待创建。
- **下一步：** 所有者合并这个拉取请求之后，开始 Phase 3 的第一步"开工核实与拆分"（和 2.0 一样，只写文档：核实路线图 Phase 3 的产出和验收，拆成每步一个拉取请求的小步）。
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
