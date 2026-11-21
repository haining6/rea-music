"""节奏引擎：BPM、拍点、下拍启发式、置信度、局部速度曲线。

方法 honesty：
- BPM/拍点来自 librosa 动态规划节拍追踪（onset 包络）。
- 倍频仲裁（R6 重写）：候选 BPM = DP 基准 × {1/3, 1/2, 1, 3/2, 2, 3}（限 40-240），
  逐候选算 onset 包络滞后自相关，先按谐波族聚合，再乘对数正态节奏先验取最高者：

      harmonic_support(b) = ac(b) + ac(b/2)/2 + ac(b/3)/3   （仅取候选集内谐波）
      score(b)            = harmonic_support(b) × lognormal_prior(b)   （中心 110，σ=0.45）

  为什么两者都要：
  · 只用"最强自相关"：真实拍级候选的自相关常低于半拍候选一倍以上（实测
    rock 夹具 74.9→0.96 vs 149.8→0.49），必然把快歌读成半速。
  · 只用先验加权：半拍候选缺慢速谐波支持，会被先验压下去；但 8 分音符层级的
    倍频候选（实测 yue2 真曲 184.6）靠 raw ac 仍能反超真实拍级 92.3（0.247 vs
    0.224），单靠先验压不住（110 附近先验差只有 ~2 倍）。
  · 谐波聚合让真实拍级拿到"慢速谐波支持票"，先验再压住过快的倍频候选，
    两个方向同时收敛。
- 倍频歧义如实外化：主读数与次候选加权分相对差 < 8% ⇒ confidence 封顶 0.5、
  octave_ambiguity=True。两可时不许包装成确定结论。
- 下拍与拍号为能量重音启发式，置信度封顶 0.55，明确标注 heuristic_v1。
"""

from __future__ import annotations

import numpy as np

HOP = 512

# 节奏先验：对数正态核中心与对数尺度（60-180 BPM 区间内权重 ≥0.4，只压极端倍频）
TEMPO_PRIOR_CENTER = 110.0
TEMPO_PRIOR_SIGMA = 0.45
# 谐波族聚合权重（sub-harmonic summation）：1、1/2、1/3 阶
_HARMONIC_WEIGHTS = (1.0, 0.5, 1.0 / 3.0)
# 谐波匹配容差：候选 BPM 相对差 3% 内视为同一谐波（候选间有浮点/取整误差）
_HARMONIC_MATCH_TOL = 0.03
# 倍频歧义：主/次候选加权分相对差阈值与置信度封顶
OCTAVE_AMBIGUITY_MARGIN = 0.08
OCTAVE_AMBIGUITY_CONF_CAP = 0.5
# 置信度：加权分差 40% 视为"完全清晰"（margin 归一满刻度）
_MARGIN_FULL_SCALE = 0.4
# 拍点证据门控（C6）：onset 包络峰值相对信号 RMS 的比值。实测 0.30（稳态正弦）
# vs 9.97（带限噪声）/152+（真实曲目），阈值 1.0 有足够安全边际。
# onset_strength 以 per-frame max 归一，故该比值与整体音量无关（-40dB 素材仍满值）。
_MIN_TRANSIENT_RATIO = 1.0
_MIN_ONSET_FRAMES = 8


def _has_pulse_evidence(y: np.ndarray, env: np.ndarray) -> bool:
    """是否存在可分析的交变瞬态证据（无证据时不该硬报 BPM）。

    Args:
        y: 单声道时域信号（未归一化）。
        env: onset 包络。

    Returns:
        True 表示包络里有实际瞬态可追踪；False 表示静音/稳态音等退化输入。
    """
    if env.size < _MIN_ONSET_FRAMES:
        return False
    peak = float(np.max(env))
    if not np.isfinite(peak) or peak <= 1e-9:
        return False
    signal = np.asarray(y, dtype=np.float64)
    rms = float(np.sqrt(np.mean(signal ** 2))) if signal.size else 0.0
    if rms <= 1e-12:
        return False                      # 全零/近零信号
    return (peak / rms) >= _MIN_TRANSIENT_RATIO


def _apply_clarity(confidence: float, clarity: float | None) -> float:
    """把拍点对齐清晰度折进置信度（clarity>=1 时不改变结论）。

    清晰度 = 拍点处包络均值 / 全局均值：真实节拍器素材实测 4.05-23.59，
    退化素材接近 0。因子下限 0.75，避免单凭一个辅助指标把结论压成 0。
    """
    if clarity is None or not np.isfinite(clarity):
        return confidence
    factor = 0.75 + 0.25 * float(np.clip(clarity, 0.0, 1.0))
    return float(np.clip(confidence * factor, 0.0, 0.95))


