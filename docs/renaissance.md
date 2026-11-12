# 以文艺复兴为参照

本系统以文艺复兴科技（Renaissance Technologies）和它的大奖章基金（Medallion Fund）为方向标杆。本文回答四个问题：关于它，公开能确认的事实有哪些；从中学什么；它公开暴露过哪些不足，我们怎样在设计上避开；哪些差距无法靠设计弥补，以及在这种情况下"超越"应当怎样定义。

文艺复兴极少公开内部信息。下文的事实来自记者调查、法庭和国会文件、学术论文和投资人信件，每条都注明出处，见文末。没有出处的内容属于推断，会明确标出。

## 1. 公开可查的事实

### 业绩

- Zuckerman 在 2019 年的书中重建了大奖章 30 年的业绩：1988 至 2018 年，扣费前年均收益 66.1%，扣费后 39.1%。[1]
- 同一时期，大奖章没有一年的扣费前年度收益为负。扣费前收益的年化标准差约 31.7%。[2]
- 2020 年疫情冲击中，大奖章上涨 76%。[3]

### 方法

- **很小的胜率优势，乘以极大的交易数量。** 据 Zuckerman 记述，Mercer 对朋友说过：他们只对了 50.75% 的交易，但在这 50.75% 上是百分之百地对。书中的解读是，文艺复兴的优势来自同时进行的成千上万笔交易里一个微小而稳定的偏斜。[4]
- **单一整体模型。** 多数量化机构按策略分成多个独立的子组合，大奖章则把全部信号和组合约束合进一个整体模型。这样新信号可以直接加进去测试，立刻看出它对整体是否有增益。[5]
- **把现实细节做进模型。** 据书评转述，Brown 和 Mercer 接手了一个本来失败的股票模型，把真实市场的各种技术细节都纳入实现，做成一个覆盖全部组合、能自我校正的单一交易系统，策略由此从失败变成成功。[6]
- **风险自动收缩。** 策略失效或市场波动骤增时，系统倾向于自动降低仓位和风险。例如 1998 年秋，大奖章把期货交易削减了 25%。[4]
- **短周期、高换手、多市场。** 大奖章每年把持仓周转几十次，交易品种从大豆期货到法国国债。[7]
- **数据为本。** 早期的数据负责人 Straus 购买历史商品价格磁带，与已有数据合并，再逐一核对交易所年鉴、报纸等一切能找到的来源，确保价格一致准确。[8]
- **只招科学家。** 现任 CEO Brown 在 2023 年说，他们只招数学家、物理学家和计算机科学家，这些人没有金融背景，也和华尔街没有关系。[9]

### 规模

- 大奖章 2005 年起不再对外部投资者开放。[10]
- 2021 年时规模约 150 亿美元，只管理员工和少数亲友的钱。[11]
- 有评论指出，在几十万亿美元的市场里，文艺复兴只能运作几十亿美元。如果真的"解开了市场"，大奖章应当能做得大得多。[1]

## 2. 学什么

| 文艺复兴的做法 | 在本系统里的对应 | 文档 |
|---|---|---|
| 单一整体模型 | 所有研究产出的都是信号，进入同一个模型；信号是否有价值，看它对整体的边际贡献 | [架构](architecture.md) 第 5 节，[决策 0006](decisions/0006-single-model.md) |
| 微弱优势 × 极大广度 | 研究宇宙优先追求品种数量和独立下注次数，而不是单个品种上的准确率 | [决策 0007](decisions/0007-breadth-first-universe.md) |
| 把现实细节做进模型 | 成本、成交、资金费率、容量在模拟器里一开始就有，不是事后修正 | [落地顺序](roadmap.md) Phase 4 |
| 风险自动收缩 | 风险控制是模型的一部分，按规则自动执行，不靠人工判断 | [架构](architecture.md) 第 5 节 |
| 数据为本，多源核对 | 原始层只增不改；多数据源相互核对是数据质量检查的一部分 | [架构](architecture.md) 第 2 节 |
| 只招科学家 | 研究委员会按学科分工；假说靠检验成立，不靠故事成立 | [跨学科分析](disciplines.md) |
| 所有人共享全部代码 | 一个仓库，一个内核库，所有研究互相可见、可复用 | [架构](architecture.md) 第 4 节 |

