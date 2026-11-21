"""曲式引擎：特征窗口池化 + 聚类边界 + 功能标签启发式。

实现细节：
- 帧级特征（MFCC+chroma+RMS）按 ~2s 窗口中位数池化，sklearn Ward 层次聚类
  选 k（轮廓系数），边界取标签变化点。
- 功能标签（intro/verse/chorus 等）由 位置+能量+重复度 启发式给出，
  confidence ≤ 0.55，method 标注 heuristic_v1。Deep 档接入更强引擎后覆盖。
"""

from __future__ import annotations

import numpy as np

HOP = 512
_CLUSTER_LETTERS = "ABCDEFGH"


def analyze_structure(y: np.ndarray, sr: int, beats_time: list[float],
                      min_segment_sec: float = 8.0) -> dict:
    """段落级曲式分析（Ward 聚类 + 功能标签启发式）。

    Args:
        y: 单声道时域信号。
        sr: 采样率。
        beats_time: 拍点时间（当前实现不用于聚类，保留以便未来对齐段落边界）。
        min_segment_sec: 段落最短时长（秒）。
    """
    import librosa
    fps = sr / HOP
    duration = len(y) / sr
    if duration < min_segment_sec * 2:
        return {"segments": [], "confidence": 0.0, "method": "heuristic_v1",
                "missing_reason": "track_too_short_for_structure"}

    mfcc = librosa.feature.mfcc(y=y, sr=sr, hop_length=HOP, n_mfcc=13)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=HOP)
    rms = librosa.feature.rms(y=y, hop_length=HOP)[0]
    n_frames = min(mfcc.shape[1], chroma.shape[1], len(rms))
    feats = np.vstack([mfcc[:, :n_frames], chroma[:, :n_frames],
                       rms[None, :n_frames]]).T  # (frames, 26)

    # 池化窗对齐段落尺度（8s ≈ 典型和弦循环周期），窗口内均值
    # 使和弦循环在窗口内平均掉，只留段落级差异驱动聚类
    pool_sec = max(min_segment_sec, 8.0)
    win = max(1, round(pool_sec * fps))  # round 而非 int：避免截断把窗期缩到阈值以下
    n_win = n_frames // win
    if n_win < 4:
        return {"segments": [], "confidence": 0.0, "method": "heuristic_v1",
                "missing_reason": "insufficient_feature_windows"}
    pooled = feats[: n_win * win].reshape(n_win, win, -1)
    points = pooled.mean(axis=1)  # (n_win, 26) 均值池化：和弦循环在窗内平均
    points = points - points.mean(axis=0, keepdims=True)
    std = points.std(axis=0, keepdims=True)
    points = points / (std + 1e-9)

    # Ward 聚类，轮廓系数选 k（均值池化后段落差异主导特征空间）
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics import silhouette_score
    best_labels, best_score = None, -2.0
    for k in range(2, min(7, n_win // 2 + 1)):
        labels = AgglomerativeClustering(n_clusters=k, linkage="ward") \
            .fit_predict(points)
        if len(set(labels)) < 2:
            continue
        score = float(silhouette_score(points, labels))
        if score > best_score:
            best_labels, best_score = labels, score
    if best_labels is None:
        return {"segments": [], "confidence": 0.0, "method": "heuristic_v1",
                "missing_reason": "clustering_failed"}
    win_labels = best_labels

    # 窗口标签 → 边界（合并短段）
    bounds = [(0.0, win_labels[0])]
    for i in range(1, n_win):
        t = i * win / fps
        if win_labels[i] != win_labels[i - 1]:
            if t - bounds[-1][0] >= min_segment_sec - 0.05:  # 帧量化容差
                bounds.append((round(t, 2), win_labels[i]))
            else:  # 过短段并入前段（保留前标签）
                continue
        else:
            bounds[-1] = (bounds[-1][0], win_labels[i])
    bounds.append((round(duration, 2), bounds[-1][1]))

    # 段落摘要 + 功能标签
    segments = []
    for i in range(len(bounds) - 1):
        t0 = bounds[i][0]
        t1 = bounds[i + 1][0] if i + 1 < len(bounds) - 1 else round(duration, 2)
        t1 = min(t1, duration)
        f0, f1 = int(t0 * fps), min(int(t1 * fps), n_frames)
        sl = slice(f0, max(f1, f0 + 1))
        energy = float(rms[sl].mean())
        segments.append({"start_sec": round(t0, 2), "end_sec": round(t1, 2),
                         "rms": round(energy, 4),
                         "cid": int(bounds[i + 1][1]) if i + 1 < len(bounds) else int(bounds[-1][1])})
    for i, seg in enumerate(segments):
        seg["cid"] = bounds[i][1] if i < len(bounds) - 1 else seg["cid"]

    max_energy = max(s["rms"] for s in segments) or 1e-9
    cids = sorted({s["cid"] for s in segments})
    energy_by_cluster = {c: float(np.mean([s["rms"] for s in segments
                                           if s["cid"] == c])) for c in cids}
    energy_sorted = sorted(cids, key=lambda c: -energy_by_cluster[c])
    label_map: dict[int, str] = {}
    if energy_sorted:
        label_map[energy_sorted[0]] = "chorus" if len(cids) >= 3 else "verse"
        if len(energy_sorted) > 1:
            label_map[energy_sorted[1]] = "verse"
        if len(energy_sorted) > 2:
            label_map[energy_sorted[-1]] = "bridge"

    for idx, seg in enumerate(segments):
        base = label_map.get(seg["cid"],
                             f"section_{_CLUSTER_LETTERS[seg['cid'] % 8]}")
        if idx == 0 and seg["rms"] < 0.75 * max_energy:
            base = "intro"
        if idx == len(segments) - 1 and idx != 0 and seg["rms"] < 0.7 * max_energy:
            base = "outro"
        seg["label"] = base
        seg["label_confidence"] = round(0.35 + 0.2 * (seg["rms"] / max_energy), 2)
        seg.pop("cid", None)

    return {
        "segments": segments,
        "boundary_count": max(len(segments) - 1, 0),
        "confidence": round(float(np.clip(0.35 + best_score * 0.2, 0.2, 0.55)), 2),
        "method": "ward_clustering_silhouette+energy_position_heuristic",
        "engine": "heuristic_v1",
        "notes": "功能标签为启发式降级实现；Deep 档接入结构模型后置信度可提升",
    }
