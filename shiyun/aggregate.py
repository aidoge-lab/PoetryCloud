"""汇总器：校验分片结果 → 合并 PR → 复判 → 更新进度（含 20 字整首账本）→ 收集评审票。

校验是严格的：汇总器把该分片重新穷举一遍（不调用判别器），要求账本逐项一致、
每个候选都确实是该分片的叶子。分数由维护者用官方判别器统一复判（rejudge），
入库的分数因此全部出自同一个判别器；参与者只可能“漏报”，无法“虚报”。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from . import github as G
from .quatrain import ledger as quatrain_ledger
from .search import Ledger, Space, load_shards
from .worker import RESULT_VERSION, dump_result, load_result, result_path


def verify(space: Space, r: dict) -> str | None:
    """返回 None 表示通过，否则返回拒绝理由。"""
    m = space.manifest
    if r.get("v") != RESULT_VERSION:
        return f"结果格式版本 {r.get('v')} 不符"
    if r.get("meter") != space.meter or r.get("space_sha256") != space.sha256:
        return "搜索空间不符（格律体系、字表或剪枝参数版本不同）"
    shards = load_shards(space.data_dir, space.meter)
    if not 0 <= r["shard"] < len(shards):
        return "分片编号越界"
    start, end = shards[r["shard"]]
    led = Ledger(space.n)
    leaves = {}
    space.run_prefixes(start, end, lambda _, x: leaves.__setitem__(x.text, x), led)
    if led.to_json() != r["ledger"]:
        return "穷举账本与复算不一致"
    if r["judged"] != led.leaves or sum(r["hist"]) != led.leaves:
        return "判别数量与叶子数不一致"
    th = m["judge"]["submit_threshold"]
    if len(r["candidates"]) > m["judge"]["max_candidates_per_shard"]:
        return "候选数超限"
    for text, score, lm, types, rhymes, _ in r["candidates"]:
        leaf = leaves.get(text)
        if leaf is None:
            return f"候选「{text}」不属于该分片"
        if (types, rhymes) != (leaf.types, leaf.rhymes):
            return f"候选「{text}」的句式或韵部与复算不一致"
        if not th <= score <= 1:
            return f"候选「{text}」分数 {score} 不合法"
    return None


def store(space: Space, r: dict) -> Path:
    p = result_path(space.data_dir, space.meter, r["shard"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(dump_result(r))
    return p


def load_results(space: Space) -> list[dict]:
    d = space.data_dir / "results" / space.meter
    return [load_result(p.read_bytes()) for p in sorted(d.glob("shard-*.json.gz"))]


def official_scores(r: dict) -> list[float]:
    """有复判分数就用复判分数，否则用提交者的分数。"""
    off = r.get("official")
    return off["scores"] if off else [c[1] for c in r["candidates"]]


# ---------------- GitHub：合并 PR ----------------
def aggregate_github(spaces: dict[str, Space], log=sys.stderr) -> None:
    some = next(iter(spaces.values()))
    m = some.manifest
    repo = G.repo_slug(m)
    for pr in G.stale_claims(repo, m["claim_ttl_days"]):
        G.close(repo, pr["number"], f"认领超过 {m['claim_ttl_days']} 天未提交，分片重新开放。")
        print(f"#{pr['number']} 过期关闭", file=log)
    for pr in G.ready_claims(repo):
        meter, shard = G.CLAIM_RE.match(pr["title"]).groups()
        shard = int(shard)
        space = spaces.get(meter)
        try:
            if space is None:
                raise ValueError(f"未知空间 {meter}")
            files, info = G.pr_files(repo, pr["number"])
            want = G.result_file(meter, shard)
            if files != [want]:
                raise ValueError(f"PR 只能改动 {want}，实际改动了：{', '.join(files) or '（无）'}")
            head = f"{info['headRepositoryOwner']['login']}/{info['headRepository']['name']}"
            r = load_result(G.fetch_file(head, want, info["headRefOid"]))
            if (r.get("meter"), r.get("shard")) != (meter, shard):
                reason = "结果中的空间或分片号与标题不符"
            elif r.get("contributor") != pr["author"]["login"]:
                reason = "结果中的提交者与 PR 作者不符"
            else:
                reason = verify(space, r)
        except Exception as e:  # noqa: BLE001 —— 任何读取/解析失败都等同于校验不通过
            reason = str(e)
        if reason:
            G.close(repo, pr["number"], f"❌ 校验未通过：{reason}。分片重新开放。", label="rejected")
            print(f"#{pr['number']} 拒绝：{reason}", file=log)
            continue
        G.merge(repo, pr["number"], f"✅ 校验通过：判 {r['judged']:,} 句，候选 {len(r['candidates'])} 个。感谢 @{pr['author']['login']}！")
        print(f"#{pr['number']} 合并 {meter}/{shard}", file=log)


def aggregate_local(space: Space, src: Path, log=sys.stderr) -> None:
    for f in sorted(src.glob(f"{space.meter}-shard-*.json.gz")):
        r = load_result(f.read_bytes())
        reason = verify(space, r)
        print(f"{f.name} {'拒绝：' + reason if reason else '并入'}", file=log)
        if not reason:
            store(space, r)


# ---------------- 维护者：统一复判 ----------------
def rejudge(space: Space, judge, log=sys.stderr) -> int:
    """用官方判别器给尚未复判的结果文件重新打分。返回处理的文件数。"""
    n = 0
    d = space.data_dir / "results" / space.meter
    for p in sorted(d.glob("shard-*.json.gz")):
        r = load_result(p.read_bytes())
        if r.get("official", {}).get("judge") == judge.id:
            continue
        scores = judge.score([c[0] for c in r["candidates"]]) if r["candidates"] else []
        r["official"] = {"judge": judge.id, "scores": [round(s, 4) for s in scores]}
        p.write_bytes(dump_result(r))
        n += 1
        print(f"复判 {p.name}：{len(scores)} 句", file=log)
    return n


# ---------------- 结果池与进度 ----------------
def load_pool(space: Space, min_score: float | None = None, include_known: bool = False) -> list[dict]:
    """第一阶段好句池，按分数降序。默认不含唐诗原句（D6：标为重新发现，不进排名）。"""
    th = space.manifest["judge"]["submit_threshold"] if min_score is None else min_score
    pool = {}
    for r in load_results(space):
        for (text, _, lm, types, rhymes, known), score in zip(r["candidates"], official_scores(r)):
            if score < th or (known and not include_known):
                continue
            if text not in pool or score > pool[text]["score"]:
                pool[text] = {"text": text, "score": score, "lm": lm, "types": types, "rhymes": rhymes,
                              "known": bool(known), "rejudged": "official" in r}
    return sorted(pool.values(), key=lambda x: -x["score"])


def write_progress(spaces: dict[str, Space]) -> dict:
    out = {}
    for meter, space in spaces.items():
        info = space.manifest["spaces"][meter]
        th = space.manifest["judge"]["submit_threshold"]
        results = load_results(space)
        shard_leaves = [int(row.split("\t")[3]) for row in
                        (space.data_dir / f"shards-{meter}.tsv").read_text("utf-8").splitlines()]
        done_ids = {r["shard"] for r in results}
        done = Ledger(space.n)
        passed: Counter = Counter()
        for r in results:
            done.merge(Ledger.from_json(space.n, r["ledger"]))
            for c, s in zip(r["candidates"], official_scores(r)):
                if s >= th:
                    passed[(c[3], c[4])] += 1
        pending = sum(n for k, n in enumerate(shard_leaves) if k not in done_ids)
        q = quatrain_ledger(space.n, passed, pending)
        known_n = sum(r["known"]["n"] for r in results)
        out[meter] = {
            "name": info["name"],
            "shards_done": len(results),
            "shards_total": info["n_shards"],
            "shards_rejudged": sum("official" in r for r in results),
            "leaves_judged": done.leaves,
            "leaves_total": info["n_leaves"],
            "judged_ratio": done.leaves / info["n_leaves"],
            "lines_passed": sum(passed.values()),
            "known_recall_search": info["known_recall"],
            "known_recall_judge": (sum(r["known"]["hit"] for r in results) / known_n) if known_n else None,
            "quatrain": {k: (str(v) if isinstance(v, int) else [str(x) for x in v] if isinstance(v, list) else v)
                         for k, v in q.items()},
            "contributors": sorted({r.get("contributor", "") for r in results} - {""}),
        }
    some = next(iter(spaces.values()))
    (some.data_dir / "progress.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return out


# ---------------- 评审团投票 ----------------
VOTE_RE = re.compile(r"```json\n(.*?)\n```", re.S)


def reviewers(data_dir: Path) -> set[str]:
    p = data_dir / "reviewers.json"
    return set(json.loads(p.read_text("utf-8"))["reviewers"]) if p.exists() else set()


def collect_votes(data_dir: Path, repo: str) -> None:
    """[vote] issue 的正文是网页生成的 JSON：{"votes": {poem_id: 1|0|-1}}。只计评审团成员的票，每人每首以最后一次为准。"""
    path = data_dir / "votes.json"
    votes: dict[str, dict[str, int]] = json.loads(path.read_text("utf-8")) if path.exists() else {}
    panel = reviewers(data_dir)
    for issue in sorted(G.list_vote_issues(repo), key=lambda i: i["number"]):
        user = issue["author"]["login"]
        if user not in panel:
            G.close_issue(repo, issue["number"], "谢谢！目前排名只计评审团成员的票（名单见 data/reviewers.json）。")
            continue
        m = VOTE_RE.search(issue["body"] or "")
        try:
            data = json.loads(m.group(1))["votes"] if m else {}
        except (json.JSONDecodeError, KeyError):
            data = {}
        for pid, v in data.items():
            if v in (-1, 0, 1):
                votes.setdefault(pid, {})[user] = v
        G.close_issue(repo, issue["number"], f"已记录 {len(data)} 票，谢谢！")
    path.write_text(json.dumps(votes, ensure_ascii=False, indent=1) + "\n", "utf-8")
