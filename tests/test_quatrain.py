"""整首账本：小规模下逐首暴力枚举，与动态规划对照。"""
import itertools
import random
from collections import Counter

from shiyun.meter import FORMS, rhymes
from shiyun.quatrain import ledger

TYPES = ["A", "B", "C", "D", "AC", "BD", "AB"]
RHYMES = ["-", "东", "先", "东|先", "支"]


def feasible(lines) -> bool:
    """前 len(lines) 句是否还可能属于某种格式（粘对 + 已出现的韵脚有公共韵部）。"""
    for form, slots in FORMS.items():
        if not all(slots[k] in lines[k][0] for k in range(len(lines))):
            continue
        sets = [set() if lines[k][1] == "-" else set(lines[k][1].split("|")) for k in rhymes(form) if k < len(lines)]
        if not sets or set.intersection(*sets):
            return True
    return False


def walk(q):
    """按 20 字树的顺序逐句走：返回 ("line"|"form"|"pending", k) 或 ("ok", 4)。"""
    for k, c in enumerate(q):
        if c == "fail":
            return "line", k
        if c == "pending":
            return "pending", k
        if not feasible(q[: k + 1]):
            return "form", k
    return "ok", 4


def test_quatrain_ledger_matches_brute_force():
    rng = random.Random(3)
    n = 2  # 单句空间 2^5 = 32 句，整首 2^20 = 32^4 ≈ 100 万首
    cats = []
    for _ in range(n ** 5):
        r = rng.random()
        cats.append("fail" if r < 0.3 else "pending" if r < 0.4 else (rng.choice(TYPES), rng.choice(RHYMES)))
    passed = Counter(c for c in cats if isinstance(c, tuple))
    pending = cats.count("pending")
    got = ledger(n, passed, pending)

    # 逐首枚举 32^4 首（整首空间 n^20 = 32^4，恰好可以全部列举）
    count = Counter(walk(q) for q in itertools.product(cats, repeat=4))
    assert got["survived"] == count[("ok", 4)]
    assert got["pending"] == sum(v for (kind, _), v in count.items() if kind == "pending")
    assert got["pruned_line"] == [count[("line", k)] for k in range(4)]
    assert got["pruned_form"] == [count[("form", k)] for k in range(4)]
    assert got["space"] == n ** 20 == sum(count.values())


def test_all_screened_when_nothing_pending():
    got = ledger(3, Counter({("B", "东"): 5, ("A", "-"): 7, ("C", "-"): 4, ("D", "东"): 6}), 0)
    assert got["pending"] == 0 and got["screened_ratio"] == 1
    # 仄起不入韵 ABCD：7 × 5 × 4 × 6，再加其余格式中可行的组合
    assert got["survived"] >= 7 * 5 * 4 * 6