def _no_pulse_result(method: str = "librosa_dp_beat_tracking+autocorr_octave_arbitration"
                     ) -> dict:
    """无拍点证据时的诚实缺省（所有字段齐全，下游无需特判）。"""
    return {
        "bpm": None,
        "confidence": 0.0,
        "missing_reason": "no_pulse_detected",
        "beat_count": 0,
        "beats_time": [],
        "meter": None,
        "meter_confidence": 0.0,
        "downbeats_time": [],
        "tempo_candidates": [],
        "tempo_curve_sec_bpm": [],
        "octave_ambiguity": False,
        "beat_clarity": None,
        "method": method,
    }


def _onset_env(y: np.ndarray, sr: int) -> np.ndarray:
    import librosa
    return librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)


def _rms_env(y: np.ndarray, sr: int) -> np.ndarray:
    import librosa
    return librosa.feature.rms(y=y, hop_length=HOP)[0]


def _pulse_clarity(env: np.ndarray, period_frames: float) -> float:
    """按给定周期的脉冲清晰度：对齐均值 / 全局均值。"""
    if period_frames < 2 or len(env) < int(period_frames) * 2:
        return 0.0
    idx = np.arange(0, len(env) - 1, max(period_frames, 1.0))
    idx = np.round(idx).astype(int)
    return float(env[idx].mean() / (env.mean() + 1e-9))


def _autocorr_scores(env: np.ndarray, sr: int, bpms: list[float]) -> dict[float, float]:
    """自相关仲裁：BPM 候选对应的包络滞后自相关值（线性插值到非整数 lag）。"""
    seg = env - env.mean()
    ac = np.correlate(seg, seg, "full")[len(env) - 1:]
    if len(ac) < 2:
        return {}
    ac = ac / (ac[0] + 1e-9)
    fps = sr / HOP
    scores = {}
    for bpm in bpms:
        lag = 60.0 / bpm * fps
        if lag < 2 or lag >= len(ac):
            continue
        lo = int(np.floor(lag))
        frac = lag - lo
        val = float((1 - frac) * ac[lo] + frac * ac[min(lo + 1, len(ac) - 1)])
        scores[bpm] = val
    return scores


def _autocorr_period(env: np.ndarray, sr: int, bpm: float) -> float:
    """BPM 对应的帧周期。"""
    fps = sr / HOP
    return 60.0 / bpm * fps


def _lognormal_prior(bpm: float) -> float:
    """对数正态节奏先验核（未归一化，∈(0, 1]，峰值在 TEMPO_PRIOR_CENTER）。

    取对数核 exp(-0.5·z²) 而非完整 pdf：倍频关系在 log 域是等距平移（×2 / ÷2
    对称），1/bpm 的归一化因子会给所有快速候选再加一层无信息惩罚，使快歌
    更易被拉回半速（实测 174 BPM 用例会被拉低）。这里只保留"离常见速度多远"
    这一个信息。
    """
    if bpm <= 0:
        return 0.0
    z = float(np.log(bpm / TEMPO_PRIOR_CENTER)) / TEMPO_PRIOR_SIGMA
    return float(np.exp(-0.5 * z * z))


def _harmonic_support(ac_scores: dict[float, float]) -> dict[float, float]:
    """把候选自相关按谐波族聚合（sub-harmonic summation）。

    support(b) = ac(b) + ac(b/2)/2 + ac(b/3)/3，只聚合候选集内可匹配的谐波
    （不额外扫 lag 网格，避免引入未定义候选）。语义：一个候选若真是拍级，
    它的 2 倍、3 倍周期（即 b/2、b/3 的候选）必然同样周期，二者共振；
    只有单点自相关高的候选（8 分音符包络、噪声泄漏）拿不到这些票。
    """
    if not ac_scores:
        return {}
    bpms = sorted(ac_scores)
    support: dict[float, float] = {}
    for bpm in bpms:
        total = ac_scores[bpm]
        for k, weight in enumerate(_HARMONIC_WEIGHTS[1:], start=2):
            target = bpm / k
            nearest = min(bpms, key=lambda cand: abs(cand - target))
            if abs(nearest - target) <= _HARMONIC_MATCH_TOL * target:
                total += weight * ac_scores[nearest]
        support[bpm] = float(total)
    return support


