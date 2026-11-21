"""自动评测：生成作品再分析 → 与目标 DNA 逐维偏差 → 修订建议。

评测结果 EvaluationResult 各维独立量化，不合成单一"质量分"。
主观维度（听感、审美）显式标注为 pending_human_review。
"""

from __future__ import annotations

import json
import time
import uuid

from rea_music.analysis import pipeline as pipeline_mod
from rea_music.comparison.compare import compare_tracks
from rea_music.core import cache as cache_mod
from rea_music.core import jobs as jobs_mod
from rea_music.core.errors import REAError


def evaluate_generation(*, generated_audio_path: str, target_dna: dict,
                        tier: str = "standard") -> dict:
    """生成作品 vs 目标 DNA。返回逐维 EvaluationResult。"""
    result_id = f"eval_{uuid.uuid4().hex[:12]}"
    job = jobs_mod.create_job("analyze", {
        "audio_path": generated_audio_path, "tier": tier,
        "context": "evaluation_reanalysis"})
    job = jobs_mod.run_inline(pipeline_mod.analyze_track_job, job,
                              generated_audio_path, tier=tier)
    if job["status"] != "succeeded":
        raise REAError(f"生成作品再分析失败: {job.get('error')}",
                       code="reanalysis_failed",
                       hint="检查生成文件是否可解码音频。")
    with open(job["result_ref"], encoding="utf-8") as f:
        generated_dna = json.load(f)

    cmp = compare_tracks(target_dna, generated_dna)
    dims = cmp["dimensions"]

    evaluation = {
        "ok": True,
        "evaluation_id": result_id,
        "schema": "evaluation_result_v1",
        "generated_song_id": generated_dna["song"]["song_id"],
        "target_song_id": target_dna["song"]["song_id"],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dimensions": {},
        "technical_quality": _technical_quality(generated_dna),
        "originality_risk": _originality_risk(target_dna, generated_dna),
        "subjective": {
            "human_listen_review": "pending_human_review",
            "note": "机器评测不替代听感；主观维度需人类试听确认",
        },
        "revision_suggestions": [],
    }

    mapping = {
        "tempo": ("节奏符合度", "tempo"),
        "key": ("调性符合度", "key"),
        "structure": ("曲式符合度", "structure"),
        "energy_curve": ("能量曲线符合度", "energy_curve"),
        "timbre": ("音色符合度", "timbre"),
        "loudness": ("响度目标符合度", "loudness"),
    }
    for dim_key, (label, _) in mapping.items():
        if dim_key in dims:
            evaluation["dimensions"][label] = {
                "detail": dims[dim_key],
                "satisfied": _verdict_satisfied(dim_key, dims[dim_key]),
            }

    evaluation["revision_suggestions"] = _revision_suggestions(
        evaluation["dimensions"], target_dna)
    cache_mod.save_cached(result_id, "evaluation", evaluation,
                          {"id": result_id})
    return evaluation


def _verdict_satisfied(dim: str, detail: dict) -> bool:
    verdict = detail.get("verdict", "")
    if dim == "tempo":
        return verdict == "same_tempo"
    if dim == "key":
        return verdict == "same_key"
    if dim == "structure":
        return detail.get("label_sequence_similarity", 0) > 0.6
    if dim == "energy_curve":
        return detail.get("correlation", 0) > 0.6
    if dim == "timbre":
        return verdict == "similar_brightness"
    if dim == "loudness":
        return verdict == "same_mastering"
    return False


