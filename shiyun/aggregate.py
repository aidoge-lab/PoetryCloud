"""汇总器：校验分片结果 → 并入结果池 → 更新进度 → 收集投票。

校验是严格的：汇总器把该分片重新穷举一遍（不调用判别器，单核约 1 秒），
要求账本逐项一致、每个候选都确实是该分片的叶子。分数无法零成本复核，
可选 --recheck 用判别器抽查。
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

from . import github as G
from .search import Ledger, Space, load_shards
from .worker import RESULT_VERSION

RESULTS = "results/s1"


def verify(space: Space, r: dict, judge=None, recheck_n: int = 0) -> str | None:
    """返回 None 表示通过，否则返回拒绝理由。"""
    m = space.manifest
    if r.get("v") != RESULT_VERSION:
        return f"结果格式版本 {r.get('v')} 不符"
    if r.get("space_sha256") != space.sha256:
        return "搜索空间哈希不符（词表或剪枝参数版本不同）"
    shards = load_shards(space.data_dir)
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
    for text, score, *_ in r["candidates"]:
        if text not in leaves:
            return f"候选「{text}」不属于该分片"
        if not th <= score <= 1:
            return f"候选「{text}」分数 {score} 不合法"
    if judge and recheck_n and r["candidates"]:
        sample = random.sample(r["candidates"], min(recheck_n, len(r["candidates"])))
        again = judge.score([c[0] for c in sample])
        diff = sum(abs(a - c[1]) for a, c in zip(again, sample)) / len(sample)
        if diff > 0.15:
            return f"抽查分数偏差过大（平均 {diff:.2f}）"
    return None


def merge_parts(parts: list[dict]) -> dict:
    parts = sorted(parts, key=lambda p: p.get("part", 0))
    out = {k: v for k, v in parts[0].items() if k not in ("part", "parts")}
    out["candidates"] = [c for p in parts for c in p["candidates"]]
    return out


def store(space: Space, r: dict, meta: dict) -> Path:
    d = space.data_dir / RESULTS
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"shard-{r['shard']:06d}.json"
    p.write_text(json.dumps(dict(r, **meta), ensure_ascii=False, separators=(",", ":")) + "\n", "utf-8")
    return p


def aggregate_github(space: Space, judge=None, recheck_n: int = 0, log=sys.stderr) -> None:
    repo = G.repo_slug(space.manifest)
    for issue in G.list_issues(repo, "[claim]", state="closed"):
        labels = {lb["name"] for lb in issue["labels"]}
        if labels & {"merged", "rejected", "duplicate"}:
            continue
        m = G.CLAIM_RE.match(issue["title"])
        if not m:
            continue
        author, parts = G.read_results(repo, issue["number"])
        if not parts or len(parts) != parts[0].get("parts", 1):
            G.label(repo, issue["number"], "rejected", "没有完整的结果评论（撞车让出或中途放弃），不计入。")
            continue
        r = merge_parts(parts)
        reason = "结果中的分片号与标题不符" if r["shard"] != int(m.group(1)) else verify(space, r, judge, recheck_n)
        if reason:
            G.label(repo, issue["number"], "rejected", f"❌ 校验未通过：{reason}。分片重新开放。")
            print(f"#{issue['number']} 拒绝：{reason}", file=log)
            continue
        store(space, r, {"contributor": author, "issue": issue["number"]})
        G.label(repo, issue["number"], "merged",
                f"✅ 已并入。判 {r['judged']:,} 句，候选 {len(r['candidates'])} 个。感谢 @{author}！")
        print(f"#{issue['number']} 并入 shard {r['shard']}", file=log)
    collect_votes(space, repo)
    write_progress(space)


def aggregate_local(space: Space, src: Path, log=sys.stderr) -> None:
    for f in sorted(src.glob("shard-*.json")):
        r = json.loads(f.read_text("utf-8"))
        reason = verify(space, r)
        if reason:
            print(f"{f.name} 拒绝：{reason}", file=log)
            continue
        store(space, r, {"contributor": "local", "issue": None})
        print(f"{f.name} 并入", file=log)
    write_progress(space)


def load_results(space: Space) -> list[dict]:
    return [json.loads(p.read_text("utf-8")) for p in sorted((space.data_dir / RESULTS).glob("shard-*.json"))]


def load_pool(space: Space, min_score: float = 0.0) -> list[dict]:
    """第一阶段结果池：全部候选句，按分数降序。"""
    pool = {}
    for r in load_results(space):
        for text, score, lm, types, rhymes, known in r["candidates"]:
            if score >= min_score and (text not in pool or score > pool[text]["score"]):
                pool[text] = {"text": text, "score": score, "lm": lm, "types": types,
                              "rhymes": rhymes, "known": bool(known), "judge": r["judge"]}
    return sorted(pool.values(), key=lambda x: -x["score"])


def write_progress(space: Space) -> dict:
    m = space.manifest
    results = load_results(space)
    led = Ledger.from_json(space.n, m["global_ledger"])
    prefix_part = space.prefix_ledger()
    done = Ledger(space.n)
    for r in results:
        done.merge(Ledger.from_json(space.n, r["ledger"]))
    covered = prefix_part.covered() + done.covered()
    known_n = sum(r["known"]["n"] for r in results)
    prog = {
        "shards_done": len(results),
        "shards_total": m["n_shards"],
        "space": f"{space.n}^5",
        "space_size": space.n ** 5,
        "covered": covered,
        "covered_ratio": covered / space.n ** 5,
        "leaves_judged": done.leaves,
        "leaves_total": led.leaves,
        "judged_ratio": done.leaves / led.leaves if led.leaves else 0.0,
        "candidates": sum(len(r["candidates"]) for r in results),
        "known_recall": (sum(r["known"]["hit"] for r in results) / known_n) if known_n else None,
        "contributors": sorted({r["contributor"] for r in results}),
    }
    (space.data_dir / "progress.json").write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return prog


VOTE_RE = re.compile(r"```json\n(.*?)\n```", re.S)


def collect_votes(space: Space, repo: str) -> None:
    """[vote] issue 的正文是网页生成的 JSON：{"votes": {poem_id: 1|0|-1}}。每人每首以最后一次为准。"""
    path = space.data_dir / "votes.json"
    votes: dict[str, dict[str, int]] = json.loads(path.read_text("utf-8")) if path.exists() else {}
    for issue in sorted(G.list_issues(repo, "[vote]", state="open"), key=lambda i: i["number"]):
        user = issue["author"]["login"]
        body = G.gh("issue", "view", "-R", repo, str(issue["number"]), "--json", "body", "--jq", ".body")
        m = VOTE_RE.search(body)
        try:
            data = json.loads(m.group(1))["votes"] if m else {}
        except (json.JSONDecodeError, KeyError):
            data = {}
        for pid, v in data.items():
            if v in (-1, 0, 1):
                votes.setdefault(pid, {})[user] = v
        G.gh("issue", "close", "-R", repo, str(issue["number"]), "--comment", f"已记录 {len(data)} 票，谢谢！")
    path.write_text(json.dumps(votes, ensure_ascii=False, indent=1) + "\n", "utf-8")
