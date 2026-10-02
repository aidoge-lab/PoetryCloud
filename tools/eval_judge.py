"""评测判别器：它到底分不分得清诗和非诗？

正样本：随机抽取的唐诗五言原句
负样本：① 同一句的字打乱顺序（字面相同，只是不通） ② 搜索树上的随机叶子（非唐诗原句）
指标：AUC（0.5 = 抛硬币，1.0 = 完美）。判别器 AUC 不显著高于 0.5 时，跑分片没有意义。

  python tools/eval_judge.py --judge laya -n 200
  python tools/eval_judge.py --judge laya --base-url http://gpu-box:8000
"""
from __future__ import annotations

import argparse
import gzip
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shiyun.judges import make_judge  # noqa: E402
from shiyun.search import DATA, Ledger, Space, load_shards  # noqa: E402


def auc(pos: list[float], neg: list[float]) -> float:
    wins = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def samples(space: Space, n: int, seed: int) -> tuple[list[str], list[str], list[str]]:
    rng = random.Random(seed)
    with gzip.open(DATA / "known_lines.txt.gz", "rt", encoding="utf-8") as f:
        known = [ln.strip() for ln in f if ln.strip()]
    known_set = set(known)
    pos = rng.sample(known, n)
    shuffled = []
    for ln in pos:
        cs = list(ln)
        while "".join(cs) == ln and len(set(cs)) > 1:
            rng.shuffle(cs)
        shuffled.append("".join(cs))
    shards, leaves = load_shards(), []
    while len(leaves) < n:
        s, e = shards[rng.randrange(len(shards))]
        pi, got = rng.randrange(s, e), []
        space.run_prefixes(pi, pi + 1, lambda _, x: got.append(x.text), Ledger(space.n))
        got = [t for t in got if t not in known_set]
        if got:
            leaves.append(rng.choice(got))
    return pos, shuffled, leaves


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", default="laya")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("-n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    space = Space()
    judge = make_judge(a.judge, space, a.base_url, a.model)
    pos, shuffled, leaves = samples(space, a.n, a.seed)
    t = time.time()
    P, S, L = (judge.score(x) for x in (pos, shuffled, leaves))
    ms = (time.time() - t) / (3 * a.n) * 1000
    print(f"判别器 {judge.id}，每组 {a.n} 句，{ms:.0f} ms/句")
    print(f"  AUC 唐诗原句 vs 打乱字序 = {auc(P, S):.3f}")
    print(f"  AUC 唐诗原句 vs 搜索叶子 = {auc(P, L):.3f}")
    print(f"  平均分：原句 {sum(P) / len(P):.2f}  打乱 {sum(S) / len(S):.2f}  叶子 {sum(L) / len(L):.2f}")


if __name__ == "__main__":
    main()
