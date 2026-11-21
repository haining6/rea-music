"""R3 创作规划深化 + R5 迭代历史硬化 验收测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # rea_music 包根在 sys.path

FIXTURES = ROOT / "fixtures"

# fixtures/*.wav 的自举统一在 tests/conftest.py（session 级 autouse），
# 本模块不再重复实现。


@pytest.fixture(scope="module")
def pop_dna():
    from rea_music.core import jobs as jobs_mod
    from rea_music.analysis import pipeline as pipeline_mod
    job = jobs_mod.create_job("r3r5", {"audio": "fixture"})
    path = jobs_mod.run_inline(
        pipeline_mod.analyze_track_job, job,
        str(FIXTURES / "pop_c_major_120.wav"), "standard", False)["result_ref"]
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------- T03: 段落级情绪/能量/策略 ----------

def test_sections_have_emotion_energy_strategy(pop_dna):
    from rea_music.generation.plan import create_composition_plan
    plan = create_composition_plan(dna_refs=[pop_dna],
                                   objective="inherit_style_new_song")
    sections = plan["sections"]
    assert sections, "段落为空"
    for sec in sections:
        assert sec.get("emotion") in ("calm", "reflective", "building",
                                      "uplifting", "tense", "neutral",
                                      "resolved")
        assert isinstance(sec.get("energy_target"), (int, float))
        assert 0.0 <= sec["energy_target"] <= 1.0
        assert isinstance(sec.get("strategy"), dict)
    labels = [s["label"] for s in sections]
    assert "chorus" in labels


def test_roman_numerals_from_reference_key(pop_dna):
    from rea_music.generation.plan import _roman_numerals
    chords = [{"chord": "C:maj"}, {"chord": "G:maj"},
              {"chord": "A:min"}, {"chord": "F:maj"}]
    out = _roman_numerals(chords, pop_dna)
    assert out is not None
    assert "I" in out and "V" in out  # C→I, G→V（C 大调）


def test_lyrics_driven_tagged_chinese():
    from rea_music.generation.plan import create_composition_plan
    lyrics = "\n".join([
        "[Verse 1]", "夜色刚刚落下", "我在街角听见",
        "[Chorus]", "把心事交给风",
    ])
    plan = create_composition_plan(
        dna_refs=[_minimal_dna()], objective="lyrics_driven",
        user_lyrics=lyrics)
    lg = plan["lyrics_guidance"]
    assert lg["mode"] == "user_provided"
    assert lg["language_detect"] == "zh-dominant"
    assert lg["syllable_profile"]["total_syllables"] == 18  # 6+6+6 CJK 字，标签行不计
    tagged = lg["sectioning_suggestion"]
    assert tagged and tagged[0]["label"] == "verse"
    assert tagged[1]["label"] == "chorus"
    assert tagged[0]["syllables_per_line"] == 6.0


def test_suno_and_yue2_renderers_carry_emotion():
    from rea_music.generation.backends import render_generation_request
    plan = {"inherit": {"tempo_bpm": 120, "key": {"value": "C major"},
                        "style_tags": ["mid_tempo"],
                        "instrumentation_hint": "balanced"},
            "sections": [{"label": "chorus", "target_duration_sec": 30.0,
                          "emotion": "uplifting", "energy_target": 0.85,
                          "strategy": {}}]}
    suno = render_generation_request(plan, "suno")
    assert "mood:uplifting" in suno["structured_lyrics_template"]
    assert "energy:0.85" in suno["structured_lyrics_template"]
    yue2 = render_generation_request(plan, "yue2")
    assert yue2["request"]["style"]["mood"] == "uplifting"
    assert yue2["request"]["lyrics_sections"][0]["emotion"] == "uplifting"


def _minimal_dna():
    return {"song": {"song_id": "sha256:minimal"},
            "rhythm": {"bpm": {"value": 90.0},
                       "meter_beats": {"value": 4}},
            "tonality": {"key": {"value": "C major"}},
            "harmony": {"progressions": {"value": []},
                        "chord_segments": {"value": []},
                        "harmonic_rhythm_beats": {"value": 2.0}},
            "form": {"segments": []},
            "production": {"rms_curve_1s": {"value": []},
                           "integrated_lufs": {"value": -14},
                           "spectral_centroid_hz": {"value": 2000},
                           "harmonic_percussive_ratio": {"value": 0.5}},
            "instrumentation": {"band_energy_ratios": {"value": {}}},
            "creative_semantics": {"style_tags": {"value": []}},
            "vocals": {"presence_estimate": 0.3}}


# ---------- T04: OptimizationHistory ----------

def test_record_iteration_persists(tmp_path, monkeypatch):
    from rea_music.core import paths as paths_mod
    from rea_music.evaluation import loop as loop_mod
    monkeypatch.setattr(paths_mod, "experiments_dir", lambda: tmp_path)
    out = loop_mod.record_iteration(
        "plan_x", goal="test", params={"bpm": 120},
        eval_summary={"dimensions": {"节奏": {"satisfied": True},
                                      "调性": {"satisfied": False}}},
        revision={"changes": [{"knob": "tempo_bpm"}]})
    assert out["iterations"] == 1
    assert out["stop"]["stop"] is False
    saved = json.loads((tmp_path / "plan_x.json").read_text(encoding="utf-8"))
    assert saved["iterations"][0]["satisfied_count"] == 1
    # resume 跨会话可读
    r = loop_mod.resume("plan_x")
    assert r["completed_iterations"] == 1 and r["next_iteration"] == 2


def test_stop_at_max_iterations(tmp_path, monkeypatch):
    from rea_music.core import paths as paths_mod
    from rea_music.evaluation import loop as loop_mod
    monkeypatch.setattr(paths_mod, "experiments_dir", lambda: tmp_path)
    for i in range(3):
        loop_mod.record_iteration("plan_y", goal="g", params={},
                                  eval_summary={"dimensions":
                                                {"a": {"satisfied": True}}},
                                  revision={})
    verdict = loop_mod.resume("plan_y")
    assert verdict["stop"]["stop"] is True
    assert "max_iterations_reached" in verdict["stop"]["reason"]


def test_stop_on_no_improvement(tmp_path, monkeypatch):
    from rea_music.core import paths as paths_mod
    from rea_music.evaluation import loop as loop_mod
    monkeypatch.setattr(paths_mod, "experiments_dir", lambda: tmp_path)
    loop_mod.record_iteration("plan_z", goal="g", params={},
                              eval_summary={"dimensions":
                                            {"a": {"satisfied": True}}},
                              revision={})
    loop_mod.record_iteration("plan_z", goal="g", params={},
                              eval_summary={"dimensions":
                                            {"a": {"satisfied": True}}},
                              revision={})
    verdict = loop_mod.resume("plan_z")
    assert verdict["stop"]["stop"] is True
    assert "no_improvement" in verdict["stop"]["reason"]


def test_evaluate_reaerror_import_fixed():
    """A8 附带缺陷修复：evaluate.py 必须真正 import 并使用 REAError。

    早前这里用 `assert hasattr(...) or True` 恒真断言，等于没测；
    现在直接断言符号存在且是异常子类（`from ... import REAError` 缺失即报错）。
    """
    from rea_music.core.errors import REAError as core_error
    from rea_music.evaluation import evaluate as ev_mod

    assert issubclass(ev_mod.REAError, Exception)
    assert ev_mod.REAError is core_error
    source = (ROOT / "src/rea_music/evaluation/evaluate.py").read_text(
        encoding="utf-8")
    assert "from rea_music.core.errors import" in source
    assert "REAError" in source
