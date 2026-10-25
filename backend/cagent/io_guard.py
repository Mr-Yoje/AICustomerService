from __future__ import annotations

import csv
from pathlib import Path


class DamagedFile(ValueError):
    """表格或题面文件损坏、编码无法识别时拒绝读取，不把异常抛成未处理崩溃。"""


def read_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = path.read_bytes()
    if not raw.strip() or b"\x00" in raw:
        raise DamagedFile(f"{path.name} 文件损坏，已拒绝读取")
    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None or _control_ratio(text) >= 0.1:
        raise DamagedFile(f"{path.name} 编码无法识别，已拒绝读取")
    return text


def read_csv_dicts(path: Path) -> tuple[list[str], list[dict]]:
    text = read_text(path)
    try:
        reader = csv.DictReader(text.splitlines())
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    except csv.Error as e:
        raise DamagedFile(f"{path.name} 表格损坏，已拒绝读取") from e
    if not fieldnames:
        raise DamagedFile(f"{path.name} 缺少表头，已拒绝读取")
    return fieldnames, rows


def _control_ratio(text: str) -> float:
    if not text:
        return 1.0
    bad = sum(1 for ch in text if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\r\n\t"))
    return bad / len(text)
