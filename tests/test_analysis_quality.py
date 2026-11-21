"""分析质量回归（R6 T1）：BPM 倍频仲裁 + 人声存在度口径。

三篇证据都自造信号，不依赖网络；真实曲目用例仅在 data/artifacts 存在时跑
（本地生成工件不入库，缺失时 skip，不伪造结论）。

覆盖的缺陷：
1. BPM 双倍频误判：92 BPM 真曲被读成 184.6（raw 自相关 184.6 反而略高）。
2. 人声存在度误判：纯钢琴曲启发式 presence 0.85，与真带人声曲 0.90 无判别力。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # rea_music 包根在 sys.path

SR = 44100
ARTIFACTS = ROOT / "data" / "artifacts"
YUE2_REAL = ARTIFACTS / "yue2_live" / "01a1f8da.flac"
ACE_PIANO_REAL = ARTIFACTS / "ace_live" / "generated_piano_15s.flac"


# ---------- 合成素材 ----------

def backbeat(bpm: float, duration: float = 20.0, sr: int = SR) -> np.ndarray:
    """kick(每拍，强拍加重) + snare(2/4 拍) + 8 分音符 hat 的标准鼓组循环。

    固定随机种子，保证同参数下信号完全一致（自相关可复现）。
    """
    n = int(sr * duration)
    buf = np.zeros(n, dtype=np.float64)
    beat = 60.0 / bpm
    rng = np.random.default_rng(11)

    for i, t in enumerate(np.arange(0.0, duration, beat)):
        pos = int(t * sr)
        kick_len = int(0.12 * sr)
        tt = np.arange(kick_len) / sr
        kick = 0.9 * np.sin(2 * np.pi * 55.0 * tt) * np.exp(-tt * 30.0)
        amp = 1.0 if i % 4 == 0 else 0.75          # 4/4 强拍加重
        _mix_in(buf, amp * kick, pos)
        if i % 4 in (1, 3):                         # snare 落 2、4 拍
            snare_len = int(0.09 * sr)
            snare = (0.5 * rng.uniform(-1, 1, snare_len)
                     * np.exp(-np.arange(snare_len) / sr * 45.0))
            _mix_in(buf, snare, pos)

    for t in np.arange(0.0, duration, beat / 2.0):  # 8 分音符 hat
        pos = int(t * sr)
        hat_len = int(0.02 * sr)
        hat = (0.18 * rng.uniform(-1, 1, hat_len)
               * np.exp(-np.arange(hat_len) / sr * 160.0))
        _mix_in(buf, hat, pos)

    peak = float(np.max(np.abs(buf))) or 1.0
    return (buf / peak * 0.8).astype(np.float32)


def _mix_in(buf: np.ndarray, signal: np.ndarray, pos: int) -> None:
    if pos >= len(buf) or not len(signal):
        return
    end = min(pos + len(signal), len(buf))
    buf[pos:end] += signal[: end - pos]


def _band_limited_noise(seed: int, duration: float = 5.0,
                        center_hz: float = 800.0,
                        width: float = 0.6) -> np.ndarray:
    """中频带限噪声：模拟"居中、中频丰富"的器乐（激发启发式的误判路径）。"""
    rng = np.random.default_rng(seed)
    n = int(SR * duration)
    noise = rng.uniform(-1, 1, n)
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    band = np.exp(-0.5 * ((np.log(freqs + 1.0) - np.log(center_hz)) / width) ** 2)
    shaped = np.fft.irfft(spec * band, n)
    shaped /= float(np.max(np.abs(shaped))) or 1.0
    return (shaped * 0.7).astype(np.float32)


def _write_stereo_wav(path: Path, mono: np.ndarray, width: float = 0.0) -> Path:
    """写 44.1k 立体声 wav；width 控制左右差异（0=完全居中）。"""
    import soundfile as sf
    if width > 0.0:
        delayed = np.roll(mono, max(1, int(SR * 0.0005)))
        stereo = np.stack([mono + width * delayed, mono - width * delayed], axis=1)
    else:
        stereo = np.stack([mono, mono], axis=1)
    sf.write(str(path), stereo.astype(np.float32), SR, subtype="PCM_16")
    return path


# ---------- BPM：倍频仲裁（合成信号） ----------

def test_bpm_92_backbeat_stays_at_92():
    """92 BPM 鼓组：raw 自相关在 184.6（双倍频）更强，主读数必须落在 91-93。"""
    from rea_music.analysis.tempo import analyze_tempo
    result = analyze_tempo(backbeat(92.0), SR)
    assert result["bpm"] is not None
    assert 91.0 <= result["bpm"] <= 93.0, f"倍频误判: {result['bpm']}"
    assert result["confidence"] >= 0.6, f"置信度过低: {result['confidence']}"
    assert result["octave_ambiguity"] is False


def test_bpm_174_fast_song_not_pulled_down():
    """174 BPM 快歌：先验不得把快歌拉回半速（期望仍 >=170）。"""
    from rea_music.analysis.tempo import analyze_tempo
    result = analyze_tempo(backbeat(174.0), SR)
    assert result["bpm"] >= 170.0, (
        f"先验把快歌拉低了: {result['bpm']}；候选 "
        f"{[c['bpm'] for c in result['tempo_candidates']]}")


def test_arbitration_table_locks_yue2_octave_choice():
    """用真曲实测候选表锁仲裁决策（不依赖 librosa，防版本漂移掩盖回归）。

    数据来自 data/artifacts/yue2_live/01a1f8da.flac 的实测自相关：
    184.6 的 raw ac 0.247 高于 92.3 的 0.224——旧规则（最强候选 0.5 倍内取最快）
    必然选 184.6。真值是 92。
    """
    from rea_music.analysis.tempo import arbitrate_tempo
    verdict = arbitrate_tempo(92.3, {46.1: 0.183, 92.3: 0.224, 138.4: 0.073,
                                     184.6: 0.247})
    assert 91.0 <= verdict["bpm"] <= 93.0, verdict
    assert verdict["confidence"] >= 0.6, verdict
    assert verdict["octave_ambiguity"] is False
    # 证据外化：加权分里 184.6 的 raw ac 更高，但加权后被压下去
    weighted = verdict["prior_weighted"]
    assert weighted[92.3] > weighted[184.6]


def test_arbitration_marks_octave_ambiguity_and_caps_confidence():
    """主/次候选加权分差 <8% 时必须如实标歧义并把置信度压到 <=0.5。"""
    from rea_music.analysis import tempo as tempo_mod
    # 100 与 150 无谐波关系；0.74/0.92 使加权分差落在阈值内（实测约 2%）
    verdict = tempo_mod.arbitrate_tempo(100.0, {100.0: 0.74, 150.0: 0.92})
    assert verdict["margin"] < tempo_mod.OCTAVE_AMBIGUITY_MARGIN
    assert verdict["octave_ambiguity"] is True
    assert verdict["confidence"] <= tempo_mod.OCTAVE_AMBIGUITY_CONF_CAP


def test_arbitration_without_evidence_does_not_invent_tempo():
    """零自相关证据时不猜：回退 DP 基准且给低置信度。"""
    from rea_music.analysis.tempo import arbitrate_tempo
    verdict = arbitrate_tempo(101.5, {})
    assert verdict["bpm"] == 101.5
    assert verdict["confidence"] <= 0.5
    assert verdict["octave_ambiguity"] is False


# ---------- BPM：歧义标记透传进 DNA ----------

def test_dna_rhythm_bpm_carries_octave_ambiguity(tmp_path):
    """92 BPM 合成曲走 standard 管线：DNA 的 rhythm.bpm 必须带 octave_ambiguity。"""
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.core import jobs as jobs_mod
    from rea_music.dna.validate import validate

    src = _write_stereo_wav(tmp_path / "bpm92_standard.wav",
                            backbeat(92.0, duration=12.0), width=0.05)
    job = jobs_mod.create_job("quality", {"case": "octave_flag_pipeline"})
    path = jobs_mod.run_inline(pipeline_mod.analyze_track_job, job, str(src),
                               "standard", True)["result_ref"]
    dna = json.loads(Path(path).read_text(encoding="utf-8"))

    bpm_field = dna["rhythm"]["bpm"]
    assert "octave_ambiguity" in bpm_field, bpm_field   # 分析层结论已透传
    assert bpm_field["octave_ambiguity"] is False       # 92 BPM 决策明确
    assert 91.0 <= bpm_field["value"] <= 93.0
    assert validate(dna) == [], validate(dna)[:3]


def test_dna_octave_ambiguity_true_and_default_absent():
    """true 场景透传；来源不给该字段时缺省不写（旧工件校验不受影响）。"""
    from rea_music.dna.model import build_music_dna
    from rea_music.dna.validate import validate

    tempo_base = {"bpm": 100.0, "confidence": 0.5,
                  "method": "librosa_dp_beat_tracking+autocorr_octave_arbitration",
                  "beat_count": 8, "beats_time": [0.0, 0.6], "meter": 4,
                  "meter_confidence": 0.3, "downbeats_time": [0.0],
                  "tempo_candidates": [], "tempo_curve_sec_bpm": []}
    common = {"content_hash": "0" * 64, "meta": {"duration_sec": 10.0},
              "key": {}, "chords": None, "structure": None, "loudness": {},
              "spectral": {}, "vocal": None, "tier": "standard",
              "duration_sec": 10.0}

    ambiguous = build_music_dna(
        tempo={**tempo_base, "octave_ambiguity": True}, **common)
    assert ambiguous["rhythm"]["bpm"]["octave_ambiguity"] is True
    assert ambiguous["rhythm"]["bpm"]["confidence"] <= 0.5   # 歧义已反映在置信度
    assert validate(ambiguous) == [], validate(ambiguous)[:3]

    legacy = build_music_dna(tempo=dict(tempo_base), **common)
    assert "octave_ambiguity" not in legacy["rhythm"]["bpm"]  # 缺省不写
    assert validate(legacy) == [], validate(legacy)[:3]


# ---------- 人声：存在度口径 ----------

def test_presence_band_thresholds():
    from rea_music.analysis.vocal import presence_band
    assert presence_band(0.0) == "absent"
    assert presence_band(0.34) == "absent"
    assert presence_band(0.35) == "ambiguous"
    assert presence_band(0.65) == "ambiguous"
    assert presence_band(0.66) == "likely"
    assert presence_band(1.0) == "likely"
    assert presence_band(None) is None  # 缺值不猜


def test_vocal_heuristic_keeps_formula_but_labels_limits(tmp_path):
    """启发式数值公式不变，但要带 presence_band / presence_source / 边界说明。"""
    from rea_music.analysis.vocal import analyze_vocal_presence
    wav = _write_stereo_wav(tmp_path / "mid_band_mix.wav",
                            _band_limited_noise(5), width=0.12)
    data = analyze_vocal_presence(str(wav))
    assert 0.0 <= data["presence_estimate"] <= 1.0
    assert data["presence_band"] in ("absent", "ambiguous", "likely")
    assert data["presence_source"] == "mid_side_heuristic"
    assert data["confidence"] <= 0.45          # 启发式置信度封顶不变
    # 边界说明必须显式写出"不能作为人声证据"，避免被当结论转述
    assert "证据" in data["heuristics_note"]
    assert "requires_vocal_stem" in json.dumps(data["fine_metrics"])


def test_stem_presence_discriminates_piano_from_vocal_mix():
    """分轨口径必须把"纯钢琴"和"真带人声"分开（实测 share 0.04 vs 0.36）。"""
    from rea_music.analysis.vocal import (
        apply_separated_stem_presence, presence_from_separated_stems)

    vocal_mix = {"drums": {"rms": 0.02}, "bass": {"rms": 0.05},
                 "other": {"rms": 0.06}, "vocals": {"rms": 0.07}}
    piano_only = {"drums": {"rms": 0.0003}, "bass": {"rms": 0.016},
                  "other": {"rms": 0.0825}, "vocals": {"rms": 0.004}}

    vocal_view = presence_from_separated_stems(vocal_mix)
    piano_view = presence_from_separated_stems(piano_only)
    assert vocal_view["stem_rms_share"] == pytest.approx(0.35, abs=0.01)
    assert piano_view["stem_rms_share"] == pytest.approx(0.0385, abs=0.01)
    assert vocal_view["presence_estimate"] > 0.65      # likely
    assert piano_view["presence_estimate"] < 0.35      # absent
    assert vocal_view["presence_band"] == "likely"
    assert piano_view["presence_band"] == "absent"
    # 判别力：同一条"纯钢琴"曲，启发式 0.85 会被分轨口径推翻
    heuristic = {"presence_estimate": 0.85, "confidence": 0.45,
                 "presence_band": "likely", "presence_source": "mid_side_heuristic"}
    merged = apply_separated_stem_presence(heuristic, piano_only)
    assert merged["presence_source"] == "separated_stem"
    assert merged["confidence"] == 0.7
    assert merged["presence_estimate"] == piano_view["presence_estimate"]
    assert merged["heuristics_presence"] == 0.85        # 原值留档备复核
    assert merged["stem_rms_share"] == pytest.approx(0.0385, abs=0.01)
    assert merged["presence_band"] == "absent"


def test_stem_presence_noop_without_vocals_track():
    """无 vocals 轨时行为不变（不覆盖、不新增分轨字段）。"""
    from rea_music.analysis.vocal import (
        apply_separated_stem_presence, presence_from_separated_stems)
    heuristic = {"presence_estimate": 0.4, "confidence": 0.45,
                 "presence_source": "mid_side_heuristic"}
    no_vocals = {"drums": {"rms": 0.1}, "bass": {"rms": 0.1}, "other": {"rms": 0.1}}
    assert presence_from_separated_stems(no_vocals) is None
    assert apply_separated_stem_presence(heuristic, no_vocals) == heuristic
    assert apply_separated_stem_presence(heuristic, {}) == heuristic
    # 全轨静音：占比无定义，同样不覆盖
    silent = {"drums": {"rms": 0.0}, "bass": {"rms": 0.0},
              "other": {"rms": 0.0}, "vocals": {"rms": 0.0}}
    assert presence_from_separated_stems(silent) is None


def test_pipeline_deep_uses_stem_presence(tmp_path, monkeypatch):
    """Deep 档接线：分轨存在时 presence 走分轨口径，DNA 仍过 schema 校验。"""
    import soundfile as sf

    from rea_music.adapters import demucs as demucs_mod
    from rea_music.adapters import registry
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.core import jobs as jobs_mod
    from rea_music.dna.validate import validate

    # 1) 造一段中频居中的器乐（启发式会给高分，分轨口径要给低分）
    src = _write_stereo_wav(tmp_path / "deep_stem_case.wav",
                            _band_limited_noise(17, duration=5.0), width=0.05)

    # 2) 假分轨产物：四轨 rms 按"纯器乐"分布（vocals 仅 0.004）
    stems = {}
    for name, rms in (("drums", 0.0003), ("bass", 0.016),
                      ("other", 0.0825), ("vocals", 0.004)):
        path = tmp_path / f"{name}.wav"
        sf.write(str(path), (np.zeros(SR, dtype=np.float32)), SR,
                 subtype="PCM_16")
        stems[name] = {"path": str(path), "rms": rms}
    manifest = {"model": "htdemucs", "model_version": "fake-htdemucs",
                "method": "demucs_htdemucs_python_api", "elapsed_sec": 0.0,
                "stems_dir": str(tmp_path), "stems": stems}

    monkeypatch.setattr(registry, "separation_status",
                        lambda: {"available": True, "reason": None,
                                 "details": {}})
    monkeypatch.setattr(demucs_mod, "separate",
                        lambda *a, **kw: dict(manifest))
    # 旋律转写与网络无关，但会跑 basic-pitch；本用例只验 presence 接线
    monkeypatch.setattr(pipeline_mod, "_transcribe_melody",
                        lambda *a, **kw: None)

    job = jobs_mod.create_job("quality", {"case": "deep_stem_presence"})
    path = jobs_mod.run_inline(
        pipeline_mod.analyze_track_job, job, str(src), "deep", True)["result_ref"]
    dna = json.loads(Path(path).read_text(encoding="utf-8"))

    vocals = dna["vocals"]
    assert vocals["presence_source"] == "separated_stem"
    assert vocals["confidence"] == 0.7
    assert vocals["presence_band"] == "absent"
    assert vocals["presence_estimate"] < 0.35
    assert vocals["heuristics_presence"] is not None       # 启发式原值留档
    assert vocals["stem_rms_share"] == pytest.approx(0.0385, abs=0.01)
    assert validate(dna) == [], validate(dna)[:3]


# ---------- 真实曲目（本地工件，缺失即 skip） ----------

@pytest.mark.skipif(not YUE2_REAL.exists(), reason="缺少 yue2 真曲工件")
def test_real_yue2_bpm_is_92_not_184(monkeypatch):
    """验收：真实 92 BPM 生成曲读成 91-93 且 confidence >=0.6。

    真实工件用例显式 opt-out 隔离：指向仓库 data/ 复用既有解码缓存
    （conftest 默认把工作区隔离到临时目录，避免测试写脏 data/）。
    """
    monkeypatch.setenv("REA_MUSIC_HOME", str(ARTIFACTS.parent))
    from rea_music.analysis import audio as audio_mod
    from rea_music.analysis.tempo import analyze_tempo

    wav = audio_mod.ensure_wav(str(YUE2_REAL))
    y, sr = audio_mod.load_mono(wav["mono_wav"])
    result = analyze_tempo(y, sr)
    assert 91.0 <= result["bpm"] <= 93.0, result["bpm"]
    assert result["confidence"] >= 0.6, result["confidence"]
    # 陷阱仍在候选表里：双倍频候选的 raw 自相关更高
    doubles = [c for c in result["tempo_candidates"] if c["bpm"] > 170]
    assert doubles, result["tempo_candidates"]
    assert doubles[0]["autocorr"] >= result["tempo_candidates"][0]["autocorr"] - 0.05


@pytest.mark.skipif(not ACE_PIANO_REAL.exists(), reason="缺少 ACE 钢琴工件")
def test_real_ace_piano_heuristic_is_not_vocal_evidence(monkeypatch):
    """纯钢琴曲：启发式会给 0.85（无判别力），分轨口径必须判 absent。

    分轨结果来自仓库 data/stems 缓存（真实工件用例显式 opt-out 隔离）；
    缓存缺失则 skip（不现场跑 demucs，避免测试时长与权重下载依赖）。
    """
    monkeypatch.setenv("REA_MUSIC_HOME", str(ARTIFACTS.parent))
    from rea_music.adapters import demucs as demucs_mod
    from rea_music.analysis import audio as audio_mod
    from rea_music.analysis.vocal import presence_from_separated_stems

    wav = audio_mod.ensure_wav(str(ACE_PIANO_REAL))
    manifest_path = demucs_mod.stems_out_dir(wav["hash"]) / "manifest.json"
    if not manifest_path.exists():
        pytest.skip("缺少该曲的 demucs 分轨缓存")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    view = presence_from_separated_stems(manifest["stems"])
    assert view is not None
    assert view["stem_rms_share"] < 0.10, view    # 实测 0.0385
    assert view["presence_band"] == "absent", view
