# 开发说明

## 给所有者

你不需要在自己的电脑上运行任何东西。每一个改动都按同样的流程进入仓库：

1. AI 在新分支上完成改动，开一个拉取请求。
2. GitHub 自动运行"检查"。
3. 检查全部通过（拉取请求页面上显示绿色的勾），你阅读说明之后合并。

检查没有通过时，页面上显示红色的叉，点"Details"可以看到是哪一项失败、失败在哪里。检查失败的改动不应合并。

建议在仓库设置里为 `main` 分支开启保护：要求"检查"通过才能合并。这样即使一时疏忽，不合格的改动也进不来。

合并拉取请求时请用默认的 **Create a merge commit**，不要选 Squash and merge 或 Rebase and merge。账本记录的是分支上的提交，压缩或变基会让这些提交从历史里消失，试验就无法重跑。可以在仓库设置的 General → Pull Requests 里只保留 Allow merge commits，从根上避免选错。

### 检查的内容

| 检查 | 拦住什么 |
|---|---|
| 安装 | 依赖版本与锁定文件不一致。保证每次运行用的都是完全相同的工具版本 |
| 代码风格 | 不带时区的时间、代码里的魔法数字、缺少类型标注、`print` 输出、常见的错误写法和安全问题、格式不统一 |
| 类型检查 | 函数的输入输出类型对不上。在运行之前就发现接口用错 |
| 测试 | 计算结果与手算的期望值不符；测试试图访问网络；文档里的链接指向不存在的文件 |
| 注册表 | 假说卡、账本、数据源等格式不对；违反科研规程，例如确认检验早于注册、金库没有你的批准 |
| 注册表历史只增不改 | 账本里已有的记录被改动或删除；已注册的检验方案被改动 |
| 账本引用的代码版本都在历史里 | 账本记录了某个代码版本，但它已经不在仓库历史里（例如拉取请求被压缩合并），那次试验就无法重跑 |
| 重现合成数据上的试验 | 切回账本记录的代码版本，从空白状态重跑，结果指纹或输出指纹与账本不一致 |

### 配置远程存储（一次性，Phase 2.1）

数据放在你的 Hugging Face 私有数据集里（[决策 0003](decisions/0003-data-storage.md)）。GitHub 上的任务需要一把只能写这个数据集的钥匙（令牌）。全程在网页上完成，大约十分钟：

