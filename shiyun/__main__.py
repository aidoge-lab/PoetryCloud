"""诗云命令行：python -m shiyun <命令>"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

from .judges import make_judge
from .search import DATA, Ledger, Space, load_shards

ROOT = DATA.parent


def judge_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("判别器")
    g.add_argument("--judge", default=os.environ.get("SHIYUN_JUDGE", "laya"),
                   choices=["laya", "jev", "openai", "ngram"])
    g.add_argument("--base-url", default=os.environ.get("SHIYUN_BASE_URL", ""))
    g.add_argument("--model", default=os.environ.get("SHIYUN_MODEL", ""))
    g.add_argument("--api-key", default=os.environ.get("SHIYUN_API_KEY"))
    g.add_argument("--concurrency", type=int, default=4, help="并发请求数")
    g.add_argument("--batch", type=int, default=64)


def get_judge(a, space):
    return make_judge(a.judge, space, a.base_url, a.model, a.api_key, a.concurrency)


def cmd_info(a) -> None:
    sp = Space()
    m = sp.manifest
    led = Ledger.from_json(sp.n, m["global_ledger"])
    print(f"诗云 · PoetryCloud  第一阶段：五言诗句")
    print(f"  字表 {sp.n} 字，全空间 {sp.n}^5 = {sp.n ** 5:.3e} 句")
    print(f"  剪枝后进入判别的句子 {led.leaves:,}（占 {led.leaves / sp.n ** 5:.1e}），分 {m['n_shards']} 片")
    print("  各规则剪掉的组合数：")
    for rule, counts in led.pruned.items():
        print(f"    {rule:6s} " + "  ".join(f"第{d + 1}字 {c * sp.n ** (4 - d):.2e}" for d, c in enumerate(counts) if c))
    print(f"  账本核对：{led.covered():.3e} == {sp.n}^5 {'✓' if led.covered() == sp.n ** 5 else '✗'}")
    if "known_recall" in m:
        print(f"  唐诗五言原句能进入判别的比例（剪枝召回率）：{m['known_recall']:.1%}")
    p = DATA / "progress.json"
    if p.exists():
        pr = json.loads(p.read_text("utf-8"))
        print(f"  进度：{pr['shards_done']}/{pr['shards_total']} 片，已穷举 {pr['covered_ratio']:.4%}，候选 {pr['candidates']:,}")


RULE_NAME = {"lm": "语言模型下界", "dup": "重字", "tone": "平仄", "charset": "不在字表"}


def cmd_trace(a) -> None:
    sp = Space()
    for text in a.lines:
        if len(text) != 5:
            print(f"{text}：不是五字句")
            continue
        rule, d = sp.trace(text)
        lm = sp.lm_score(text)
        lm_s = f"  lm={lm:.2f}（叶子下界 {sp.lm_floor[4] / 5:.2f}）" if lm is not None else ""
        if rule is None:
            print(f"{text}：✓ 进入判别{lm_s}")
        else:
            print(f"{text}：✗ 第{d + 1}字「{text[d]}」被剪（{RULE_NAME[rule]}），同时剪掉其后 {sp.n ** (4 - d):.1e} 句{lm_s}")


def cmd_sample(a) -> None:
    sp = Space()
    shards = load_shards()
    rng = random.Random(a.seed)
    leaves = []
    while len(leaves) < a.n:
        s, e = shards[rng.randrange(len(shards))]
        pi = rng.randrange(s, e)
        got = []
        sp.run_prefixes(pi, pi + 1, lambda _, x: got.append(x), Ledger(sp.n))
        if got:
            leaves.append(rng.choice(got))
    scores = get_judge(a, sp).score([x.text for x in leaves])
    for x, s in sorted(zip(leaves, scores), key=lambda t: -t[1]):
        print(f"{s:.3f}  {x.text}  句式{x.types}  韵{x.rhymes}  lm={x.lm:.2f}")


def cmd_shard(a) -> None:
    from .worker import run_shard

    sp = Space()
    r = run_shard(sp, a.shard, get_judge(a, sp), a.batch)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"shard-{a.shard:06d}.json"
    f.write_text(json.dumps(r, ensure_ascii=False) + "\n", "utf-8")
    print(f"写入 {f}：判 {r['judged']:,} 句，候选 {len(r['candidates'])}，用时 {r['elapsed']:.0f}s")


def cmd_run(a) -> None:
    from . import github as G
    from .worker import run_shard

    sp = Space()
    m = sp.manifest
    judge = get_judge(a, sp)
    repo = G.repo_slug(m)
    print(f"以 {G.me()} 身份为 {repo} 计算，判别器 {judge.id}", file=sys.stderr)
    judge.score(["白日依山尽"])  # 先确认判别器可用，再去认领
    for k in range(a.max_shards or 10**9):
        shard, issue = G.claim(repo, m["n_shards"], m["claim_ttl_days"], judge.id)
        print(f"认领 shard {shard}（#{issue}）", file=sys.stderr)
        r = run_shard(sp, shard, judge, a.batch)
        G.submit(repo, issue, r)
        print(f"已提交 shard {shard}：候选 {len(r['candidates'])}", file=sys.stderr)


def cmd_status(a) -> None:
    from collections import Counter

    from . import github as G

    m = Space().manifest
    st = G.shard_status(G.repo_slug(m), m["n_shards"], m["claim_ttl_days"])
    c = Counter(st.values())
    print(f"完成 {c['done']}  进行中 {c['running']}  空闲 {c['free']}  共 {m['n_shards']}")


def cmd_aggregate(a) -> None:
    from .aggregate import aggregate_github, aggregate_local

    sp = Space()
    if a.local:
        aggregate_local(sp, Path(a.local))
    else:
        judge = get_judge(a, sp) if a.recheck else None
        aggregate_github(sp, judge, a.recheck)
    print(json.dumps(json.loads((DATA / "progress.json").read_text("utf-8")), ensure_ascii=False, indent=2))


def cmd_assemble(a) -> None:
    from .aggregate import load_pool
    from .assemble import assemble

    sp = Space()
    pool = load_pool(sp, a.min_line)
    print(f"好句池 {len(pool)} 句", file=sys.stderr)
    poems, ledger = assemble(pool, get_judge(a, sp), a.top_k, a.couplet_min, a.keep, a.batch)
    (DATA / "poems.json").write_text(json.dumps({"ledger": ledger, "poems": poems}, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(json.dumps(ledger, ensure_ascii=False))
    for p in poems[:10]:
        print(f"{p['score']:.3f} [{p['form']}·{p['rhyme']}韵]  " + "，".join(p["text"].split("/")) + "。")


def cmd_site(a) -> None:
    from . import github as G
    from .aggregate import load_pool
    from .rank import build_site

    sp = Space()
    pf = DATA / "poems.json"
    poems = json.loads(pf.read_text("utf-8"))["poems"] if pf.exists() else []
    repo = a.repo or sp.manifest.get("github_repo") or G.repo_slug(sp.manifest)
    build_site(DATA, ROOT / "site", repo, poems, load_pool(sp))
    print(f"已生成 site/data.json（{len(poems)} 首）")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="shiyun", description="诗云：逐字穷举五言绝句")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="搜索空间、剪枝账本与进度")
    p = sub.add_parser("trace", help="查一句诗在搜索树上的命运")
    p.add_argument("lines", nargs="+")
    p = sub.add_parser("sample", help="随机抽几句叶子并打分（试判别器）")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--seed", type=int)
    judge_args(p)
    p = sub.add_parser("shard", help="本地跑指定分片（不经过 GitHub）")
    p.add_argument("shard", type=int)
    p.add_argument("--out", default="out")
    judge_args(p)
    p = sub.add_parser("run", help="自动认领 → 计算 → 提交，循环")
    p.add_argument("--max-shards", type=int, default=0, help="跑几个分片后停止，0 为不限")
    judge_args(p)
    sub.add_parser("status", help="查看分片认领状态")
    p = sub.add_parser("aggregate", help="汇总并校验结果（维护者 / Actions）")
    p.add_argument("--local", help="从本地目录汇总 shard-*.json，而不是 GitHub")
    p.add_argument("--recheck", type=int, default=0, help="每片用判别器抽查几个候选")
    judge_args(p)
    p = sub.add_parser("assemble", help="第二阶段：从好句池组五言绝句")
    p.add_argument("--min-line", type=float, default=0.5)
    p.add_argument("--top-k", type=int, default=120, help="每种句式取前 K 句参与组诗")
    p.add_argument("--couplet-min", type=float, default=0.5)
    p.add_argument("--keep", type=int, default=300, help="每种格式送整诗判别的数量")
    judge_args(p)
    p = sub.add_parser("site", help="生成投票网页数据 site/data.json")
    p.add_argument("--repo", default="")
    a = ap.parse_args(argv)
    globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
