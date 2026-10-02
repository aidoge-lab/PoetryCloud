"""从《全唐诗》构建诗云的字表与统计数据（维护者使用，普通参与者无需运行）。

产物（全部提交进仓库，参与者 clone 即用，不需要额外依赖）：
  data/chars.tsv          字表，按频次排序，顺序即枚举顺序。列：
                          char  新韵平仄  新韵韵部  平水韵平仄  平水韵平声韵部  freq
                          平仄为 P/Z/PZ；韵部可多个，用 | 分隔，“-” 表示不能作韵脚
  data/ngram.tsv.gz       字级 unigram/bigram 计数，以及分位置字频 “@d字”（剪枝与打分用）
  data/known_lines.txt.gz 唐诗五言原句（标记“重新发现”的古人原句，并用于校准召回率）
  data/shards-<空间>.tsv  任务分片表（由 tools/build_shards.py 生成）
  data/manifest.json      版本、哈希、搜索参数

运行：
  uv run --with pypinyin --with opencc-python-reimplemented tools/build_lexicon.py
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shiyun.rhyme import rhyme_of_pinyin  # noqa: E402

RAW = "https://raw.githubusercontent.com/chinese-poetry/chinese-poetry/master/%E5%85%A8%E5%94%90%E8%AF%97/poet.tang.{}.json"
CACHE = ROOT / ".cache" / "tang"
# 平水韵：https://github.com/charlesix59/chinese_word_rhyme （MIT）
PINGSHUI = "https://raw.githubusercontent.com/charlesix59/chinese_word_rhyme/HEAD/data/Pingshui_Rhyme.json"
CJK = re.compile(r"^[一-鿿]{5}$")
SPLIT = re.compile(r"[，。？！；、,.?!;]")


def fetch_all() -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    poems: list[dict] = []
    for k in range(0, 58000, 1000):
        f = CACHE / f"poet.tang.{k}.json"
        if not f.exists():
            print(f"downloading {f.name}", file=sys.stderr)
            with urllib.request.urlopen(RAW.format(k), timeout=60) as r:
                f.write_bytes(r.read())
        poems.extend(json.loads(f.read_text("utf-8")))
    return poems


def load_pingshui() -> dict[str, list[tuple[str, str]]]:
    """字 -> [(声部, 韵目)]，如 看 -> [("上平声部", "十四寒"), ("去声部", "十五翰")]。"""
    f = ROOT / ".cache" / "Pingshui_Rhyme.json"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(PINGSHUI, timeout=120) as r:
            f.write_bytes(r.read())
    out: dict[str, list[tuple[str, str]]] = {}
    for part, groups in json.loads(f.read_text("utf-8")).items():
        for g, chars in groups.items():
            for c in chars:
                if (part, g) not in out.setdefault(c, []):
                    out[c].append((part, g))
    return out


def pingshui_info(c: str, psy: dict, fallback_tones: str) -> tuple[str, str]:
    """平水韵的 (平仄, 平声韵目)。平水韵未收的字（多为异体）按今音定平仄，且不作韵脚。"""
    if c not in psy:
        return fallback_tones, "-"
    flat = [g for part, g in psy[c] if part.endswith("平声部")]
    tones = ("P" if flat else "") + ("Z" if any(not p.endswith("平声部") for p, _ in psy[c]) else "")
    return tones, "|".join(dict.fromkeys(flat)) or "-"


def char_info(c: str) -> tuple[str, str]:
    """返回 (声调 P/Z, 韵部)。只用常用读音：pypinyin 的全部异读里冷僻读音太多（如“池 tuó”），会污染平仄和韵部。"""
    from pypinyin import Style, pinyin
    from pypinyin.style._utils import get_finals, get_initials

    tones, rhymes = set(), []
    for py in pinyin(c, style=Style.TONE3, heteronym=False, neutral_tone_with_five=True)[0]:
        flat = py[-1] in "12"
        tones.add("P" if flat else "Z")
        if flat:
            bare = py.rstrip("12345")
            r = rhyme_of_pinyin(get_initials(bare, strict=False), get_finals(bare, strict=False))
            if r != "?" and r not in rhymes:
                rhymes.append(r)
    return "".join(sorted(tones)), "|".join(rhymes) or "-"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-chars", type=int, default=4000, help="字表大小（按频次取前 N 个字）")
    args = ap.parse_args()

    import opencc

    t2s = opencc.OpenCC("t2s")
    lines: list[str] = []
    for p in fetch_all():
        segs = [s.strip() for s in SPLIT.split("".join(p.get("paragraphs", []))) if s.strip()]
        if segs and all(len(s) == 5 for s in segs):  # 只取通篇五言的诗
            lines.extend(t2s.convert(s) for s in segs if CJK.match(s))
    print(f"五言诗句 {len(lines)} 句", file=sys.stderr)

    uni, bi, pos = Counter(), Counter(), Counter()
    for ln in lines:
        uni.update(ln)
        pos.update(f"@{d}{c}" for d, c in enumerate(ln))
        bi.update(ln[i : i + 2] for i in range(4))

    data = ROOT / "data"
    top = sorted(uni.items(), key=lambda x: (-x[1], x[0]))[: args.n_chars]
    psy = load_pingshui()
    missing = 0
    with open(data / "chars.tsv", "w", encoding="utf-8") as f:
        for c, n in top:
            tones, rhymes = char_info(c)
            p_tones, p_rhymes = pingshui_info(c, psy, tones)
            missing += c not in psy
            f.write(f"{c}\t{tones}\t{rhymes}\t{p_tones}\t{p_rhymes}\t{n}\n")
    print(f"平水韵未收 {missing} 字（按今音定平仄，不作韵脚）", file=sys.stderr)
    with gzip.open(data / "ngram.tsv.gz", "wt", encoding="utf-8", compresslevel=9) as f:
        for w, n in sorted(uni.items()):
            f.write(f"{w}\t{n}\n")
        for w, n in sorted(bi.items()):
            if n >= 2:
                f.write(f"{w}\t{n}\n")
        for w, n in sorted(pos.items()):
            f.write(f"{w}\t{n}\n")
    with gzip.open(data / "known_lines.txt.gz", "wt", encoding="utf-8", compresslevel=9) as f:
        f.write("\n".join(sorted(set(lines))) + "\n")
    print(f"字表 {len(top)} 字，覆盖语料 {sum(n for _, n in top) / sum(uni.values()):.2%} 字次", file=sys.stderr)


if __name__ == "__main__":
    main()
