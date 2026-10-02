"""端到端：用 ngram 判别器跑真实分片 → 本地汇总校验 → 篡改结果应被拒。"""
import copy

import pytest

from shiyun.aggregate import verify
from shiyun.judges import NgramJudge
from shiyun.search import DATA, Space
from shiyun.worker import run_shard


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    if not (DATA / "shards.tsv").exists():
        pytest.skip("尚未生成分片表")
    sp = Space()
    r = run_shard(sp, 0, NgramJudge(sp), state_dir=tmp_path_factory.mktemp("state"))
    return sp, r


def test_result_verifies(result):
    sp, r = result
    assert r["judged"] > 0 and r["candidates"]
    assert verify(sp, r) is None


@pytest.mark.parametrize("attack", ["fake_candidate", "ledger", "count"])
def test_tampering_rejected(result, attack):
    sp, r = result
    bad = copy.deepcopy(r)
    if attack == "fake_candidate":
        bad["candidates"][0][0] = "床前明月光"
    elif attack == "ledger":
        bad["ledger"]["pruned"]["lm"][4] += 1
    else:
        bad["judged"] -= 1
    assert verify(sp, bad) is not None
