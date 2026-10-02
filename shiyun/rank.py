"""人工校准：用投票把判别器分数定标（Platt scaling），并给出综合排名。

投票：只计评审团（data/reviewers.json）成员的票。好=1，一般=0.5，不行=0（软标签）。
定标：P(人觉得好 | 判别分 s) = σ(a·s + b)，用全部有票的诗拟合 a、b。
排名：把定标后的概率当作先验（权重 PRIOR 票），与真实投票做贝叶斯平均。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

PRIOR = 3.0
VOTE_VALUE = {1: 1.0, 0: 0.5, -1: 0.0}


def fit_platt(pairs: list[tuple[float, float]], steps: int = 3000, lr: float = 0.5) -> tuple[float, float]:
    a, b = 1.0, 0.0
    if len(pairs) < 5:
        return a, b
    for _ in range(steps):
        ga = gb = 0.0
        for s, y in pairs:
            p = 1 / (1 + math.exp(-(a * s + b)))
            ga += (p - y) * s
            gb += p - y
        a -= lr * ga / len(pairs)
        b -= lr * gb / len(pairs)
    return a, b


def calibrate(poems: list[dict], votes: dict[str, dict[str, int]]) -> dict:
    pairs = []
    for p in poems:
        v = votes.get(p["id"])
        if v:
            pairs.append((p["score"], sum(VOTE_VALUE[x] for x in v.values()) / len(v)))
    a, b = fit_platt(pairs)
    return {"a": a, "b": b, "n_poems_voted": len(pairs),
            "n_votes": sum(len(votes.get(p["id"], {})) for p in poems)}


def rank(poems: list[dict], votes: dict[str, dict[str, int]], cal: dict) -> list[dict]:
    out = []
    for p in poems:
        prior = 1 / (1 + math.exp(-(cal["a"] * p["score"] + cal["b"])))
        v = votes.get(p["id"], {})
        good = sum(VOTE_VALUE[x] for x in v.values())
        tally = {"好": sum(x == 1 for x in v.values()), "一般": sum(x == 0 for x in v.values()),
                 "不行": sum(x == -1 for x in v.values())}
        out.append(dict(p, calibrated=round(prior, 4), votes=tally,
                        final=round((good + PRIOR * prior) / (len(v) + PRIOR), 4)))
    return sorted(out, key=lambda p: -p["final"])


def build_site(data_dir: Path, site_dir: Path, repo: str, by_meter: dict[str, dict]) -> None:
    """by_meter: {空间: {"name", "poems", "lines", "rediscovered"}}。投票只含评审团的票。"""
    votes_path = data_dir / "votes.json"
    votes = json.loads(votes_path.read_text("utf-8")) if votes_path.exists() else {}
    reviewers_path = data_dir / "reviewers.json"
    panel = json.loads(reviewers_path.read_text("utf-8"))["reviewers"] if reviewers_path.exists() else []
    progress_path = data_dir / "progress.json"
    spaces, cals = {}, {}
    for meter, d in by_meter.items():
        cal = calibrate(d["poems"], votes)
        cals[meter] = cal
        spaces[meter] = {"name": d["name"], "calibration": cal, "poems": rank(d["poems"], votes, cal)[:500],
                         "lines": d["lines"][:300], "rediscovered": d["rediscovered"][:300]}
    (data_dir / "calibration.json").write_text(json.dumps(cals, indent=2) + "\n", "utf-8")
    data = {
        "repo": repo,
        "reviewers": panel,
        "progress": json.loads(progress_path.read_text("utf-8")) if progress_path.exists() else None,
        "spaces": spaces,
    }
    site_dir.mkdir(exist_ok=True)
    (site_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")