有一处需要说明。据书评转述，到 1997 年，文艺复兴发现的交易信号里已有一半以上是"不符合直觉"的，即说不出机制。[6] 这和本系统的原则 2"先有机制，再看数据"看起来矛盾。

其实不矛盾。文艺复兴有几十年、覆盖全球市场的清洗过的逐笔数据，单靠统计证据就能把显著性做到很高。我们的数据少得多，同样的统计强度做不到，所以需要机制作为先验来补足。原则 2 并不禁止没有机制的信号，只是要求它通过更高的门槛。数据积累得越多，这个门槛对它的约束就越小。

## 3. 它公开暴露过的不足，以及我们怎样避开

这一节只根据公开报道。我们不知道文艺复兴内部怎样处理这些问题，所以下面说的是"公开可见的不足"，不是对其内部的判断。

| 公开事实 | 暴露的问题 | 本系统的设计 |
|---|---|---|
| 2020 年大奖章 +76%，而对外开放的 RIEF 亏 19%、RIDA 亏 32%。[3] 一位大奖章投资人说，RIEF 持仓 6 个月到 1 年，用因子风险模型对冲，所以亏损并不意外。[12] | 同一家机构、同一批人，方法换了一个持仓周期就不再有效 | 每条结论登记适用的周期和市场范围。用到范围之外，等于提出一个新假说，必须重新检验 |
| 2007 年 8 月"量化地震"中，RIEF 当月亏损超过 9%，公司归因于同类量化基金的抛售。[13] 研究认为那次危机源于全市场去杠杆和流动性骤然撤出。[14] | 与其他量化机构持仓重叠，在别人被迫平仓时一起受损 | 拥挤度和流动性压力是模型的风险输入；压力测试包含"同类策略集体平仓"情景 |
| 2005 至 2015 年，大奖章的高管通过一揽子期权，把短期交易收益按长期资本利得报税。[15] 参议院调查小组 2014 年估计少缴税款约 68 亿美元。[16] 2021 年和解，金额约 70 亿美元，是美国史上最大的税务和解之一。[17] | 收益的一部分来自监管和税务的灰色地带，最终以巨额和解收场 | 任何依赖法律或税务灰色地带的收益，一律不计入优势 |
| 大奖章的业绩来自记者重建，有学者认为它难以用现有理论解释。[2] | 外界无法独立验证 | 对我们自己而言，每个结论都能由账本和运行清单完整重算 |
| 规模被限制在百亿美元级别。[1] | 优势有容量上限 | 这是所有短周期策略的共性，对资金小的个人反而是优势：我们可以进入它因规模太大而进不去的地方 |

## 4. 做不到的事

有些差距不是设计能弥补的，必须直说。

| 文艺复兴拥有的 | 我们的现状 | 后果 |
|---|---|---|
| 几十年、覆盖全球主要市场、清洗过的逐笔数据 | 只有免费数据。加密市场有免费的逐笔历史，股票和期货基本只有日线 | 在传统市场上，短周期研究的原料不足 |
| 托管在交易所旁的执行系统、主经纪商的融资和杠杆 | 没有 | 毫秒级的优势与我们无关；杠杆受限 |
| 几百名研究员和工程师，三十多年的积累 | 一个人加 AI 助手 | 信号的发现速度和数量差几个数量级 |
| 大规模专用算力 | GitHub Actions 的免费运行器 | 模型的复杂度和数据规模受限 |
| 付费的另类数据 | 只有免费数据 | — |

因此，"年化收益超过大奖章"不是一个能通过设计达到的目标。把它定为目标，只会诱导系统去追逐回测里的高收益，而这正是过拟合的来源。

## 5. "超越"的可度量定义

本系统追求的超越，放在资金小的个人在结构上能够做到的维度上。

| 维度 | 目标 | 怎样度量 |
|---|---|---|
| 过程的严谨性 | 每个结论都能被独立重算；每次试验都有记录；误报率经过实测 | 元研究量出的误报率；可重算检查的通过率 |
| 风险调整后的收益 | 在不超过所有者设定的最大回撤的前提下，长期增长率超过低成本的基准组合 | 模拟盘和实盘的前向记录，对照基准 |
| 容量的利用 | 进入大机构因规模进不去的地方 | 每个信号的容量估计 |
| 范围纪律 | 没有一条结论被用到它被验证的范围之外 | 结论库里的适用范围与实际使用的对照 |
| 不依赖灰色地带 | 收益全部来自市场本身 | 收益来源分解 |

