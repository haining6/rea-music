"""R1 验收测试。真实引擎跑真实音频，与 ground truth 对照；不用 Mock 冒充。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # rea_music 包根在 sys.path

FIXTURES = ROOT / "fixtures"

# fixtures/*.wav 的自举已统一收敛到 tests/conftest.py（session 级 autouse），
# 本模块不再各自实现，避免"单独运行某文件时缺 fixture"的顺序依赖。


def _truth(name: str) -> dict:
    return json.loads((FIXTURES / name).with_suffix(".truth.json")
                      .read_text(encoding="utf-8"))


def _load_mono(path: Path):
    import soundfile as sf
    y, sr = sf.read(str(path), always_2d=True)
    return y.mean(axis=1), sr


# ---------- inspect ----------

def test_inspect_real_file():
    from rea_music.analysis.inspect import inspect_audio
    meta = inspect_audio(str(FIXTURES / "pop_c_major_120.wav"))
    assert meta["ok"] is True
    assert abs(meta["duration_sec"] - 80.0) < 1.0
    assert meta["channels"] == 2
    assert meta["sample_rate_hz"] == 44100
    assert len(meta["content_hash"]) == 64


def test_inspect_missing_file():
    from rea_music.analysis.inspect import inspect_audio
    from rea_music.core.errors import FileNotFound
    with pytest.raises(FileNotFound):
        inspect_audio(str(FIXTURES / "no_such.wav"))


# ---------- tempo (ground truth) ----------

@pytest.mark.parametrize("fixture,expected_bpm", [
    ("pop_c_major_120.wav", 120.0),
    ("ballad_a_minor_96.wav", 96.0),
    ("rock_g_major_150.wav", 150.0),
])
def test_tempo_ground_truth(fixture, expected_bpm):
    from rea_music.analysis.tempo import analyze_tempo
    y, sr = _load_mono(FIXTURES / fixture)
    result = analyze_tempo(y, sr)
    bpm = result["bpm"]
    assert bpm is not None, f"BPM 未检出: {result}"
    rel_err = abs(bpm - expected_bpm) / expected_bpm
    assert rel_err <= 0.02, f"BPM {bpm} 偏离真值 {expected_bpm}"
    assert result["confidence"] >= 0.5
    assert result["meter"] in (3, 4)


def test_beats_monotonic():
    from rea_music.analysis.tempo import analyze_tempo
    y, sr = _load_mono(FIXTURES / "pop_c_major_120.wav")
    result = analyze_tempo(y, sr)
    beats = result["beats_time"]
    assert beats == sorted(beats)
    assert len(beats) > 100  # 80s 曲 @120bpm ≈ 160 拍


# ---------- key (ground truth) ----------

@pytest.mark.parametrize("fixture,expected_key", [
    ("pop_c_major_120.wav", "C major"),
    ("ballad_a_minor_96.wav", "A minor"),
    ("rock_g_major_150.wav", "G major"),
])
def test_key_ground_truth(fixture, expected_key):
    from rea_music.analysis.key import analyze_key
    y, sr = _load_mono(FIXTURES / fixture)
    result = analyze_key(y, sr)
    assert result["key"] == expected_key, f"调性误判: {result['key']}"
    assert result["confidence"] >= 0.5


def test_key_profiles_agree_branch():
    """双 profile 融合路径回归（R2 实测缺陷）：纯 A 大调长音。
    旧代码在 agree=True 分支三元组解包崩溃（首次被 ACE 生成曲触发）；
    修复后无论 agree 与否都必须完成并给出正确调性。"""
    import numpy as np
    from rea_music.analysis.key import analyze_key
    sr = 44100
    t = np.linspace(0, 10.0, sr * 10, endpoint=False)
    freqs = [220.0, 277.18, 329.63, 440.0, 554.37]  # A3 C#4 E4 A4 C#5
    y = sum(0.12 * np.sin(2 * np.pi * f * t) for f in freqs)
    y *= np.linspace(0.2, 1.0, len(y))  # 简单渐强，避免纯稳态
    result = analyze_key(y.astype(np.float32), sr)
    assert result["key"] == "A major"
    assert isinstance(result["profiles_agree"], bool)  # 不再崩溃即回归通过
    assert result["confidence"] >= 0.4


# ---------- structure (ground truth) ----------

def test_structure_boundaries():
    from rea_music.analysis.structure import analyze_structure
    truth = _truth("pop_c_major_120.wav")
    y, sr = _load_mono(FIXTURES / "pop_c_major_120.wav")
    result = analyze_structure(y, sr, [], min_segment_sec=8.0)
    segs = result["segments"]
    assert len(segs) >= 3, f"段落过少: {segs}"
    # 每个真值边界 ±3s 内应存在检出边界
    for t in (truth["sections"][1]["start_sec"],):  # 8s intro→verse
        assert any(abs(s["start_sec"] - t) <= 3.0 for s in segs), \
            f"边界 {t}s 未检出: {[s['start_sec'] for s in segs]}"
    labels = {s["label"]["value"] if isinstance(s["label"], dict) else s["label"]
              for s in segs}
    assert "intro" in labels


# ---------- chords ----------

def test_chords_progression():
    from rea_music.analysis.chords import analyze_chords
    truth = _truth("pop_c_major_120.wav")
    y, sr = _load_mono(FIXTURES / "pop_c_major_120.wav")
    result = analyze_chords(y, sr, [], len(y) / sr)
    # 无拍点时应缺省而不是编造
    assert result.get("missing_reason") == "no_beats"

    from rea_music.analysis.tempo import analyze_tempo
    beats = analyze_tempo(y, sr)["beats_time"]
    result = analyze_chords(y, sr, beats, len(y) / sr)
    labels = {seg["chord"] for seg in result["segments"]}
    expected = set(truth["chords"])
    overlap = labels & expected
    assert len(overlap) >= 3, f"和弦识别覆盖不足: {labels}"


# ---------- loudness / spectral ----------

def test_loudness_real():
    from rea_music.analysis.loudness import analyze_loudness
    data = analyze_loudness(str(FIXTURES / "pop_c_major_120.wav"))
    assert data["integrated_lufs"] is not None
    assert -40 < data["integrated_lufs"] < 0
    assert data["true_peak_dbtp"] > -6
    assert data["method"].startswith("bs1770")


def test_spectral_real():
    from rea_music.analysis.spectral import analyze_spectral
    y, sr = _load_mono(FIXTURES / "pop_c_major_120.wav")
    data = analyze_spectral(y, sr)
    assert 300 < data["spectral_centroid_hz"] < 8000
    bands = data["band_energy_ratios"]
    assert abs(sum(bands.values()) - 1.0) < 0.05
    assert len(data["rms_curve_1s"]) > 50


# ---------- vocal heuristic ----------

def test_vocal_presence_synth():
    from rea_music.analysis.vocal import analyze_vocal_presence
    data = analyze_vocal_presence(str(FIXTURES / "pop_c_major_120.wav"))
    assert data["presence_estimate"] is not None
    assert data["confidence"] <= 0.45  # 启发式置信度封顶
    assert "requires_vocal_stem" in json.dumps(data["fine_metrics"])


# ---------- DNA schema + pipeline ----------

def test_full_pipeline_dna_schema():
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.core import jobs as jobs_mod
    from rea_music.dna.validate import validate
    job = jobs_mod.create_job("test", {"audio_path": "fixture"})
    path = jobs_mod.run_inline(
        pipeline_mod.analyze_track_job, job,
        str(FIXTURES / "pop_c_major_120.wav"), "standard", False)["result_ref"]
    dna = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate(dna)
    assert errors == [], f"schema 校验失败: {errors[:5]}"
    assert dna["rhythm"]["bpm"]["value"] is not None
    assert dna["tonality"]["key"]["value"] == "C major"
    assert len(dna["form"]["segments"]) >= 3
    # 段落级 DNA 必须存在（不允许只有整曲均值）
    for seg in dna["form"]["segments"]:
        assert "label" in seg and "rms" in seg
    # 未知字段必须带 missing_reason
    assert dna["melody"]["notes"]["value"] is None
    assert "missing_reason" in dna["melody"]["notes"]


def test_pipeline_cache_hit():
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.core import jobs as jobs_mod
    src = str(FIXTURES / "ballad_a_minor_96.wav")
    j1 = jobs_mod.create_job("t1", {})
    p1 = jobs_mod.run_inline(pipeline_mod.analyze_track_job, j1, src, "standard", False)["result_ref"]
    j2 = jobs_mod.create_job("t2", {})
    p2 = jobs_mod.run_inline(pipeline_mod.analyze_track_job, j2, src, "standard", False)["result_ref"]
    assert p1 == p2  # 同内容同档位命中缓存


# ---------- compare / blend / plan / evaluate ----------

def test_compare_tracks_dimensions():
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.comparison.compare import compare_tracks
    from rea_music.core import jobs as jobs_mod
    def _dna(name):
        j = jobs_mod.create_job("cmp", {})
        p = jobs_mod.run_inline(pipeline_mod.analyze_track_job, j,
                                str(FIXTURES / name), "standard", False)["result_ref"]
        return json.loads(Path(p).read_text(encoding="utf-8"))
    a = _dna("pop_c_major_120.wav")
    b = _dna("ballad_a_minor_96.wav")
    cmp = compare_tracks(a, b)
    assert "tempo" in cmp["dimensions"]
    assert cmp["dimensions"]["tempo"]["verdict"] == "different"
    assert "key" in cmp["dimensions"]
    # 不允许合成单一总分：只回逐维结果 + 缺维度说明
    assert {"dimensions", "a_song_id", "b_song_id"} <= set(cmp)
    assert "overall" not in cmp and "score" not in cmp
    assert cmp["comparable"] is True
    assert "missing_dimensions" in cmp


def test_blend_and_plan():
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.comparison.compare import blend_music_dna
    from rea_music.core import jobs as jobs_mod
    from rea_music.generation.plan import create_composition_plan
    def _dna(name):
        j = jobs_mod.create_job("blend", {})
        p = jobs_mod.run_inline(pipeline_mod.analyze_track_job, j,
                                str(FIXTURES / name), "standard", False)["result_ref"]
        return json.loads(Path(p).read_text(encoding="utf-8"))
    dnas = [_dna("pop_c_major_120.wav"), _dna("rock_g_major_150.wav")]
    blended = blend_music_dna(dnas, {"tempo": 1})
    assert blended["dimensions"]["tempo"]["value"] == dnas[1]["rhythm"]["bpm"]["value"]
    plan = create_composition_plan(dna_refs=dnas, objective="blend_styles",
                                   dimension_map={"tempo": 1})
    assert plan["schema"] == "composition_plan_v1"
    assert plan["sections"]
    assert plan["originality_guardrails"]["reuse_reference_lyrics"] is False
    assert plan["inherit"]["tempo_bpm"] == dnas[1]["rhythm"]["bpm"]["value"]


def test_evaluate_generation_loop():
    """评测闭环：两首合成曲互为目标与生成品。"""
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.core import jobs as jobs_mod
    from rea_music.evaluation.evaluate import evaluate_generation
    def _dna(name):
        j = jobs_mod.create_job("ev", {})
        p = jobs_mod.run_inline(pipeline_mod.analyze_track_job, j,
                                str(FIXTURES / name), "standard", False)["result_ref"]
        return json.loads(Path(p).read_text(encoding="utf-8"))
    target = _dna("pop_c_major_120.wav")
    ev = evaluate_generation(generated_audio_path=str(FIXTURES / "ballad_a_minor_96.wav"),
                            target_dna=target)
    assert ev["ok"] is True
    assert "节奏符合度" in ev["dimensions"]
    assert ev["dimensions"]["节奏符合度"]["satisfied"] is False  # 120 vs 96 必不符
    assert ev["technical_quality"]["integrated_lufs"]["value"] is not None
    assert isinstance(ev["revision_suggestions"], list)


def test_revise_plan_from_evaluation(tmp_path, monkeypatch):
    """修订闭环：结构化 target 必须落成数值参数，并可被后端渲染。"""
    from rea_music.generation.backends import render_generation_request
    from rea_music.generation.plan import create_composition_plan
    from rea_music.mcp.server import revise_plan

    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    dna = {"song": {"song_id": "sha256:test"},
           "rhythm": {"bpm": {"value": 120.0}},
           "tonality": {"key": {"value": "C major"}},
           "harmony": {"progressions": {"value": []},
                       "chord_segments": {"value": []}},
           "form": {"segments": []},
           "production": {"rms_curve_1s": {"value": []},
                          "integrated_lufs": {"value": -14},
                          "spectral_centroid_hz": {"value": 2000},
                          "harmonic_percussive_ratio": {"value": 0.5}},
           "instrumentation": {"band_energy_ratios": {"value": {}}},
           "creative_semantics": {"style_tags": {"value": []}},
           "vocals": {"presence_estimate": 0.3}}
    plan = create_composition_plan(dna_refs=[dna], objective="inherit_style_new_song")
    evaluation = {"evaluation_id": "eval_test",
                  "revision_suggestions": [
                      {"dimension": "节奏符合度", "knob": "tempo_bpm",
                       "target": 120.0,
                       "adjustment": "把生成请求 BPM 调整到 120.0"}]}
    result = revise_plan(plan, evaluation)

    assert result["ok"] is True and result["revised"] is True
    revised = result["plan"]
    assert revised["inherit"]["tempo_bpm"] == 120.0        # 数值而非句子
    assert revised["target_modifications"]["bpm"] == 120.0
    assert revised["revision_history"]
    # 两条后端渲染都必须能吃下修订后的 plan
    assert "120 bpm" in render_generation_request(revised, "suno")["style_prompt"]
    assert render_generation_request(revised, "yue2")["request"]["style"][
        "tempo_bpm"] == 120.0


# ---------- jobs / cache / errors ----------

def test_job_lifecycle():
    from rea_music.core import jobs as jobs_mod
    job = jobs_mod.create_job("unit", {"x": 1})
    got = jobs_mod.get_job(job["job_id"])
    assert got["status"] == "queued"
    jobs_mod.update_job(job["job_id"], status="running", stage="tempo",
                        progress=0.4)
    got = jobs_mod.get_job(job["job_id"])
    assert got["stage"] == "tempo" and abs(got["progress"] - 0.4) < 1e-9


def test_job_not_found():
    from rea_music.core import jobs as jobs_mod
    from rea_music.core.errors import JobNotFound
    with pytest.raises(JobNotFound):
        jobs_mod.get_job("nonexistent001")


def test_capability_honesty():
    from rea_music.adapters import registry
    caps = registry.capabilities()
    sep = caps["analysis_engines"]["separation"]
    # 分轨能力必须给出结构化结论：不可用时点名原因与安装指引
    assert isinstance(sep["available"], bool)
    assert "engine" in sep and "install_hint" in sep
    if not sep["available"]:
        assert sep.get("unavailable_reason"), sep
    # yue2 可用性随 studio 开窗翻转，断言字段结构而非固定值
    yue2 = caps["generation"]["backends"]["yue2_studio_mcp"]
    assert isinstance(yue2["available"], bool)
    assert isinstance(yue2.get("url"), str)
    assert yue2["url"].startswith(("http://", "https://"))
    if not yue2["available"]:
        assert yue2.get("reason") and yue2.get("hint")


def test_suno_package_no_lyrics_leak():
    from rea_music.generation.backends import render_generation_request
    plan = {"inherit": {"tempo_bpm": 120.0, "key": {"value": "C major"},
                        "style_tags": ["mid_tempo"],
                        "instrumentation_hint": "balanced"},
            "sections": [{"label": "chorus", "target_duration_sec": 30.0,
                          "energy": "high"}]}
    out = render_generation_request(plan, "suno")
    assert out["ok"] is True
    assert "120 bpm" in out["style_prompt"]
    assert "[CHORUS]" in out["structured_lyrics_template"]
    assert "参考曲" not in out["structured_lyrics_template"]
