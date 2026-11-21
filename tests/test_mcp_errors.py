"""R7-A 错误路径矩阵：14 个 MCP 工具 × 典型错误，断言 _safe 结构化转换。

全部走 server 模块直接调用（与 stdio 路径同一实现），不启动分析引擎。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # rea_music 包根在 sys.path

from rea_music.mcp import server as srv  # noqa: E402


def _err_code(result: dict) -> str | None:
    return ((result or {}).get("error") or {}).get("code")


# ---------- 基础工具 ----------

def test_capabilities_always_ok():
    assert srv.capabilities()["ok"] is True


def test_inspect_missing_file():
    r = srv.inspect_audio("Z:/no/such/file.wav")
    assert r["ok"] is False and _err_code(r) == "file_not_found"


def test_inspect_bad_extension(tmp_path, monkeypatch):
    """内容非法 → invalid_audio。

    显式模拟"ffprobe 存在但读不了该文件"，让本用例与本机是否装 FFmpeg 无关
    （干净 venv 里没有 ffprobe 时，探测层会如实报 capability_unavailable）。
    """
    from rea_music.analysis import inspect as inspect_mod
    from rea_music.core.errors import InvalidAudio

    def _unreadable(_path):  # noqa: ARG001 —— 签名与 ffprobe 对齐
        raise InvalidAudio("ffprobe 无法读取")

    monkeypatch.setattr(inspect_mod, "ffprobe", _unreadable)

    p = tmp_path / "bad.xyz"
    p.write_text("junk", encoding="utf-8")
    r = srv.inspect_audio(str(p))
    assert r["ok"] is False and _err_code(r) == "invalid_audio"


def test_analyze_missing_file():
    r = srv.analyze_track("Z:/no/such.wav")
    assert r["ok"] is False and _err_code(r) == "file_not_found"


def test_get_analysis_bad_ref():
    r = srv.get_analysis("deadbeef0000")
    assert r["ok"] is False and _err_code(r) == "analysis_not_found"


def test_get_job_status_bad_id():
    r = srv.get_job_status("nonexistent999")
    assert r["ok"] is False and _err_code(r) == "job_not_found"


def test_extract_dna_missing_file():
    r = srv.extract_music_dna("Z:/no/such.wav")
    assert r["ok"] is False  # job 失败结构化返回，无裸异常


# ---------- 分轨能力门控 ----------

def test_separate_stems_unavailable(monkeypatch, tmp_path):
    import soundfile as sf
    import numpy as np
    from rea_music.adapters import registry
    wav = tmp_path / "probe.wav"
    sf.write(str(wav), np.zeros(22050, dtype=np.float32), 22050)
    monkeypatch.setattr(
        registry, "separation_status",
        lambda: {"available": False, "engine": "demucs",
                 "install_hint": "test", "gpu_free_mib": None})
    r = srv.separate_stems(str(wav))
    assert r["ok"] is False and _err_code(r) == "capability_unavailable"


# ---------- 比较 / 融合 / 计划 ----------

def test_compare_bad_refs():
    r = srv.compare("deadbeef0000", "deadbeef1111")
    assert r["ok"] is False and _err_code(r) == "analysis_not_found"


def test_compare_one_bad_ref():
    r = srv.compare("deadbeef0000", "deadbeef1111")
    assert r["ok"] is False


def test_blend_bad_ref():
    r = srv.blend(["deadbeef0000"])
    assert r["ok"] is False and _err_code(r) == "analysis_not_found"


def test_create_plan_missing_dna():
    r = srv.create_plan(["deadbeef0000"], objective="inherit_style_new_song")
    assert r["ok"] is False and _err_code(r) == "analysis_not_found"


def test_create_plan_lyrics_driven_without_lyrics():
    r = srv.create_plan(["deadbeef0000"], objective="lyrics_driven")
    assert r["ok"] is False and _err_code(r) in ("analysis_not_found",
                                                "validation_error")


def test_render_bad_plan_ref():
    r = srv.render_request("plan_nope", backend="suno")
    assert r["ok"] is False and _err_code(r) == "analysis_not_found"


# ---------- 生成 / 评测 / 修订 ----------

def test_generate_bad_plan_ref():
    r = srv.generate_music("plan_nope", backend="auto")
    assert r["ok"] is False and _err_code(r) == "analysis_not_found"


def test_generate_yue2_offline_falls_back_to_suno(monkeypatch):
    """studio 离线 + backend=yue2 → 结构化降级 Suno 包（ok:true，不是错误）。"""
    from rea_music.generation import backends as bm
    monkeypatch.setattr(
        bm, "yue2_probe",
        lambda cfg=None: {"available": False, "url": "x",
                          "reason": "offline", "hint": "test"})
    plan = {"inherit": {"tempo_bpm": 120, "key": {"value": "C major"},
                        "style_tags": ["mid_tempo"],
                        "instrumentation_hint": "balanced"},
            "sections": [{"label": "verse", "target_duration_sec": 20.0,
                          "emotion": "reflective", "energy_target": 0.5,
                          "strategy": {}}]}
    r = srv.generate_music(plan, backend="yue2")
    assert r["ok"] is True and r["backend"] == "suno_prompt_package"


def test_evaluate_missing_generated_file(tmp_path):
    """evaluate.py REAError import 修复后的运行时验证：坏文件 → 结构化错误。"""
    dna = _minimal_dna()
    target = tmp_path / "target_dna.json"
    target.write_text(__import__("json").dumps(dna), encoding="utf-8")
    r = srv.evaluate(str(tmp_path / "no_such_output.wav"),
                     str(target))
    assert r["ok"] is False  # REAError 被捕获而非 NameError 冒泡
    assert "error" in r or "job" in r


def test_revise_bad_refs():
    r = srv.revise_plan("plan_nope", "eval_nope")
    assert r["ok"] is False and _err_code(r) == "analysis_not_found"


def test_revise_no_suggestions_returns_unrevised():
    plan = {"plan_id": "plan_t", "schema": "composition_plan_v1",
            "inherit": {"tempo_bpm": 120}}
    ev = {"evaluation_id": "eval_t", "revision_suggestions": []}
    r = srv.revise_plan(plan, ev)
    assert r["ok"] is True and r["revised"] is False


def _minimal_dna():
    return {
        "schema": "music_dna_v1",
        "song": {"song_id": "sha256:minimal", "content_hash": "a" * 64,
                 "duration_sec": 10.0, "analyzed_at": "t", "analyzer": "t",
                 "analysis_tier": "fast"},
        "rhythm": {"layer": "objective_audio_features",
                   "bpm": {"value": 100.0}},
        "tonality": {"layer": "objective_audio_features",
                     "key": {"value": "C major"}},
        "harmony": {"layer": "inferred",
                    "chord_segments": {"value": []}},
        "form": {"layer": "inferred", "segments": []},
        "production": {"layer": "objective_audio_features",
                       "integrated_lufs": {"value": -14},
                       "spectral_centroid_hz": {"value": 2000},
                       "harmonic_percussive_ratio": {"value": 0.5},
                       "rms_curve_1s": {"value": []}},
        "provenance_summary": {"analyzer": "t", "schema": "music_dna_v1",
                               "tier": "fast", "engines_used": []},
    }
