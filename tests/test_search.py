"""用一个 12 字的小字表，对比剪枝树搜索与暴力枚举全部 12^5 种组合，验证穷举账本严格正确。"""
import gzip
import itertools
import json
from pathlib import Path

import pytest

from shiyun.meter import line_type, plain_ok
from shiyun.search import Ledger, Space

CHARS = [("山", "P", "寒"), ("水", "Z", "-"), ("青", "P", "庚"), ("白", "Z", "-"), ("云", "P", "文"),
         ("日", "Z", "-"), ("风", "P", "庚"), ("月", "Z", "-"), ("人", "P", "文"), ("不", "PZ", "姑"),
         ("春", "P", "文"), ("落", "Z", "-")]
LINES = ["青山不落日", "白云山水青", "春风落日人", "白日落青山", "山水白云人", "青青山水月",
         "明月不知人", "风落白云山", "人日春风落", "春水白云月", "云落青山水", "日落春风人"]


@pytest.fixture(scope="module")
def space(tmp_path_factory) -> Space:
    d: Path = tmp_path_factory.mktemp("data")
    uni, bi = {}, {}
    for ln in LINES:
        for c in ln:
            uni[c] = uni.get(c, 0) + 1
        for i in range(4):
            bi[ln[i : i + 2]] = bi.get(ln[i : i + 2], 0) + 1
    (d / "chars.tsv").write_text("".join(f"{c}\t{t}\t{r}\t{uni.get(c, 1)}\n" for c, t, r in CHARS), "utf-8")
    with gzip.open(d / "ngram.tsv.gz", "wt", encoding="utf-8") as f:
        for c, _, _ in CHARS:
            f.write(f"{c}\t{uni.get(c, 1)}\n")
        for w, n in bi.items():
            f.write(f"{w}\t{n}\n")
        for ln in LINES:
            for k, c in enumerate(ln):
                f.write(f"@{k}{c}\t1\n")
    m = {"search": {"lambda": 0.9, "caesura": True, "lm_floor": [-3.0, -4.5, -6.5, -8.5, -10.5]}}
    (d / "manifest.json").write_text(json.dumps(m), "utf-8")
    return Space(d)


def brute(space: Space) -> set[str]:
    """不剪枝、不排序，逐一检查全部 N^5 种组合是否满足同样的规则。"""
    out = set()
    for combo in itertools.product(range(space.n), repeat=5):
        if any(Space._dup(combo[d], combo[:d]) for d in range(1, 5)):
            continue
        if not any(line_type("".join(t)) for t in itertools.product(*(space.tones[i] for i in combo))):
            continue
        s, ok = 0.0, True
        for d in range(5):
            s += space.logp(d, combo[d - 1] if d else -1, combo[d])
            ok &= s >= space.lm_floor[d]
        if ok:
            out.add("".join(space.chars[i] for i in combo))
    return out


def test_exhaustive_ledger(space):
    led = space.prefix_ledger()
    leaves = set()
    space.run_prefixes(0, len(space.prefixes()), lambda _, x: leaves.add(x.text), led)
    assert led.covered() == space.n ** 5  # 每一种组合都被计入
    assert led.leaves == len(leaves)
    assert leaves == brute(space)  # 剪枝不多剪、不漏剪
    assert len(leaves) > 50  # 小字表也能搜出句子
    assert all(space.trace(t) == (None, 5) for t in leaves)
    pruned = {"".join(space.chars[i] for i in c) for c in itertools.product(range(space.n), repeat=5)} - leaves
    assert all(space.trace(t)[0] for t in pruned)


def test_shards_partition(space):
    from shiyun.search import build_shards

    total = space.prefix_ledger()
    shards = build_shards(space, 3, total)
    assert shards[0][0] == 0 and shards[-1][1] == len(space.prefixes())
    assert all(a[1] == b[0] for a, b in zip(shards, shards[1:]))
    assert total.covered() == space.n ** 5


def test_ledger_roundtrip(space):
    led = Ledger(space.n, 3, {r: [1, 2, 3, 4, 5] for r in ("lm", "dup", "tone")})
    assert Ledger.from_json(space.n, led.to_json()).covered() == led.covered()


def test_meter():
    assert line_type("ZZPPZ") == "A"
    assert line_type("PPZZP") == "B"
    assert line_type("PPPZZ") == "C"
    assert line_type("ZZZPP") == "D"
    assert line_type("ZPZZP") is None  # 孤平
    assert line_type("ZZPPP") is None  # 三平尾
    assert line_type("PPPPZ") is None  # 二四同声
    assert plain_ok("青青山水月") and not plain_ok("白日白山尽")
