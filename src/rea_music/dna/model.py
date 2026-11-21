"""Music DNA v1 数据模型与构建器。

设计硬约束（总指令第 6 节）：
- 可推断字段一律携带 value/unit/confidence/method/model_version/provenance。
- 测不到的字段 null + missing_reason，禁止凭空填值。
- 客观音频特征与模型推断、审美评价分层存储。
- 支持段落级 DNA，不允许只有整曲平均值。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from rea_music import SCHEMA_VERSION, __version__

ANALYZER_ID = f"rea-music/{__version__}"


def fv(value, *, unit: str | None = None, confidence: float | None = None,
       method: str | None = None, model_version: str | None = None,
       time_range: list | None = None, provenance: str = "measured",
       missing_reason: str | None = None) -> dict:
    """字段包装器（field-with-provenance）。value=None 时记录 missing_reason。"""
    out = {"value": value}
    if unit:
        out["unit"] = unit
    if confidence is not None:
        out["confidence"] = confidence
    if method:
        out["method"] = method
    if model_version:
        out["model_version"] = model_version
    if time_range:
        out["source_time_range_sec"] = time_range
    if provenance:
        out["provenance"] = provenance
    if value is None and missing_reason:
        out["missing_reason"] = missing_reason
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_music_dna(*, content_hash: str, meta: dict, tempo: dict, key: dict,
                    chords: dict | None, structure: dict | None,
                    loudness: dict, spectral: dict, vocal: dict | None,
                    tier: str, duration_sec: float,
                    segments_extra: list[dict] | None = None,
                    source_path: str = "", license_note: str | None = None
                    ) -> dict:
    """汇总各引擎输出为 Music DNA v1。每个区块带 layer 标记。"""
    seg_out = []
    if structure and structure.get("segments"):
        for i, seg in enumerate(structure["segments"]):
            extra = (segments_extra or [{}] * len(structure["segments"]))[i] or {}
            seg_out.append({
                "index": i,
                "start_sec": seg["start_sec"],
                "end_sec": seg["end_sec"],
                "label": fv(seg["label"], confidence=seg.get("label_confidence", 0.4),
                            method=structure.get("method", "heuristic_v1"),
                            time_range=[seg["start_sec"], seg["end_sec"]]),
                "rms": fv(extra.get("rms", seg.get("rms")), unit="linear",
                          confidence=0.8, provenance="measured",
                          time_range=[seg["start_sec"], seg["end_sec"]]),
                "spectral_centroid_hz": fv(
                    extra.get("centroid_hz"), unit="Hz", confidence=0.75,
                    provenance="measured",
                    time_range=[seg["start_sec"], seg["end_sec"]]),
                "chords_local": extra.get("chords_local"),
                "key_local": extra.get("key_local"),
            })

    lyrics_block = {
        "lyrics_available": False,
        "provenance": "not_extracted",
        "fields": fv(None, missing_reason="lyrics_require_user_provided_text_or_asr",
                     provenance="unavailable"),
        "note": "歌词只接受用户提供或有合法来源的文本；提取能力见 capabilities",
    }

    # 节奏倍频歧义标记（可选字段）：仅当分析层给出该结论时写入。
    # 旧工件 / 无该字段的来源保持缺省，不影响历史数据校验。
    bpm_field = fv(tempo.get("bpm"), unit="bpm",
                   confidence=tempo.get("confidence", 0.0),
                   method=tempo.get("method"),
                   missing_reason=tempo.get("missing_reason"))
    if tempo.get("octave_ambiguity") is not None:
        bpm_field["octave_ambiguity"] = bool(tempo["octave_ambiguity"])

    # 和弦切换频率按实测 BPM 换算（单位 beats/chord）；缺 BPM 时如实记 None，
    # 不能按 120 BPM 的固定除数硬换（那会把 150 BPM 曲目算小 20%）。
    harmonic_rhythm, harmonic_rhythm_missing = _harmonic_rhythm(
        (chords or {}).get("segments", []), tempo.get("bpm"))
    lufs_missing = loudness.get("integrated_lufs_missing_reason")

    dna = {
        "schema": SCHEMA_VERSION,
        "song": {
            "song_id": f"sha256:{content_hash[:16]}",
            "content_hash": content_hash,
            "source_path": source_path,
            "file_name": meta.get("file_name"),
            "container": meta.get("container"),
            "duration_sec": meta.get("duration_sec", duration_sec),
            "sample_rate_hz": meta.get("sample_rate_hz"),
            "channels": meta.get("channels"),
            "analyzed_at": _now_iso(),
            "analyzer": ANALYZER_ID,
            "analysis_tier": tier,
            "usage_rights_note": license_note or "用户须自行确保参考素材的合法使用权限",
        },
        "rhythm": {
            "layer": "objective_audio_features",
            "bpm": bpm_field,
            "meter_beats": fv(tempo.get("meter"), unit="beats/measure",
                              confidence=tempo.get("meter_confidence", 0.0),
                              method="beat_accent_autocorr_heuristic_v1",
                              missing_reason=tempo.get("missing_reason")),
            "beat_count": tempo.get("beat_count", 0),
            "beats_time_sec": {"value": tempo.get("beats_time", []),
                               "provenance": "measured"},
            "downbeats_time_sec": {"value": tempo.get("downbeats_time", []),
                                   "confidence": tempo.get("meter_confidence", 0.0),
                                   "method": "accent_heuristic_v1",
                                   "provenance": "inferred"},
            "groove_syncopation": fv(None, missing_reason="requires_onset_grid_deep_analysis"),
            "tempo_candidates": tempo.get("tempo_candidates", []),
            "tempo_curve": {"value": tempo.get("tempo_curve_sec_bpm", []),
                            "method": "windowed_autocorr",
                            "provenance": "measured"},
        },
        "tonality": {
            "layer": "objective_audio_features",
            "key": fv(key.get("key"), confidence=key.get("confidence", 0.0),
                      method=key.get("method"),
                      missing_reason=key.get("missing_reason")),
            "tonic_pitch_class": key.get("tonic_pitch_class"),
            "mode": key.get("mode"),
            "key_regions": {"value": key.get("key_regions", []),
                            "confidence": 0.6,
                            "method": key.get("method"),
                            "provenance": "inferred"},
        },
        "harmony": {
            "layer": "inferred",
            "chord_segments": {"value": (chords or {}).get("segments", []),
                                "confidence": (chords or {}).get("confidence", 0.0),
                                "method": (chords or {}).get("method"),
                                "engine": (chords or {}).get("engine")},
            "progressions": {"value": (chords or {}).get("progression", []),
                             "confidence": 0.55,
                             "provenance": "inferred"},
            "harmonic_rhythm_beats": fv(
                harmonic_rhythm, unit="beats/chord", confidence=0.5,
                provenance="inferred",
                missing_reason=harmonic_rhythm_missing),
        },
        "melody": {
            "layer": "inferred",
            "notes": fv(None, missing_reason="requires_transcription_engine_deep_tier"),
            "vocal_range_semitones": fv(None, missing_reason="requires_vocal_stem"),
            "phrase_repetition": fv(None, missing_reason="requires_transcription_engine"),
        },
        "form": {
            "layer": "inferred",
            "segments": seg_out,
            "boundary_count": (structure or {}).get("boundary_count", 0),
            "method": (structure or {}).get("method"),
            "engine": (structure or {}).get("engine", "heuristic_v1"),
            "confidence": (structure or {}).get("confidence", 0.0),
        },
        "instrumentation": {
            "layer": "inferred",
            "stems": fv(None, missing_reason="requires_separation_engine",
                         provenance="unavailable"),
            "band_energy_ratios": {"value": spectral.get("band_energy_ratios", {}),
                                    "provenance": "measured",
                                    "method": spectral.get("method")},
        },
        "vocals": {
            "layer": "inferred",
            **(vocal or {}),
        },
        "lyrics": lyrics_block,
        "production": {
            "layer": "objective_audio_features",
            "integrated_lufs": fv(loudness.get("integrated_lufs"), unit="LUFS",
                                   confidence=0.9, method=loudness.get("method"),
                                   missing_reason=lufs_missing),
            "lra_lu": fv(loudness.get("lra_lu"), unit="LU", confidence=0.85),
            "true_peak_dbtp": fv(loudness.get("true_peak_dbtp"), unit="dBTP",
                                 confidence=0.85),
            "crest_factor_db": fv(loudness.get("crest_factor_db"), unit="dB",
                                  confidence=0.85),
            "spectral_centroid_hz": fv(spectral.get("spectral_centroid_hz"), unit="Hz",
                                       confidence=0.9),
            "spectral_flatness": fv(spectral.get("spectral_flatness"), confidence=0.9),
            "harmonic_percussive_ratio": fv(spectral.get("harmonic_percussive_ratio"),
                                            confidence=0.7, provenance="measured"),
            "short_term_lufs_series": {"value": loudness.get("short_term_lufs_series", []),
                                       "provenance": "measured"},
            "rms_curve_1s": {"value": spectral.get("rms_curve_1s", []),
                             "provenance": "measured"},
        },
        "creative_semantics": {
            "layer": "interpretive",
            "style_tags": fv(_style_tags(spectral, tempo, vocal),
                             confidence=0.4, provenance="heuristic"),
            "mood_tags": fv(None, missing_reason="requires_deep_tier_or_user_input",
                            provenance="unavailable"),
            "note": "interpretive 层标签仅供创作参考，非客观测量",
        },
        "provenance_summary": {
            "analyzer": ANALYZER_ID,
            "schema": SCHEMA_VERSION,
            "tier": tier,
            "engines_used": _engines_used(chords, structure, tempo, key),
            "analysis_completed_at": _now_iso(),
        },
    }
    return dna


def _harmonic_rhythm(chord_segments: list[dict],
                     bpm: float | None = None) -> tuple[float | None, str | None]:
    """和弦切换频率（平均每和弦拍数，单位 beats/chord）。

    换算必须用实测 BPM：beats = 平均和弦时长(秒) × bpm / 60。
    早前实现用固定除数 0.5（等价于假设 120 BPM），在 150 BPM 曲目上偏小 20%。

    Args:
        chord_segments: 和弦段列表（含 start_sec/end_sec）。
        bpm: 实测 BPM；缺失时无法换算，如实返回 None + 原因。

    Returns:
        (beats_per_chord, missing_reason)。
    """
    if not chord_segments:
        return None, "no_chord_segments"
    total_sec = sum(s["end_sec"] - s["start_sec"] for s in chord_segments)
    if total_sec <= 0:
        return None, "zero_length_chord_segments"
    try:
        tempo_value = float(bpm) if bpm is not None else 0.0
    except (TypeError, ValueError):
        tempo_value = 0.0
    if tempo_value <= 0:
        return None, "requires_bpm"
    avg_sec = total_sec / len(chord_segments)
    return round(avg_sec * tempo_value / 60.0, 2), None


def _style_tags(spectral: dict, tempo: dict, vocal: dict | None) -> list[str]:
    """基于客观特征的粗粒度风格线索，置信度低，标注 heuristic。"""
    tags = []
    bpm = (tempo or {}).get("bpm")
    if bpm:
        if bpm < 80:
            tags.append("slow_tempo")
        elif bpm > 140:
            tags.append("fast_tempo")
        else:
            tags.append("mid_tempo")
    cent = spectral.get("spectral_centroid_hz")
    if cent and cent > 3000:
        tags.append("bright_timbre")
    elif cent and cent < 1500:
        tags.append("dark_timbre")
    hp = spectral.get("harmonic_percussive_ratio")
    if hp is not None and hp > 0.6:
        tags.append("harmonic_dominant")
    elif hp is not None:
        tags.append("percussive_dominant")
    if vocal and (vocal.get("presence_estimate") or 0) > 0.25:
        tags.append("likely_vocal")
    return tags


def _engines_used(chords, structure, tempo, key) -> list[str]:
    engines = [tempo.get("method", "?"), key.get("method", "?")]
    if chords:
        engines.append(chords.get("method", "?"))
    if structure:
        engines.append(structure.get("method", "?"))
    return engines
