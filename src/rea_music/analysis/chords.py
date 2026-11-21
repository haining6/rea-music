"""和弦引擎：chordotomy（主）+ 模板匹配两阶段（后备）。

方法 honesty：chordotomy（MIT, DSP 引擎，beat-aligned）为默认主引擎；
模板 v2 为自研两阶段模板匹配后备。config analysis.chords_engine:
auto|chordotomy|template 切换（auto = chordotomy 优先失败回退模板，
engine 字段如实标注实际来源）。
"""

from __future__ import annotations

import numpy as np

HOP = 512

# 和弦识别两阶段：先三和弦模板（maj/min/dim/aug/sus4），再按频谱证据
# 决定是否扩展 7 音（7/maj7/m7）。直接匹配 7 和弦模板时缺失音惩罚不足，
# 会系统性误报（实测合成曲 C:maj 被判 C:maj7），故改为两阶段。
_TRIADS: list[tuple[str, np.ndarray]] = []
_TRIAD_INTERVALS = {
    "maj": [0, 4, 7], "min": [0, 3, 7], "dim": [0, 3, 6],
    "aug": [0, 4, 8], "sus4": [0, 5, 7],
}
for suffix, intervals in _TRIAD_INTERVALS.items():
    t = np.zeros(12)
    for i in intervals:
        t[i % 12] += 1.0
    _TRIADS.append((suffix, t))

_EXTENSIONS = {
    ("maj", 10): "7", ("maj", 11): "maj7",
    ("min", 10): "m7", ("min", 7): "min_maj7",
}
_EXT_THRESHOLD = 0.35  # 扩展音需达到根音 chroma 能量的 35% 才承认

ENGINES = ("auto", "chordotomy", "template")


def _chord_label(tonic_idx: int, suffix: str) -> str:
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[tonic_idx % 12]}:{suffix}"


def _bass_chroma(y: np.ndarray, sr: int) -> np.ndarray:
    """低频段 chroma，用于根音判别。"""
    import librosa
    from scipy.signal import butter, sosfilt
    sos = butter(4, 260, btype="low", fs=sr, output="sos")
    y_low = sosfilt(sos, y)
    return librosa.feature.chroma_cqt(y=y_low, sr=sr, hop_length=HOP)


def chordotomy_available() -> bool:
    """向后兼容入口：chordotomy CLI 是否实测可用。"""
    from rea_music.adapters import chordotomy as _adapter
    return bool(_adapter.probe()["available"])


def _resolve_engine(engine: str | None) -> tuple[str, str | None]:
    """解析实际引擎：返回 (engine, config_note)。

    None → 读 config analysis.chords_engine（默认 auto）；
    非法值按 auto 处理并如实记录 note。
    """
    if engine is None:
        from rea_music.core.config import load_config
        cfg_val = ((load_config().get("analysis", {}) or {})
                   .get("chords_engine", "auto"))
        engine = cfg_val
    note = None
    if engine not in ENGINES:
        note = f"chords_engine 配置值 {engine!r} 非法，按 auto 处理"
        engine = "auto"
    return engine, note


def analyze_chords(y: np.ndarray, sr: int, beats_time: list[float],
                    duration_sec: float, external_engine_path: str | None = None,
                    engine: str | None = None
                    ) -> dict:
    """和弦分析。engine: auto|chordotomy|template（None 读 config 默认 auto）。

    auto：external_engine_path 提供且 chordotomy 可用 → 主引擎；
    失败/缺失 → 模板后备并如实标注 fallback 原因。
    chordotomy：显式主引擎，缺失/失败抛 CapabilityUnavailable。
    """
    from rea_music.adapters import chordotomy as chordotomy_adapter
    from rea_music.core.errors import CapabilityUnavailable

    resolved, note = _resolve_engine(engine)

    if resolved in ("auto", "chordotomy") and external_engine_path:
        status = chordotomy_adapter.probe()
        if status["available"]:
            try:
                ext = chordotomy_adapter.analyze(external_engine_path)
                out = dict(ext)  # 契约同构：engine/method/segments/...
                out["engine_requested"] = resolved
                if note:
                    out["engine_config_note"] = note
                return out
            except CapabilityUnavailable as exc:
                if resolved == "chordotomy":
                    raise  # 显式请求失败按约定抛 REAError 子类
                fallback_reason = f"chordotomy_failed: {exc.message}"
        else:
            if resolved == "chordotomy":
                raise CapabilityUnavailable(
                    "chordotomy 引擎不可用", details=status["details"],
                    hint=status["details"].get("hint") or
                         "analysis.chords_engine=auto 可自动回退模板引擎")
            fallback_reason = "chordotomy_unavailable"
        if resolved == "auto":
            result = _template_analyze(y, sr, beats_time, duration_sec)
            result["engine_fallback_note"] = fallback_reason
            if note:
                result["engine_config_note"] = note
            return result

    result = _template_analyze(y, sr, beats_time, duration_sec)
    if resolved == "template" and note:
        result["engine_config_note"] = note
    return result


