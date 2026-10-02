"""端到端：用 ngram 判别器跑真实分片 → 本地汇总校验 → 篡改结果应被拒。"""
import copy

import pytest

from shiyun.aggregate import verify
from shiyun.judges import NgramJudge
from shiyun.search import DATA, Space, shards_path
from shiyun.worker import dump_result, load_result, run_shard


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    path = shards_path(DATA, "psy")
    if not path.exists():
        pytest.skip("尚未生成分片表")
    rows = [r.split("\t") for r in path.read_text("utf-8").splitlines()]
    smallest = min(range(len(rows)), key=lambda k: int(rows[k][3]))  # 挑最小的分片，测试快一些
    sp = Space("psy")
    r = run_shard(sp, smallest, NgramJudge(sp), state_dir=tmp_path_factory.mktemp("state"))
    return sp, load_result(dump_result(r))


def test_result_verifies(result):
    sp, r = result
    assert r["judged"] > 0 and r["candidates"]
    assert verify(sp, r) is None


@pytest.mark.parametrize("attack", ["fake_candidate", "ledger", "count", "types", "meter"])
def test_tampering_rejected(result, attack):
    sp, r = result
    bad = copy.deepcopy(r)
    if attack == "fake_candidate":
        bad["candidates"][0][0] = "床前明月光"
    elif attack == "ledger":
        bad["ledger"]["pruned"]["lm"][4] += 1
    elif attack == "count":
        bad["judged"] -= 1
    elif attack == "types":
        bad["candidates"][0][3] = "ABCD"
    else:
        bad["meter"] = "xin"
    assert verify(sp, bad) is not None
