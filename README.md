# 诗云 · PoetryCloud

> 「我们要把所有的诗都写出来。」——刘慈欣《诗云》

《诗云》里，神级文明穷举了所有汉字组合，写尽了一切可能的诗，却找不出哪首是好诗。
**诗云**想把这件事倒过来做：逐字穷举五言绝句，同时用开源判别模型和众人的眼睛把好诗挑出来。

第一阶段目标：**五言绝句**。

## 它怎么工作

```
            4000 字 × 5 位 = 1.02×10¹⁸ 句
                     │  逐字穷举 + 剪枝（每剪一枝都记账，总账恒等于 4000⁵）
                     ▼
               约 5 亿句合律、通顺的五言句   ── 切成 742 片，大家认领
                     │  laya 判别（yes/no 概率，单次前向，不生成文字）
                     ▼
                  好句池（每片 ≤3000 句）     ── 自动提交到 GitHub Issue
                     │  汇总器复算校验（GitHub Actions，每小时）
                     ▼
         第二阶段：在好句池里穷举组诗（句式 → 粘对 → 押韵 → 不重字 → 对句判别 → 整诗判别）
                     │
                     ▼
               网页投票 → 校准判别器 → 排行榜
```

### 为什么不是“真·全穷举”

五言绝句 20 个字，常用字 4000 个：4000²⁰ ≈ 10⁷² 首，宇宙里的原子都不够用。所以：

1. **以字为单元穷举，在“句”这一层做到严格完整。** 每句 5 个位置，每个位置都对 4000 个字逐一考虑，整棵搜索树 4000⁵ ≈ 1.02×10¹⁸ 个叶子。
2. **剪枝是成片判死，而不是跳过。** 第 d 个字被剪掉，就等于同时判掉了它下面 4000^(4-d) 句。每条剪枝都记在账本里，任何时候都满足：

   ```
   Σ 进入判别的句数 + Σ 第 d 位剪枝数 × 4000^(4-d) = 4000⁵   （精确整数相等）
   ```

   测试里用小字表暴力枚举全部组合，逐句对照证明剪枝既不多剪也不漏剪（`tests/test_search.py`）。
3. **组诗那一层也是穷举 + 剪枝**，只是单位从字换成了句。

剪枝规则（全部固定在 `data/manifest.json`，所有人一致，结果可复算）：

| 规则 | 含义 |
|---|---|
| `lm` | 前缀的累计对数概率低于该层下界。带“顿”的分位置模型：五言是“二\|三”节奏，句首和第 3 字只看该位置的字频，其余位置用插值二元模型。语料里没出现过的搭配**不会被直接判死**，只是更难过界。孩子按概率降序遍历，跌破下界后剩下的整批剪掉（分支定界）。 |
| `dup` | 与前文重字（“青青”这种紧邻二字叠可以，三连叠不行） |
| `tone` | 已不可能凑成任何合律句式（二四异、无三平尾、无孤平；按**中华新韵**） |

看一句诗在树上的命运：

```console
$ python -m shiyun trace 白日依山尽 千山鸟飞绝
白日依山尽：✓ 进入判别  lm=-4.85（叶子下界 -5.16）
千山鸟飞绝：✗ 第4字「飞」被剪（平仄），同时剪掉其后 4.0e+03 句
```

### 已知的取舍（请一起改进）

- **剪枝召回率约 5%**：拿全部 29 万句唐人五言原句来测，大约 5% 能活到判别器面前。字级 n-gram 分不清“好诗”和“通顺的套话”，而好诗往往出人意料。召回率主要由最后一层下界决定；中间层是线性下界，会误杀“开头冷、结尾好”的句子（比如“床前明月光”）。只保留最后一层下界，召回率能到约 12%，但计算量会成倍增加。
- **新韵**：平仄用现代读音（一二声为平，三四声为仄），不处理入声，所以“千山鸟飞绝”在新韵下不合律。将来可以加平水韵模式。
- 改动 `search` 参数或字表都会改变搜索空间的哈希，旧结果就作废了。所以每个版本的参数要先讨论清楚、定下来，再开跑。

## 参与计算

