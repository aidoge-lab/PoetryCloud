"""第二阶段：从第一阶段的好句池里穷举组诗。

同样是“穷举 + 剪枝”，单位从字换成句：
  1. 每种格式（仄起/平起 × 首句入韵与否）的四个位置，各自穷举句式相符的句子；
  2. 前两句、后两句分别组成对句，剪枝规则：押韵、全诗不重字、对句判别分 < 阈值，
     每联只保留分数前 couplet_cap 名；
  3. 两联拼合成全诗，再剪押韵/重字，最后用整诗判别打分排序。
对句的好坏只取决于这两句，所以“先判对句再拼合”与在四层树上逐层剪枝等价。
"""
from __future__ import annotations

import hashlib
import sys
from collections import Counter

from .meter import FORMS, rhymes


def _rset(line: dict) -> set[str]:
    return set() if line["rhymes"] == "-" else set(line["rhymes"].split("|"))


def _chars(*texts: str) -> set[str]:
    return set("".join(texts))


def _disjoint(a: str, b: str) -> bool:
    return not (set(a) & set(b))


def poem_id(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


def assemble(pool: list[dict], judge, top_k: int = 120, couplet_min: float = 0.5,
             poem_keep: int = 300, batch: int = 64, couplet_cap: int = 600,
             log=sys.stderr) -> tuple[list[dict], dict]:
    ledger = Counter()

    def judged(pairs, kind):
        out = []
        for k in range(0, len(pairs), batch):
            chunk = pairs[k : k + batch]
            out += judge.score(["/".join(x["text"] for x in p) for p in chunk], kind)
        return out

    poems: list[dict] = []
    for form, slots in FORMS.items():
        rh = rhymes(form)
        by_type = {t: [x for x in pool if t in x["types"]][:top_k] for t in set(slots)}
        couplets = []
        for half in (0, 2):
            ta, tb = slots[half], slots[half + 1]
            pairs = []
            for a in by_type[ta]:
                for b in by_type[tb]:
                    ledger["枚举对句"] += 1
                    if not _disjoint(a["text"], b["text"]):
                        ledger["剪:重字"] += 1
                        continue
                    need = [x for i, x in ((half, a), (half + 1, b)) if i in rh]
                    if len(need) == 2 and not (_rset(need[0]) & _rset(need[1])):
                        ledger["剪:押韵"] += 1
                        continue
                    if any(not _rset(x) for x in need):
                        ledger["剪:押韵"] += 1
                        continue
                    pairs.append((a, b))
            scores = judged(pairs, "couplet")
            kept = [(p, s) for p, s in zip(pairs, scores) if s >= couplet_min]
            ledger["剪:对句分"] += len(pairs) - len(kept)
            kept.sort(key=lambda x: -x[1])
            ledger["剪:对句排名"] += max(0, len(kept) - couplet_cap)  # 定界：只留每联前 couplet_cap 名
            kept = kept[:couplet_cap]
            couplets.append(kept)
            print(f"[{form}] 第{half // 2 + 1}联：候选 {len(pairs)}，保留 {len(kept)}", file=log)

        joined = []
        for (p1, s1) in couplets[0]:
            for (p2, s2) in couplets[1]:
                ledger["枚举全诗"] += 1
                lines = [*p1, *p2]
                if len(_chars(*(x["text"] for x in lines))) < 20 - _redup(lines):
                    ledger["剪:重字"] += 1
                    continue
                common = set.intersection(*(_rset(lines[i]) for i in rh))
                ends = [lines[i]["text"][-1] for i in rh]
                if not common or len(set(ends)) < len(ends):
                    ledger["剪:押韵"] += 1
                    continue
                joined.append((lines, s1 * s2, sorted(common)[0]))
        joined.sort(key=lambda x: -x[1])
        joined = joined[:poem_keep]
        scores = judged([j[0] for j in joined], "poem")
        for (lines, cs, rhyme), s in zip(joined, scores):
            text = "/".join(x["text"] for x in lines)
            poems.append({"id": poem_id(text), "text": text, "form": form, "rhyme": rhyme,
                          "score": round(s, 4), "couplet_score": round(cs, 4),
                          "line_scores": [x["score"] for x in lines],
                          "known_lines": sum(x["known"] for x in lines)})
        print(f"[{form}] 拼合 {len(joined)} 首送整诗判别", file=log)

    poems.sort(key=lambda p: -p["score"])
    return poems, dict(ledger)


def _redup(lines: list[dict]) -> int:
    """句内叠字（如“青青”）占用的重复字数，不算重字。"""
    return sum(sum(1 for i in range(4) if x["text"][i] == x["text"][i + 1]) for x in lines)
