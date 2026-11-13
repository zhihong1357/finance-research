# 注册表

这里是科研规程的机器可读部分。格式和规则见[架构](../docs/architecture.md)第 3 节和[科研规程](../docs/protocol.md)。

| 位置 | 内容 | 谁来写 |
|---|---|---|
| `hypotheses/` | 假说卡，一个假说一个 TOML 文件，文件名就是编号（`H-0001.toml`） | 人或 AI 会话，走拉取请求 |
| `trials.jsonl` | 试验账本 | 只由程序自动追加 |
| `owner_decisions.jsonl` | 所有者的决定：开启金库、放行到下一级、资金配置 | 所有者在对话里明确同意后由 AI 追加，所有者合并即确认 |
| `refusals.jsonl` | 拒绝记录：时点查询接口拒绝的每一次请求（前视、金库、未注册） | 只由程序自动追加 |
| `relations/` | 无套利关系 | 人或 AI 会话 |
| `sources/` | 数据源目录 | 人或 AI 会话 |

`owner_decisions.jsonl` 记录的是所有者的具体决定，和 `docs/decisions/` 里的设计决策记录不是一回事。

每条试验的标准报告在仓库根目录的 `reports/` 里，文件名就是试验编号。

每次提交，持续集成都运行四项检查：

- `finresearch registry check`：每个文件的格式，跨文件的规程规则，以及报告与账本互相对应。
- `finresearch registry check-append-only`：与上一个版本比较，三个只追加文件和假说卡的历史只能增加。
- `finresearch registry check-commits`：账本记录的代码版本都在历史里。
- `finresearch reproduce --synthetic`：重跑合成数据上的试验，指纹必须与账本一致。
