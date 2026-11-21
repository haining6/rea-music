"""调性引擎：调式判定与调性区域（转调探测）。

Krumhansl-Schmuckler 与 Temperley(Kostka-Payne) 双 profile 相关系数融合。
全局调性 + 滑窗局部调性区域，方法与置信度如实记录。
"""

from __future__ import annotations

import numpy as np

KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
TEMPERLEY_MAJOR = np.array([0.748, 0.44, 0.95, 0.446, 0.745, 0.613, 0.323, 0.898, 0.552, 0.571, 0.488, 0.507])
TEMPERLEY_MINOR = np.array([0.713, 0.466, 0.749, 0.613, 0.419, 0.576, 0.417, 0.783, 0.606, 0.537, 0.449, 0.598])

PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# chroma 总能量的"有音高内容"下限：全零/近零（静音、纯噪声残差）时 12 个调
# 相关系数全为 0，此时任何调性结论都是 max() 的字典序偶然结果（实测静音被
# 报成 B minor），必须如实记 None。
_MIN_CHROMA_ENERGY = 1e-6
METHOD = "krummhansl_schmuckler+temperley_cqt_chroma_fusion"


def _chroma(y: np.ndarray, sr: int) -> np.ndarray:
    """HPSS 谐波层 chroma：隔离底鼓/军鼓等打击乐对音级分布的污染。"""
    import librosa
    harmonic, _percussive = librosa.effects.hpss(y)
    tuning = librosa.estimate_tuning(y=harmonic, sr=sr)
    return librosa.feature.chroma_cqt(y=harmonic, sr=sr, hop_length=512,
                                      tuning=tuning)


def _score_key(chroma_mean: np.ndarray, profile: np.ndarray) -> np.ndarray | None:
    """12 个移位的 profile 相关系数（对全 24 调由调用方分别给 major/minor profile）。

    Returns:
        长度 12 的相关系数数组；输入零方差/含 NaN 时返回 None —— 全零分数没有
        任何判别力，交回调用方记 missing_reason，而不是挑一个"最高分"。
    """
    c = chroma_mean - chroma_mean.mean()
    if not np.isfinite(c).all():
        return None
    c_norm = float(np.linalg.norm(c))
    if c_norm <= 1e-9:
        return None
    scores = np.zeros(12)
    for shift in range(12):
        p = np.roll(profile, shift)
        p = p - p.mean()
        scores[shift] = float(np.dot(c, p) / (c_norm * (np.linalg.norm(p) + 1e-9)))
    return scores


def _key_name(tonic_idx: int, mode: str) -> str:
    return f"{PITCH_NAMES[tonic_idx % 12]} {mode}"


def analyze_key(y: np.ndarray, sr: int, window_sec: float = 24.0,
                hop_sec: float | None = None) -> dict:
    chroma = _chroma(y, sr)
    if chroma.size == 0:
        return _no_pitch_result()
    if not np.isfinite(chroma).all():
        chroma = np.nan_to_num(chroma)
    if not _has_pitch_content(chroma):
        return _no_pitch_result()
    fps = sr / 512
    global_mean = chroma.mean(axis=1)

    results = {}
    for name, maj, mi in (("krummhansl", KS_MAJOR, KS_MINOR),
                          ("temperley", TEMPERLEY_MAJOR, TEMPERLEY_MINOR)):
        maj_scores = _score_key(global_mean, maj)
        min_scores = _score_key(global_mean, mi)
        if maj_scores is None or min_scores is None:
            return _no_pitch_result()
        all_scores = [(float(s), i, "major") for i, s in enumerate(maj_scores)] + \
                     [(float(s), i, "minor") for i, s in enumerate(min_scores)]
        best = max(all_scores)
        results[name] = best

    # 融合：两 profile 是否一致（一致时取 KS 结果的 tonic/mode 二元组）
    ks_best, tp_best = results["krummhansl"], results["temperley"]
    agree = (ks_best[1] == tp_best[1] and ks_best[2] == tp_best[2])
    tonic, mode = (ks_best[1:] if agree else
                   max([ks_best, tp_best], key=lambda r: r[0])[1:])
    key_name = _key_name(tonic, mode)
    strength = float((ks_best[0] + tp_best[0]) / 2.0)
    confidence = float(np.clip((strength - 0.4) / 0.5, 0.1, 0.9))
    if agree:
        confidence = min(confidence + 0.1, 0.92)

    # 调性区域（转调）：滑窗逐窗判定
    hop_sec = hop_sec or window_sec / 2
    win_frames = int(window_sec * fps)
    hop_frames = max(1, int(hop_sec * fps))
    regions = []
    if chroma.shape[1] > win_frames:
        prev = None
        for start in range(0, chroma.shape[1] - win_frames + 1, hop_frames):
            seg_mean = chroma[:, start:start + win_frames].mean(axis=1)
            maj_scores = _score_key(seg_mean, KS_MAJOR)
            min_scores = _score_key(seg_mean, KS_MINOR)
            if maj_scores is None or min_scores is None:
                continue                      # 该窗无音高内容：不产出区域结论
            best = max([(float(s), i, "major") for i, s in enumerate(maj_scores)] +
                       [(float(s), i, "minor") for i, s in enumerate(min_scores)])
            cur = (best[1], best[2])
            t0 = start / fps
            if prev and prev["key"] == _key_name(*cur):
                prev["end_sec"] = round(t0 + window_sec, 1)
            else:
                if prev:
                    regions.append(prev)
                prev = {"key": _key_name(*cur), "start_sec": round(t0, 1),
                        "end_sec": round(t0 + window_sec, 1),
                        "strength": round(best[0], 3)}
                prev["_tonic_mode"] = cur
        if prev:
            regions.append(prev)
        # 清理内部字段
        for r in regions:
            r.pop("_tonic_mode", None)

    # 保留"相邻同调压缩"作为对外契约的防御层：正常路径上 regions 已在上面按
    # key 合并，这里不会真正丢弃任何项；若未来改成每窗一条记录则自动生效。
    distinct = [r for i, r in enumerate(regions)
                if i == 0 or r["key"] != regions[i - 1]["key"]]
    return {
        "key": key_name,
        "tonic_pitch_class": PITCH_NAMES[tonic % 12],
        "mode": mode,
        "confidence": round(confidence, 2),
        "profiles_agree": agree,
        "strength_krummhansl": round(ks_best[0], 3),
        "strength_temperley": round(tp_best[0], 3),
        "key_regions": distinct,
        "method": METHOD,
    }


def _has_pitch_content(chroma: np.ndarray) -> bool:
    """chroma 是否有实际音高能量（静音/全零返回 False）。"""
    if chroma.size == 0:
        return False
    mean = chroma.mean(axis=1)
    if not np.isfinite(mean).all():
        return False
    return float(np.sum(mean)) > _MIN_CHROMA_ENERGY


def _no_pitch_result() -> dict:
    """无音高内容时的诚实缺省：不给具体调性，只说明为什么测不出。"""
    return {
        "key": None,
        "tonic_pitch_class": None,
        "mode": None,
        "confidence": 0.0,
        "profiles_agree": False,
        "strength_krummhansl": 0.0,
        "strength_temperley": 0.0,
        "key_regions": [],
        "missing_reason": "no_pitch_content",
        "method": METHOD,
    }