需要 Python ≥ 3.10、[`gh`](https://cli.github.com) CLI（运行 `gh auth login` 登录），以及一个判别器。

```bash
git clone https://github.com/aidoge-lab/PoetryCloud && cd PoetryCloud
pip install -e ".[laya]"          # 核心只依赖标准库；[laya] 装判别模型

python -m shiyun info             # 看看搜索空间和剪枝账本
python -m shiyun sample -n 20     # 随机抽 20 句给判别器打分，确认一切正常
python -m shiyun run              # 自动认领 → 计算 → 提交，循环
```

`run` 的流程：

1. 开一个标题为 `[claim] shard 000123` 的 issue 来认领分片。不需要 fork，也不需要写权限。撞车时 issue 编号小的一方胜出。
2. 本地逐字穷举并判别，**每处理完一个前缀就存一次断点**（存在 `state/`），中断后再跑会自动续跑。
3. 分数 ≥ 0.5 的句子作为候选，连同穷举账本和分数直方图一起贴到这个 issue 下，然后关闭 issue。
4. 汇总器把这个分片**重新穷举一遍**（不调判别器，几秒钟）核对账本，确认每个候选确实属于这个分片，然后打上 `merged` 标签。

认领 7 天还没提交的，视为放弃，分片会重新开放。

### 判别器

默认用 [laya](https://github.com/NandhaKishorM/laya)：开源、兼容 Jev 接口的非自回归判别模型，一次前向直接给出 yes/no 概率。中文会自动路由到 `laya-multilingual`。

```bash
# 方式一：进程内运行（默认）
python -m shiyun run

# 方式二：单独起服务，可以放在另一台 GPU 机器上
pip install "laya[serve]"
LAYA_DEVICE=cuda LAYA_PRELOAD=1 laya-serve          # 默认 :8000
python -m shiyun run --base-url http://gpu-box:8000  # 走 /v1/systemone/batch，每批 64 句
```

> ⚠️ **现状：零样本的 laya 还不会判诗。** laya 是为工单路由、意图判断训练的，`tools/eval_judge.py` 实测（2026-10，laya 0.3.23，`laya-multilingual`）：
>
> | 判别器 | 唐诗原句 vs 打乱字序 | 唐诗原句 vs 搜索叶子 | 速度（M 系列 Mac） |
> |---|---|---|---|
> | laya 零样本（试了 5 种问法） | AUC 0.44–0.54 | AUC 0.46–0.59 | 7–25 ms/句 |
> | ngram（对照） | 0.86 | 0.08（叶子本就是按 ngram 挑的） | — |
>
> AUC 0.5 就是抛硬币。所以**正式开跑前必须先微调 laya**（见路线图）。在那之前请不要运行 `run` 提交结果，否则会浪费算力。

其他后端：`--judge jev`（jev-rs 等兼容 Jev 的服务）、`--judge openai`（llama.cpp / vLLM / Ollama，取“是/否”的 logprob）、`--judge ngram`（不用模型，只用于测试）。
不同判别器的分数不能直接比较，所以结果里会记录 `judge` 字段，人工校准也按判别器分开做。

算力估计：T4 上 laya 批量处理约 150 句/秒，一片约 70 万句，大约要 1 个多小时。全部 742 片约需 40 个 GPU·天。

## 人工校准

- `site/` 是一个静态网页，由 Actions 部署到 GitHub Pages。点“好诗 / 一般 / 不行”投票，再点“提交到 GitHub”，会打开一个预填好内容的 `[vote]` issue，提交即可。不需要任何后端。
- 汇总器收集投票（每人每首以最后一票为准），用 Platt 定标把判别分映射成“人觉得好”的概率，再与投票做贝叶斯平均，得出排行榜。
- 投票也是判别器的训练数据：攒够之后，可以用 `laya-typed-decisions` 的方式微调出一个“懂诗”的判别器。

## 维护者

```bash
# 重建字表和统计（会下载 chinese-poetry 的《全唐诗》）
uv run --with pypinyin --with opencc-python-reimplemented tools/build_lexicon.py
python tools/build_shards.py          # 生成分片表、核对全局账本，8 核约 4 分钟

python -m shiyun aggregate            # 校验并汇总（Actions 每小时自动跑）
python -m shiyun assemble             # 第二阶段：组诗
python -m shiyun site                 # 生成网页数据
python tools/eval_judge.py --judge laya   # 判别器评测（AUC）
pytest
```

首次部署：在 `data/manifest.json` 里填好 `github_repo`，在仓库设置里把 Pages 的来源设为 GitHub Actions。

## 目录

```
shiyun/search.py      逐字穷举 + 剪枝 + 账本（核心）
shiyun/meter.py       五绝格律：句式、粘对、押韵位置
shiyun/rhyme.py       中华新韵十四韵
shiyun/judges/        判别器后端：laya / jev / openai / ngram
shiyun/worker.py      跑一个分片（断点续跑）
shiyun/github.py      Issue 认领、提交
shiyun/aggregate.py   复算校验、汇总、投票收集
shiyun/assemble.py    第二阶段组诗
shiyun/rank.py        定标与排名
data/                 字表、n-gram、唐诗原句、分片表、manifest、结果
site/                 投票网页
```

## 路线图

- [ ] **微调 laya 判别器（开跑前的阻塞项）**：用 laya 自带的 `research/scripts/finetune_single_device.py`。正样本是唐诗原句，负样本包括打乱字序的句子、搜索叶子、替换一两个字的“近似句”；留出一部分唐诗做评测，用 `tools/eval_judge.py` 验收。之后再用人工投票继续微调。
- [ ] 用小型神经网络字模型做前缀打分（需要结果确定、可复算，比如量化后的整数分数），提高剪枝召回率
- [ ] 平水韵 / 入声模式
- [ ] 第二阶段也拆成分布式任务
- [ ] 用人工投票微调判别器
- [ ] 第三阶段：七言

## 数据来源

唐诗语料来自 [chinese-poetry](https://github.com/chinese-poetry/chinese-poetry)。