def _technical_quality(dna: dict) -> dict:
    lufs = dna["production"]["integrated_lufs"]
    tp = dna["production"]["true_peak_dbtp"]
    lra = dna["production"]["lra_lu"]
    notes = []
    lufs_val = lufs.get("value")
    tp_val = tp.get("value")
    if lufs_val is not None and lufs_val < -18:
        notes.append(f"集成响度 {lufs_val} LUFS 偏低（流媒体目标常见 -14 左右）")
    if tp_val is not None and tp_val > -0.3:
        notes.append(f"真峰值 {tp_val} dBTP 可能超限，存在削波风险")
    lra_val = lra.get("value")
    if lra_val is not None and lra_val < 3:
        notes.append(f"动态范围 LRA {lra_val} LU 偏窄，段落对比可能不足")
    return {
        "integrated_lufs": lufs,
        "true_peak_dbtp": tp,
        "lra_lu": lra,
        "issues": notes,
        "verdict": "pass" if not notes else "check_issues",
    }


def _originality_risk(target: dict, generated: dict) -> dict:
    """原创性风险检查：和弦序列 n-gram 重叠度（旋律级检查依赖转写引擎）。"""
    def _chords(dna):
        segs = dna.get("harmony", {}).get("chord_segments", {}).get("value") or []
        return [s["chord"] for s in segs]

    a, b = _chords(target), _chords(generated)
    risk = "not_computable"
    overlap = None
    if a and b:
        grams_a = {tuple(a[i:i + 8]) for i in range(len(a) - 7)}
        grams_b = {tuple(b[i:i + 8]) for i in range(len(b) - 7)}
        if grams_a and grams_b:
            inter = len(grams_a & grams_b)
            overlap = round(inter / max(len(grams_a), 1), 3)
            risk = "high_similarity_warning" if overlap > 0.5 else \
                   ("medium" if overlap > 0.2 else "low")
    return {
        "chord_8gram_overlap": overlap,
        "risk_level": risk,
        "melody_note_overlap": {
            "value": None,
            "missing_reason": "requires_transcription_engine_deep_tier"},
        "note": "和弦高重叠不代表必然侵权，但应人工复核旋律与歌词的相似性",
    }


def _revision_suggestions(dimensions: dict, target_dna: dict) -> list[dict]:
    """把未满足维度转成可执行修订建议。

    每条建议同时给两样东西：
    - ``adjustment``：给人看的中文说明；
    - ``target``：机器可用的目标值（数值/标签），供 revise_plan 直接写入
      target_modifications。历史实现只给自然语言句，revise_plan 把整句当参数
      值写进 plan，导致 Suno 渲染 ``round(str)`` 崩溃 / YuE2 报参数缺失。
    """
    out = []
    for label, block in dimensions.items():
        if block["satisfied"]:
            continue
        detail = block["detail"]
        if "节奏" in label:
            suggestion = {"dimension": label,
                          "adjustment": f"把生成请求 BPM 调整到 {detail.get('a_bpm')}",
                          "knob": "tempo_bpm"}
            if isinstance(detail.get("a_bpm"), (int, float)):
                suggestion["target"] = float(detail["a_bpm"])
            out.append(suggestion)
        if "调性" in label:
            suggestion = {"dimension": label,
                          "adjustment": f"指定调性 {detail.get('a_key')} 或相对转换",
                          "knob": "key"}
            if isinstance(detail.get("a_key"), str) and detail["a_key"]:
                suggestion["target"] = detail["a_key"]
            out.append(suggestion)
        if "响度" in label:
            delta = detail.get("delta_lu", 0)
            suggestion = {
                "dimension": label,
                "adjustment": f"目标响度 {'提高' if delta < 0 else '降低'} "
                              f"{abs(delta)} LU",
                "knob": "integrated_lufs_target"}
            if isinstance(detail.get("a_lufs"), (int, float)):
                suggestion["target"] = float(detail["a_lufs"])
            out.append(suggestion)
        if "能量" in label:
            out.append({"dimension": label,
                        "adjustment": "按目标段落能量曲线调整段落强度标签/编曲密度",
                        "knob": "section_energy"})
        if "曲式" in label:
            out.append({"dimension": label,
                        "adjustment": "对齐段落标签序列与数量",
                        "knob": "sections"})
    return out