这些目标里，第一项完全由我们自己决定，可以做到最好。其余各项要靠市场给出答案。

## 6. 由此引出的三项设计调整

以下调整已经写进其他文档，这里汇总：

1. **单一模型。** 研究项目不再各自产出独立的策略，而是产出信号，全部进入同一个模型。见[决策 0006](decisions/0006-single-model.md)。
2. **广度优先的研究宇宙。** 第一个深入研究的市场是加密永续合约。原因是它是唯一一个免费就有大量品种、逐笔数据、多年历史、并且包含已下架品种的市场。见[决策 0007](decisions/0007-breadth-first-universe.md)。
3. **模拟器提前。** 成本和成交模型在任何信号研究之前就位，因为信号的价值只能在扣除成本后的整体组合里评估。见[落地顺序](roadmap.md)。

## 来源

1. [Jim Simons Built the Best Hedge Fund Ever](https://www.wealthmanagement.com/alternative-investments/jim-simons-built-the-best-hedge-fund-ever)，WealthManagement.com，转述 Zuckerman《The Man Who Solved the Market》附录 1
2. [Proof of Superior Investment Expertise?](https://www.cxoadvisory.com/investing-expertise/proof-of-superior-investment-expertise/)，CXO Advisory，转述 Cornell（2020）
3. [Renaissance Clients Exit After Firm's Anemic Run of Results](https://www.bnnbloomberg.ca/renaissance-clients-pull-out-after-firm-s-rotten-run-of-results-1.1560482)，Bloomberg
4. [The Man Who Solved the Market 读书笔记](https://calvinrosser.com/?p=5557)，Calvin Rosser
5. [Non-intuitive Lessons from The Man Who Solved the Market](https://marcellus.in/story/non-intuitive-lessons-from-the-man-who-solved-the-market/)，Marcellus
6. ["The Man Who Solved the Market": And the solution was… HMMs and regression](https://medium.com/@ilyakavalerov/the-man-who-solved-the-market-and-the-solution-was-hmms-and-regression-dd60cea5a6d7)，Ilya Kavalerov
7. [Renaissance 报道](https://web.stanford.edu/class/archive/cs/cs224n/cs224n.1106/renaissance.html)，Bloomberg 2007 年报道的存档
8. [How Renaissance Technologies Solved the Market: Part 1 — Pipeline](https://mleverything.substack.com/p/how-renaissance-technologies-solved-the-market-part-1-2814eb271dc3)
9. [Renaissance Technologies culture](https://www.efinancialcareers.com/news/2023/09/renaissance-technologies-culture)，eFinancialCareers，转述 Brown 在 Goldman Sachs 播客中的发言
10. [Renaissance's 2024 Rebirth](https://www.institutionalinvestor.com/index%2ephp/article/2e0uykr3vn5booz0smrcw/hedge-funds/renaissances-2024-rebirth)，Institutional Investor
11. [James Simons, Robert Mercer, others at Renaissance to pay up to $7 billion](https://www.foxbusiness.com/markets/james-simons-robert-mercer-renaissance-pay-7-billion-settle-tax-probe)，Fox Business 转载 WSJ
12. [Renaissance's Medallion Fund Surged 76% in 2020](https://www.institutionalinvestor.com/article/b1q3fndg77d0tg/)，Institutional Investor
13. [Quant funds in turmoil](https://mobile.next-finance.net/Quant-funds-in-turmoil)，Next Finance，2007 年 8 月
14. [Khandani & Lo, What Happened to the Quants in August 2007?](https://www.newyorkfed.org/medialibrary/media/research/conference/2010/cb/Lo1.pdf)，纽约联储会议论文
15. [Executives at hedge fund Renaissance to pay $7bn in back taxes](https://www.irishtimes.com/business/financial-services/executives-at-hedge-fund-renaissance-to-pay-7bn-in-back-taxes-1.4664159)，Irish Times
16. [Renaissance explores settlement as IRS seeks billions in taxes](https://www.accountingtoday.com/articles/renaissance-technologies-explores-settlement-as-irs-seeks-billions-in-taxes)，Accounting Today
17. [Renaissance executives agree to pay $7 billion to settle tax dispute](https://www.cnbc.com/2021/09/03/renaissance-executives-agree-to-pay-7-billion-to-settle-tax-dispute.html)，CNBC
