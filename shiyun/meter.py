"""五言绝句格律（新韵）。

四种基本句式（“一三五不论”，只严格检查第 2、4、5 字，另禁孤平、三平尾）：
  A 仄仄平平仄   B 平平仄仄平   C 平平平仄仄   D 仄仄仄平平
四种绝句格式（粘对规则展开后）：
  仄起不入韵 A B C D   仄起入韵 D B C D
  平起不入韵 C D A B   平起入韵 B D A B
"""
from __future__ import annotations

FORMS = {
    "仄起不入韵": "ABCD",
    "仄起入韵": "DBCD",
    "平起不入韵": "CDAB",
    "平起入韵": "BDAB",
}
_TYPE = {("Z", "Z"): "A", ("P", "P"): "B", ("P", "Z"): "C", ("Z", "P"): "D"}


def line_type(tones: str) -> str | None:
    """tones 为 5 个 P/Z。不合律返回 None。"""
    if tones[1] == tones[3]:  # 二四异
        return None
    if tones[2:] == "PPP":  # 三平尾
        return None
    t = _TYPE[(tones[1], tones[4])]
    if t == "B" and tones[0] == "Z" and tones[2] == "Z":  # 孤平：仄平仄仄平
        return None
    return t


def plain_ok(text: str) -> bool:
    """非叠字的重复字（如“白日白山尽”）直接淘汰。"""
    for k, c in enumerate(text):
        if c in text[k + 1 :] and not (k + 1 < len(text) and text[k + 1] == c):
            return False
    return True


def rhymes(form: str) -> list[int]:
    """该格式下需要押韵的句子下标。"""
    return [0, 1, 3] if form.endswith("入韵") and not form.endswith("不入韵") else [1, 3]
