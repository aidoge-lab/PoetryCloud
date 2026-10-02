"""中华新韵（十四韵）。v1 用新韵：现代声调定平仄（一二声平，三四声仄），不处理入声。"""

FINAL_TO_RHYME = {
    "a": "麻", "ia": "麻", "ua": "麻",
    "o": "波", "uo": "波", "e": "波",
    "ie": "皆", "ve": "皆", "üe": "皆", "ue": "皆",
    "ai": "开", "uai": "开",
    "ei": "微", "ui": "微", "uei": "微",
    "ao": "豪", "iao": "豪",
    "ou": "尤", "iu": "尤", "iou": "尤",
    "an": "寒", "ian": "寒", "uan": "寒", "van": "寒", "üan": "寒",
    "en": "文", "in": "文", "un": "文", "uen": "文", "vn": "文", "ün": "文",
    "ang": "唐", "iang": "唐", "uang": "唐",
    "eng": "庚", "ing": "庚", "ong": "庚", "iong": "庚", "ueng": "庚",
    "i": "齐", "er": "齐", "v": "齐", "ü": "齐",
    "u": "姑",
}
SIBILANTS = {"zh", "ch", "sh", "r", "z", "c", "s"}
RHYMES = ["麻", "波", "皆", "开", "微", "豪", "尤", "寒", "文", "唐", "庚", "齐", "支", "姑"]


def rhyme_of_pinyin(initial: str, final: str) -> str:
    """initial/final 用 pypinyin 非严格模式的输出（ü 写作 v，ju 的韵母为 v）。"""
    if final == "i" and initial in SIBILANTS:
        return "支"
    if final == "u" and initial in {"j", "q", "x", "y"}:
        return "齐"  # ju/qu/xu/yu 实为 ü
    if final == "uan" and initial in {"j", "q", "x", "y"}:
        return "寒"
    return FINAL_TO_RHYME.get(final, "?")
