"""20 字五言绝句的穷举账本。

整首的空间是 N^20（N 个字，20 个位置），等价于“四句各取 N^5 种”的笛卡尔积。
逐句展开这棵 20 层的树：第 k 句一旦被判死，就同时判掉其后 (N^5)^(3-k) 首。

一首诗通过初筛，当且仅当：
  · 四句都通过第一阶段单句初筛（是搜索叶子，且判别分 ≥ 阈值）；
  · 存在一种绝句格式（仄起/平起 × 首句入韵与否），四句的句式与之相符（粘对）；
  · 该格式要求押韵的句子末字同属一个平声韵部。
全诗不重字留到第二阶段检查。

单句的结果只分三类：通过（带句式与韵部签名）、未通过、尚未初筛（所在分片还没跑完）。
按签名分组做动态规划，就能精确算出 N^20 首里每一类各有多少首，不需要逐首列举：

    Σ 剪掉的首数 + 通过初筛的首数 + 尚未初筛的首数 == N^20      （精确整数相等）

记账按树的顺序逐句进行：第 k 句未通过 → 剪掉；第 k 句尚未初筛 → 其下整棵子树记为待定；
第 k 句通过但与前文已无可行格式 → 剪掉。尚未初筛的首数降到 0，就是 20 字空间初筛完成。
tests/test_quatrain.py 在 2^20 的小空间里逐首枚举，核对每一项都精确相等。
"""
from __future__ import annotations

from collections import Counter

from .meter import FORMS, rhymes

ALL = None  # 尚未遇到需押韵的句子，韵部不限


def _step(state: frozenset, k: int, types: str, rset: frozenset) -> frozenset:
    """state 中每个元素是 (格式, 允许的韵部集合)，加入第 k 句后仍可行的那些。"""
    out = set()
    for form, req in state:
        if FORMS[form][k] not in types:
            continue  # 粘对不合
        if k in rhymes(form):
            req = rset if req is ALL else req & rset
            if not req:
                continue  # 不押韵
        out.add((form, req))
    return frozenset(out)


def ledger(n: int, passed: Counter, pending: int) -> dict:
    """
    n       字表大小
    passed  通过单句初筛的句子，按签名计数：{(句式, 韵部 "a|b" 或 "-"): 句数}
    pending 尚未初筛的句数
    返回整首账本，所有数都是精确整数。
    """
    n5 = n ** 5
    failed = n5 - pending - sum(passed.values())
    assert failed >= 0
    sigs = [(t, frozenset() if r == "-" else frozenset(r.split("|")), c) for (t, r), c in passed.items()]
    states = Counter({frozenset((f, ALL) for f in FORMS): 1})
    pruned_line = [0] * 4  # 第 k 句未通过单句初筛
    pruned_form = [0] * 4  # 第 k 句粘对或押韵不合
    pending_q = 0
    for k in range(4):
        rest = n5 ** (3 - k)
        nxt: Counter = Counter()
        for st, cnt in states.items():
            pruned_line[k] += cnt * failed * rest
            pending_q += cnt * pending * rest
            for types, rset, c in sigs:
                ns = _step(st, k, types, rset)
                if ns:
                    nxt[ns] += cnt * c
                else:
                    pruned_form[k] += cnt * c * rest
        states = nxt
    survived = sum(states.values())
    total = sum(pruned_line) + sum(pruned_form) + survived + pending_q
    assert total == n ** 20, "整首账本不平"
    return {
        "space": n ** 20,
        "pruned_line": pruned_line,
        "pruned_form": pruned_form,
        "survived": survived,
        "pending": pending_q,
        "screened_ratio": 1 - pending_q / n ** 20,
    }
