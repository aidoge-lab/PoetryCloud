"""评测判别器：它到底分不分得清诗和非诗？

只用留出集（tools/finetune_judge.py 的 is_heldout），微调时没见过这些句子。
正样本：留出的唐诗五言原句
负样本：① 同一句打乱字序 ② 同一句替换一两个字 ③ 搜索树上的随机叶子（非原句，且属留出集）
指标：AUC（0.5 = 抛硬币，1.0 = 完美）。

  python tools/eval_judge.py                                  # 零样本 laya
  python tools/eval_judge.py --model models/laya-shiyun       # 微调后的检查点
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from finetune_judge import is_heldout, known_lines, random_leaves, shuffled, substituted  # noqa: E402

from shiyun.judges import make_judge  # noqa: E402
from shiyun.search import Space  # noqa: E402


def auc(pos: list[float], neg: list[float]) -> float:
    wins = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", default="laya")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--device", default="")
    ap.add_argument("--meter", default="psy", help="搜索叶子取自哪个空间")
    ap.add_argument("-n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    space = Space(a.meter)
    known = known_lines()
    known_set = set(known)
    pos = rng.sample([t for t in known if is_heldout(t)], a.n)
    groups = {
        "打乱字序": [shuffled(t, rng) for t in pos],
        "替换一两字": [s for s in (substituted(t, space, rng) for t in pos) if s not in known_set],
        "搜索叶子": random_leaves(space, a.n, rng, known_set, heldout=True),
    }
    judge = make_judge(a.judge, space, a.base_url, a.model, device=a.device)
    t = time.time()
    P = judge.score(pos)
    scores = {k: judge.score(v) for k, v in groups.items()}
    n_all = len(P) + sum(len(v) for v in groups.values())
    print(f"判别器 {judge.id}，留出集，每组约 {a.n} 句，{(time.time() - t) / n_all * 1000:.1f} ms/句")
    print(f"  唐诗原句 平均分 {sum(P) / len(P):.2f}")
    for k, S in scores.items():
        print(f"  vs {k:<6s} AUC {auc(P, S):.3f}   平均分 {sum(S) / len(S):.2f}")


if __name__ == "__main__":
    main()
