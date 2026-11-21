"""多曲比较与 DNA 融合。

compare_tracks：BPM/调性/能量曲线/频谱/结构 五维量化距离 + 兼容性判读。
blend_music_dna：按维度映射融合多首参考曲的宏观特征。
不拼接参考曲旋律、采样或歌词，只做统计特征层融合。
"""

from __future__ import annotations

import numpy as np

from rea_music.core.errors import ValidationError

PITCHES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# 对外承诺的可比维度：缺数据时必须显式列出，不能只回一个空 dimensions
_COMPARE_DIMENSIONS = ("tempo", "key", "energy_curve", "timbre", "loudness",
                       "structure")


def _val(block: dict | None):
    return (block or {}).get("value")


def compare_tracks(dna_a: dict, dna_b: dict) -> dict:
    """两个 Music DNA 的多维比较。每个维度独立量化，不做单一总分。"""
    dims = {}

    bpm_a = _val(dna_a["rhythm"]["bpm"])
    bpm_b = _val(dna_b["rhythm"]["bpm"])
    if bpm_a and bpm_b:
        ratio = bpm_b / bpm_a
        dims["tempo"] = {
            "a_bpm": bpm_a, "b_bpm": bpm_b,
            "ratio": round(ratio, 3),
            "distance": round(abs(np.log2(ratio)), 3),
            "verdict": "same_tempo" if 0.97 < ratio < 1.03 else
                       ("double_time" if 1.9 < ratio < 2.1 else
                        ("half_time" if 0.48 < ratio < 0.52 else "different")),
        }

    key_a = _val(dna_a["tonality"]["key"])
    key_b = _val(dna_b["tonality"]["key"])
    if key_a and key_b:
        fifths = _circle_distance(key_a, key_b)
        same_mode = key_a.split()[-1] == key_b.split()[-1]
        dims["key"] = {
            "a_key": key_a, "b_key": key_b,
            "fifths_distance": fifths,
            "same_mode": same_mode,
            "verdict": "same_key" if fifths == 0 and same_mode else
                       ("relative_or_parallel" if fifths <= 2 else "remote"),
        }

    ea = _energy_series(dna_a)
    eb = _energy_series(dna_b)
    if ea is not None and eb is not None and min(len(ea), len(eb)) >= 5:
        n = min(len(ea), len(eb))
        corr = float(np.corrcoef(ea[:n], eb[:n])[0, 1]) if n >= 2 else 0.0
        corr = corr if np.isfinite(corr) else 0.0
        dims["energy_curve"] = {
            "correlation": round(corr, 3),
            "verdict": "similar_dynamics" if corr > 0.7 else
                       ("contrasting" if corr < 0 else "neutral"),
        }

    cent_a = _val(dna_a["production"]["spectral_centroid_hz"])
    cent_b = _val(dna_b["production"]["spectral_centroid_hz"])
    if cent_a and cent_b:
        dims["timbre"] = {
            "a_centroid_hz": cent_a, "b_centroid_hz": cent_b,
            "octave_distance": round(abs(np.log2(cent_b / cent_a)), 3),
            "verdict": "similar_brightness" if abs(cent_b / cent_a - 1) < 0.3 else
                       "different_brightness",
        }

    la = _val(dna_a["production"]["integrated_lufs"])
    lb = _val(dna_b["production"]["integrated_lufs"])
    if la is not None and lb is not None:
        dims["loudness"] = {
            "a_lufs": la, "b_lufs": lb, "delta_lu": round(lb - la, 1),
            "verdict": "same_mastering" if abs(lb - la) < 2 else "different_mastering",
        }

    sa = dna_a["form"]["segments"]
    sb = dna_b["form"]["segments"]
    if sa and sb:
        seq_a = [s["label"]["value"] for s in sa]
        seq_b = [s["label"]["value"] for s in sb]
        dims["structure"] = {
            "a_sections": seq_a, "b_sections": seq_b,
            "section_count_delta": len(sb) - len(sa),
            "label_sequence_similarity": round(_seq_similarity(seq_a, seq_b), 3),
            "verdict": "similar_form" if _seq_similarity(seq_a, seq_b) > 0.6 else
                       "different_form",
        }

    missing = [name for name in _COMPARE_DIMENSIONS if name not in dims]
    return {
        "ok": True,
        "a_song_id": dna_a["song"]["song_id"],
        "b_song_id": dna_b["song"]["song_id"],
        "dimensions": dims,
        # 空 dimensions 不等于"两曲完全一致"：缺数据的维度必须点名，调用方
        # 才能区分"没测出来"与"测出来一样"。
        "missing_dimensions": missing,
        "comparable": bool(dims),
        "note": "各维度独立量化，不合成单一总分；missing_dimensions 为数据缺失、"
                "未参与比较的维度",
    }


def _circle_distance(key_a: str, key_b: str) -> int:
    try:
        na, ma = key_a.split()
        nb, mb = key_b.split()
    except ValueError:
        return 99
    ia = PITCHES.index(na) % 12
    ib = PITCHES.index(nb) % 12
    fa = (ia * 7) % 12
    fb = (ib * 7) % 12
    d = abs(fa - fb)
    return min(d, 12 - d)


def _energy_series(dna: dict) -> np.ndarray | None:
    curve = _val(dna["production"].get("rms_curve_1s"))
    if not curve:
        return None
    return np.array([p["rms"] for p in curve], dtype=float)


def _seq_similarity(a: list[str], b: list[str]) -> float:
    """标签序列相似度：位置对齐 + 集合 Jaccard 的折中。"""
    if not a or not b:
        return 0.0
    pos = sum(1 for x, y in zip(a, b) if x == y) / max(len(a), len(b))
    jac = len(set(a) & set(b)) / max(len(set(a) | set(b)), 1)
    return 0.5 * pos + 0.5 * jac


def blend_music_dna(dnas: list[dict], dimension_map: dict[str, int] | None = None
                    ) -> dict:
    """多曲 DNA 融合。dimension_map 指定每个维度取哪首曲（0 基索引）。"""
    if not dnas:
        raise ValidationError("至少需要一份 Music DNA")
    dimension_map = dimension_map or {}

    def pick(dimension: str, default_idx: int = 0) -> dict:
        idx = dimension_map.get(dimension, default_idx)
        idx = int(idx) % len(dnas)
        return dnas[idx]

    blended = {
        "ok": True,
        "schema": "music_dna_blend_v1",
        "sources": [d["song"]["song_id"] for d in dnas],
        "dimensions": {
            "tempo": pick("tempo")["rhythm"]["bpm"],
            "key": pick("key")["tonality"]["key"],
            "structure_template": pick("structure")["form"]["segments"][:12],
            "production_target": {
                "integrated_lufs": pick("production")["production"]["integrated_lufs"],
                "spectral_centroid_hz": pick("timbre")["production"]["spectral_centroid_hz"],
            },
            "harmony_progression": pick("harmony")["harmony"]["progressions"],
            "vocal_style": pick("vocal")["vocals"].get("presence_estimate"),
        },
        "originality_guardrails": {
            "no_reference_melody_copy": True,
            "no_reference_lyrics_reuse": True,
            "no_audio_sampling": True,
            "note": "融合只作用于统计特征层；旋律/歌词由生成端原创",
        },
        "note": "各维度来源见 sources + dimension_map",
    }
    return blended
