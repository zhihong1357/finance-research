# 0009 · 美股期权链：先向 Cboe 申请许可，获批前暂停录制

**状态：** 已采纳（2026-11-13）。所有者选择了待决定事项第 9 项的选项 (b)。

## 背景

Phase 0.5 起，旧仓库 `orderflow` 每个交易日从 Cboe 网站的延迟报价接口保存 SPX、SPY、QQQ、IBIT 四个标的的期权链原始文件（[架构](../architecture.md) 7.3）。

Phase 2.0 读 Cboe 网站条款时发现：条款只允许下载一份供个人非商业使用；把内容"存入电子检索系统"、发布、转发，都需要 Cboe 事先书面同意（[架构](../architecture.md) 7.5）。Cboe 的"Use of Cboe Content"页面说明了申请方式：发邮件到 permissions@cboe.com，写明用途、展示方式、发布计划和使用期限；获批之后还要签一份许可协议才算获得授权。

## 决定

1. 向 Cboe 申请许可，由所有者本人发邮件（申请内容由 AI 起草）。
2. 获批并签署许可协议之前，暂停录制：`orderflow` 的 `config/lab.yaml` 里加一行 `paused`，录制任务完全不访问 Cboe。
3. 已经保存的原始期权链原样保留，不使用，等待答复。如果 Cboe 拒绝，再决定是否删除。
4. 获批后，按许可协议的条件恢复录制，条件写进数据源目录。

## 代价

暂停期间的期权链永久缺失。美股期权属于第二个研究宇宙 U2，近期的研究不依赖它。

## 何时重新考虑

Cboe 答复之后；或者找到条款允许存档的替代数据源。

## 来源

- [Cboe 网站条款](https://www.cboe.com/terms)
- [Use of Cboe Content](https://www.cboe.com/use-of-content)
