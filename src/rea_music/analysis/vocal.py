"""人声启发式（整混音视角，无分轨时的诚实降级）与分轨口径重算。

两层口径，按证据强弱分级：
1. 启发式（默认，Fast/Standard）：mid 通道人声频带能量占比 + 居中程度 →
   presence_estimate。它只说明"中频且居中的能量多"，纯钢琴/弦乐同样会命中
   （实测 ACE 纯钢琴曲 presence≈0.85，与真带人声曲 0.90 无法区分）。
   因此数值公式保持不变，另外输出 presence_band 三档标签 + heuristics_note，
   让调用方按"估计"转述而不是当作人声存在的证据。
2. 分轨口径（仅 Deep 档且 demucs 可用）：vocals 轨 RMS 占比 → presence。
   实测判别力：真带人声曲（yue2）share=0.36，纯钢琴曲 share=0.04，差 9 倍。
   覆盖时保留启发式原值在 heuristics_presence，并置 presence_source=
   "separated_stem"，置信度 0.7。

vibrato/气声等人声精细字段仍显式 unavailable，等待专门引擎。
"""

from __future__ import annotations

import numpy as np

HOP = 512

# presence 三档标签阈值（只做标注，不改启发式数值公式）
PRESENCE_BAND_ABSENT = 0.35
PRESENCE_BAND_LIKELY = 0.65
# 分轨口径：vocals 轨占四轨 RMS 之和 40% 视为人声主导（presence=1）。
# 依据实测两点：真带人声曲 0.36、纯钢琴曲 0.04；单点增益只影响档位映射，
# 不改变候选排序与比值关系，原始占比另存 stem_rms_share 供复核。
STEM_PRESENCE_GAIN = 2.5
STEM_PRESENCE_CONFIDENCE = 0.7

_HEURISTICS_NOTE = (
    "mid/side + 200-4000Hz 占比是整混音启发式：居中且中频丰富的器乐（钢琴、"
    "弦乐、失真吉他）同样会让它变高（实测纯钢琴曲 0.85），它不能作为人声存在"
    "的证据。请按 presence_band 的三档标签转述；需要人声真值时用 Deep 档，"
    "presence_source=separated_stem 的分轨口径才有判别力。")


def presence_band(presence: float | None) -> str | None:
    """把 0-1 的 presence 估计映射成三档标签；缺值返回 None（不猜）。

    absent: <0.35 / ambiguous: 0.35-0.65 / likely: >0.65
    """
    if presence is None:
        return None
    value = float(presence)
    if value < PRESENCE_BAND_ABSENT:
        return "absent"
    if value > PRESENCE_BAND_LIKELY:
        return "likely"
    return "ambiguous"


def analyze_vocal_presence(stereo_wav: str, sr: int = 44100) -> dict:
    import soundfile as sf

    y, sr = sf.read(stereo_wav, always_2d=True)
    if y.shape[1] < 2:
        return {"presence_estimate": None, "confidence": 0.0,
                "presence_band": None, "presence_source": "unavailable",
                "method": "mid_side_heuristic_v1",
                "heuristics_note": _HEURISTICS_NOTE,
                "missing_reason": "mono_source_no_side_channel"}
    mid = y.mean(axis=1)
    side = (y[:, 0] - y[:, 1]) / 2.0

    mid_vocal = _band_energy(mid, sr, 200.0, 4000.0)
    mid_total = float(np.mean(mid ** 2)) + 1e-12
    side_energy = float(np.mean(side ** 2)) + 1e-12

    # 人声通常居中：mid 占比高且 side 相对低
    stereo_focus = float(np.clip(1.0 - side_energy / (mid_total + side_energy), 0.0, 1.0))
    vocal_band_ratio = float(mid_vocal / mid_total)
    presence = float(np.clip(stereo_focus * vocal_band_ratio * 1.6, 0.0, 1.0))

    # 活动占比：人声频带能量超过其自身中位数 1.5 倍的帧比例
    import librosa
    vocal_band = _band_series(mid, sr, 200.0, 4000.0)
    if len(vocal_band):
        thr = 1.5 * float(np.median(vocal_band))
        activity = float(np.mean(vocal_band > thr))
    else:
        activity = None

    return {
        "presence_estimate": round(presence, 2),
        "presence_band": presence_band(presence),
        "presence_source": "mid_side_heuristic",
        "vocal_band_energy_ratio": round(vocal_band_ratio, 3),
        "center_focus": round(stereo_focus, 3),
        "activity_fraction": round(activity, 2) if activity is not None else None,
        "confidence": 0.45 if presence > 0.25 else 0.35,
        "method": "mid_side_vocal_band_heuristic_v1",
        "heuristics_note": _HEURISTICS_NOTE,
        "fine_metrics": {
            "vibrato": {"value": None, "missing_reason": "requires_vocal_stem"},
            "breathiness": {"value": None, "missing_reason": "requires_vocal_stem"},
            "vocal_range_semitones": {"value": None,
                                       "missing_reason": "requires_vocal_stem"},
            "melody_notes": {"value": None,
                             "missing_reason": "requires_stem_and_transcription_engine"},
        },
    }


