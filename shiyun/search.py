"""逐字穷举 + 剪枝（branch and bound）。

一句五言 = 5 个位置，每个位置都在 N 字字表里穷举，整棵树共 N^5 个叶子。
每个结点的 N 个孩子要么被展开，要么被某条规则剪掉——剪掉一个深度为 d 的结点，
就等于一次性“判掉”了它下面 N^(4-d) 句诗。所有剪枝都记账，因此可以严格证明：

    Σ 展开到叶子的句数 + Σ 各层剪枝数 × N^(4-d) == N^5

这就是“穷举”：每一种字的排列都被考虑过，只是绝大多数被成片地判死。

剪枝规则（参数固定在 manifest.json 的 search 段，所有参与者一致，结果可复核）：
  lm    前缀的累计对数概率低于第 d 层下界。五言是“二|三”节奏，模型带“顿”：
          句首、第 3 字（顿后）：logP_d(c)，只看该位置的字频，不受前字约束；
          其余位置：logP_d(b|a) = log(λ·count(ab)/count(a) + (1-λ)·P_d(b))。
        语料中没出现过的搭配不会被直接判死，只是更难过界——新奇的组合仍有机会。
        孩子按 logP 降序遍历，一旦跌破下界，其余孩子整批剪掉（定界），
        所以无需真的逐个尝试 N 个字，账本照样精确。
  dup   与前文重复（紧邻的二字叠如“青青”除外，三连叠不行）
  tone  已无任何合律句式（二四异、无三平尾、无孤平）可容纳当前平仄
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path

from .meter import line_type

DATA = Path(__file__).resolve().parent.parent / "data"
RULES = ("lm", "dup", "tone")
# 两套格律体系各是一个独立的搜索空间：平仄、韵部取 chars.tsv 的不同列
METERS = {"xin": ("中华新韵", 1, 2), "psy": ("平水韵", 3, 4)}

# 所有合律的五字平仄模式，及其句式
PATTERNS = [("".join(p), line_type("".join(p))) for p in product("PZ", repeat=5)]
PATTERNS = [(p, t) for p, t in PATTERNS if t]
FULL_MASK = (1 << len(PATTERNS)) - 1


def load_manifest(data_dir: Path = DATA) -> dict:
    return json.loads((data_dir / "manifest.json").read_text("utf-8"))


@dataclass
class Leaf:
    text: str
    lm: float  # 平均每字对数概率
    types: str  # 可能的句式，如 "AC"
    rhymes: str  # 末字平声韵部，如 "寒|文"；仄收为 "-"


@dataclass
class Ledger:
    """穷举账本。pruned[rule][d] = 在第 d 位（0 起）被该规则剪掉的结点数。"""

    n: int
    leaves: int = 0
    pruned: dict = field(default_factory=lambda: {r: [0] * 5 for r in RULES})

    def covered(self) -> int:
        """本账本覆盖了多少句（叶子 + 被剪子树的全部叶子）。"""
        return self.leaves + sum(c * self.n ** (4 - d) for r in RULES for d, c in enumerate(self.pruned[r]))

    def merge(self, other: "Ledger") -> None:
        self.leaves += other.leaves
        for r in RULES:
            for d in range(5):
                self.pruned[r][d] += other.pruned[r][d]

    def to_json(self) -> dict:
        return {"leaves": self.leaves, "pruned": self.pruned}

    @classmethod
    def from_json(cls, n: int, d: dict) -> "Ledger":
        return cls(n, d["leaves"], {r: list(d["pruned"][r]) for r in RULES})


class Space:
    def __init__(self, meter: str = "psy", data_dir: Path = DATA, manifest: dict | None = None):
        if meter not in METERS:
            raise ValueError(f"未知格律体系 {meter}，可选：{', '.join(METERS)}")
        self.meter = meter
        self.meter_name, t_col, r_col = METERS[meter]
        self.data_dir = data_dir
        self.manifest = manifest or load_manifest(data_dir)
        p = self.manifest["search"]
        lam: float = p["lambda"]
        self.lm_floor: list[float] = p["lm_floor"]  # 第 d 位放字后的累计对数概率下界，长度 5

        raw = (data_dir / "chars.tsv").read_bytes()
        self.chars, self.tones, self.rhymes = [], [], []
        for row in raw.decode("utf-8").splitlines():
            f = row.split("\t")
            self.chars.append(f[0])
            self.tones.append(f[t_col])
            self.rhymes.append(f[r_col])
        self.n = len(self.chars)
        self.index = {c: i for i, c in enumerate(self.chars)}

        uni, bi, pos = {}, {}, [[1] * self.n for _ in range(5)]  # 分位置字频，加一平滑
        ngram_raw = (data_dir / "ngram.tsv.gz").read_bytes()
        for row in gzip.decompress(ngram_raw).decode("utf-8").splitlines():
            w, c = row.split("\t")
            if w[0] == "@":
                i = self.index.get(w[2])
                if i is not None:
                    pos[int(w[1])][i] += int(c)
            else:
                (uni if len(w) == 1 else bi)[w] = int(c)
        self.unigram, self.bigram = uni, bi
        p_pos = [[x / sum(row) for x in row] for row in pos]

        # 每个位置的打分方式：顿后（句首、第 3 字）只看该位置字频；其余位置用插值二元模型
        #   logP_d(b|a) = log(λ·count(ab)/count(a) + (1-λ)·P_d(b))
        self.free = {0, 2} if p.get("caesura") else {0}
        self.free_lp = {d: [math.log(x) for x in p_pos[d]] for d in self.free}
        self.free_order = {d: sorted(((lp, i) for i, lp in enumerate(self.free_lp[d])), key=lambda x: (-x[0], x[1]))
                           for d in self.free}
        bound = [d for d in range(1, 5) if d not in self.free]
        self.unseen = {d: sorted(((math.log((1 - lam) * p_pos[d][i]), i) for i in range(self.n)),
                                 key=lambda x: (-x[0], x[1])) for d in bound}
        self.unseen_lp = {d: dict((i, lp) for lp, i in lst) for d, lst in self.unseen.items()}
        self.seen: dict[int, list[list[tuple[float, int]]]] = {d: [[] for _ in range(self.n)] for d in bound}
        for w, c in bi.items():
            a, b = self.index.get(w[0]), self.index.get(w[1])
            if a is not None and b is not None:
                for d in bound:
                    self.seen[d][a].append((math.log(lam * c / uni[w[0]] + (1 - lam) * p_pos[d][b]), b))
        for d in bound:
            for lst in self.seen[d]:
                lst.sort(key=lambda x: (-x[0], x[1]))
        self.seen_set = [frozenset(b for _, b in lst) for lst in self.seen[bound[0]]]
        self.seen_lp = {d: [dict((b, lp) for lp, b in lst) for lst in self.seen[d]] for d in bound}

        # tone_mask[d][i]：第 d 位放第 i 个字后仍可能成立的平仄模式集合
        self.tone_mask = [
            [sum(1 << k for k, (pat, _) in enumerate(PATTERNS) if pat[d] in self.tones[i]) for i in range(self.n)]
            for d in range(5)
        ]

        h = hashlib.sha256(raw + ngram_raw + json.dumps(p, sort_keys=True).encode() + meter.encode())
        self.sha256 = h.hexdigest()
        self._prefixes: list[tuple[int, int]] | None = None

    def logp(self, d: int, a: int, b: int) -> float:
        """第 d 位放字 b（前一字为 a）的对数概率。"""
        if d in self.free:
            return self.free_lp[d][b]
        return self.seen_lp[d][a].get(b, self.unseen_lp[d][b])

    def children(self, d: int, prev: int, bound: float):
        """按 logP 降序产出第 d 位的孩子 (logP, 字)，只产出 logP >= bound 的。"""
        if d in self.free:
            for lp, c in self.free_order[d]:
                if lp < bound:
                    return
                yield lp, c
            return
        for lp, c in self.seen[d][prev]:
            if lp < bound:
                break
            yield lp, c
        seen = self.seen_set[prev]
        for lp, c in self.unseen[d]:
            if lp < bound:
                return
            if c not in seen:
                yield lp, c

    @staticmethod
    def _dup(c: int, path: tuple) -> bool:
        if c not in path:
            return False
        return c != path[-1] or (len(path) >= 2 and path[-2] == c)

    def _expand(self, d: int, path: tuple, mask: int, score: float, ledger: Ledger):
        """展开一个结点的全部 N 个孩子：返回通过的 (c, mask, score)，其余记账。"""
        out, considered = [], 0
        pruned = ledger.pruned
        tm = self.tone_mask[d]
        for lp, c in self.children(d, path[-1] if path else -1, self.lm_floor[d] - score):
            considered += 1
            if c in path and self._dup(c, path):
                pruned["dup"][d] += 1
                continue
            m = mask & tm[c]
            if not m:
                pruned["tone"][d] += 1
                continue
            out.append((c, m, score + lp))
        pruned["lm"][d] += self.n - considered
        return out

    # ---------- 第 0、1 位：全局前缀表，分片的基本单位 ----------
    def prefixes(self) -> list[tuple[int, int]]:
        """所有通过剪枝的二字前缀，按 (第一字序, 第二字序) 排列。"""
        if self._prefixes is None:
            self._prefixes = self._walk_prefixes(Ledger(self.n))
        return self._prefixes

    def prefix_ledger(self) -> Ledger:
        """前两位的剪枝账（全局只算一次，不属于任何分片）。"""
        led = Ledger(self.n)
        self._walk_prefixes(led)
        return led

    def _walk_prefixes(self, led: Ledger) -> list[tuple[int, int]]:
        out = []
        for a, ma, sa in self._expand(0, (), FULL_MASK, 0.0, led):
            for b, _, _ in self._expand(1, (a,), ma, sa, led):
                out.append((a, b))
        return sorted(out)

    # ---------- 第 2~4 位：分片内的深度优先穷举 ----------
    def run_prefixes(self, start: int, end: int, on_leaf, ledger: Ledger) -> None:
        """穷举前缀表 [start, end) 下的全部子树。on_leaf(prefix_idx, Leaf)。"""
        pre = self.prefixes()
        tm = self.tone_mask
        for pi in range(start, end):
            a, b = pre[pi]
            s = self.logp(0, -1, a) + self.logp(1, a, b)
            stack = [((a, b), tm[0][a] & tm[1][b], s)]
            while stack:
                path, mask, score = stack.pop()
                d = len(path)
                kids = self._expand(d, path, mask, score, ledger)
                if d == 4:
                    for c, m, sc in sorted(kids):
                        ledger.leaves += 1
                        on_leaf(pi, self._leaf(path + (c,), m, sc))
                else:
                    stack.extend((path + (c,), m, sc) for c, m, sc in sorted(kids, reverse=True))

    def _leaf(self, path: tuple, mask: int, score: float) -> Leaf:
        types = "".join(sorted({PATTERNS[k][1] for k in range(len(PATTERNS)) if mask >> k & 1}))
        flat_end = any(t in "BD" for t in types)
        return Leaf(
            text="".join(self.chars[i] for i in path),
            lm=score / 5,
            types=types,
            rhymes=self.rhymes[path[-1]] if flat_end else "-",
        )

    # ---------- 复核与诊断 ----------
    def trace(self, text: str) -> tuple[str | None, int]:
        """一句诗在树上走到哪一步被剪：返回 (规则, 位置)；成为叶子则返回 (None, 5)。"""
        path, mask, score = (), FULL_MASK, 0.0
        for d, ch in enumerate(text):
            c = self.index.get(ch)
            if c is None:
                return "charset", d
            score += self.logp(d, path[-1] if path else -1, c)
            if score < self.lm_floor[d]:
                return "lm", d
            if self._dup(c, path):
                return "dup", d
            mask &= self.tone_mask[d][c]
            if not mask:
                return "tone", d
            path += (c,)
        return None, 5

    def lm_score(self, text: str) -> float | None:
        """任意五字句的平均每字对数概率（与 Leaf.lm 同口径）。字表外的字返回 None。"""
        idx = [self.index.get(ch) for ch in text]
        if None in idx:
            return None
        return sum(self.logp(d, idx[d - 1] if d else -1, idx[d]) for d in range(5)) / 5


# ---------- 分片表 ----------
def shards_path(data_dir: Path, meter: str) -> Path:
    return data_dir / f"shards-{meter}.tsv"


def load_shards(data_dir: Path, meter: str) -> list[tuple[int, int]]:
    rows = shards_path(data_dir, meter).read_text("utf-8").splitlines()
    return [(int(s), int(e)) for _, s, e, *_ in (r.split("\t") for r in rows)]


_W: Space | None = None


def _count_init(meter: str, data_dir: Path) -> None:
    global _W
    _W = Space(meter, data_dir)


def _count(rng: tuple[int, int]) -> tuple[list[int], dict]:
    led, counts = Ledger(_W.n), []
    for pi in range(*rng):
        before = led.leaves
        _W.run_prefixes(pi, pi + 1, lambda *_: None, led)
        counts.append(led.leaves - before)
    return counts, led.to_json()


def build_shards(space: Space, target_leaves: int, total: Ledger, workers: int = 1) -> list[tuple[int, int, int]]:
    """按真实叶子数均衡切分前缀表（对每个前缀真跑一遍，只计数），同时累计全局账本。"""
    pre = space.prefixes()
    step = 200
    chunks = [(k, min(k + step, len(pre))) for k in range(0, len(pre), step)]
    if workers > 1:
        from multiprocessing import get_context

        with get_context("spawn").Pool(workers, _count_init, (space.meter, space.data_dir)) as pool:
            parts = pool.map(_count, chunks, chunksize=1)
    else:
        _count_init(space.meter, space.data_dir)
        parts = [_count(c) for c in chunks]
    counts = [n for cs, _ in parts for n in cs]
    for _, led in parts:
        total.merge(Ledger.from_json(space.n, led))

    out, start, acc = [], 0, 0
    for pi, n in enumerate(counts):
        acc += n
        if acc >= target_leaves:
            out.append((start, pi + 1, acc))
            start, acc = pi + 1, 0
    if start < len(pre):
        out.append((start, len(pre), acc))
    return out
