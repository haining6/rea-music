"""引用解析器：Agent 可以传路径、内容哈希（前缀）、song_id 或直接传对象。

MCP 与 CLI 共用，避免两套业务逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path

from rea_music.analysis import pipeline as pipeline_mod
from rea_music.core import cache as cache_mod
from rea_music.core.errors import AnalysisNotFound, ValidationError


def load_dna(ref: str | dict, tier: str = "standard") -> dict:
    """把任意引用解析为 Music DNA dict。"""
    if isinstance(ref, dict):
        if ref.get("schema") != "music_dna_v1":
            raise ValidationError("传入对象不是 music_dna_v1",
                                   hint="先经 rea_music_analyze_track 产出 DNA。")
        return ref
    if isinstance(ref, str):
        p = Path(ref)
        if p.exists() and p.suffix.lower() == ".json":
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        if p.exists():
            dna = _dna_from_file(p, tier)
            if dna:
                return dna
        dna = pipeline_mod.get_analysis(ref.replace("sha256:", ""), tier)
        if dna:
            return dna
    raise AnalysisNotFound(
        f"找不到可用的 Music DNA: {ref}",
        hint="先调用 rea_music_analyze_track，再用返回的 content_hash/song_id 引用。")


def _dna_from_file(p: Path, tier: str) -> dict | None:
    """按文件内容哈希取 DNA；缓存键与 pipeline 共用同一构造，避免键漂移。"""
    from rea_music.core.hash import file_sha256
    return cache_mod.load_cached(file_sha256(p), "dna",
                                 pipeline_mod.dna_cache_params(tier))


def load_plan(ref: str | dict) -> dict:
    if isinstance(ref, dict):
        return ref
    path = cache_mod.cached_path(ref, "plan", {"id": ref})
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    p = Path(ref)
    if p.exists() and p.suffix.lower() == ".json":
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    raise AnalysisNotFound(
        f"找不到 CompositionPlan: {ref}",
        hint="先调用 rea_music_create_composition_plan 获取 plan_id。")


def load_evaluation(ref: str | dict) -> dict:
    if isinstance(ref, dict):
        return ref
    path = cache_mod.cached_path(ref, "evaluation", {"id": ref})
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    raise AnalysisNotFound(f"找不到评测记录: {ref}",
                           hint="先调用 rea_music_evaluate_generation。")


def compact_dna(dna: dict) -> dict:
    """Agent 友好视图：剥掉长数组，保留全部标量与结构。"""
    compact = json.loads(json.dumps(dna, ensure_ascii=False))
    compact["rhythm"].pop("beats_time_sec", None)
    compact["rhythm"].pop("downbeats_time_sec", None)
    compact["rhythm"]["tempo_curve"] = {
        "value": (dna["rhythm"].get("tempo_curve", {}).get("value") or [])[:20],
        "note": "截断预览；完整曲线用 detail=full"}
    compact["rhythm"].pop("tempo_candidates", None)
    compact["production"].pop("short_term_lufs_series", None)
    compact["production"]["rms_curve_1s"] = {
        "value": (dna["production"].get("rms_curve_1s", {}).get("value") or [])[:12],
        "note": "截断预览"}
    return compact
