"""频谱与音色：谱重心、带宽、平直度、分频段能量、HPSS 谐/噪比、能量曲线。"""

from __future__ import annotations

import numpy as np

HOP = 512


def analyze_spectral(y: np.ndarray, sr: int) -> dict:
    import librosa

    cent = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=HOP)[0]
    bw = librosa.feature.spectral_bandwidth(y=y, sr=sr, hop_length=HOP)[0]
    flat = librosa.feature.spectral_flatness(y=y, hop_length=HOP)[0]
    rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr, hop_length=HOP)[0]

    harmonic, percussive = librosa.effects.hpss(y)
    h_energy = float(np.mean(harmonic ** 2))
    p_energy = float(np.mean(percussive ** 2))
    hp_ratio = round(h_energy / (h_energy + p_energy + 1e-12), 3)

    bands = _band_ratios(y, sr)
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    fps = sr / HOP
    curve = [{"time_sec": round(i / fps, 1), "rms": round(float(v), 4)}
             for i, v in enumerate(rms[:: max(1, int(fps))])]  # ~1s 分辨率

    return {
        "spectral_centroid_hz": round(float(np.mean(cent)), 1),
        "spectral_centroid_std_hz": round(float(np.std(cent)), 1),
        "spectral_bandwidth_hz": round(float(np.mean(bw)), 1),
        "spectral_flatness": round(float(np.mean(flat)), 4),
        "spectral_rolloff_hz": round(float(np.mean(rolloff)), 1),
        "harmonic_percussive_ratio": hp_ratio,
        "band_energy_ratios": bands,
        "rms_curve_1s": curve,
        "method": "librosa_stft_hpss_band_split",
    }


def _band_ratios(y: np.ndarray, sr: int) -> dict:
    """分频段能量占比：sub/low/mid/high/air。"""
    import librosa
    spec = np.abs(librosa.stft(y, hop_length=HOP)) ** 2
    freqs = librosa.fft_frequencies(sr=sr)
    total = float(spec.sum()) + 1e-12
    edges = [("sub_bass_lt60", 0, 60), ("low_60_250", 60, 250),
             ("mid_250_2000", 250, 2000), ("high_2000_8000", 2000, 8000),
             ("air_gt8000", 8000, freqs[-1])]
    out = {}
    for name, lo, hi in edges:
        idx = (freqs >= lo) & (freqs <= hi)
        out[name] = round(float(spec[idx].sum()) / total, 4)
    return out


def segment_band_summary(y: np.ndarray, sr: int, t0: float, t1: float) -> dict:
    """段落级频谱摘要，供段落 DNA 用。"""
    import librosa
    f0, f1 = int(t0 * sr), min(int(t1 * sr), len(y))
    seg = y[f0:f1]
    if len(seg) < sr // 4:
        return {}
    cent = librosa.feature.spectral_centroid(y=seg, sr=sr, hop_length=HOP)[0]
    rms = librosa.feature.rms(y=seg, hop_length=HOP)[0]
    return {
        "centroid_hz": round(float(np.mean(cent)), 1),
        "rms_mean": round(float(np.mean(rms)), 4),
        "rms_std": round(float(np.std(rms)), 4),
    }
