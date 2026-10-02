"""生成任务分片表 data/shards.tsv，并核对全局穷举账本（维护者使用）。

每个分片 = 前缀表上的一段连续区间，按实际叶子数均衡切分。
运行：python3 tools/build_shards.py [--target 200000]
"""
from __future__ import annotations

import argparse
import os
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shiyun.search import DATA, Space, build_shards  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=500_000, help="每个分片的目标叶子数")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()

    space = Space()
    total = space.prefix_ledger()
    shards = build_shards(space, args.target, total, args.workers)
    with open(DATA / "shards.tsv", "w", encoding="utf-8") as f:
        for k, (s, e, leaves) in enumerate(shards):
            f.write(f"{k}\t{s}\t{e}\t{leaves}\n")

    assert total.covered() == space.n ** 5, (total.covered(), space.n ** 5)

    import gzip

    with gzip.open(DATA / "known_lines.txt.gz", "rt", encoding="utf-8") as f:
        known = [ln.strip() for ln in f if ln.strip()]
    recall = sum(space.trace(t)[0] is None for t in known) / len(known)

    mpath = DATA / "manifest.json"
    m = json.loads(mpath.read_text("utf-8"))
    m.update(
        {
            "space_sha256": space.sha256,
            "n_chars": space.n,
            "n_prefixes": len(space.prefixes()),
            "n_shards": len(shards),
            "n_leaves": total.leaves,
            "known_recall": round(recall, 4),
            "global_ledger": total.to_json(),
        }
    )
    mpath.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"唐诗原句召回率 {recall:.1%}")
    print(f"{len(shards)} 个分片，{total.leaves:,} 句进入判别；账本覆盖 {total.covered():.3e} = {space.n}^5 ✓")


if __name__ == "__main__":
    main()
