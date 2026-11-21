"""响度与动态：BS.1770 LUFS 积分响度、LRA、近似真峰值、RMS 动态。

pyloudnorm 为 samples-first 约定：输入 (samples,) 或 (samples, channels)。

诚实口径（R8 加固）：
- 短于一个 BS.1770 积分块（0.4s）的输入没有定义，返回 ``integrated_lufs=None``
  + ``missing_reason=audio_too_short``，绝不把 pyloudnorm 的 ValueError 抛成
  ``internal_error``。
- 静音块的积分响度是 ``-inf``，一律记 None + ``missing_reason=silent_block``：
  ``-Infinity`` 会让 DNA 工件不再是严格 JSON（JS 客户端直接解析失败）。
- 真峰值按 30s 分块 4x 过采样取 max，内存恒定（全曲一次性过采样在 900s 上会
  产生 GB 级中间数组）。
"""

from __future__ import annotations

import numpy as np

# pyloudnorm 一个积分块 = 0.4s；短于它没有可定义的积分响度
_MIN_BLOCK_SEC = 0.4
# 真峰值分块参数：块间重叠避免边界漏峰，单块内存约 42MB
_TP_CHUNK_SEC = 30.0
_TP_OVERLAP_SEC = 0.1
# LRA 需要至少 5 个 3s 短时块才有统计意义
_LRA_MIN_BLOCKS = 5


def analyze_loudness(stereo_wav: str) -> dict:
    """测整曲响度/动态。

    Args:
        stereo_wav: 标准化立体声 wav（ensure_wav 产物）。

    Returns:
        {integrated_lufs, integrated_lufs_missing_reason, lra_lu, true_peak_dbtp,
         rms_dbfs, peak_dbfs, crest_factor_db, short_term_lufs_series,
         silent_block_count, method}
        不可测的字段为 None，并在 integrated_lufs_missing_reason 说明原因。
    """
    import soundfile as sf
    import pyloudnorm as pyln

    y, sr = sf.read(stereo_wav, always_2d=True)  # (samples, channels)
    if not np.isfinite(y).all():
        y = np.nan_to_num(y)
    meter = pyln.Meter(sr)

    # 近似真峰值 / 波形统计与 BS.1770 解耦：短音频也能给出这两项
    tp = _true_peak(y, sr)
    mono = y.mean(axis=1) if len(y) else np.zeros(1, dtype=np.float64)
    rms = float(np.sqrt(np.mean(mono ** 2))) if len(mono) else 0.0
    peak = float(np.max(np.abs(mono))) if len(mono) else 0.0
    crest = float(20 * np.log10((peak + 1e-12) / (rms + 1e-12)))

    min_samples = max(int(round(meter.block_size * sr)), 1)
    lufs: float | None = None
    lra: float | None = None
    short_term: list[dict] = []
    skipped = 0
    missing_reason: str | None = None

    if len(y) < min_samples:
        # BS.1770 无定义：如实标"太短"，不抛异常、不编造数值
        missing_reason = "audio_too_short"
    else:
        raw = float(meter.integrated_loudness(y))
        if np.isfinite(raw):
            lufs = round(raw, 1)
        else:
            missing_reason = "silent_block"
        short_term, skipped = _short_term_series(y, sr, meter)
        lra = _lra([p["lufs"] for p in short_term])

    return {
        "integrated_lufs": lufs,
        "integrated_lufs_missing_reason": missing_reason,
        "lra_lu": lra,
        "true_peak_dbtp": round(tp, 1) if np.isfinite(tp) else None,
        "rms_dbfs": round(float(20 * np.log10(rms + 1e-12)), 1),
        "peak_dbfs": round(float(20 * np.log10(peak + 1e-12)), 1),
        "crest_factor_db": round(crest, 1),
        "short_term_lufs_series": short_term,
        "silent_block_count": skipped,
        "method": "bs1770_pyloudnorm_integrated_shortterm",
    }


def _short_term_series(y: np.ndarray, sr: int, meter) -> tuple[list[dict], int]:
    """3s 短时响度序列：非有限值丢弃，时间轴按块起点如实记录。

    Returns:
        (series, skipped) —— series 每项 {time_sec, lufs}；skipped 为被丢弃的
        静音块数（-inf 块不能进序列，否则 DNA 会出现 -Infinity）。
    """
    win = int(3 * sr)
    if win <= 0:
        return [], 0
    series: list[dict] = []
    skipped = 0
    for start in range(0, max(len(y) - win + 1, 1), win):
        seg = y[start:start + win]
        if len(seg) < max(int(sr // 2), 1):
            continue
        value = float(meter.integrated_loudness(seg))
        if not np.isfinite(value):
            skipped += 1
            continue
        series.append({"time_sec": round(start / sr, 1), "lufs": round(value, 1)})
    return series, skipped


def _lra(values: list[float]) -> float | None:
    """LRA = 短时响度 P95 - P10；块数不足返回 None。"""
    if len(values) < _LRA_MIN_BLOCKS:
        return None
    return round(float(np.percentile(values, 95) - np.percentile(values, 10)), 1)


def _true_peak(y: np.ndarray, sr: int) -> float:
    """y: (samples, channels)。分块 4x 过采样取最大绝对值（dBTP）。

    块间留 0.1s 重叠：否则块边界的采样间峰值可能落在两块的接缝处被漏掉。
    返回 -inf 表示无样本（调用方会转成 None）。
    """
    import librosa

    n = len(y)
    if n == 0 or sr <= 0:
        return float("-inf")
    if n < 2:                       # 单样本/极短输入没有采样间插值可言
        return float(20 * np.log10(float(np.max(np.abs(y))) + 1e-12))
    chunk = max(2, int(_TP_CHUNK_SEC * sr))
    overlap = max(0, int(_TP_OVERLAP_SEC * sr))
    hop = max(1, chunk - overlap)
    peak = 0.0
    for start in range(0, n, hop):
        seg = y[start:start + chunk]
        if len(seg) < 2:
            seg = y[max(0, n - 2):n]
        up = librosa.resample(seg.T, orig_sr=sr, target_sr=sr * 4, axis=-1)
        peak = max(peak, float(np.max(np.abs(up))))
    return float(20 * np.log10(peak + 1e-12))
