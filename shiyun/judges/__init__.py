"""判别器：只输出“是好诗的概率” ∈ [0, 1]，不生成文本。

  laya    开源 Jev 兼容的非自回归判别模型 https://github.com/NandhaKishorM/laya —— 默认
          未给 --base-url 时进程内加载（pip install laya），给了则调用 laya-serve 的批量接口
  jev     其他 Jev 兼容引擎（jev-rs / open-cricket）的 /v1/systemone 接口
  openai  任意 OpenAI 兼容服务（llama.cpp / vLLM / Ollama），取首 token 中“是/否”的概率
  ngram   唐诗字级 n-gram 打分，无需模型，用于离线测试和冒烟
"""
from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

PROMPT_VERSION = 1
LINE_QUESTION = (
    "这是一句五言诗。它是否语意通顺、意象鲜明、有诗味，"
    "放进一首唐人五言绝句里也不显得突兀？"
)
COUPLET_QUESTION = "这是五言诗中相邻的两句。上下句是否衔接自然、意脉连贯，像出自同一首好诗？"
POEM_QUESTION = (
    "这是一首五言绝句。它是否意境完整、前后连贯、语言凝练，"
    "称得上一首好诗？"
)


class Judge(Protocol):
    id: str

    def score(self, texts: list[str], kind: str = "line") -> list[float]: ...


def _question(kind: str) -> str:
    return {"line": LINE_QUESTION, "couplet": COUPLET_QUESTION, "poem": POEM_QUESTION}[kind]


def _state(text: str, kind: str) -> str:
    """kind: line（单句）/ couplet（两句，用 / 分隔）/ poem（四句，用 / 分隔）。"""
    if kind == "line":
        return f"五言诗句：{text}"
    title = "五言对句" if kind == "couplet" else "五言绝句"
    return f"{title}：\n" + "\n".join(text.split("/"))


def _post(url: str, payload: dict, api_key: str | None, timeout: float = 120) -> dict:
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _noul_question(kind: str) -> dict:
    return {"type": "noul", "instructions": _question(kind),
            "criteria": {"false": "平庸、不通或没有诗味", "true": "通顺而有诗意的好诗"}}


def _noul(ans: dict) -> float:
    ans = ans.get("answers") or ans.get("results") or ans
    v = ans["good"]
    return float(v["noul"] if isinstance(v, dict) else v)


class LayaJudge:
    """laya：一次前向得到 noul（是好诗的概率）。中文自动路由到 laya-multilingual。"""

    def __init__(self, base_url: str = "", model: str = "", api_key: str | None = None, concurrency: int = 4):
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.id = f"laya:{model or 'auto'}:p{PROMPT_VERSION}"
        self._pool = ThreadPoolExecutor(concurrency)
        self._router = None
        if not base_url:
            try:
                from laya import Router
            except ImportError as e:
                raise SystemExit("未安装 laya：pip install laya，或用 --base-url 指向 laya-serve") from e
            self._router = Router()

    def _batch(self, texts: list[str], kind: str) -> list[float]:
        states = [{"body": _state(t, kind)} for t in texts]
        questions = {"good": _noul_question(kind)}
        if self._router is not None:
            # Router.predict_batch(requests)：每个 request 自带 state 与 questions，同 schema 的会共享前向
            reqs = [{"state": st, "questions": questions} for st in states]
            if self.model:
                for r in reqs:
                    r["model"] = self.model
            return [_noul(r) for r in self._router.predict_batch(reqs, batch_size=64, sort_by_length=True)]
        payload = {"states": states, "questions": questions}
        if self.model:
            payload["model"] = self.model
        r = _post(self.base_url + "/v1/systemone/batch", payload, self.api_key)
        return [_noul(x) for x in r["results"]]

    def score(self, texts: list[str], kind: str = "line") -> list[float]:
        if self._router is not None:  # 进程内：交给 laya 自己分批，不多线程调同一个模型
            return self._batch(texts, kind)
        chunks = [texts[k : k + 64] for k in range(0, len(texts), 64)]  # laya-serve 单批上限 64
        return [s for part in self._pool.map(lambda c: self._batch(c, kind), chunks) for s in part]


