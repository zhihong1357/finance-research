# 0003 · 数据存放在仓库之外的私有存储

**状态：** 提议（2026-11-13）。Phase 2 开始前确认即可。

## 背景

约束是：免费、云端、不在本地运行、能从 GitHub Actions 读写。

数据量的估计：仅 CBOE 四个标的的原始期权链，压缩后约 3.5 MB/天，一年约 0.9 GB。加上 Deribit 快照、逐笔成交和规范层，一年之内会超过几个 GB。

候选方案：

| 方案 | 免费额度 | 优点 | 缺点 |
|---|---|---|---|
| Git 分支或单独的 Git 仓库 | 建议 1 GB 以内，单文件不超过 100 MiB | 不需要新账户；旧系统已在使用 | 体积只增不减，一年内触及建议上限；不适合大文件 |
| Hugging Face 私有数据集 | 私有 100 GB | 额度大；原生支持 Parquet；可以只读取文件的一部分 | 需要注册账户和配置令牌；额度政策由对方决定 |
| Cloudflare R2 | 每月 10 GB，流出流量不收费 | 标准的对象存储接口，工具支持最广 | 额度较小；需要注册账户和配置令牌 |

## 决定

- 存储通过一个统一的文件系统接口访问，后端是一项配置。
- 首选 Hugging Face 私有数据集，R2 作为备选。
- Git 分支只作为过渡，用于 Phase 2 之前的录制（见落地顺序的 Phase 0.5）。
- 代码仓库里只保留数据的目录和指纹，不保留数据本身。

## 理由

- 100 GB 的私有额度足够覆盖可预见的几年。
- 后端可替换，所以这个选择的风险是有限的。

## 代价

- 所有者需要注册一个 Hugging Face 账户，并在 GitHub 里配置一个令牌。
- 多一个外部依赖。

## 何时重新考虑

Hugging Face 调整免费额度；或者 Phase 2 开始前的实测显示从 GitHub Actions 读写的速度或稳定性不可接受。

## 来源

- [GitHub：仓库与文件的体积限制](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)
- [Hugging Face：存储额度](https://huggingface.co/docs/hub/storage-limits)
- [Cloudflare R2](https://www.cloudflare.com/developer-platform/r2/)
