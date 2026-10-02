"""用 Pull Request 做分布式任务协调（参与者只需 gh CLI 登录；本地工作区不会被改动）。

一个分片 = 一个 PR，PR 的状态就是分片的状态：
  认领  在自己的 fork 上建分支，提交 claims/<空间>/shard-XXXXXX.json，
        向上游开草稿 PR，标题 “[claim] <空间> shard XXXXXX”。同一分片有多个认领时，PR 编号最小者胜。
  提交  在同一分支上删掉认领文件、加入 data/results/<空间>/shard-XXXXXX.json.gz，然后把 PR 标为就绪。
        PR 最终只改动这一个结果文件。
  合并  汇总器（GitHub Actions，只读取 PR 里的数据、不执行 PR 的代码）复算校验后 squash 合并。
  过期  草稿 PR 超过 claim_ttl_days 天未就绪，汇总器关闭它，分片重新开放。
所有文件操作都走 GitHub API，不碰参与者本地的 git 工作区。
"""
from __future__ import annotations

import base64
import json
import random
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

CLAIM_RE = re.compile(r"^\[claim\] (\w+) shard (\d+)$")


def gh(*args: str, input: str | None = None, check: bool = True) -> str:
    r = subprocess.run(["gh", *args], input=input, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} 失败：{r.stderr.strip()}")
    return r.stdout


def gh_json(*args: str, body: dict | None = None):
    out = gh(*args, *(["--input", "-"] if body is not None else []),
             input=json.dumps(body) if body is not None else None)
    return json.loads(out) if out.strip() else None