def arbitrate_tempo(base_bpm: float,
                    ac_scores: dict[float, float]) -> dict:
    """倍频仲裁：谐波族聚合 + 对数正态先验加权。

    Args:
        base_bpm: librosa DP 节拍追踪给出的基准 BPM（无证据时作为兜底）。
        ac_scores: {candidate_bpm: 滞后自相关}，由 _autocorr_scores 产出。

    Returns:
        {bpm, support, weighted, prior_weighted, margin, octave_ambiguity,
         confidence}
        · support/weighted 是主读数的两个分量，margin 是主/次候选加权相对差。
        · prior_weighted 是 {bpm: 加权分}，供 tempo_candidates 外化证据。
        · confidence 已含"倍频歧义封顶"（<8% 分差时 ≤0.5）。
    """
    if not ac_scores:
        return {"bpm": base_bpm, "support": None, "weighted": None,
                "prior_weighted": {}, "margin": None,
                "octave_ambiguity": False,
                "confidence": _tempo_confidence(None, None)}

    support = _harmonic_support(ac_scores)
    prior_weighted = {bpm: support[bpm] * _lognormal_prior(bpm) for bpm in support}
    # 排序：加权分高者优先；同分时离 DP 基准近者优先（并列不无证据翻倍频）
    ranked = sorted(prior_weighted.items(),
                    key=lambda item: (-item[1], abs(item[0] - base_bpm), item[0]))
    best_bpm, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0

    if best_score <= 0:
        # 无正向周期性证据：不猜，回退 DP 基准并由上层给低置信度
        return {"bpm": base_bpm, "support": support.get(base_bpm),
                "weighted": None, "prior_weighted": prior_weighted,
                "margin": None, "octave_ambiguity": False,
                "confidence": _tempo_confidence(None, None)}

    margin = (best_score - second_score) / best_score if len(ranked) > 1 else 1.0
    octave_ambiguity = bool(margin < OCTAVE_AMBIGUITY_MARGIN)
    confidence = _tempo_confidence(float(support[best_bpm]), float(margin))
    if octave_ambiguity:
        confidence = min(confidence, OCTAVE_AMBIGUITY_CONF_CAP)
    return {
        "bpm": float(best_bpm),
        "support": float(support[best_bpm]),
        "weighted": float(best_score),
        "prior_weighted": prior_weighted,
        "margin": float(margin),
        "octave_ambiguity": octave_ambiguity,
        "confidence": float(confidence),
    }


def _tempo_confidence(support: float | None, margin: float | None) -> float:
    """置信度 = 0.30 基础分 + 0.45 决策清晰度 + 0.25 周期强度。

    基础分表示"确实检出拍点"；清晰度看主/次候选加权分差；强度看胜出候选的
    谐波聚合支持度（封顶 1）。三档证据都不足时自然落到低分。
    """
    if support is None or margin is None:
        return 0.5  # 无自相关证据：只保留"检出拍点"这一条，不给高分
    strength = float(np.clip(support, 0.0, 1.0))
    clarity = float(np.clip(margin / _MARGIN_FULL_SCALE, 0.0, 1.0))
    return float(np.clip(0.30 + 0.45 * clarity + 0.25 * strength, 0.05, 0.95))


