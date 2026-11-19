# 同类项目可借鉴清单

本文对照两个公开的同类项目，列出本仓库可以借鉴的具体做法：

- **Microsoft Qlib**：面向量化研究的 Python 平台，MIT 许可。看它的时点数据库（处理财报修订）和实验记录（Recorder），对照本仓库的"三个时间"、时点查询接口和试验账本。
- **QuantConnect LEAN**：开源的事件驱动回测与实盘引擎（C#），Apache-2.0 许可。看它的成交、滑点、手续费、保证金和强制平仓模型，以及加密永续合约的资金费，对照本仓库的 `sim/`。

格式和口径同[旧系统中可取用的部分](prior-systems.md)：只取做法，按本仓库的约定重写并配上测试，不继承结构。

## 0. 读了什么、怎么标

**版本。** 两个项目都在 2026-11-19 浅克隆到会话的临时目录读源码，不进仓库：

| 项目 | 提交 | 提交日期 |
|---|---|---|
| Qlib | [`5435523`](https://github.com/microsoft/qlib/tree/54355232463878d2eebb91fe0ee5fa7fa1f5976c) | 2026-10-08 |
| LEAN | [`53236fa`](https://github.com/QuantConnect/Lean/tree/53236fa4a23433a96c7f57cbe7850dba9be840ac) | 2026-11-18 |

下文的源码路径都相对于这两个提交；行号会随上游改动漂移，以函数名为准。

**标记。** 每条写明：

- **已核实**：读了上面版本的源码或仓库内的文档，描述的是代码实际做的事。
- **推测**：没读到源码或官方文档，或者是对"这样做值不值"的判断。

**许可。** 本文只描述做法、注明出处，没有复制任何代码。将来如果照搬某段实现（例如 LEAN 的强平数量二分搜索），按许可证署名：照搬 LEAN 的代码要保留 Apache-2.0 许可证文本和上游的版权与 NOTICE 声明，并在文件里写明改了什么；照搬 Qlib 的代码要保留 MIT 许可证的版权声明。只借鉴思路、自己重写的不需要。

**阶段约束。** 建议改的地方按[落地顺序](roadmap.md)和决策 [0017](decisions/0017-phase-3-during-phase-2-acceptance.md)、[0020](decisions/0020-later-phases-during-waits.md)、[0022](decisions/0022-fixes-to-wait-modules.md) 排：Phase 2 验收（预计 2026-12-04）之前，只能新增文件，或改 `tests/test_wait_isolation.py` 划定的等待期间模块（`sim/` 全部、`config/costs.toml`、`config/simulator.toml` 在内）；`runner.py`、`registry/`、`data/pit.py` 等录制流程加载的文件，要等验收之后。

## 1. 总结

| 编号 | 一句话 | 改哪里 | 何时 | 值不值 |
|---|---|---|---|---|
| L1 | **补上保证金和强平**：现在的模拟器里现金可以无限为负，爆仓的策略也能"熬回来" | 新增 `sim/margin.py`，`sim/engine.py` 接入 | Phase 4，需要先由所有者定一条新步骤（见 L1） | 最高：红队指出的缺口，杠杆策略的回测结果会系统性偏乐观 |
| L2 | 能立即成交的限价单按吃单算，不按挂单 | `sim/engine.py` | Phase 4，现在可做 | 高：主动挂价的策略手续费被低估 |
| L3 | 每根 K 线的成交量上限（参与率），超出的部分留到下一根或撤单 | `sim/engine.py`、`config/simulator.toml` | Phase 4.6 与参与率约束一起 | 高：小币种上容量和成本被高估 |
| L4 | 订单没写到期时给一个默认的最长等待，避免在很久以后的价格上成交 | `sim/engine.py`、`config/simulator.toml` | Phase 4，现在可做 | 中：Bybit 没有成交的分钟没有 K 线，订单可能挂很久 |
| L5 | 最小下单单位、最小名义金额、价格最小变动 | 上市表加字段，`sim/engine.py` 取整 | Phase 4.8 | 中：小资金、低价币上影响明显 |
| L6 | 资金费用的标记价格要取结算那一分钟的，接口加一条测试 | 4.8 的数据到模拟器的转换 | Phase 4.8 | 中：防止一个容易写错的地方 |
| Q1 | 运行开始就记一笔"尝试"，中途崩溃的运行也看得见 | `runner.py`，新的只追加文件 | Phase 2 验收之后、第一次真实数据运行之前 | 中高：堵住"跑到一半看了结果、让它崩掉、重跑"的漏洞 |
| Q2 | 试验之间的依赖：边际贡献检验引用它用到的信号版本和基准模型的试验 | `registry/models.py` 的试验记录 | Phase 4.7 | 中：重现和审查时能顺藤摸瓜 |
| Q3 | 时点数据库：我们的设计已经覆盖 Qlib，不需要改；Qlib 的日粒度陷阱写成一条回归测试（现有测试没有这一格） | `tests/`（新增） | 现在可做 | 低，但便宜 |

不建议借鉴的，见第 4 节。

## 2. Qlib

### Q0. 时点数据库：对照"三个时间"（已核实，结论是不需要改）

**它怎么做。**

- 每个财务指标一个二进制文件 `<字段>_q.data`（季度）或 `_a.data`（年度），每行四列：`date`（公布日期，整数 `YYYYMMDD`）、`period`（报告期，整数，如 `201904`）、`value`、`_next`（同一报告期下一次修订所在的字节位置，构成链表）。另有一个 `.index` 文件记录每个报告期第一次出现的位置。见 [`docs/advanced/PIT.rst`](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/docs/advanced/PIT.rst)。
- 查询在 [`qlib/data/data.py`](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/qlib/data/data.py) 的 `LocalPITProvider.period_feature`：给定当前时刻 `cur_time`，先用 `np.searchsorted(data["date"], cur_time_int, side="right")` 找到公布日期不晚于当天的全部记录，再对每个报告期沿链表找到当天之前最新的那次修订（`qlib/utils/__init__.py` 的 `read_period_data`）。`end_index` 必须 ≤ 0，不支持引用未来的报告期。
- 表达式层（[`qlib/data/pit.py`](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/qlib/data/pit.py)）的 `P` 算子把报告期序列按交易日历"展开"成每日序列，每天取当时最新报告期的值；`PRef(字段, 报告期)` 查固定一个报告期在每天的已知值。
- 数据来自 baostock，`pubDate` 当作 `date`、`statDate` 当作 `period`（`scripts/data_collector/pit/collector.py`）。写入脚本 `scripts/dump_pit.py` 有 `overwrite` 选项，可以整份重写。

**我们现在怎么做。** 每行带 `event_time`、`available_at`、`ingested_at`，精确到微秒（[架构](architecture.md) 2.1）；修订版本表一行是一个观测在一个版本里的值，`versions_as_of` 取每个观测在 t 时已公布的最新一版（2.2 的"修订版本表"），这与 Qlib 的"每个报告期取当时最新的一次修订"语义相同。所有查询经过带时钟上限的接口（2.3），原始层只增不改。

**对照。**

| 方面 | Qlib | 本仓库 |
|---|---|---|
| 公布时刻的精度 | 日。当天公布的数据，当天任何时刻都看得到 | 微秒。只知道日期的，`available_at` 取当天结束（2.2 的"宏观数据的公布时刻"） |
| 入库时刻 | 没有。答不了"我们的数据库在某天知道什么" | `ingested_at`，原始层只增不改，规范层逐次加工都留着 |
| 防前视 | `end_index ≤ 0` 的断言 | 接口的时钟上限，越界即拒绝并记账；结构测试禁止绕过 |
| 修订数据的覆盖范围 | 只支持季度和年度（文档的"已知局限"） | 日、月、季度共用一种表 |
| 写入 | 可以 `overwrite` | 只追加 |

**建议。** 数据模型不需要改。Qlib 的日粒度是一个典型陷阱：盘后公布的财报在当天开盘时就"已知"。我们的接口不会犯这个错，但值得把它写成一条回归测试固定下来（Q3）。

### Q1. 记下"开始了但没有结束"的运行（已核实做法；值不值是推测）

**它怎么做。** [`qlib/workflow/recorder.py`](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/qlib/workflow/recorder.py) 的 `MLflowRecorder`：

- `start_run` 一开始就建立一条运行记录，状态为"运行中"，记下命令行和以 `_QLIB_` 开头的环境变量；`end_run` 把状态改成"完成"或"失败"等四种之一。
- `_log_uncommitted_code` 把 `git diff`、`git status`、`git diff --cached` 的输出作为附件存下来，所以工作区不干净的运行也能事后看出改了什么。
- 记录是可变的：`Experiment.delete_recorder` 可以删除一次运行（`qlib/workflow/exp.py`），没有指纹链。

**我们现在怎么做。** 试验账本只在运行**结束**并带 `--record` 时追加一条（[架构](architecture.md) 3.1）；工作区不干净的运行直接拒绝入账。账本只追加、带指纹链、与 git 历史比对，这些都比 Qlib 严。但一次在真实数据上读了数据、算到一半就崩溃（或被人中断）的运行，不在任何地方留下痕迹。

**缺口（推测）。** 规程第 7 节说漏记的恰恰是结果不好、被随手丢弃的尝试。"跑到一半、在日志里看到了中间结果、然后让它崩掉、改一改重跑"是同一种漏记，只是更隐蔽。拒绝记录（`refusals.jsonl`）解决了越界的请求，没有解决这一种。

**建议。** 运行框架在建好时钟、读第一张表之前，向一个新的只追加文件（例如 `registry/attempts.jsonl`，同样带指纹链）写一条"开始"，结束时由试验账本那一条引用它。账本检查加一条：开始了却没有对应试验的尝试，在报告里列出来。尝试不计入折损次数（它没有产出绩效指标），但所有者看得到。不借鉴 Qlib 存 `git diff` 的做法：我们干脆不让不干净的运行入账，更简单也更严。

**何时、改哪里。** `runner.py`、`registry/ledger.py`、`registry/validate.py`，都是录制流程加载的文件，Phase 2 验收之后；必须在第一次真实数据运行（Phase 5）之前。改账本格式要写一条决策记录。

### Q2. 试验之间的依赖（已核实做法；值不值是推测）

**它怎么做。** [`qlib/workflow/record_temp.py`](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/qlib/workflow/record_temp.py) 的 `RecordTemp` 有 `depend_cls`：信号分析（`SigAnaRecord`）和组合回测（`PortAnaRecord`）都声明依赖信号预测（`SignalRecord`），`check()` 在生成之前确认上游的产物存在。

**我们现在怎么做。** 一条试验记录有运行清单（代码、数据、配置、环境、种子、时钟），但不记录它用到了哪些**别的试验**的产物。假说卡的 `plan.baseline_model` 是一段文字。

**建议。** 边际贡献检验（Phase 4.7）天然依赖"这个信号的哪个版本"和"作为基准的整体模型是哪个版本"。在试验记录里加一个 `inputs` 字段，列出所依赖的试验编号；账本检查确认被引用的试验存在、早于本条。这样审查者（质疑者）能从一条确认检验一路追到它用到的每个信号版本。

**何时、改哪里。** `registry/models.py` 的 `TrialRecord`，Phase 4.7 与边际贡献检验一起做；改账本格式要写决策记录。

### Q3. 把"当天公布、当天可见"的陷阱写成回归测试（已核实做法；建议很小）

**它怎么做。** 见 Q0：Qlib 用 `searchsorted(..., side="right")` 按日期比较，一份 13:30 公布的数据在当天 00:00 就能查到。

**我们现在怎么做。** 2.6 的验收在真实数据上抽查"新版本公开前一微秒查到旧值、公开那一刻查到新值"（[架构](architecture.md) 2.2 末尾）。合成数据上的单元测试（`tests/data/test_vintages.py`、`tests/data/test_pit.py`）查询的时刻与公布时刻都相隔一天以上，或者是在 0 点公布；没有"同一天、公布时刻之前"这一格（已核实）。

**建议。** 新增一条测试（只新增文件）：一个观测的新版本在某天 13:30 公布，在 13:29:59.999999 用 `as_of` 和 `window` 查，必须是旧值；在当天 00:00 查也必须是旧值。现在可做，不碰 `data/pit.py`。

### Qlib 的回测器：参与率上限（已核实；并入 L3）

[`qlib/backtest/exchange.py`](https://github.com/microsoft/qlib/blob/54355232463878d2eebb91fe0ee5fa7fa1f5976c/qlib/backtest/exchange.py) 的 `Exchange` 有 `volume_threshold`：按当天或累计成交量给买卖设上限，订单数量被截到上限以内；冲击成本是 `impact_cost × (成交金额 ÷ 当日总成交金额)²`，即参与率的平方，没有母单的概念。冲击公式不借鉴（我们的平方根定律有实证依据，见路线图 4.0 的核对），参与率上限借鉴，见 L3。

## 3. LEAN

### L1. 保证金和强制平仓（重点）

先解释几个词。**保证金**：开永续合约仓位时押在交易所的钱。**初始保证金**是开仓需要的，等于仓位价值 ÷ 杠杆倍数；**维持保证金**是持仓期间至少要留着的，低于它就会被**强制平仓**（强平）：交易所接管仓位、按市价平掉，通常还收一笔强平手续费。交易所判断强平用的是**标记价格**（[术语表](glossary.md)），不是最新成交价。

**它怎么做（已核实，读了源码）。**

- **保证金模型**：[`Common/Securities/CryptoFuture/CryptoFutureMarginModel.cs`](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Securities/CryptoFuture/CryptoFutureMarginModel.cs)。初始保证金 = |仓位价值| ÷ 杠杆，默认杠杆 25。**维持保证金直接等于初始保证金**（`GetMaintenanceMargin`），没有按仓位大小分档；带维持保证金率参数的旧构造函数已标为弃用，参数被忽略。USDT 本位合约以计价货币作抵押，币本位以基础货币作抵押；抵押相同的合约共享保证金（全仓）。
- **剩余保证金**：组合总价值 − 未结算现金 − 已用保证金（`Common/Securities/SecurityPortfolioManager.cs` 的 `GetMarginRemaining`）。仓位用 `security.Price` 估值，即最新价，不是标记价格。
- **追加保证金和强平**：[`Common/Securities/DefaultMarginCallModel.cs`](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Securities/DefaultMarginCallModel.cs)。剩余保证金 ≤ 组合价值的 5% 时发警告；剩余保证金 ≤ 0、且扣掉已经在路上的强平单之后已用保证金仍大于组合价值 × (1 + 10% 缓冲) 时，为每组仓位生成**刚好够**把保证金拉回来的反向市价单（数量用二分搜索或按差额反推）。
- **检查频率**：[`Engine/AlgorithmManager.cs`](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Engine/AlgorithmManager.cs) 里 `marginCallFrequency = TimeSpan.FromMinutes(5)`：回测时每 5 分钟检查一次。强平单走普通的成交模型，按普通吃单手续费，**没有**强平手续费、保险基金、自动减仓（ADL）和"强平价格"的概念。
- **下单前检查**：`Common/Securities/BuyingPowerModel.cs` 的 `HasSufficientBuyingPowerForOrder`，保证金不够的订单被拒。

**LEAN 这样做的偏差（推测，方向由源码推出）。**

| 偏差 | 方向 |
|---|---|
| 维持保证金 = 初始保证金，比交易所的分档维持保证金率高得多 | 偏保守：比现实更早被强平 |
| 每 5 分钟、按最新价检查，看不到中间的影线 | 偏乐观：现实中会被强平的影线被跳过 |
| 强平按下一个市价、普通手续费成交，没有强平费 | 偏乐观：现实的强平损失更大 |
| 只平到"刚好够"，不是整仓 | 视交易所规则而定（部分强平只适用于部分档位，未核实） |

**我们现在怎么做（已核实，读了 `sim/engine.py`）。** 没有保证金、没有杠杆上限、没有强平。`Result.cash` 可以任意为负，权益曲线可以跌破零再"恢复"。对一个用杠杆做多小币种的策略，回测会把现实中早已爆仓的路径当成活着的路径，最终收益和夏普都会偏高，而且偏高的恰恰是厚尾、高杠杆这一类最危险的策略。外部红队审查指出的缺口属实。

**建议怎么改。** 新增 `sim/margin.py`（纯函数，配手算测试），在 `sim/engine.py` 里每根 K 线收盘记权益之前调用：

1. **配置**（新文件 `config/margin.toml`，或并入 `config/simulator.toml`）：每家交易所按名义金额分档的维持保证金率和速算扣除额、强平手续费率、初始杠杆上限。数值必须在实现时从 Binance 和 Bybit 的官方页面核对（**未核实**，本次没有读交易所文档），像路线图 4.0 那样记下来源和读取日期。
2. **下单检查**：成交后所需的初始保证金超过可用权益的订单不成交，记入 `unfilled` 并标原因（借鉴 LEAN 的 `HasSufficientBuyingPowerForOrder`）。
3. **每根 K 线检查，用对自己不利的极值**：多头用这根 K 线的最低价、空头用最高价估值，算权益与维持保证金。理由：我们的标记价格表 `u1.mark_1m` 只存每分钟的收盘（[决策 0010](decisions/0010-u1-minute-data.md)），看不到分钟内的标记价格极值；标记价格比成交价平滑，用成交价的极值判断，触发得不会比现实晚（偏保守）。另在有标记价格时用它的收盘再查一次。这一步不借鉴 LEAN 的"每 5 分钟、按最新价"。
4. **强平怎么成交**：触发即整仓平掉（比 LEAN 的"刚好够"保守；部分强平的规则核实之后再考虑），价格取触发价再按冲击模型扣滑点，另收强平手续费。权益到零即这次运行"爆仓"，之后不再交易。压力情形下按 K 线的极值成交。
5. **输出**：是否爆仓、何时爆仓、全程离强平最近的距离（最低的保证金率）。规程第 8 节"成本与容量"一节报告它们；判定函数可以把"保守情形下爆仓"作为直接不通过的条件（这一点改判定规则，要走决策记录）。
6. **测试**：手算场景（一笔 10 倍多单、下一根 K 线最低价跌 9.5%，按分档维持保证金率算出是否强平和剩余现金）；性质测试：没有杠杆（权益始终大于仓位价值）时，结果与不接入保证金模块逐位相同。

**何时。** 它只是计算规则，不读数据、不依赖 Phase 3 的结果，`sim/` 在决策 0022 的范围内，按决策 0020 可以在等待期间做。但它是路线图 Phase 4 拆分（4.0 至 4.9）里没有的一步，加步骤改变了本阶段的范围，属于需要所有者确认的事：建议写一条决策记录（提议），在 4.3 之后加"4.3b 保证金与强平"，同时把"保守情形下不爆仓"加进 Phase 4 的验收。所有者同意之后再写代码。

**为什么值得。** 这是本清单里唯一一条会让现有回测结论**方向性**偏乐观的缺口；其余各条都是精度问题。Phase 7 模拟盘和这里共用代码，现在不补，到时也要补。

### L2. 能立即成交的限价单按吃单算（已核实）

**它怎么做。** [`Common/Orders/Fees/BinanceFeeModel.cs`](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Orders/Fees/BinanceFeeModel.cs) 的 `GetFee`：限价单只有在"只做挂单"（PostOnly）或下单时不能立即成交（`!order.IsMarketable`）时才按挂单费率，否则按吃单。

**我们现在怎么做。** `sim/engine.py` 里所有限价单成交都按挂单（`Liquidity.MAKER`），只收挂单费，不收价差和冲击，成交在限价上。一张买入限价单生效时，如果第一根 K 线的开盘价已经低于限价，现实中它会立即按接近开盘价的价格作为吃单成交。我们的处理在价格上偏保守（按限价，比开盘价差），在费用上偏乐观（挂单费、没有价差和冲击）；两者相抵后哪边大，取决于限价离开盘价多远。

**建议。** 生效时第一根 K 线的开盘价已经穿过限价的，按配置二选一：(a) 当作吃单，按开盘价成交，收吃单费、半个价差和冲击；(b) 当作"只做挂单"被交易所拒绝，记入 `unfilled`。默认 (a)。改 `sim/engine.py`，加 `config/simulator.toml` 的一个开关和手算测试。现在可做（决策 0022 的范围）。

### L3. 每根 K 线的成交量上限（已核实做法）

**它怎么做。**

- LEAN 的 [`Common/Orders/Slippage/VolumeShareSlippageModel.cs`](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Orders/Slippage/VolumeShareSlippageModel.cs)：滑点 = (订单量 ÷ K 线成交量)² × 系数，参与率被**截在**上限（默认 2.5%）。截断之后滑点不再增长，订单却照样全部成交，这是一个反面例子：大单的成本被低估。默认成交模型 `FillModel.MarketFill` 总是全部成交（"assume the order completely filled"）。
- Qlib 的 `volume_threshold`（见第 2 节末尾）是正面例子：订单数量被截到成交量上限以内。

**我们现在怎么做。** 模拟器的 `Bar` 没有成交量，订单不论多大都在一根 K 线里全部成交；成交量只以 `Market.daily_volume` 的形式进冲击成本。参与率上限写在架构 5.3 的组合构建约束里（Phase 4.6），模拟器本身不设防。

**建议。** `Bar` 加一个可选的成交量；一根 K 线上同一品种的成交合计不超过 `max_participation × 成交量`（放 `config/simulator.toml`，压力情形更低），超出部分留到下一根，订单到期仍未成交的部分撤单。组合构建器的约束是第一道防线，模拟器的上限是第二道：防止组合构建器的错误悄悄变成不可能的成交。和 4.6 的参与率约束一起做，用同一个配置值。

### L4. 订单的最长等待（已核实做法）

**它怎么做。** `Common/Orders/Fills/FillModel.cs` 的 `MarketFill`：最近一笔数据比订单时刻旧一个周期以上（`StalePriceTimeSpan`）时，订单等新数据，不按陈旧的价格成交；按陈旧价格成交的，在成交记录里写明。

**我们现在怎么做。** 市价单在"生效之后开始的第一根 K 线"的开盘价成交，没有上限。Bybit 的 1 分钟数据在没有成交的分钟没有行（[架构](architecture.md) 2.2），冷门合约可能几小时没有 K 线；没写到期的订单会在很久以后成交，那时的价格与做决定时已无关系。这不是前视，但会让成交时刻和延迟变得不可控。

**建议。** 没写 `expires_at` 的订单给一个默认的最长等待（配置，例如几个 K 线周期）；结果里报告每笔成交的实际延迟（成交时刻 − 决定时刻）的分布。现在可做。

### L5. 下单单位和最小金额（已核实 LEAN 有这些字段；交易所的具体数值未核实）

**它怎么做。** [`Common/Securities/SymbolProperties.cs`](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Securities/SymbolProperties.cs) 记每个品种的 `LotSize`（数量的最小单位）、`MinimumOrderSize`、`MinimumPriceVariation`（价格最小变动）；`Common/Orders/OrderSizing.cs` 的 `AdjustByLotSize` 把下单数量向零取整到整数个单位。

**我们现在怎么做。** 数量和价格都是任意浮点数。架构 2.5 的上市表记合约乘数，还没有数量单位、最小金额和价格最小变动。

**建议。** 上市表加这三项（来自交易所的品种信息，Phase 2 的上市表填入时一并取）；模拟器把数量向零取整、低于最小金额的订单不成交、限价取整到价格最小变动。Phase 4.8 在真实品种上验收时做。对小资金、低价币，取整误差可以占到一笔交易的可观比例。

### L6. 资金费：按数据里的结算时刻和标记价格（已核实；我们的做法更好）

**它怎么做。** [`Common/Securities/CryptoFuture/BinanceFutureMarginInterestRateModel.cs`](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Securities/CryptoFuture/BinanceFutureMarginInterestRateModel.cs)：结算时刻写死为 00:00、08:00、16:00（`GetNextFundingRateApplication`）；费率取缓存里最近一条资金费率数据；资金费 = −费率 × 仓位价值，仓位按 `security.Price`（最新价）估值。Bybit 的模型（`BybitFutureMarginInterestRateModel.cs`）直接继承它，规则相同。

**我们现在怎么做。** 按数据里的实际结算时刻结算（4.0 核实过：有 8、4、2、1 小时的间隔），仓位按 `FundingEvent.mark_price` 估值；结算落在限价单成交那根 K 线内部时，取对自己更不利的一种（PR #119）。比 LEAN 准确，不改。

**建议。** 只加一条防错：Phase 4.8 把数据表转成模拟器输入时，`FundingEvent.mark_price` 要取 `u1.mark_1m` 里**结束于结算时刻**的那一分钟的收盘（结算 08:00:00，取 `event_time` 为 07:59 的一行），不能取 08:00 开始的那一分钟（它的收盘在结算之后）。为这个转换写一条手算测试。`samples` 为 0 的分钟价格是旧的（架构 2.2），转换时要么报错要么标出。

### LEAN 的手续费常数：一处与我们不同（已核实 LEAN 的值；哪个对未核实）

`Common/Orders/Fees/BinanceFuturesFeeModel.cs` 的 USDT 合约默认吃单费率是 0.04%，我们的 `config/costs.toml` 是 0.05%。路线图 4.0 已经记了"另有一个来源说 0.04%、官方页面没读到、取较高的 0.05%"，LEAN 是又一个说 0.04% 的二手来源，可能是旧费率。不改配置（偏保守），4.9 验收时从 Binance 官方页面核对。Bybit 合约的默认值（挂单 0.02%、吃单 0.055%，`BybitFuturesFeeModel.cs`）与我们一致。

### LEAN 的冲击模型：只作为校准时的对照（已核实）

[`Common/Orders/Slippage/MarketImpactSlippageModel.cs`](https://github.com/QuantConnect/Lean/blob/53236fa4a23433a96c7f57cbe7850dba9be840ac/Common/Orders/Slippage/MarketImpactSlippageModel.cs) 按 Almgren, Thum, Hauptmann & Li (2005) 分永久冲击和临时冲击，临时冲击的指数默认 0.6（我们的平方根定律是 0.5），再加正态噪声并乘一个 0 到 1 的随机数。源码注释自己说默认参数是二十年前美股的校准，建议重新校准。不借鉴它的随机性（我们要求同一结果指纹逐位重现，确定性的成本更简单）；Phase 3 之后用 U1 探索集校准冲击系数 Y 时，可以把指数 0.5 和 0.6 都试一下，作为敏感性检查（推测：差别不大，值得量一次）。

## 4. 不借鉴的

| 做法 | 出处 | 不借鉴的理由 |
|---|---|---|
| 市价单按最新价立即成交（没有延迟） | LEAN `FillModel.MarketFill`，`GetMarketFillPrice` 返回 `prices.Current` | 我们按决策 0012 要求延迟加下一根开盘价，更保守 |
| 限价单在跳空时按更好的价格成交（买单按 `min(最高价, 限价)`） | LEAN `FillModel.LimitFill` | 我们一律按限价，且必须被穿过，更保守 |
| 维持保证金 = 初始保证金、每 5 分钟检查 | LEAN `CryptoFutureMarginModel`、`AlgorithmManager` | 见 L1：一边过严、一边看不到影线 |
| 资金费的结算时刻写死 | LEAN `BinanceFutureMarginInterestRateModel` | 见 L6：现实里间隔会变 |
| 带随机数的滑点 | LEAN `MarketImpactSlippageModel` | 与逐位重现冲突 |
| 时点数据按日比较 | Qlib `LocalPITProvider` | 见 Q0、Q3 |
| 可删除、可覆盖的实验记录；存 `git diff` 代替拒绝不干净的运行 | Qlib `Recorder`、`dump_pit.py` | 我们的账本只追加、带指纹链，不干净的运行不能入账 |
| 用 pickle 存实验产物 | Qlib `Recorder.save_objects`（Qlib 自己已改为默认限制加载，见 `docs/component/recorder.rst`） | 我们的产物是 Markdown 和规范 JSON，不引入可执行的序列化格式 |

## 5. 本次没有核实的

- Binance、Bybit 的分档维持保证金率、强平手续费、部分强平的规则、保险基金和自动减仓：L1 实现前必须从官方文档核对。
- Binance U 本位合约当前的最低档吃单费率（0.04% 还是 0.05%）。
- LEAN 的官方文档页面（只读了源码）；Qlib 只读了仓库里的文档和源码，没有读它的论文。
