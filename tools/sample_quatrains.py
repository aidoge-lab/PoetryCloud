"""本地试跑一批五言绝句，看判别器的效果（不经过分片和 GitHub）。

1. 单句：在搜索树上按语言模型概率随机下降，抽 N 句合法的五言句（去重，排除唐诗原句）；
2. 打分：用判别器（默认 models/laya-shiyun）逐句打分；
3. 组诗：按第二阶段规则（粘对、押韵、全诗不重字）穷举组合；
   微调模型只学过“单句像不像唐诗”，所以整首按四句平均分排序，同时给出最低句分；
4. 选出前 K 首，同一句默认只用一次（--max-use），避免少数高分句霸榜。

  python tools/sample_quatrains.py --meter psy --lines 30000 --top 100
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from finetune_judge import known_lines  # noqa: E402

from shiyun.assemble import assemble  # noqa: E402
from shiyun.judges import make_judge  # noqa: E402
from shiyun.search import FULL_MASK, Ledger, Space  # noqa: E402


def descend(space: Space, rng: random.Random, scratch: Ledger) -> tuple[str, float, str, str] | None:
    """按 P(字) ∝ exp(logP) 逐层随机下降，返回 (句子, 平均对数概率, 句式, 韵部)；走进死路返回 None。"""
    path, mask, score = (), FULL_MASK, 0.0
    for d in range(5):
        kids = space._expand(d, path, mask, score, scratch)
        if not kids:
            return None
        base = kids[0][2]
        weights = [math.exp(sc - base) for _, _, sc in kids]
        c, mask, score = rng.choices(kids, weights)[0]
        path += (c,)
    leaf = space._leaf(path, mask, score)
    return leaf.text, leaf.lm, leaf.types, leaf.rhymes


class LineMean:
    """把对句/整首的分数定义为各句判别分的平均（查表，不再调用模型）。"""

    def __init__(self, line_scores: dict[str, float]):
        self.s = line_scores

    def score(self, texts: list[str], kind: str = "line") -> list[float]:
        return [sum(self.s[x] for x in t.split("/")) / len(t.split("/")) for t in texts]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--meter", default="psy")
    ap.add_argument("--lines", type=int, default=30000)
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--model", default=str(ROOT / "models" / "laya-shiyun"))
    ap.add_argument("--device", default="")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--max-use", type=int, default=1, help="同一句最多出现在几首诗里")
    ap.add_argument("--out", default=str(ROOT / "out"))
    a = ap.parse_args()

    space = Space(a.meter)
    rng = random.Random(a.seed)
    known = set(known_lines())
    scratch = Ledger(space.n)

    t0 = time.time()
    lines: dict[str, tuple[float, str, str]] = {}
    tries = 0
    while len(lines) < a.lines:
        tries += 1
        got = descend(space, rng, scratch)
        if got and got[0] not in known and got[0] not in lines:
            lines[got[0]] = got[1:]
    print(f"[1/3] 抽样 {len(lines):,} 句（尝试 {tries:,} 次），{time.time() - t0:.0f}s", flush=True)

    judge = make_judge("laya", space, model=a.model, device=a.device)
    texts = list(lines)
    scores: list[float] = []
    t1 = time.time()
    for k in range(0, len(texts), 2048):
        scores += judge.score(texts[k : k + 2048])
        done = len(scores)
        rate = done / (time.time() - t1)
        print(f"[2/3] 打分 {done:,}/{len(texts):,}  {rate:.0f} 句/秒  预计还需 {(len(texts) - done) / rate / 60:.1f} 分钟", flush=True)
    line_score = dict(zip(texts, scores))

    pool = sorted(({"text": t, "score": s, "lm": lines[t][0], "types": lines[t][1], "rhymes": lines[t][2], "known": False}
                   for t, s in line_score.items()), key=lambda x: -x["score"])
    poems, ledger = assemble(pool, LineMean(line_score), top_k=200, couplet_min=0.0,
                             poem_keep=5000, couplet_cap=1000, log=open("/dev/null", "w"))
    print(f"[3/3] 组诗：{json.dumps(ledger, ensure_ascii=False)}", flush=True)

    # 去掉同一组句子换序得到的重复，再限制每句最多出现 2 次
    seen, used, top = set(), Counter(), []
    for p in sorted(poems, key=lambda p: (-p["score"], -min(p["line_scores"]))):
        ls = p["text"].split("/")
        if frozenset(ls) in seen or any(used[x] >= a.max_use for x in ls):
            continue
        seen.add(frozenset(ls))
        used.update(ls)
        top.append(dict(p, min_line=min(p["line_scores"])))
        if len(top) == a.top:
            break

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"quatrains-{a.meter}.json").write_text(json.dumps(
        {"judge": judge.id, "meter": a.meter, "lines_sampled": len(lines), "ledger": ledger, "top": top},
        ensure_ascii=False, indent=1) + "\n", "utf-8")
    md = [f"# 诗云试跑：{space.meter_name} 前 {len(top)} 首\n",
          f"判别器 `{judge.id}`；抽样 {len(lines):,} 句；整首分 = 四句判别分平均。\n",
          "| # | 均分 | 最低句 | 格式 | 韵 | 诗 |", "|---|---|---|---|---|---|"]
    for i, p in enumerate(top, 1):
        ls = p["text"].split("/")
        poem = f"{ls[0]}，{ls[1]}。{ls[2]}，{ls[3]}。"
        md.append(f"| {i} | {p['score']:.3f} | {p['min_line']:.3f} | {p['form']} | {p['rhyme']} | {poem} |")
    (out / f"quatrains-{a.meter}.md").write_text("\n".join(md) + "\n", "utf-8")
    print(f"写入 {out / f'quatrains-{a.meter}.md'}，总用时 {(time.time() - t0) / 60:.1f} 分钟")


if __name__ == "__main__":
    main()