def analyze_tempo(y: np.ndarray, sr: int) -> dict:
    import librosa

    me = "librosa_dp_beat_tracking+autocorr_octave_arbitration"
    env = _onset_env(y, sr)
    # 门控：静音/稳态音/过短输入没有可追踪的瞬态，直接如实报"未检出"，
    # 而不是让近常量包络在自相关里"看起来完美周期性"（会把噪声读成高置信 BPM）。
    if not _has_pulse_evidence(y, env):
        return _no_pulse_result(me)

    tempo, beat_frames = librosa.beat.beat_track(
        y=None, sr=sr, onset_envelope=env, hop_length=HOP, trim=False)
    base_bpm = float(np.atleast_1d(tempo)[0])
    if base_bpm <= 0:
        return _no_pulse_result(me)

    # 倍频候选：DP 基准的 1/3、1/2、1、3/2、2、3 倍
    cand_bpms = [base_bpm * m for m in (1 / 3, 1 / 2, 1, 3 / 2, 2, 3)]
    cand_bpms = [b for b in cand_bpms if 40 <= b <= 240]
    ac_scores = _autocorr_scores(env, sr, cand_bpms)
    arbitration = arbitrate_tempo(base_bpm, ac_scores)
    bpm = arbitration["bpm"]
    prior_weighted = arbitration["prior_weighted"]

    candidates = [{"bpm": round(b, 1),
                   "pulse_clarity": round(_pulse_clarity(
                       env, _autocorr_period(env, sr, b)), 3),
                   "autocorr": round(ac_scores.get(b, 0.0), 3),
                   "prior_weighted": round(prior_weighted.get(b, 0.0), 3)}
                  for b in cand_bpms]

    # 以裁决后的 BPM 重定位拍点
    if abs(bpm - base_bpm) > 0.5:
        period = _autocorr_period(env, sr, bpm)
        phases = np.arange(int(period))
        best_phase, best_score = 0, -1.0
        for ph in phases:
            idx = np.round(np.arange(ph, len(env), max(period, 1.0))).astype(int)
            idx = idx[idx < len(env)]
            if idx.size == 0:
                continue
            score = float(env[idx].mean())
            if score > best_score:
                best_phase, best_score = ph, score
        beat_frames = np.round(best_phase + np.arange(0, len(env) - best_phase, period)).astype(int)
        beat_frames = beat_frames[beat_frames < len(env)]

    beats_time = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)

    # 拍点对齐清晰度：拍点处包络均值 / 全局均值。折进置信度，避免"自相关高但
    # 拍点落在能量谷"的素材被包装成确定结论。
    beat_clarity = _beat_aligned_clarity(env, beat_frames)
    confidence = _apply_clarity(arbitration["confidence"], beat_clarity)

    # 下拍启发式：能量包络最强的拍位为 1（RMS 帧值在拍点采样）
    low_env = _rms_env(y, sr)
    beat_env = low_env[np.clip(beat_frames, 0, len(low_env) - 1)] if len(beat_frames) else np.array([])
    meter, downbeat_positions, meter_conf = _guess_meter(beat_env)
    downbeats_time = [float(beats_time[i]) for i in downbeat_positions]

    # 局部速度曲线：8s 窗口自相关主周期
    curve = _tempo_curve(env, sr, window_sec=8.0, hop_sec=4.0)

    return {
        "bpm": round(bpm, 1),
        "confidence": round(confidence, 2),
        "beat_count": int(len(beats_time)),
        "beats_time": [round(float(b), 3) for b in beats_time],
        "meter": meter,
        "meter_confidence": meter_conf,
        "downbeats_time": [round(d, 3) for d in downbeats_time],
        "tempo_candidates": sorted(candidates,
                                   key=lambda c: -c["prior_weighted"])[:4],
        "tempo_curve_sec_bpm": curve,
        "octave_ambiguity": arbitration["octave_ambiguity"],
        "beat_clarity": round(float(beat_clarity), 3)
        if beat_clarity is not None else None,
        "octave_arbitration": {
            "prior_center_bpm": TEMPO_PRIOR_CENTER,
            "prior_sigma_log": TEMPO_PRIOR_SIGMA,
            "dp_base_bpm": round(base_bpm, 1),
            "chosen_weighted": round(arbitration["weighted"], 4)
            if arbitration["weighted"] is not None else None,
            "runner_up_margin": round(arbitration["margin"], 4)
            if arbitration["margin"] is not None else None,
        },
        "method": "librosa_dp_beat_tracking+autocorr_octave_arbitration",
    }


def _beat_aligned_clarity(env: np.ndarray, beat_frames) -> float | None:
    """拍点处的包络均值 / 全局均值（无拍点返回 None）。"""
    frames = np.asarray(beat_frames, dtype=int)
    if frames.size == 0 or env.size == 0:
        return None
    frames = frames[(frames >= 0) & (frames < len(env))]
    if frames.size == 0:
        return None
    return float(env[frames].mean() / (env.mean() + 1e-9))


def _guess_meter(beat_env: np.ndarray) -> tuple[int | None, list[int], float]:
    """拍号启发式：比较 3 拍与 4 拍周期的重音自相关。无拍点时返回 None。"""
    if len(beat_env) < 8:
        return None, [], 0.0
    env = beat_env - beat_env.mean()
    def score(m: int) -> float:
        lag = m
        if len(env) <= lag:
            return -1.0
        return float(np.dot(env[:-lag], env[lag:]) / (np.linalg.norm(env[:-lag]) * np.linalg.norm(env[lag:]) + 1e-9))
    s3, s4 = score(3), score(4)
    meter = 4 if s4 >= s3 else 3
    conf = float(np.clip(abs(s4 - s3) * 2.0, 0.15, 0.55))
    # 下拍 = 每周期内重音最大的拍
    phase_scores = []
    for ph in range(meter):
        idx = list(range(ph, len(beat_env), meter))
        if idx:
            phase_scores.append((float(beat_env[idx].mean()), ph))
    if phase_scores:
        best_phase = max(phase_scores)[1]
    else:
        best_phase = 0
    positions = list(range(best_phase, len(beat_env), meter))
    return meter, positions, round(conf, 2)


def _tempo_curve(env: np.ndarray, sr: int, window_sec: float, hop_sec: float) -> list:
    fps = sr / HOP
    win = int(window_sec * fps)
    hop = int(hop_sec * fps)
    curve = []
    if len(env) < win:
        return curve
    for start in range(0, len(env) - win + 1, hop):
        seg = env[start:start + win] - env[start:start + win].mean()
        ac = np.correlate(seg, seg, "full")[win - 1:]
        if len(ac) < 2:
            continue
        ac[0] = 0  # 去掉零滞后
        lag = int(np.argmax(ac[1:])) + 1
        if lag < 2 or ac[lag] <= 0:
            continue
        bpm = 60.0 * fps / lag
        if 40 <= bpm <= 240:
            curve.append({"time_sec": round(start / fps, 1),
                          "bpm": round(bpm, 1)})
    return curve
