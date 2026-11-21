"""内容寻址缓存。

键 = sha256(文件内容) + 分析档位 + 参数摘要。
同一文件重复分析必须命中缓存；force=True 才允许重算。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rea_music.core.errors import ValidationError
from rea_music.core.paths import content_dir


def cache_key(content_hash: str, kind: str, params: dict | None = None) -> str:
    base = f"{kind}:{json.dumps(params or {}, sort_keys=True, ensure_ascii=False)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def cached_path(content_hash: str, kind: str, params: dict | None = None) -> Path:
    return content_dir(kind, content_hash) / f"{cache_key(content_hash, kind, params)}.json"


def load_cached(content_hash: str, kind: str, params: dict | None = None) -> dict | None:
    path = cached_path(content_hash, kind, params)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None  # 缓存损坏视同未命中，重算即可


def save_cached(content_hash: str, kind: str, data: dict,
                params: dict | None = None) -> Path:
    """写入缓存工件。

    显式禁止 NaN/Infinity：缓存工件会被 MCP 客户端（含 JS 实现）按严格 JSON
    解析，`-Infinity` 这类字面量会让整条结果不可用。出现非有限数值说明分析层
    有缺陷（应记 None 而不是 -inf），这里尽早抛结构化错误而不是落一个坏工件。
    """
    path = cached_path(content_hash, kind, params)
    try:
        payload = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False)
    except ValueError as exc:
        raise ValidationError(
            f"缓存工件含非有限数值（NaN/Infinity），拒绝落盘: {kind}",
            hint="分析层应把 -inf/NaN 记为 null 并给 missing_reason；这是代码缺陷。",
            details={"kind": kind, "content_hash": content_hash,
                     "error": str(exc)},
        ) from exc
    path.write_text(payload, encoding="utf-8")
    return path


def drop_cached(content_hash: str, kind: str, params: dict | None = None) -> None:
    path = cached_path(content_hash, kind, params)
    if path.exists():
        path.unlink()
