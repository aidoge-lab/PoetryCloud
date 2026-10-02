"""跑一个分片：逐字穷举 → 判别器打分 → 留下候选。按前缀做断点续跑。"""
from __future__ import annotations

import gzip
import json
import sys
import time
from pathlib import Path

from .judges import PROMPT_VERSION, Judge
from .search import Leaf, Ledger, Space, load_shards

STATE = Path(__file__).resolve().parent.parent / "state"
RESULT_VERSION = 1


class Known:
    """唐诗原句集合：被搜出来的原句是“重新发现”，也用来估计判别器的召回率。"""

    def __init__(self, space: Space):
        with gzip.open(space.data_dir / "known_lines.txt.gz", "rt", encoding="utf-8") as f:
            self.lines = frozenset(ln.strip() for ln in f)

    def __contains__(self, text: str) -> bool:
        return text in self.lines


def run_shard(space: Space, shard: int, judge: Judge, batch: int = 64,
              state_dir: Path = STATE, log=sys.stderr) -> dict:
    m = space.manifest
    threshold = m["judge"]["submit_threshold"]
    start, end = load_shards(space.data_dir)[shard]
    known = Known(space)
    state_dir.mkdir(parents=True, exist_ok=True)
    ckpt = state_dir / f"shard-{shard:06d}.json"

    st = {"next": start, "ledger": Ledger(space.n).to_json(), "hist": [0] * 10,
          "known_n": 0, "known_hit": 0, "cands": [], "judged": 0, "elapsed": 0.0}
    if ckpt.exists():
        saved = json.loads(ckpt.read_text("utf-8"))
        if saved.get("judge") == judge.id and saved.get("space") == space.sha256:
            st = saved["state"]
            print(f"[shard {shard}] 从前缀 {st['next']} 续跑", file=log)

    ledger = Ledger.from_json(space.n, st["ledger"])
    t0 = time.time() - st["elapsed"]
    for pi in range(st["next"], end):
        leaves: list[Leaf] = []
        space.run_prefixes(pi, pi + 1, lambda _, leaf: leaves.append(leaf), ledger)
        for k in range(0, len(leaves), batch):
            chunk = leaves[k : k + batch]
            scores = judge.score([x.text for x in chunk])
            for leaf, s in zip(chunk, scores):
                st["hist"][min(int(s * 10), 9)] += 1
                is_known = leaf.text in known
                if is_known:
                    st["known_n"] += 1
                    st["known_hit"] += s >= threshold
                if s >= threshold:
                    st["cands"].append([leaf.text, round(s, 4), round(leaf.lm, 3), leaf.types, leaf.rhymes, int(is_known)])
            st["judged"] += len(chunk)
        st["next"] = pi + 1
        st["ledger"] = ledger.to_json()
        st["elapsed"] = time.time() - t0
        ckpt.write_text(json.dumps({"judge": judge.id, "space": space.sha256, "state": st}, ensure_ascii=False))
        done = pi + 1 - start
        if done % 20 == 0 or pi + 1 == end:
            rate = st["judged"] / max(st["elapsed"], 1e-9)
            print(f"[shard {shard}] 前缀 {done}/{end - start}  已判 {st['judged']:,}  候选 {len(st['cands'])}  {rate:.0f} 句/秒", file=log)

    cands = sorted(st["cands"], key=lambda c: -c[1])[: m["judge"]["max_candidates_per_shard"]]
    return {
        "v": RESULT_VERSION,
        "shard": shard,
        "space_sha256": space.sha256,
        "judge": judge.id,
        "prompt_version": PROMPT_VERSION,
        "ledger": ledger.to_json(),
        "judged": st["judged"],
        "hist": st["hist"],
        "known": {"n": st["known_n"], "hit": st["known_hit"]},
        "elapsed": round(st["elapsed"], 1),
        "candidates": cands,
    }
