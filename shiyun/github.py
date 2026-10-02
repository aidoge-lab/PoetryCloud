"""用 GitHub Issues 做分布式任务协调（只需要 gh CLI 登录，无需 fork/PR/写权限）。

认领：开一个标题为 “[claim] shard 000123” 的 issue。同一分片若有多个认领，issue 编号最小者胜。
提交：在自己的认领 issue 下评论结果（<!-- shiyun-result --> + JSON），然后关闭 issue。
过期：认领后 claim_ttl_days 天仍未关闭，视为放弃，分片重新开放。
汇总器（GitHub Actions）校验后给 issue 打 merged / rejected 标签。
投票：网页生成 “[vote] ...” issue，汇总器解析。
"""
from __future__ import annotations

import json
import random
import re
import subprocess
from datetime import datetime, timedelta, timezone

CLAIM_RE = re.compile(r"^\[claim\] shard (\d+)$")
RESULT_MARK = "<!-- shiyun-result"
COMMENT_LIMIT = 60000


def gh(*args: str, input: str | None = None) -> str:
    r = subprocess.run(["gh", *args], input=input, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} 失败：{r.stderr.strip()}")
    return r.stdout


def repo_slug(manifest: dict) -> str:
    if manifest.get("github_repo"):
        return manifest["github_repo"]
    return json.loads(gh("repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]


def me() -> str:
    return gh("api", "user", "--jq", ".login").strip()


def list_issues(repo: str, prefix: str, state: str = "all") -> list[dict]:
    out = gh("issue", "list", "-R", repo, "--state", state, "--limit", "100000",
             "--search", f'"{prefix}" in:title',
             "--json", "number,title,state,author,createdAt,labels")
    return [i for i in json.loads(out) if i["title"].startswith(prefix)]


def claims_by_shard(repo: str) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for i in list_issues(repo, "[claim]"):
        m = CLAIM_RE.match(i["title"])
        if m:
            out.setdefault(int(m.group(1)), []).append(i)
    for v in out.values():
        v.sort(key=lambda i: i["number"])
    return out


def _labels(issue: dict) -> set[str]:
    return {lb["name"] for lb in issue.get("labels", [])}


def _alive(issue: dict, ttl_days: int) -> bool:
    """这个认领是否仍占着分片：已完成（关闭且未被拒）或未过期的进行中。"""
    if "rejected" in _labels(issue):
        return False
    if issue["state"] == "CLOSED":
        return "duplicate" not in _labels(issue)
    created = datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - created < timedelta(days=ttl_days)


def shard_status(repo: str, n_shards: int, ttl_days: int) -> dict[int, str]:
    """每个分片的状态：free / running / done。"""
    status = {k: "free" for k in range(n_shards)}
    for k, issues in claims_by_shard(repo).items():
        for i in issues:
            if _alive(i, ttl_days):
                status[k] = "done" if i["state"] == "CLOSED" else "running"
                break
    return status


def claim(repo: str, n_shards: int, ttl_days: int, judge_id: str, retries: int = 5) -> tuple[int, int]:
    """认领一个空闲分片，返回 (shard, issue_number)。"""
    for _ in range(retries):
        status = shard_status(repo, n_shards, ttl_days)
        free = [k for k, s in status.items() if s == "free"]
        if not free:
            raise RuntimeError("所有分片都已认领或完成 🎉")
        shard = random.choice(free[:32])  # 在最靠前的空闲分片里随机挑，减少撞车
        body = f"诗云第一阶段分片认领。\n\n- shard: {shard}\n- judge: `{judge_id}`\n"
        url = gh("issue", "create", "-R", repo, "--title", f"[claim] shard {shard:06d}", "--body", body).strip()
        number = int(url.rsplit("/", 1)[-1])
        winner = next((i for i in claims_by_shard(repo).get(shard, []) if _alive(i, ttl_days)), None)
        if winner is None or winner["number"] == number:
            return shard, number
        gh("issue", "close", "-R", repo, str(number), "--comment", f"与 #{winner['number']} 撞车，让出。")
    raise RuntimeError("连续撞车，稍后再试")


def submit(repo: str, number: int, result: dict) -> None:
    """把结果作为评论贴到认领 issue 下（过长则拆成多条），然后关闭。"""
    cands = result["candidates"]
    head = {k: v for k, v in result.items() if k != "candidates"}
    parts, cur = [], []
    budget = COMMENT_LIMIT - len(json.dumps(head, ensure_ascii=False)) - 500
    size = 0
    for c in cands:
        s = len(json.dumps(c, ensure_ascii=False)) + 1
        if cur and size + s > budget:
            parts.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += s
    parts.append(cur)
    for k, chunk in enumerate(parts):
        payload = dict(head, candidates=chunk, part=k, parts=len(parts))
        text = (f"{RESULT_MARK} v{result['v']} part {k + 1}/{len(parts)} -->\n"
                f"判了 {result['judged']:,} 句，提交 {len(chunk)} 个候选。\n\n"
                f"```json\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n```\n")
        gh("issue", "comment", "-R", repo, str(number), "--body-file", "-", input=text)
    gh("issue", "close", "-R", repo, str(number))


def read_results(repo: str, number: int) -> tuple[str, list[dict]]:
    """读取某认领 issue 的作者与其贴出的结果分片。"""
    d = json.loads(gh("issue", "view", "-R", repo, str(number), "--json", "author,comments"))
    author = d["author"]["login"]
    parts = []
    for c in d["comments"]:
        if c["author"]["login"] != author or RESULT_MARK not in c["body"]:
            continue
        m = re.search(r"```json\n(.*?)\n```", c["body"], re.S)
        if m:
            parts.append(json.loads(m.group(1)))
    return author, parts


def label(repo: str, number: int, name: str, comment: str | None = None) -> None:
    gh("issue", "edit", "-R", repo, str(number), "--add-label", name)
    if comment:
        gh("issue", "comment", "-R", repo, str(number), "--body", comment)
