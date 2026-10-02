"""微调 laya 判别器：教它分辨“像唐诗的句子”与“不像的”（决策 D1-A、D2-A）。

数据（全部从仓库现有数据生成）：
  正样本  唐诗五言原句                                           目标 P(好)=0.95
  负样本  ① 同一句打乱字序                                       目标 0.05
          ② 原句随机替换一两个字                                 目标 0.05
          ③ 搜索树上的随机叶子（非原句；其中可能真有好句，所以软一些） 目标 0.15
  评测集  按哈希留出约 1/16 的原句（is_heldout），训练绝不使用；tools/eval_judge.py 只在留出集上评测。

训练用 laya 官方的单机脚本（RLCD + 温度定标），按固定 commit 下载到 .cache，支持 --device mps。
默认只训练最上面 6 层编码器和判别头（约 4500 万参数）：词嵌入占全部 3.2 亿参数的 61%，
全量微调要 5GB 以上的权重、梯度和优化器状态，8GB 内存的 Mac 会一直换页。
state 与问题的格式直接取自 shiyun.judges，保证训练与推理的输入完全一致。

  python tools/finetune_judge.py prepare -n 20000
  python tools/finetune_judge.py train --epochs 2            # 输出 models/laya-shiyun
  python tools/eval_judge.py --model models/laya-shiyun
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shiyun.judges import _noul_question, _state  # noqa: E402
from shiyun.search import DATA, FULL_MASK, Ledger, Space  # noqa: E402

LAYA_COMMIT = "4aa6761be8173de4ce6d92c31b3e40b6eaf59a7c"
SCRIPT_URL = f"https://raw.githubusercontent.com/NandhaKishorM/laya/{LAYA_COMMIT}/research/scripts/finetune_single_device.py"
CACHE = ROOT / ".cache" / "finetune"
TARGET = {"tang": 0.95, "shuffled": 0.05, "substituted": 0.05, "leaf": 0.15}


def is_heldout(text: str) -> bool:
    """约 1/16 的句子留作评测，训练与评测按同一规则划分。"""
    return hashlib.sha1(text.encode()).hexdigest()[0] == "0"


def known_lines() -> list[str]:
    with gzip.open(DATA / "known_lines.txt.gz", "rt", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def shuffled(text: str, rng: random.Random) -> str:
    cs = list(text)
    for _ in range(10):
        rng.shuffle(cs)
        if "".join(cs) != text:
            break
    return "".join(cs)


def substituted(text: str, space: Space, rng: random.Random) -> str:
    cs = list(text)
    for i in rng.sample(range(5), rng.choice((1, 2))):
        cs[i] = space.chars[rng.randrange(space.n)]
    return "".join(cs)


def random_leaves(space: Space, n: int, rng: random.Random, known: set[str], heldout: bool) -> list[str]:
    """随机下降抽样：从根开始每层在合格的孩子里随机挑一个字，走满 5 层得到一句叶子。

    每句只展开 5 个结点，比枚举整棵子树快几个数量级；分布不是严格均匀，作负样本足够。
    """
    out, scratch = [], Ledger(space.n)
    while len(out) < n:
        path, mask, score = (), FULL_MASK, 0.0
        for d in range(5):
            kids = space._expand(d, path, mask, score, scratch)
            if not kids:
                break
            c, mask, score = rng.choice(kids)
            path += (c,)
        else:
            text = "".join(space.chars[i] for i in path)
            if text not in known and is_heldout(text) == heldout:
                out.append(text)
    return out


def row(text: str, p: float) -> dict:
    return {"state": {"body": _state(text, "line")},
            "questions": {"good": _noul_question("line")},
            "gold": {"good": {"probabilities": {"false": round(1 - p, 4), "true": p}}},
            "meta": {"text": text}}


def prepare(n: int, seed: int) -> Path:
    rng = random.Random(seed)
    known = known_lines()
    known_set = set(known)
    train_pos = [t for t in known if not is_heldout(t)]
    pos = rng.sample(train_pos, n)
    rows = [row(t, TARGET["tang"]) for t in pos]
    third = n // 3
    rows += [row(shuffled(t, rng), TARGET["shuffled"]) for t in rng.sample(pos, third)]
    space = Space("psy")
    subs = []
    for t in rng.sample(pos, third * 2):
        s = substituted(t, space, rng)
        if s not in known_set:
            subs.append(s)
    rows += [row(s, TARGET["substituted"]) for s in subs[:third]]
    leaves = random_leaves(space, (n - 2 * third) // 2, rng, known_set, heldout=False)
    leaves += random_leaves(Space("xin"), n - 2 * third - len(leaves), rng, known_set, heldout=False)
    rows += [row(t, TARGET["leaf"]) for t in leaves]
    rng.shuffle(rows)
    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / "train.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"写入 {out}：{len(rows)} 条（正 {n}，负 {len(rows) - n}）")
    return out


PATCHES = [
    # 1. 部分微调：冻结词嵌入与下层编码器，只训练最上面 SHIYUN_TOP_LAYERS 层与判别头（8GB 内存也能跑）
    ("    model.to(device)\n    model.train()\n",
     "    _top = int(os.environ.get('SHIYUN_TOP_LAYERS', '0'))\n"
     "    if _top:\n"
     "        _n = 1 + max(int(k.split('.')[2]) for k, _ in model.named_parameters() if k.startswith('encoder.layers.'))\n"
     "        _keep = tuple(f'encoder.layers.{i}.' for i in range(_n - _top, _n)) + ('encoder.final_norm',)\n"
     "        for _k, _p in model.named_parameters():\n"
     "            if _k.startswith('encoder.') and not _k.startswith(_keep):\n"
     "                _p.requires_grad_(False)\n"
     "        print('Trainable params: %.1fM' % (sum(p.numel() for p in model.parameters() if p.requires_grad) / 1e6), flush=True)\n"
     "    model.to(device)\n    model.train()\n"),
    # 2. 优化器只收可训练参数
    ('enc_params = [p for n, p in model.named_parameters() if "encoder." in n]',
     'enc_params = [p for n, p in model.named_parameters() if "encoder." in n and p.requires_grad]'),
    ('head_params = [p for n, p in model.named_parameters() if "encoder." not in n]',
     'head_params = [p for n, p in model.named_parameters() if "encoder." not in n and p.requires_grad]'),
    # 3. 每 50 步打印一次进度（原脚本只在每个 epoch 末尾打印）
    ("            epoch_loss += loss.item()\n            n_batches += 1\n",
     "            epoch_loss += loss.item()\n            n_batches += 1\n"
     "            if n_batches % 50 == 0:\n"
     "                print('  step {}/{} | loss {:.4f}'.format(n_batches, steps_per_epoch, epoch_loss / n_batches), flush=True)\n"),
]


def laya_script() -> Path:
    """下载固定 commit 的 laya 训练脚本并打上补丁（每个补丁都必须命中，否则报错）。"""
    raw = CACHE / f"finetune_single_device-{LAYA_COMMIT[:8]}.py"
    if not raw.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(SCRIPT_URL, timeout=120) as r:
            raw.write_bytes(r.read())
    src = raw.read_text("utf-8")
    for old, new in PATCHES:
        if src.count(old) != 1:
            raise SystemExit(f"laya 训练脚本已变化，补丁未命中：{old[:60]!r}")
        src = src.replace(old, new)
    patched = CACHE / f"finetune_shiyun-{LAYA_COMMIT[:8]}.py"
    patched.write_text(src, "utf-8")
    return patched


def train(data: Path, epochs: int, device: str, out_dir: Path, top_layers: int) -> None:
    if not device:
        from shiyun.judges import _default_device

        device = _default_device()
    cmd = [sys.executable, str(laya_script()), "--data", str(data),
           "--output-dir", str(out_dir), "--device", device, "--epochs", str(epochs)]
    env = dict(os.environ, SHIYUN_TOP_LAYERS=str(top_layers), PYTORCH_ENABLE_MPS_FALLBACK="1")
    print(" ".join(cmd), f"(SHIYUN_TOP_LAYERS={top_layers})", flush=True)
    subprocess.run(cmd, check=True, env=env)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("-n", type=int, default=20000, help="正样本数（负样本同数量）")
    p.add_argument("--seed", type=int, default=0)
    p = sub.add_parser("train")
    p.add_argument("--data", default=str(CACHE / "train.jsonl"))
    p.add_argument("--epochs", type=int, default=2)
    p.add_argument("--top-layers", type=int, default=6, help="只训练最上面几层编码器（0 = 全量微调，需要大内存）")
    p.add_argument("--device", default="", help="mps / cuda / cpu，默认自动")
    p.add_argument("--out", default=str(ROOT / "models" / "laya-shiyun"))
    a = ap.parse_args()
    if a.cmd == "prepare":
        prepare(a.n, a.seed)
    else:
        train(Path(a.data), a.epochs, a.device, Path(a.out), a.top_layers)


if __name__ == "__main__":
    main()