class JevJudge:
    """Jev 兼容接口：POST {base}/v1/systemone  {state, questions:{good:{type:noul,...}}} -> {good:{noul}}"""

    def __init__(self, base_url: str, model: str = "", api_key: str | None = None,
                 path: str = "/v1/systemone", concurrency: int = 8):
        self.url = base_url.rstrip("/") + path
        self.model, self.api_key, self.concurrency = model, api_key, concurrency
        self.id = f"jev:{model or base_url}:p{PROMPT_VERSION}"
        self._pool = ThreadPoolExecutor(concurrency)

    def _one(self, text: str, kind: str) -> float:
        q = _question(kind)
        payload = {"state": _state(text, kind), "questions": {"good": dict(_noul_question(kind), question=q)}}
        if self.model:
            payload["model"] = self.model
        return _noul(_post(self.url, payload, self.api_key))

    def score(self, texts: list[str], kind: str = "line") -> list[float]:
        return list(self._pool.map(lambda t: self._one(t, kind), texts))


class OpenAIJudge:
    """OpenAI 兼容 chat/completions + logprobs，P(是) / (P(是)+P(否))。"""

    YES, NO = ("是", "yes", "Yes"), ("否", "no", "No")

    def __init__(self, base_url: str, model: str, api_key: str | None = None, concurrency: int = 8):
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.model, self.api_key = model, api_key
        self.id = f"openai:{model}:p{PROMPT_VERSION}"
        self._pool = ThreadPoolExecutor(concurrency)

    def _one(self, text: str, kind: str) -> float:
        msg = f"{_state(text, kind)}\n\n{_question(kind)}\n只回答一个字：是 或 否。"
        r = _post(self.url, {"model": self.model, "messages": [{"role": "user", "content": msg}],
                             "max_tokens": 1, "temperature": 0, "logprobs": True, "top_logprobs": 20},
                  self.api_key)
        top = r["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        py = sum(math.exp(t["logprob"]) for t in top if t["token"].strip() in self.YES)
        pn = sum(math.exp(t["logprob"]) for t in top if t["token"].strip() in self.NO)
        return py / (py + pn) if py + pn > 0 else 0.0

    def score(self, texts: list[str], kind: str = "line") -> list[float]:
        return list(self._pool.map(lambda t: self._one(t, kind), texts))


class NgramJudge:
    """平均每字对数概率映射到 (0,1)。只看字面流畅度，不懂诗意——仅供测试。"""

    id = f"ngram:p{PROMPT_VERSION}"

    def __init__(self, space):
        self.space = space

    def _line(self, text: str) -> float:
        lm = self.space.lm_score(text)
        return 0.0 if lm is None else 1 / (1 + math.exp(-(lm + 4.2) * 3))

    def score(self, texts: list[str], kind: str = "line") -> list[float]:
        if kind == "line":
            return [self._line(t) for t in texts]
        return [sum(self._line(x) for x in t.split("/")) / len(t.split("/")) for t in texts]


def make_judge(name: str, space=None, base_url: str = "", model: str = "",
               api_key: str | None = None, concurrency: int = 8) -> Judge:
    if name == "laya":
        return LayaJudge(base_url, model, api_key, concurrency)
    if name == "jev":
        return JevJudge(base_url or "http://127.0.0.1:8090", model, api_key, concurrency=concurrency)
    if name == "openai":
        return OpenAIJudge(base_url or "http://127.0.0.1:8080", model, api_key, concurrency)
    if name == "ngram":
        return NgramJudge(space)
    raise ValueError(f"未知判别器 {name}")


def prompt_hash() -> str:
    return hashlib.sha256((LINE_QUESTION + COUPLET_QUESTION + POEM_QUESTION).encode()).hexdigest()[:12]