def presence_from_separated_stems(stems: dict) -> dict | None:
    """分轨口径的 presence：vocals 轨 RMS 占全部轨 RMS 之和的比例。

    Args:
        stems: demucs manifest 的 stems 字段 {name: {"path", "rms"}}。

    Returns:
        {presence_estimate, presence_band, stem_rms_share, vocals_rms,
         total_rms} 或 None（无 vocals 轨 / RMS 数据缺失 / 全轨静音）。
    """
    if not stems or not isinstance(stems, dict):
        return None
    rms_by_stem: dict[str, float] = {}
    for name, info in stems.items():
        if not isinstance(info, dict):
            return None
        value = info.get("rms")
        if not isinstance(value, (int, float)):
            return None
        rms_by_stem[str(name)] = max(float(value), 0.0)
    if "vocals" not in rms_by_stem:
        return None
    total = float(sum(rms_by_stem.values()))
    if total <= 0.0:
        return None
    share = float(rms_by_stem["vocals"]) / total
    presence = float(np.clip(share * STEM_PRESENCE_GAIN, 0.0, 1.0))
    return {
        "presence_estimate": round(presence, 2),
        "presence_band": presence_band(presence),
        "stem_rms_share": round(share, 4),
        "vocals_rms": round(rms_by_stem["vocals"], 6),
        "total_rms": round(total, 6),
    }


def apply_separated_stem_presence(vocal: dict | None,
                                  stems: dict) -> dict | None:
    """Deep 档：用分轨结果覆盖启发式 presence，原值留档备复核。

    无 vocals 轨或 RMS 数据不全时原样返回启发式结果（行为不变）。
    """
    stem_view = presence_from_separated_stems(stems)
    if stem_view is None:
        return vocal
    out = {**(vocal or {})}
    out["heuristics_presence"] = out.get("presence_estimate")
    out["presence_estimate"] = stem_view["presence_estimate"]
    out["presence_band"] = stem_view["presence_band"]
    out["presence_source"] = "separated_stem"
    out["presence_method"] = "demucs_vocals_rms_share"
    out["stem_rms_share"] = stem_view["stem_rms_share"]
    out["confidence"] = STEM_PRESENCE_CONFIDENCE
    return out


def _band_energy(y: np.ndarray, sr: int, lo: float, hi: float) -> float:
    from scipy.signal import butter, sosfilt
    sos = butter(4, [lo, hi], btype="band", fs=sr, output="sos")
    filtered = sosfilt(sos, y)
    return float(np.mean(filtered ** 2))


def _band_series(y: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    from scipy.signal import butter, sosfilt
    sos = butter(4, [lo, hi], btype="band", fs=sr, output="sos")
    filtered = sosfilt(sos, y)
    import librosa
    return librosa.feature.rms(y=filtered, hop_length=HOP)[0]
