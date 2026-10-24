from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

PATTERNS = [
    ("NAME", re.compile(r"客户\d{4}")),
    ("CID", re.compile(r"(?<![A-Za-z0-9])C\d{4}(?!\d)")),
    ("HONORIFIC", re.compile(r"[\u4e00-\u9fff]{1,2}(?:先生|女士)")),
    ("ID", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("AMOUNT", re.compile(r"(?<![\dA-Za-z_])\d+(?:,\d{3})*(?:\.\d+)?\s*元")),
]

_YUAN = re.compile(r"(?<![\dA-Za-z_])\d+(?:,\d{3})*(?:\.\d+)?\s*元")
_HONORIFIC = re.compile(r"[\u4e00-\u9fff]{1,2}(?:先生|女士)")
_CID = re.compile(r"(?<![A-Za-z0-9])C(\d{2})(\d{2})(?!\d)")
SENSITIVE_KEYS = {"customer_name", "monthly_income", "credit_score", "age", "occupation", "phone", "id_card"}


@dataclass
class MaskMap:
    forward: dict[str, str] = field(default_factory=dict)

    def token(self, kind: str, original: str) -> str:
        if original not in self.forward:
            n = sum(1 for t in self.forward.values() if t.startswith(f"<{kind}_")) + 1
            self.forward[original] = f"<{kind}_{n}>"
        return self.forward[original]

    def unmask(self, text: str) -> str:
        for original, tok in self.forward.items():
            text = text.replace(tok, original)
        return text


def mask(text: str, mm: MaskMap | None = None, names: Iterable[str] = ()) -> tuple[str, MaskMap]:
    mm = mm or MaskMap()
    for name in names:
        if name:
            text = text.replace(name, mm.token("NAME", name))
    for kind, pattern in PATTERNS:
        text = pattern.sub(lambda m, k=kind: mm.token(k, m.group(0)), text)
    return text, mm


def display_mask(text: str) -> str:
    text = re.sub(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)", r"\1********\2", text)
    text = re.sub(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)", r"\1****\2", text)
    return re.sub(r"客户\d{2}(\d{2})", r"客户**\1", text)


def export_mask(text: str) -> str:
    """评测结果、轨迹和审计导出使用，比界面展示多遮住金额、称谓和画像数字。"""
    text = display_mask(text)
    text = _CID.sub(r"C**\2", text)
    text = _YUAN.sub("**元", text)
    text = re.sub(r"(?<!\d)\d{1,3}岁", "**岁", text)
    text = re.sub(r"(\*\*岁，)[^，\n]{1,16}(，)", r"\1**\2", text)
    text = re.sub(r"信用评分\d+", "信用评分**", text)
    return _HONORIFIC.sub(lambda m: "*" + m.group(0)[-2:], text)


def redact(value):
    """导出轨迹和评测结果时去掉画像明文、金额和称谓。"""
    if isinstance(value, str):
        return export_mask(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in SENSITIVE_KEYS and not isinstance(item, (dict, list)):
                out[key] = "**" if item not in (None, "") else item
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