def _template_analyze(y: np.ndarray, sr: int, beats_time: list[float],
                       duration_sec: float) -> dict:
    """模板 v2 两阶段匹配（既有 R1 路线，保持行为不变）。"""
    if not beats_time:
        return {"engine": "template_v2_two_stage", "confidence": 0.0,
                "segments": [], "progression": [], "method": "template_v2",
                "missing_reason": "no_beats"}

    import librosa
    harmonic, _perc = librosa.effects.hpss(y)  # 隔离打击乐对 chroma 的污染
    chroma = librosa.feature.chroma_cqt(y=harmonic, sr=sr, hop_length=HOP)
    bass = _bass_chroma(y, sr)
    fps = sr / HOP

    def _match(c: np.ndarray, b: np.ndarray) -> tuple[str, float, float]:
        """两阶段匹配：三和弦模板 + 7 音扩展检查。
        返回 (和弦标签, 归一化得分, top1-top2 裕度)。"""
        best_label, best_score, second = None, -1.0, -1.0
        c_norm = np.linalg.norm(c) + 1e-9
        best_root, best_suffix = 0, "maj"
        for root in range(12):
            for suffix, tmpl in _TRIADS:
                t = np.roll(tmpl, root)
                t = t / (np.linalg.norm(t) + 1e-9)
                score = float(np.dot(c, t) / c_norm)
                score += 0.15 * float(b[root])  # 低频根音加权
                if score > best_score:
                    second = best_score
                    best_root, best_suffix = root, suffix
                    best_label = _chord_label(root, suffix)
                    best_score = score
                elif score > second:
                    second = score
        # 第二阶段：7 音扩展证据（仅对 maj/min 判定，避免小词表误报）
        root_chroma = float(c[best_root] / (c.max() + 1e-9))
        if best_suffix in ("maj", "min") and root_chroma > 0:
            for (suffix_key, interval), ext in _EXTENSIONS.items():
                if suffix_key != best_suffix:
                    continue
                ext_energy = float(c[(best_root + interval) % 12] / (c.max() + 1e-9))
                if ext_energy >= _EXT_THRESHOLD:
                    best_label = _chord_label(best_root, ext)
                    break
        margin = float(best_score - second)
        return best_label, best_score, margin

    segments = []
    for i, t0 in enumerate(beats_time):
        t1 = beats_time[i + 1] if i + 1 < len(beats_time) else duration_sec
        f0, f1 = int(t0 * fps), min(int(t1 * fps), chroma.shape[1])
        if f1 - f0 < 2:
            continue
        c = chroma[:, f0:f1].mean(axis=1)
        b = bass[:, f0:f1].mean(axis=1)
        label, score, margin = _match(c, b)
        segments.append({"start_sec": round(float(t0), 2),
                         "end_sec": round(float(t1), 2),
                         "chord": label,
                         "score": round(score, 3),
                         "margin": round(margin, 3)})

    # 连段合并 + 裕度转置信度
    merged = []
    for seg in segments:
        if merged and merged[-1]["chord"] == seg["chord"]:
            merged[-1]["end_sec"] = seg["end_sec"]
            merged[-1]["margins"].append(seg["margin"])
        else:
            merged.append({"chord": seg["chord"], "start_sec": seg["start_sec"],
                           "end_sec": seg["end_sec"], "margins": [seg["margin"]]})
    out = []
    margins_all = []
    for m in merged:
        avg_margin = float(np.mean(m["margins"]))
        margins_all.append(avg_margin)
        out.append({"start_sec": m["start_sec"], "end_sec": m["end_sec"],
                    "chord": m["chord"],
                    "confidence": round(min(avg_margin * 2.5, 0.85), 2)})
    conf = round(float(np.clip(np.mean(margins_all) * 2.5, 0.1, 0.75)), 2) if margins_all else 0.0
    return {
        "engine": "template_v2_two_stage",
        "confidence": conf,
        "segments": out,
        "progression": _progression([m["chord"] for m in merged]),
        "method": "hpss_chroma_beat_synced_two_stage_templates_bass_weighted",
    }


def _progression(labels: list[str]) -> list[dict]:
    """最常见的 4 级和弦循环（n-gram 统计）。"""
    from collections import Counter
    n = 4
    grams = [tuple(labels[i:i + n]) for i in range(len(labels) - n + 1)]
    if not grams:
        return []
    top = Counter(grams).most_common(3)
    return [{"progression": list(g), "count": c} for g, c in top if c >= 2]