def repo_slug(manifest: dict) -> str:
    if manifest.get("github_repo"):
        return manifest["github_repo"]
    return json.loads(gh("repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]


def me() -> str:
    return gh("api", "user", "--jq", ".login").strip()


def claim_title(meter: str, shard: int) -> str:
    return f"[claim] {meter} shard {shard:06d}"


def claim_file(meter: str, shard: int) -> str:
    return f"claims/{meter}/shard-{shard:06d}.json"


def result_file(meter: str, shard: int) -> str:
    return f"data/results/{meter}/shard-{shard:06d}.json.gz"


# ---------------- 状态 ----------------
def list_claims(repo: str) -> list[dict]:
    out = gh("pr", "list", "-R", repo, "--state", "all", "--limit", "100000",
             "--search", '"[claim]" in:title',
             "--json", "number,title,state,isDraft,author,createdAt,headRefName,labels")
    return [p for p in json.loads(out) if CLAIM_RE.match(p["title"])]


def _occupies(pr: dict, ttl_days: int) -> bool:
    """这个 PR 是否占着分片：已合并，或仍开着且（就绪 / 草稿未过期）。"""
    if pr["state"] == "MERGED":
        return True
    if pr["state"] != "OPEN":
        return False
    if not pr["isDraft"]:
        return True
    created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - created < timedelta(days=ttl_days)


def shard_status(repo: str, meter: str, n_shards: int, ttl_days: int) -> dict[int, str]:
    """每个分片：free / running（草稿）/ review（待合并）/ done（已合并）。"""
    status = {k: "free" for k in range(n_shards)}
    for pr in sorted(list_claims(repo), key=lambda p: p["number"]):
        m = CLAIM_RE.match(pr["title"])
        k = int(m.group(2))
        if m.group(1) != meter or k not in status or status[k] != "free" or not _occupies(pr, ttl_days):
            continue
        status[k] = {"MERGED": "done"}.get(pr["state"], "running" if pr["isDraft"] else "review")
    return status


# ---------------- 参与者：认领与提交 ----------------
class Contributor:
    def __init__(self, repo: str):
        self.repo = repo
        self.login = me()
        name = repo.split("/")[1]
        if repo.split("/")[0] == self.login:
            self.fork = repo  # 维护者在自己仓库上直接开分支
        else:
            gh("repo", "fork", repo, "--clone=false", "--default-branch-only", check=False)
            self.fork = f"{self.login}/{name}"
            for _ in range(10):  # fork 是异步创建的
                if subprocess.run(["gh", "api", f"repos/{self.fork}"], capture_output=True).returncode == 0:
                    break
                time.sleep(3)

    def _put(self, branch: str, path: str, content: bytes, message: str) -> None:
        gh_json("api", "-X", "PUT", f"repos/{self.fork}/contents/{path}",
                body={"message": message, "branch": branch, "content": base64.b64encode(content).decode()})

    def _delete(self, branch: str, path: str, message: str) -> None:
        sha = gh_json("api", f"repos/{self.fork}/contents/{path}?ref={branch}")["sha"]
        gh_json("api", "-X", "DELETE", f"repos/{self.fork}/contents/{path}",
                body={"message": message, "branch": branch, "sha": sha})

    def claim(self, meter: str, n_shards: int, ttl_days: int, judge_id: str, retries: int = 5) -> tuple[int, int, str]:
        """认领一个空闲分片，返回 (shard, PR 编号, 分支名)。"""
        for _ in range(retries):
            status = shard_status(self.repo, meter, n_shards, ttl_days)
            free = [k for k, s in status.items() if s == "free"]
            if not free:
                raise RuntimeError(f"{meter} 空间的分片都已认领或完成")
            shard = random.choice(free[:32])  # 在最靠前的空闲分片里随机挑，减少撞车
            branch = f"shard/{meter}-{shard:06d}-{int(time.time())}"
            base = gh("api", f"repos/{self.repo}/git/ref/heads/main", "--jq", ".object.sha").strip()
            gh_json("api", "-X", "POST", f"repos/{self.fork}/git/refs", body={"ref": f"refs/heads/{branch}", "sha": base})
            info = {"meter": meter, "shard": shard, "user": self.login, "judge": judge_id,
                    "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._put(branch, claim_file(meter, shard), (json.dumps(info, ensure_ascii=False, indent=1) + "\n").encode(),
                      f"认领 {meter} 分片 {shard}")
            head = branch if self.fork == self.repo else f"{self.login}:{branch}"
            url = gh("pr", "create", "-R", self.repo, "--draft", "--base", "main", "--head", head,
                     "--title", claim_title(meter, shard),
                     "--body", f"诗云第一阶段分片认领。\n\n- 空间：{meter}\n- 分片：{shard}\n- 判别器：`{judge_id}`\n").strip()
            number = int(url.rsplit("/", 1)[-1])
            rivals = [p for p in list_claims(self.repo) if p["title"] == claim_title(meter, shard) and _occupies(p, ttl_days)]
            winner = min(rivals, key=lambda p: p["number"], default=None)
            if winner is None or winner["number"] == number:
                return shard, number, branch
            gh("pr", "close", "-R", self.repo, str(number), "--comment", f"与 #{winner['number']} 撞车，让出。")
        raise RuntimeError("连续撞车，稍后再试")

    def submit(self, meter: str, shard: int, number: int, branch: str, blob: bytes, summary: str) -> None:
        self._put(branch, result_file(meter, shard), blob, f"{meter} 分片 {shard} 结果")
        self._delete(branch, claim_file(meter, shard), "移除认领文件")
        gh("pr", "comment", "-R", self.repo, str(number), "--body", summary)
        gh("pr", "ready", "-R", self.repo, str(number))


# ---------------- 汇总器 ----------------
def ready_claims(repo: str) -> list[dict]:
    return [p for p in list_claims(repo) if p["state"] == "OPEN" and not p["isDraft"]]


def stale_claims(repo: str, ttl_days: int) -> list[dict]:
    return [p for p in list_claims(repo) if p["state"] == "OPEN" and p["isDraft"] and not _occupies(p, ttl_days)]


def pr_files(repo: str, number: int) -> tuple[list[str], dict]:
    d = json.loads(gh("pr", "view", "-R", repo, str(number),
                      "--json", "files,headRefOid,headRepository,headRepositoryOwner,author"))
    return [f["path"] for f in d["files"]], d


def fetch_file(repo_full: str, path: str, ref: str) -> bytes:
    r = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo_full}/contents/{path}?ref={ref}"],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"读取 {path} 失败：{r.stderr.decode().strip()}")
    return r.stdout


def merge(repo: str, number: int, comment: str) -> None:
    gh("pr", "comment", "-R", repo, str(number), "--body", comment)
    gh("pr", "merge", "-R", repo, str(number), "--squash", "--delete-branch")


def close(repo: str, number: int, comment: str, label: str | None = None) -> None:
    if label:
        gh("pr", "edit", "-R", repo, str(number), "--add-label", label, check=False)
    gh("pr", "close", "-R", repo, str(number), "--comment", comment)


# ---------------- 投票（评审团） ----------------
def list_vote_issues(repo: str) -> list[dict]:
    out = gh("issue", "list", "-R", repo, "--state", "open", "--limit", "1000",
             "--search", '"[vote]" in:title', "--json", "number,title,author,body")
    return [i for i in json.loads(out) if i["title"].startswith("[vote]")]


def close_issue(repo: str, number: int, comment: str) -> None:
    gh("issue", "close", "-R", repo, str(number), "--comment", comment)
