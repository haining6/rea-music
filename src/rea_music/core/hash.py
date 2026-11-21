"""内容哈希：全部缓存与工件寻址的键。"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1 << 20


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
