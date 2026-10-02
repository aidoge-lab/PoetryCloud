"""诗云命令行：python -m shiyun <命令>"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

from .judges import make_judge
from .search import DATA, METERS, Ledger, Space, load_shards

ROOT = DATA.parent
RULE_NAME = {"lm": "语言模型下界", "dup": "重字", "tone": "平仄", "charset": "不在字表"}


def meter_arg(p: argparse.ArgumentParser, multi: bool = False) -> None:
    if multi:
        p.add_argument("--meter", choices=list(METERS), help="只看一个格律空间（默认全部）")
    else:
        p.add_argument("--meter", choices=list(METERS), default=os.environ.get("SHIYUN_METER", "psy"),
                       help="格律空间：psy 平水韵（默认）/ xin 中华新韵")


def judge_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("判别器")
    g.add_argument("--judge", default=os.environ.get("SHIYUN_JUDGE", "laya"), choices=["laya", "jev", "openai", "ngram"])
    g.add_argument("--base-url", default=os.environ.get("SHIYUN_BASE_URL", ""))
    g.add_argument("--model", default=os.environ.get("SHIYUN_MODEL", ""), help="laya：本地微调检查点目录")
    g.add_argument("--device", default=os.environ.get("SHIYUN_DEVICE", ""), help="mps / cuda / cpu，默认自动")
    g.add_argument("--api-key", default=os.environ.get("SHIYUN_API_KEY"))
    g.add_argument("--concurrency", type=int, default=4, help="HTTP 并发请求数")
    g.add_argument("--batch", type=int, default=64)


def get_judge(a, space):
    return make_judge(a.judge, space, a.base_url, a.model, a.api_key, a.concurrency, a.device)


def spaces_of(a) -> dict[str, Space]:
    built = Space("psy").manifest["spaces"]
    meters = [a.meter] if getattr(a, "meter", None) else [m for m in METERS if m in built]
    return {m: Space(m) for m in meters}


def fmt_big(x) -> str:
    x = int(x)
    return f"{x:.3e}" if x >= 10**6 else f"{x:,}"


def cmd_info(a) -> None:
    for meter, sp in spaces_of(a).items():
        info = sp.manifest["spaces"].get(meter)
        print(f"诗云 · {sp.meter_name}（{meter}）")
        if not info:
            print("  尚未生成分片表：python3 tools/build_shards.py --meter", meter)
            continue
        led = Ledger.from_json(sp.n, info["global_ledger"])
        print(f"  单句：{sp.n}^5 = {sp.n ** 5:.3e} 句，剪枝后 {led.leaves:,} 句进入判别（占 {led.leaves / sp.n ** 5:.1e}），分 {info['n_shards']} 片")
        print("  各规则剪掉的句数：")
        for rule, counts in led.pruned.items():
            print(f"    {RULE_NAME[rule]:<6s} " + "  ".join(f"第{d + 1}字 {c * sp.n ** (4 - d):.2e}" for d, c in enumerate(counts) if c))
        print(f"  单句账本：{led.covered():.3e} == {sp.n}^5 {'✓' if led.covered() == sp.n ** 5 else '✗'}")
        print(f"  剪枝召回率（唐诗原句能进入判别的比例）：{info['known_recall']:.1%}")
        p = DATA / "progress.json"
        pr = json.loads(p.read_text("utf-8")).get(meter) if p.exists() else None
        if pr:
            q = pr["quatrain"]
            print(f"  进度：{pr['shards_done']}/{pr['shards_total']} 片，已判 {pr['judged_ratio']:.2%}，"
                  f"通过初筛 {pr['lines_passed']:,} 句，复判 {pr['shards_rejudged']} 片")
            print(f"  整首（20 字）：{sp.n}^20 = {sp.n ** 20:.3e} 首，已初筛 {q['screened_ratio']:.6%}，"
                  f"通过初筛 {fmt_big(q['survived'])} 首，待初筛 {fmt_big(q['pending'])} 首")
        print()


def cmd_trace(a) -> None:
    for meter, sp in spaces_of(a).items():
        print(f"[{sp.meter_name}]")
        for text in a.lines:
            if len(text) != 5:
                print(f"  {text}：不是五字句")
                continue
            rule, d = sp.trace(text)
            lm = sp.lm_score(text)
            lm_s = f"  lm={lm:.2f}（叶子下界 {sp.lm_floor[4] / 5:.2f}）" if lm is not None else ""
            if rule is None:
                print(f"  {text}：✓ 进入判别{lm_s}")
            else:
                print(f"  {text}：✗ 第{d + 1}字「{text[d]}」被剪（{RULE_NAME[rule]}），同时剪掉其后 {sp.n ** (4 - d):.1e} 句{lm_s}")


def cmd_sample(a) -> None:
    sp = Space(a.meter)
    shards = load_shards(DATA, a.meter)
    rng = random.Random(a.seed)
    leaves = []
    while len(leaves) < a.n:
        s, e = shards[rng.randrange(len(shards))]
        pi, got = rng.randrange(s, e), []
        sp.run_prefixes(pi, pi + 1, lambda _, x: got.append(x), Ledger(sp.n))
        if got:
            leaves.append(rng.choice(got))
    scores = get_judge(a, sp).score([x.text for x in leaves])
    for x, s in sorted(zip(leaves, scores), key=lambda t: -t[1]):
        print(f"{s:.3f}  {x.text}  句式{x.types}  韵{x.rhymes}  lm={x.lm:.2f}")


def cmd_shard(a) -> None:
    from .worker import dump_result, run_shard

    sp = Space(a.meter)
    r = run_shard(sp, a.shard, get_judge(a, sp), a.batch)
    r["contributor"] = "local"
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{a.meter}-shard-{a.shard:06d}.json.gz"
    f.write_bytes(dump_result(r))
    print(f"写入 {f}：判 {r['judged']:,} 句，候选 {len(r['candidates'])}，用时 {r['elapsed']:.0f}s")


def cmd_run(a) -> None:
    from . import github as G
    from .worker import dump_result, run_shard

    sp = Space(a.meter)
    m = sp.manifest
    judge = get_judge(a, sp)
    if m["judge"]["official"] and judge.id != m["judge"]["official"]:
        print(f"⚠️  当前判别器 {judge.id} 不是官方判别器 {m['judge']['official']}；"
              "结果会被汇总端统一复判，但候选是按你的判别器筛出来的。", file=sys.stderr)
    repo = G.repo_slug(m)
    who = G.Contributor(repo)
    print(f"以 {who.login} 身份为 {repo} 计算 {sp.meter_name} 空间，判别器 {judge.id}", file=sys.stderr)
    judge.score(["白日依山尽"])  # 先确认判别器可用，再去认领
    for _ in range(a.max_shards or 10**9):
        shard, number, branch = who.claim(a.meter, m["spaces"][a.meter]["n_shards"], m["claim_ttl_days"], judge.id)
        print(f"认领 {a.meter} 分片 {shard}（PR #{number}）", file=sys.stderr)
        r = run_shard(sp, shard, judge, a.batch)
        r["contributor"] = who.login
        summary = (f"判了 {r['judged']:,} 句，提交 {len(r['candidates'])} 个候选，用时 {r['elapsed'] / 3600:.1f} 小时。\n"
                   f"判别器 `{judge.id}`，唐诗原句命中 {r['known']['hit']}/{r['known']['n']}。")
        who.submit(a.meter, shard, number, branch, dump_result(r), summary)
        print(f"已提交 {a.meter} 分片 {shard}：候选 {len(r['candidates'])}", file=sys.stderr)


def cmd_status(a) -> None:
    from collections import Counter

    from . import github as G

    for meter, sp in spaces_of(a).items():
        m = sp.manifest
        st = G.shard_status(G.repo_slug(m), meter, m["spaces"][meter]["n_shards"], m["claim_ttl_days"])
        c = Counter(st.values())
        print(f"{sp.meter_name}：完成 {c['done']}  待合并 {c['review']}  计算中 {c['running']}  空闲 {c['free']}  共 {len(st)}")


def cmd_aggregate(a) -> None:
    from . import github as G
    from .aggregate import aggregate_github, aggregate_local, collect_votes, write_progress

    spaces = spaces_of(a)
    if a.local:
        for sp in spaces.values():
            aggregate_local(sp, Path(a.local))
    else:
        aggregate_github(spaces)
        if not a.no_votes:
            collect_votes(DATA, G.repo_slug(next(iter(spaces.values())).manifest))
    prog = write_progress(spaces)
    for meter, p in prog.items():
        print(f"{p['name']}：{p['shards_done']}/{p['shards_total']} 片，通过初筛 {p['lines_passed']:,} 句，"
              f"整首已初筛 {p['quatrain']['screened_ratio']:.6%}")


def cmd_progress(a) -> None:
    from .aggregate import write_progress

    for p in write_progress(spaces_of(a)).values():
        print(f"{p['name']}：{p['shards_done']}/{p['shards_total']} 片，整首已初筛 {p['quatrain']['screened_ratio']:.6%}")


def cmd_rejudge(a) -> None:
    from .aggregate import rejudge, write_progress

    spaces = spaces_of(a)
    for sp in spaces.values():
        judge = get_judge(a, sp)
        official = sp.manifest["judge"]["official"]
        if official and judge.id != official and not a.force:
            raise SystemExit(f"当前判别器 {judge.id} 不是官方判别器 {official}；确需如此请加 --force")
        print(f"{sp.meter_name}：复判 {rejudge(sp, judge)} 个分片")
    write_progress(spaces)


def cmd_assemble(a) -> None:
    from .aggregate import load_pool
    from .assemble import assemble

    sp = Space(a.meter)
    pool = load_pool(sp, a.min_line)
    print(f"{sp.meter_name} 好句池 {len(pool)} 句（不含唐诗原句）", file=sys.stderr)
    poems, ledger = assemble(pool, get_judge(a, sp), a.top_k, a.couplet_min, a.keep, a.batch)
    (DATA / f"poems-{a.meter}.json").write_text(
        json.dumps({"ledger": ledger, "poems": poems}, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(json.dumps(ledger, ensure_ascii=False))
    for p in poems[:10]:
        print(f"{p['score']:.3f} [{p['form']}·{p['rhyme']}]  " + "，".join(p["text"].split("/")) + "。")


def cmd_site(a) -> None:
    from .aggregate import load_pool
    from .rank import build_site

    spaces = spaces_of(a)
    by_meter = {}
    for meter, sp in spaces.items():
        pf = DATA / f"poems-{meter}.json"
        everything = load_pool(sp, include_known=True)
        by_meter[meter] = {
            "name": sp.meter_name,
            "poems": json.loads(pf.read_text("utf-8"))["poems"] if pf.exists() else [],
            "lines": [x for x in everything if not x["known"]],
            "rediscovered": [x for x in everything if x["known"]],
        }
    repo = next(iter(spaces.values())).manifest["github_repo"]
    build_site(DATA, ROOT / "site", repo, by_meter)
    print("已生成 site/data.json：" + "，".join(f"{d['name']} {len(d['poems'])} 首" for d in by_meter.values()))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="shiyun", description="诗云：逐字穷举五言绝句")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info", help="搜索空间、剪枝账本、整首账本与进度")
    meter_arg(p, multi=True)
    p = sub.add_parser("trace", help="查一句诗在搜索树上的命运")
    p.add_argument("lines", nargs="+")
    meter_arg(p, multi=True)
    p = sub.add_parser("sample", help="随机抽几句叶子并打分（试判别器）")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--seed", type=int)
    meter_arg(p)
    judge_args(p)
    p = sub.add_parser("shard", help="本地跑指定分片（不经过 GitHub）")
    p.add_argument("shard", type=int)
    p.add_argument("--out", default="out")
    meter_arg(p)
    judge_args(p)
    p = sub.add_parser("run", help="自动认领 → 计算 → 提交 PR，循环")
    p.add_argument("--max-shards", type=int, default=0, help="跑几个分片后停止，0 为不限")
    meter_arg(p)
    judge_args(p)
    p = sub.add_parser("status", help="查看分片认领状态")
    meter_arg(p, multi=True)
    p = sub.add_parser("aggregate", help="校验合并 PR、收集评审票、更新进度（Actions 自动运行）")
    p.add_argument("--local", help="从本地目录汇总 <空间>-shard-*.json.gz，而不是 GitHub")
    p.add_argument("--no-votes", action="store_true")
    meter_arg(p, multi=True)
    p = sub.add_parser("progress", help="根据仓库里的结果重算 data/progress.json")
    meter_arg(p, multi=True)
    p = sub.add_parser("rejudge", help="维护者用官方判别器统一复判全部候选")
    p.add_argument("--force", action="store_true")
    meter_arg(p, multi=True)
    judge_args(p)
    p = sub.add_parser("assemble", help="第二阶段：从好句池组五言绝句")
    p.add_argument("--min-line", type=float, default=None)
    p.add_argument("--top-k", type=int, default=120, help="每种句式取前 K 句参与组诗")
    p.add_argument("--couplet-min", type=float, default=0.5)
    p.add_argument("--keep", type=int, default=300, help="每种格式送整诗判别的数量")
    meter_arg(p)
    judge_args(p)
    p = sub.add_parser("site", help="生成网页数据 site/data.json")
    meter_arg(p, multi=True)
    a = ap.parse_args(argv)
    globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
