"""R2 引擎接线验收测试：真实引擎 + 真实音频 + ground truth 对照。

覆盖 R2-A（demucs CPU 分轨）/ R2-B（basic-pitch 隔离 venv 转写）/
R2-C（chordotomy 主引擎 + 模板后备）/ R2-D（songbrain 无 key 降级）/
R2-E（NATTEN 降级已记 DECISIONS.md D15，此处不重复展开）。
引擎缺失走 monkeypatch 错误路径断言结构化降级，不用 Mock 冒充成功。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # rea_music 包根在 sys.path

FIXTURES = ROOT / "fixtures"
DEEP_FIXTURES = ["pop_c_major_120.wav", "ballad_a_minor_96.wav",
                 "rock_g_major_150.wav"]

# fixtures/*.wav 的自举统一在 tests/conftest.py（session 级 autouse），
# 本模块不再重复实现。


def _truth(name: str) -> dict:
    return json.loads((FIXTURES / name).with_suffix(".truth.json")
                      .read_text(encoding="utf-8"))


def _load_mono(path: Path):
    import soundfile as sf
    y, sr = sf.read(str(path), always_2d=True)
    return y.mean(axis=1), sr


# ---------- R2-A demucs ----------

def _demucs_installed() -> bool:
    """torch + demucs 是否装上（只看能否 import，不在 collection 期加载权重）。"""
    import importlib.util
    return (importlib.util.find_spec("torch") is not None
            and importlib.util.find_spec("demucs") is not None)


# 干净 venv（只装本包 + pytest，无 [stems] extras）里应 skip 而非 fail
_DEMUCS_SKIP = pytest.mark.skipif(
    not _demucs_installed(),
    reason="未安装 torch/demucs（可选引擎 extras [stems]），干净环境跳过")


def _require_demucs():
    """运行期守卫：torch/demucs 或权重缺失时 skip（含下载失败/显存不足等）。"""
    pytest.importorskip("torch", reason="未安装 torch/demucs（可选引擎）")
    pytest.importorskip("demucs", reason="未安装 torch/demucs（可选引擎）")
    from rea_music.adapters import demucs
    status = demucs.probe()
    if not status["available"]:
        pytest.skip(f"demucs 不可用：{status.get('reason')}")
    return demucs


@_DEMUCS_SKIP
def test_demucs_probe_real():
    demucs = _require_demucs()
    status = demucs.probe()
    assert status["available"] is True
    assert status["details"]["runtime"] in ("cpu", "gpu")
    assert set(status["details"]["sources"]) == {"drums", "bass", "other",
                                                 "vocals"}


@_DEMUCS_SKIP
def test_demucs_cpu_separate_rock():
    """rock 曲 CPU 分轨：四轨落盘 + manifest 带各轨 rms 与耗时。

    首次运行约 1.5x 曲长；manifest 已缓存时直接命中（幂等性）。"""
    demucs = _require_demucs()
    from rea_music.analysis import audio as audio_mod
    from rea_music.core.hash import file_sha256

    path = FIXTURES / "rock_g_major_150.wav"
    chash = file_sha256(path)
    wav = audio_mod.ensure_wav(path, chash)
    manifest = demucs.separate(wav["stereo_wav"], chash)
    assert manifest["ok"] is True
    assert manifest["method"] == "demucs_htdemucs_python_api"
    assert manifest["runtime"] in ("cpu", "gpu")
    assert manifest["elapsed_sec"] > 0
    assert set(manifest["stems"]) == {"drums", "bass", "other", "vocals"}
    for name, info in manifest["stems"].items():
        assert Path(info["path"]).exists(), f"{name} 轨未落盘"
        assert info["rms"] >= 0.0
    # 幂等：重复调用命中 manifest 缓存
    again = demucs.separate(wav["stereo_wav"], chash)
    assert again["manifest_path"] == manifest["manifest_path"]


def test_demucs_pick_melody_stem():
    """选轨逻辑（纯函数）：vocals 优先，静音则回退 other，全静音返回 None。

    用合成 manifest，不依赖真实分轨缓存 —— 早前该用例直接读 data/stems 下由
    另一个用例产出的 manifest，隔离环境/单跑时必然 FileNotFoundError。
    """
    from rea_music.adapters import demucs

    vocal_mix = {"stems": {
        "drums": {"path": "d.wav", "rms": 0.02},
        "bass": {"path": "b.wav", "rms": 0.05},
        "other": {"path": "o.wav", "rms": 0.06},
        "vocals": {"path": "v.wav", "rms": 0.07},
    }}
    assert demucs.pick_melody_stem(vocal_mix) == ("vocals", "v.wav")

    silent_vocals = {"stems": {
        "drums": {"path": "d.wav", "rms": 0.02},
        "bass": {"path": "b.wav", "rms": 0.02},
        "other": {"path": "o.wav", "rms": 0.08},
        "vocals": {"path": "v.wav", "rms": 1e-5},      # 低于 SILENT_STEM_RMS
    }}
    assert demucs.pick_melody_stem(silent_vocals) == ("other", "o.wav")

    all_silent = {"stems": {name: {"path": f"{name}.wav", "rms": 0.0}
                            for name in ("drums", "bass", "other", "vocals")}}
    assert demucs.pick_melody_stem(all_silent) is None
    assert demucs.pick_melody_stem({"stems": {}}) is None


@_DEMUCS_SKIP
def test_demucs_pick_melody_stem_from_real_manifest():
    """真实分轨缓存在场时也要能选出有效轨；缓存缺失即 skip（不做环境假设）。"""
    from rea_music.adapters import demucs
    from rea_music.core.hash import file_sha256

    manifest_path = (demucs.stems_out_dir(
        file_sha256(FIXTURES / "rock_g_major_150.wav")) / "manifest.json")
    if not manifest_path.exists():
        pytest.skip("当前工作区缺少该曲的 demucs 分轨缓存")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pick = demucs.pick_melody_stem(manifest)
    assert pick is not None
    stem_name, stem_path = pick
    assert stem_name in ("vocals", "other")
    assert Path(stem_path).exists()


# ---------- R2-B basic-pitch ----------

def test_basicpitch_probe_real():
    from rea_music.adapters import basicpitch
    status = basicpitch.probe()
    if not status["available"]:
        pytest.skip(f"bp-venv 不可用（可选引擎）：{status.get('reason')}")
    assert Path(status["details"]["model_path"]).exists()


def test_basicpitch_transcribe_real(tmp_path):
    """bp-venv 子进程转写：契约 JSON + MIDI 落盘。

    输入用 ensure_wav 产物（同一条真实音频），输出落 tmp_path —— 不再依赖
    另一个用例事先跑出的 demucs 分轨缓存，也不往仓库 data/ 写文件。
    """
    from rea_music.adapters import basicpitch
    from rea_music.analysis import audio as audio_mod

    if not basicpitch.probe()["available"]:
        pytest.skip("bp-venv / ICASSP 模型缺失（可选引擎）")

    wav = audio_mod.ensure_wav(str(FIXTURES / "rock_g_major_150.wav"))
    result = basicpitch.transcribe(
        wav["stereo_wav"],
        json_out=tmp_path / "bp_test.json",
        midi_out=tmp_path / "bp_test.mid")
    assert result["ok"] is True
    assert result["method"] == "basic_pitch_icassp_2022"
    assert result["model_version"]
    assert result["note_count"] > 0
    note = result["notes"][0]
    assert set(note) == {"pitch_midi", "start_sec", "end_sec", "amplitude"}
    assert 21 <= note["pitch_midi"] <= 108
    assert Path(result["midi_path"]).exists()


def test_basicpitch_missing_input_raises():
    from rea_music.adapters import basicpitch
    from rea_music.core.errors import FileNotFound
    with pytest.raises(FileNotFound):
        basicpitch.transcribe(
            FIXTURES / "no_such.wav",
            json_out=ROOT / "data" / "tmp_probe" / "bp_none.json",
            midi_out=ROOT / "data" / "tmp_probe" / "bp_none.mid")


# ---------- R2-C chordotomy ----------

def test_chordotomy_probe_real():
    from rea_music.adapters import chordotomy
    status = chordotomy.probe()
    if not status["available"]:
        pytest.skip(f"chordotomy 未安装（可选引擎）：{status.get('reason')}")
    assert status["details"]["version"].startswith("chordotomy")


def test_chordotomy_analyze_pop_truth():
    from rea_music.adapters import chordotomy
    result = chordotomy.analyze(FIXTURES / "pop_c_major_120.wav")
    assert result["engine"] == "chordotomy_dsp"
    assert result["key_from_engine"] == "C:maj"
    assert len(result["segments"]) > 5
    seg = result["segments"][0]
    assert "chord" in seg and "start_sec" in seg and "end_sec" in seg
    # ground truth: 4 个真值和弦至少覆盖 3 个（根音+性质）
    truth = set(_truth("pop_c_major_120.wav")["chords"])
    detected = {s["chord"] for s in result["segments"]} - {"N"}
    assert len(detected & truth) >= 3, \
        f"chordotomy 覆盖不足: detected={detected}, truth={truth}"


def test_chords_default_engine_chordotomy():
    """auto 引擎：external path 提供时走 chordotomy 并如实标注。"""
    from rea_music.analysis import chords as chords_mod
    from rea_music.analysis import audio as audio_mod
    from rea_music.core.hash import file_sha256

    path = FIXTURES / "pop_c_major_120.wav"
    wav = audio_mod.ensure_wav(path, file_sha256(path))
    y, sr = _load_mono(path)
    result = chords_mod.analyze_chords(
        y, sr, [], len(y) / sr, external_engine_path=wav["mono_wav"])
    assert result["engine"] == "chordotomy_dsp"
    assert result["method"].startswith("chordotomy_")
    assert result["model_version"]


def test_chords_template_engine_explicit():
    from rea_music.analysis import chords as chords_mod
    path = FIXTURES / "pop_c_major_120.wav"
    y, sr = _load_mono(path)
    from rea_music.analysis import tempo as tempo_mod
    beats = tempo_mod.analyze_tempo(y, sr)["beats_time"]
    result = chords_mod.analyze_chords(
        y, sr, beats, len(y) / sr, engine="template")
    assert result["engine"] == "template_v2_two_stage"
    assert len(result["segments"]) > 10


def test_chordotomy_beats_template_on_pop():
    """验收对比（单曲）：chordotomy 真值覆盖 ≥ 模板引擎（bench 全量三曲
    另由 scripts/bench_chords.py 落盘 benchmarks/chords_compare.json）。"""
    from rea_music.adapters import chordotomy
    from rea_music.analysis import chords as chords_mod
    from rea_music.analysis import tempo as tempo_mod

    truth = set(_truth("pop_c_major_120.wav")["chords"])
    y, sr = _load_mono(FIXTURES / "pop_c_major_120.wav")
    beats = tempo_mod.analyze_tempo(y, sr)["beats_time"]
    tmpl = chords_mod.analyze_chords(y, sr, beats, len(y) / sr,
                                     engine="template")
    ext = chordotomy.analyze(FIXTURES / "pop_c_major_120.wav")
    tmpl_cov = len({s["chord"] for s in tmpl["segments"]} & truth)
    ext_cov = len(({s["chord"] for s in ext["segments"]} - {"N"}) & truth)
    assert ext_cov >= tmpl_cov, \
        f"chordotomy {ext_cov} < template {tmpl_cov}"


# ---------- R2-D songbrain 无 key 降级 ----------

def test_songbrain_probe_no_key(monkeypatch):
    from rea_music.adapters import songbrain
    monkeypatch.delenv("SONGBRAIN_API_KEY", raising=False)
    status = songbrain.probe({"remote": {}})
    assert status["available"] is False
    assert status["reason"] == "songbrain_api_key_missing"
    assert status["details"]["api_key_present"] is False


def test_songbrain_key_without_authorization():
    from rea_music.adapters import songbrain
    status = songbrain.probe({"remote": {
        "songbrain_api_key": "sb_test_key",
        "songbrain_upload_authorized": False}})
    assert status["available"] is False
    assert status["reason"] == "songbrain_upload_not_authorized"
    assert status["details"]["api_key_present"] is True


def test_songbrain_cross_validate_raises_without_key(monkeypatch):
    from rea_music.adapters import songbrain
    from rea_music.core.errors import CapabilityUnavailable
    monkeypatch.delenv("SONGBRAIN_API_KEY", raising=False)
    with pytest.raises(CapabilityUnavailable) as exc_info:
        songbrain.cross_validate(str(FIXTURES / "pop_c_major_120.wav"),
                                 {"rhythm": {"bpm": {"value": 120.0}}},
                                 cfg={"remote": {}})
    assert "missing_reason" in json.dumps(exc_info.value.details)


def test_registry_remote_status_honest(monkeypatch):
    from rea_music.adapters import registry
    monkeypatch.delenv("SONGBRAIN_API_KEY", raising=False)
    remote = registry.remote_status({})
    sb = remote["songbrain_api"]
    assert sb["available"] is False
    assert sb["missing_reason"] == "songbrain_api_key_missing"


def test_songbrain_key_normalization():
    from rea_music.adapters import songbrain
    assert songbrain.normalize_key("C:maj") == ("C", "major")
    assert songbrain.normalize_key("A minor") == ("A", "minor")
    assert songbrain.normalize_key("F#maj") == ("F#", "major")
    assert songbrain.normalize_key("garbage!!") is None
    assert songbrain.normalize_key(None) is None


# ---------- Deep 档全链（三曲） ----------

@pytest.mark.parametrize("fixture", DEEP_FIXTURES)
def test_deep_pipeline_full_chain(fixture):
    """Deep 档三曲全链：真实分轨 + 转写 + 和弦，DNA 全绿。

    首跑约 1.5x 曲长/曲（demucs CPU）；缓存命中后为回归模式。"""
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.core import jobs as jobs_mod
    from rea_music.dna.validate import validate

    job = jobs_mod.create_job("deep_test", {"fixture": fixture})
    out = jobs_mod.run_inline(pipeline_mod.analyze_track_job, job,
                              str(FIXTURES / fixture), "deep", False)
    assert out["status"] == "succeeded", f"deep 分析失败: {out.get('error')}"
    dna = json.loads(Path(out["result_ref"]).read_text(encoding="utf-8"))

    assert validate(dna) == []
    assert dna["song"]["analysis_tier"] == "deep"

    # R2-A: 分轨字段真实填充
    stems = dna["instrumentation"]["stems"]
    assert stems["value"] is not None
    assert set(stems["value"]) == {"drums", "bass", "other", "vocals"}
    assert stems["method"] == "demucs_htdemucs"
    assert stems["model_version"]
    assert stems["elapsed_sec"] > 0
    for info in stems["value"].values():
        assert Path(info["path"]).exists()

    # R2-B: 旋律音符 + MIDI 工件（fixtures 无真实人声 → other 轨，如实标注）
    notes_fv = dna["melody"]["notes"]
    assert isinstance(notes_fv["value"], list) and notes_fv["value"], \
        f"旋律转写为空: {notes_fv}"
    assert notes_fv["method"] == "basic_pitch_icassp_2022"
    assert notes_fv["source_stem"] in ("vocals", "other")
    midi_fv = dna["melody"]["midi_path"]
    assert midi_fv and Path(midi_fv["value"]).exists()
    rng_fv = dna["melody"]["vocal_range_semitones"]
    assert rng_fv["value"] > 0
    if notes_fv["source_stem"] != "vocals":
        assert "非人声音域" in rng_fv.get("note", "")

    # R2-C: 和弦主引擎标注
    assert dna["harmony"]["chord_segments"]["engine"] == "chordotomy_dsp"

    # R2-D: 无 key → 交叉验证字段缺省（不伪造）
    assert "cross_validation" not in dna["provenance_summary"]


def test_deep_pipeline_no_key_keeps_melody_honest():
    """fixtures 无真实人声：vocals fine_metrics 保持 requires_vocal_stem
    诚实缺失（不入声轨不编造人声指标）。"""
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.core import jobs as jobs_mod
    job = jobs_mod.create_job("deep_honesty", {})
    out = jobs_mod.run_inline(pipeline_mod.analyze_track_job, job,
                              str(FIXTURES / "pop_c_major_120.wav"), "deep",
                              False)
    dna = json.loads(Path(out["result_ref"]).read_text(encoding="utf-8"))
    fm = dna["vocals"]["fine_metrics"]
    assert "requires_vocal_stem" in json.dumps(fm)


# ---------- 错误路径（引擎缺失 monkeypatch） ----------

def test_separate_stems_mcp_engine_missing(monkeypatch):
    """MCP separate_stems：引擎缺失 → 结构化 capability_unavailable。"""
    from rea_music.adapters import registry
    from rea_music.mcp import server

    monkeypatch.setattr(
        registry, "separation_status",
        lambda: {"available": False, "engine": "demucs_htdemucs",
                 "unavailable_reason": "demucs_not_importable",
                 "install_hint": "uv pip install torch demucs"})
    result = server.separate_stems(str(FIXTURES / "pop_c_major_120.wav"))
    assert result["ok"] is False
    assert result["error"]["code"] == "capability_unavailable"
    assert "hint" in result["error"]


def test_demucs_separate_engine_missing(monkeypatch):
    from rea_music.adapters import demucs
    from rea_music.core.errors import CapabilityUnavailable
    monkeypatch.setattr(demucs, "probe",
                        lambda: {"available": False,
                                 "reason": "torch_not_importable",
                                 "details": {}})
    with pytest.raises(CapabilityUnavailable):
        demucs.separate(FIXTURES / "pop_c_major_120.wav", "deadbeef" * 8)


def test_chords_fallback_when_chordotomy_missing(monkeypatch):
    """auto 引擎 + chordotomy 缺失 → 模板后备 + 如实 fallback 标注。"""
    from rea_music.adapters import chordotomy
    from rea_music.analysis import chords as chords_mod

    monkeypatch.setattr(chordotomy, "probe",
                        lambda: {"available": False,
                                 "reason": "chordotomy_cli_not_found",
                                 "details": {}})
    path = FIXTURES / "pop_c_major_120.wav"
    y, sr = _load_mono(path)
    from rea_music.analysis import tempo as tempo_mod
    beats = tempo_mod.analyze_tempo(y, sr)["beats_time"]
    result = chords_mod.analyze_chords(
        y, sr, beats, len(y) / sr, external_engine_path=str(path))
    assert result["engine"] == "template_v2_two_stage"
    assert result["engine_fallback_note"] == "chordotomy_unavailable"


def test_chords_explicit_chordotomy_missing_raises(monkeypatch):
    from rea_music.adapters import chordotomy
    from rea_music.analysis import chords as chords_mod
    from rea_music.core.errors import CapabilityUnavailable

    monkeypatch.setattr(chordotomy, "probe",
                        lambda: {"available": False,
                                 "reason": "chordotomy_cli_not_found",
                                 "details": {}})
    path = FIXTURES / "pop_c_major_120.wav"
    y, sr = _load_mono(path)
    with pytest.raises(CapabilityUnavailable):
        chords_mod.analyze_chords(y, sr, [], len(y) / sr,
                                  external_engine_path=str(path),
                                  engine="chordotomy")


@_DEMUCS_SKIP
def test_melody_pyin_fallback_when_bp_missing(monkeypatch):
    """bp 不可用 → pyin 粗 f0 后备；静音轨如实跳过不编造。

    需要真实分轨缓存；缓存不在当前工作区时 skip（不再假定别的用例先跑过）。
    """
    from rea_music.adapters import basicpitch, demucs
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.core.hash import file_sha256

    monkeypatch.setattr(basicpitch, "probe",
                        lambda: {"available": False,
                                 "reason": "bp_venv_python_missing",
                                 "details": {}})
    chash = file_sha256(FIXTURES / "rock_g_major_150.wav")
    manifest_path = demucs.stems_out_dir(chash) / "manifest.json"
    if not manifest_path.exists():
        pytest.skip("当前工作区缺少该曲的 demucs 分轨缓存")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # 指向静音 vocals 轨：pyin 后备应诚实跳过（fixtures 无人声）
    monkeypatch.setattr(
        demucs, "pick_melody_stem",
        lambda m: ("vocals", m["stems"]["vocals"]["path"]))
    result = pipeline_mod._transcribe_melody(manifest, chash)
    assert result is not None
    assert result["method"] == "librosa_pyin_fallback"
    assert result["notes"] is None
    assert result["missing_reason"] == "stem_silent_pyin_skipped"


# ---------- benchmark 落盘（scripts/bench_chords.py 产物） ----------

def test_bench_chords_report_written():
    """benchmarks/chords_compare.json 已由 bench 脚本产出且判据达标。"""
    report_path = ROOT / "benchmarks" / "chords_compare.json"
    assert report_path.exists(), \
        "先运行: PYTHONPATH=. .venv/Scripts/python.exe scripts/bench_chords.py"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["acceptance"] == "MET"
    cov = report["summary"]["mean_truth_coverage"]
    assert cov["chordotomy"] >= cov["template"]
    assert len(report["fixtures"]) == 3
