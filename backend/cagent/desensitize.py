from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

PATTERNS = [
    ("NAME", re.compile(r"客户\d{4}")),
    ("ID", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("AMOUNT", re.compile(r"(?<![\dA-Za-z_])\d+(?:,\d{3})*(?:\.\d+)?\s*元")),
]


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