1. **注册。** 在 [huggingface.co](https://huggingface.co/join) 注册一个免费账户。
2. **建数据集。** 右上角头像 → New Dataset。名字建议 `finance-research-data`，可见性选 **Private**（私有），然后创建。记下完整的名字，形如 `你的用户名/finance-research-data`。
3. **建令牌。** 右上角头像 → Settings → Access Tokens → Create new token。类型选 **Fine-grained**（细粒度）。在 Repositories permissions 里搜索并选中上一步的数据集，勾选 **Write access to contents/settings of selected repos**，其余全部不勾。名字写 `finance-research-actions`。创建后令牌只显示一次，先复制下来。
4. **把令牌交给 GitHub。** 打开本仓库的 Settings → Secrets and variables → Actions：
   - 在 **Secrets** 标签页点 New repository secret，名字填 `HF_TOKEN`，值粘贴令牌。
   - 在 **Variables** 标签页点 New repository variable，名字填 `HF_DATASET`，值填第 2 步的完整名字。
5. 告诉 AI 已经配好。AI 会运行"存储自检"任务：写入随机内容、读回、核对、删除，并把速度和失败率写进架构文档。

令牌只放在 GitHub Secrets 里，不要贴进对话、提交或任何文件。令牌泄露时，在 Hugging Face 的 Access Tokens 页面删除它，再按第 3、4 步换一把新的。数据集名带着你的用户名，所以放在 Variables 里，而不是写进公开仓库。

### 收到数据中断通知时

监控每 4 小时检查一次每个数据源。某个数据源超过它的上限时间没有新数据，监控会在仓库里开一个标题为"数据中断：数据源名"的问题，GitHub 会给你发邮件。

GitHub 的通知邮件来自 `notifications@github.com`。2.2 演练时这封邮件被分进了垃圾邮件，所以要先设一次过滤器（以 Gmail 为例）：在搜索框输入 `from:notifications@github.com`，点搜索框右侧的筛选图标 → 创建过滤器 → 勾选"一律不要将其发送到垃圾邮件" → 创建过滤器。

收到通知后，你不需要自己修：

1. 告诉 AI"有数据中断"，或者把问题的链接发给它。
2. AI 查看采集任务的日志，找出原因，修好后开拉取请求给你合并。
3. 数据恢复后，监控会在问题里留言并自动关闭它，不用你手动关。

- `heartbeat.canary`（心跳）中断，说明是整条采集流程出了问题，例如 Hugging Face 的令牌失效，而不是某个数据源。
- `heartbeat.drill` 是演练用的。Phase 2.2 验收时它会故意中断一次，那封邮件是预期之中的。
- 名字以 `binance.um.` 或 `bybit.` 开头的是归档清单，每周一更新一次，超过 200 小时没有新清单才算中断。多半是交易所改了归档的格式，或者拒绝了 GitHub 的访问。
- 收到"私有数据集已用 X GB"的提醒时，说明存储快到免费额度了（决策 0010 定的线是 80 GB）。告诉 AI 来处理，它会提出删减或换存储的方案给你决定。
- 收到"仓库将近 60 天没有活动"的提醒时，合并任意一个拉取请求即可。如果定时任务已经被停用，在仓库的 Actions 页面分别打开"采集"和"监控"，点 Enable workflow。

## 给 AI 会话

### 常用命令

```bash
uv sync --locked          # 按锁定文件安装
uv run ruff check .       # 风格与常见错误
uv run ruff format .      # 自动格式化
uv run pyright            # 类型检查
uv run pytest             # 测试
uv run finresearch registry check                          # 注册表
uv run finresearch registry check-append-only --base main  # 历史只增不改
uv run finresearch registry check-commits                  # 账本引用的提交都在历史里
uv run finresearch run --study thin_slice --hypothesis H-0001 --segment exploration
uv run finresearch reproduce --synthetic                   # 从空白状态重跑合成数据上的试验
uv run finresearch storage selftest                        # 远程存储自检，需要 HF_TOKEN 和 HF_DATASET
uv run finresearch collect plan --group every8h            # 列出一个定时组里启用的数据源
uv run finresearch collect run --source heartbeat.canary   # 采集一个数据源并推送，需要 HF_TOKEN 和 HF_DATASET
uv run finresearch monitor                                 # 监控一遍，还需要 GITHUB_TOKEN（只在 Actions 里运行）
uv run finresearch archive plan                            # 列出启用的归档数据集
uv run finresearch archive run --dataset bybit.trades.daily  # 列目录、推送增量清单、抽查文件，需要 HF_TOKEN 和 HF_DATASET
uv run finresearch u1 plan --exchange binance             # 列出还没加工的月份
uv run finresearch u1 build --exchange binance --month 2026-10  # 加工一个月并推送，需要 HF_TOKEN 和 HF_DATASET
```

### 记一次试验

1. 先把代码、配置和假说卡全部提交。工作区有任何未提交的改动，程序都拒绝入账。
2. 在干净的工作区上运行 `uv run finresearch run ... --record`。程序追加账本、写出 `reports/<试验编号>.md`。
3. 把账本和报告作为下一个提交，不要和代码改动混在一起。

账本记录的是第 1 步的提交。持续集成会切回这个提交重跑，所以它必须留在历史里（合并提交，不压缩、不变基）。

结果指纹包含环境指纹（锁定文件、Python 版本、处理器架构、操作系统）。本地会话与持续集成的环境一致（CPython 3.14、x86_64、Linux），在本地记的账能在持续集成里重现；环境不同时结果指纹会不同，重现会报告不一致。

提交之前这些都要在本地通过。持续集成运行的是同样的命令。

### 依赖

- 增加依赖用 `uv add <包名>`（开发工具用 `uv add --dev <包名>`），它会同时更新 `pyproject.toml` 和 `uv.lock`。不要手工编辑 `uv.lock`。
- 每个新依赖都要在拉取请求里说明为什么需要它，见[决策 0004](decisions/0004-toolchain.md)的"依赖越少越好"。
- 升级工具版本单独开一个拉取请求。

### 运行时依赖

| 包 | 加入的步骤 | 理由 |
|---|---|---|
| tzdata | 1.2 | 时区规则随代码锁定，不依赖运行机器的系统时区 |
| pydantic | 1.4 | 注册表文件的格式校验，错误信息指出具体字段（[决策 0004](decisions/0004-toolchain.md) 已列入） |
| pyarrow | 1.5 | 时点表的内存格式和 Parquet 读写（决策 0004 已列入）。另有开发依赖 `pyarrow-stubs` 补充类型标注 |
| huggingface-hub | 2.1 | 远程存储（[决策 0003](decisions/0003-data-storage.md)）：批量提交、列出、下载。官方客户端，处理大文件的分块上传。它会带进一批间接依赖（httpx2、hf-xet、fsspec 等） |
| httpx2 | 2.1 | huggingface-hub 的网络层。直接声明是因为代码要按它的异常类型区分"值得重试"的网络错误 |

### 测试的约束

这些约束由 `tests/conftest.py` 自动执行：

- **不联网。** 任何网络连接都会抛出 `NetworkAccessError`。需要外部数据的测试，用仓库内的小型样本或在测试里构造数据。
- **性质测试是确定的。** hypothesis 每次生成相同的例子，所以持续集成的结果总能复现。
- **警告即错误。** 任何警告都会让测试失败。

### 代码规则

规则写在 `pyproject.toml` 的 `[tool.ruff]` 和 `[tool.pyright]` 里，每条都对应 `CLAUDE.md` 的一条约定。`src/` 下的代码按最严格的类型检查标准；`tests/` 下允许 `assert` 和手算的数字。
