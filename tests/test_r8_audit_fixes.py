"""R8 审计修复回归（C1-C24）。

每条用例对应审计清单里的一个编号，命名 ``test_c<编号>_<行为>``，便于回溯。
原则：
- 全部离线（网络打桩到 127.0.0.1 之外不发起任何请求；C2 用假 urlopen）；
- 重型引擎（demucs/basic-pitch/chordotomy 实盘）一律不打真跑，用合成数据或桩；
- 工作区由 conftest 的 session 级隔离夹具指向临时目录，不写仓库 data/。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

FIXTURES = ROOT / "fixtures"
SR = 44100


# ------------------------------------------------------------------ helpers

class _FakeResponse:
    """最小 urllib 响应替身：read(n) 递减，headers 为普通 dict。"""

    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self._pos = 0
        self.headers = headers or {}
        self.status = 200

    def read(self, amount: int = -1) -> bytes:
        if self._pos >= len(self._body):
            return b""
        end = (len(self._body) if amount is None or amount < 0
               else min(len(self._body), self._pos + amount))
        chunk = self._body[self._pos:end]
        self._pos = end
        return chunk

    def close(self) -> None:
        self.closed = True


def _tone(path: Path, seconds: float = 1.0, freq: float = 440.0,
          amp: float = 0.3, channels: int = 2) -> Path:
    t = np.arange(int(SR * seconds)) / SR
    mono = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    data = np.stack([mono] * channels, axis=1) if channels > 1 else mono
    import soundfile as sf
    sf.write(str(path), data, SR)
    return path


def _silence(path: Path, seconds: float = 5.0) -> Path:
    import soundfile as sf
    sf.write(str(path), np.zeros((int(SR * seconds), 2), dtype=np.float32), SR)
    return path


def _analyze(path: Path, tier: str = "fast") -> dict:
    """跑一次完整分析并返回 DNA dict（force=True，避免受既有缓存影响）。"""
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.core import jobs as jobs_mod

    job = jobs_mod.create_job("r8_regression", {"path": str(path)})
    done = jobs_mod.run_inline(pipeline_mod.analyze_track_job, job, str(path),
                               tier, True)
    assert done["status"] == "succeeded", done.get("error")
    return json.loads(Path(done["result_ref"]).read_text(encoding="utf-8"))


def _minimal_dna() -> dict:
    return {"schema": "music_dna_v1",
            "song": {"song_id": "sha256:r8", "content_hash": "a" * 64,
                     "duration_sec": 40.0, "analyzed_at": "t", "analyzer": "t",
                     "analysis_tier": "standard"},
            "rhythm": {"layer": "objective_audio_features",
                       "bpm": {"value": 120.0, "confidence": 0.9},
                       "meter_beats": {"value": 4}},
            "tonality": {"layer": "objective_audio_features",
                         "key": {"value": "C major", "confidence": 0.8}},
            "harmony": {"layer": "inferred",
                        "chord_segments": {"value": [
                            {"start_sec": 0.0, "end_sec": 2.0, "chord": "C:maj"},
                            {"start_sec": 2.0, "end_sec": 4.0, "chord": "G:maj"},
                        ]},
                        "progressions": {"value": []},
                        "harmonic_rhythm_beats": {"value": 4.0}},
            "form": {"layer": "inferred", "segments": []},
            "production": {"layer": "objective_audio_features",
                           "integrated_lufs": {"value": -14.0},
                           "spectral_centroid_hz": {"value": 2000.0},
                           "harmonic_percussive_ratio": {"value": 0.5},
                           "rms_curve_1s": {"value": []}},
            "instrumentation": {"layer": "inferred",
                                "band_energy_ratios": {"value": {}}},
            "creative_semantics": {"layer": "interpretive",
                                   "style_tags": {"value": ["mid_tempo"]}},
            "vocals": {"layer": "inferred", "presence_estimate": 0.3},
            "provenance_summary": {"analyzer": "t", "schema": "music_dna_v1",
                                   "tier": "standard", "engines_used": []}}


# ------------------------------------------------------------------ C1 并发写

def test_c1_concurrent_job_updates_keep_record_readable():
    """并发 update_job 不得产出半截 JSON、不得丢字段、读取端不得崩。"""
    from rea_music.core import jobs as jobs_mod
    from rea_music.core.paths import jobs_dir

    job = jobs_mod.create_job("c1", {})
    job_id = job["job_id"]
    path = jobs_dir() / f"{job_id}.json"
    errors: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        # 走真实 API：轮询方（MCP get_job_status）看到的就是这条路径
        while not stop.is_set():
            try:
                jobs_mod.get_job(job_id)
            except Exception as exc:  # noqa: BLE001 —— 任何异常都是缺陷证据
                errors.append(f"{type(exc).__name__}: {exc}")

    def writer(index: int) -> None:
        for step in range(20):
            jobs_mod.update_job(job_id, **{f"w{index}": step,
                                           "stage": f"s{index}_{step}"})

    watcher = threading.Thread(target=reader, daemon=True)
    watcher.start()
    workers = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    stop.set()
    watcher.join(timeout=5)

    assert errors == [], f"并发读到了坏记录: {errors[:3]}"
    final = json.loads(path.read_text(encoding="utf-8"))
    assert [f"w{i}" for i in range(6) if f"w{i}" not in final] == []
    assert final["w0"] == 19                              # 最后一次写入生效


# ------------------------------------------------------------------ C2 断流

def test_c2_truncated_download_is_rejected(monkeypatch, tmp_path):
    """Content-Length 与实际字节数不符 → BackendUnavailable，且不留半截文件。"""
    from rea_music.core import source as source_mod
    from rea_music.core.errors import BackendUnavailable

    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    body = b"fLaC" + b"\x00" * 4996                     # 实际 5000 字节
    url = "http://media.invalid/truncated.flac"

    monkeypatch.setattr(
        source_mod, "urlopen",
        lambda request, timeout=None: _FakeResponse(
            body, {"Content-Type": "audio/flac",
                   "Content-Length": "200000"}))

    with pytest.raises(BackendUnavailable) as excinfo:
        source_mod.resolve_source(url)

    err = excinfo.value
    assert err.code == "backend_unavailable"
    assert err.details["expected_bytes"] == 200000
    assert err.details["written_bytes"] == len(body)
    out_dir = source_mod.downloads_dir() / source_mod._url_key(url)
    leftovers = [p.name for p in out_dir.iterdir()] if out_dir.is_dir() else []
    assert leftovers == [], f"半截文件被留下: {leftovers}"


def test_c2_cached_download_size_mismatch_is_ignored(monkeypatch, tmp_path):
    """侧车记录的字节数与落盘不一致 → 视为未命中，重新下载。"""
    from rea_music.core import source as source_mod

    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    url = "http://media.invalid/size.flac"
    out_dir = source_mod._target_dir(url)
    dest = out_dir / "size.flac"
    dest.write_bytes(b"fLaC" + b"\x00" * 100)            # 实际 104 字节

    meta = {"kind": "http_media", "tool": "urllib", "sha256": None,
            "content_type": "audio/flac", "file_name": "size.flac"}
    source_mod._write_meta(url, {**meta, "bytes": 999999})
    assert source_mod._cached_download(url) is None       # 半截缓存不复用

    source_mod._write_meta(url, {**meta, "bytes": 104})
    cached = source_mod._cached_download(url)
    assert cached is not None and cached["local_path"].endswith("size.flac")


# ------------------------------------------------------- C25 SSRF 护栏（R8+）

def test_c25_metadata_endpoint_is_blocked(monkeypatch, tmp_path):
    """云元数据地址永远拒绝，且在发起任何请求之前就拒绝。"""
    from rea_music.core import source as source_mod
    from rea_music.core.errors import ValidationError

    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    calls: list = []
    monkeypatch.setattr(source_mod, "urlopen",
                        lambda *a, **kw: calls.append(a) or _FakeResponse(b""))

    with pytest.raises(ValidationError) as excinfo:
        source_mod.resolve_source(
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/")
    assert excinfo.value.details["blocked"], excinfo.value.details
    assert calls == [], "被拒地址不允许真的发出请求"

    # allow_private 也救不了元数据地址（无开关）
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"input": {"allow_private_networks": True}}), encoding="utf-8")
    with pytest.raises(ValidationError):
        source_mod.resolve_source("http://169.254.169.254/latest/meta-data/")


def test_c25_malformed_url_is_validation_error(monkeypatch, tmp_path):
    """缺主机名的链接在入口即报 validation_error，不被放行到网络层。"""
    from rea_music.core import source as source_mod
    from rea_music.core.errors import ValidationError

    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    calls: list = []
    monkeypatch.setattr(source_mod, "urlopen",
                        lambda *a, **kw: calls.append(a) or _FakeResponse(b""))

    with pytest.raises(ValidationError) as excinfo:
        source_mod.resolve_source("http://")
    assert "主机" in excinfo.value.message
    assert calls == []


def test_c25_private_network_needs_explicit_opt_in(monkeypatch, tmp_path):
    """私网/回环默认拒绝；配置 input.allow_private_networks=true 后放行到网络层。"""
    from rea_music.core import source as source_mod
    from rea_music.core.errors import BackendUnavailable, ValidationError
    from urllib.error import URLError

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("REA_MUSIC_HOME", str(home))
    url = "http://127.0.0.1:9/nas/song.flac"

    with pytest.raises(ValidationError) as excinfo:
        source_mod.resolve_source(url)
    assert "127.0.0.1" in str(excinfo.value)

    def _refused(request, timeout=None):  # noqa: ARG001
        raise URLError("connection refused")

    monkeypatch.setattr(source_mod, "urlopen", _refused)
    (home / "config.json").write_text(
        json.dumps({"input": {"allow_private_networks": True}}), encoding="utf-8")
    with pytest.raises(BackendUnavailable):     # 已过护栏，错在网络层
        source_mod.resolve_source(url)


# ------------------------------------------------------------------ C3 修订闭环

def test_c3_revise_writes_numeric_target_and_renders(tmp_path, monkeypatch):
    """结构化 target 必须落成数值参数，且两条后端渲染都不再崩。"""
    from rea_music.generation.backends import (build_yue2_style,
                                               render_generation_request)
    from rea_music.generation.plan import create_composition_plan
    from rea_music.mcp import server as srv

    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    plan = create_composition_plan(dna_refs=[_minimal_dna()],
                                    objective="inherit_style_new_song")
    evaluation = {
        "evaluation_id": "eval_r8",
        "revision_suggestions": [
            {"dimension": "节奏符合度", "knob": "tempo_bpm", "target": 92.0,
             "adjustment": "把生成请求 BPM 调整到 92.0"},
            {"dimension": "调性符合度", "knob": "key", "target": "A minor",
             "adjustment": "指定调性 A minor"},
        ],
    }
    result = srv.revise_plan(plan, evaluation)

    assert result["ok"] is True and result["revised"] is True
    revised = result["plan"]
    # inherit 保留参考曲的继承值；修订目标写进 target_modifications
    assert revised["inherit"]["tempo_bpm"] == 120.0
    assert revised["target_modifications"]["bpm"] == 92.0        # 数值，不是句子
    assert revised["target_modifications"]["key"] == "A minor"
    assert revised["revision_history"]

    # 渲染层必须让 target_modifications 覆盖 inherit，否则修订等于空转
    suno = render_generation_request(revised, "suno")
    assert "92 bpm" in suno["style_prompt"]
    assert "A minor tonality" in suno["style_prompt"]
    yue2 = render_generation_request(revised, "yue2")
    assert yue2["request"]["style"]["tempo_bpm"] == 92.0
    assert "92 BPM" in build_yue2_style(revised, "hello")


def test_c3_legacy_prose_suggestion_is_skipped_not_written(tmp_path, monkeypatch):
    """只有自然语言说明的旧式建议：不猜测解析，记入 skipped 且不改 plan。"""
    from rea_music.generation.plan import create_composition_plan
    from rea_music.mcp import server as srv

    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    plan = create_composition_plan(dna_refs=[_minimal_dna()],
                                    objective="inherit_style_new_song")
    legacy = {"evaluation_id": "eval_legacy",
              "revision_suggestions": [
                  {"dimension": "节奏符合度", "knob": "tempo_bpm",
                   "adjustment": "把生成请求 BPM 调整到 92.0"}]}
    result = srv.revise_plan(plan, legacy)

    assert result["revised"] is False
    assert result["skipped"][0]["reason"] == "missing_numeric_target"


def test_c3_plan_rejects_prose_in_bpm(tmp_path, monkeypatch):
    """target_modifications.bpm 收到自然语言 → ValidationError，不静默写坏 plan。"""
    from rea_music.core.errors import ValidationError
    from rea_music.generation.plan import create_composition_plan

    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    with pytest.raises(ValidationError) as excinfo:
        create_composition_plan(
            dna_refs=[_minimal_dna()], objective="modify_dimension",
            target_modifications={"bpm": "把生成请求 BPM 调整到 92.0"})
    assert excinfo.value.code == "validation_error"


# ------------------------------------------------------------------ C4/C5 响度

def test_c4_silence_dna_is_strict_json(tmp_path, monkeypatch):
    """静音曲的 DNA 必须是严格 JSON（不允许 -Infinity/NaN），响度记 None。"""
    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    dna = _analyze(_silence(tmp_path / "silence.wav", seconds=5.0))
    raw = json.dumps(dna, ensure_ascii=False)

    assert "-Infinity" not in raw and "Infinity" not in raw
    assert "NaN" not in raw
    lufs = dna["production"]["integrated_lufs"]
    assert lufs["value"] is None
    assert lufs["missing_reason"] == "silent_block"
    assert dna["production"]["short_term_lufs_series"]["value"] == []


def test_c5_short_audio_is_honest_not_internal_error(tmp_path, monkeypatch):
    """<0.4s 输入：分析成功，响度/节拍如实缺失（不再抛 internal_error）。"""
    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    dna = _analyze(_tone(tmp_path / "short.wav", seconds=0.35))

    lufs = dna["production"]["integrated_lufs"]
    assert lufs["value"] is None
    assert lufs["missing_reason"] == "audio_too_short"
    assert dna["rhythm"]["bpm"]["value"] is None
    assert dna["rhythm"]["bpm"]["missing_reason"] == "no_pulse_detected"


# ------------------------------------------------------------------ C6 无节拍

def test_c6_steady_tone_has_no_pulse(tmp_path):
    """稳态正弦没有可追踪瞬态：BPM 必须缺失，而不是高置信度乱报。"""
    from rea_music.analysis import audio as audio_mod
    from rea_music.analysis import tempo as tempo_mod

    path = _tone(tmp_path / "drone.wav", seconds=6.0, freq=220.0, channels=1)
    wav = audio_mod.ensure_wav(str(path))
    y, sr = audio_mod.load_mono(wav["mono_wav"])
    result = tempo_mod.analyze_tempo(y, sr)

    assert result["bpm"] is None
    assert result["missing_reason"] == "no_pulse_detected"
    assert result["beat_count"] == 0


def test_c6_drum_loop_still_detected():
    """门控不能误伤真实节拍：合成鼓组仍要读出 BPM 与置信度。"""
    sys.path.insert(0, str(ROOT / "tests"))
    from test_analysis_quality import backbeat
    from rea_music.analysis import tempo as tempo_mod

    result = tempo_mod.analyze_tempo(backbeat(92.0, duration=12.0), SR)
    assert 91.0 <= result["bpm"] <= 93.0
    assert result["confidence"] >= 0.6


# ------------------------------------------------------------------ C7 单位换算

def test_c7_harmonic_rhythm_uses_measured_bpm():
    """平均每和弦拍数必须按实测 BPM 换算（不是固定的 120 BPM 除数）。"""
    from rea_music.dna import model as model_mod

    segments = [{"start_sec": 0.0, "end_sec": 1.75, "chord": "C:maj"}]
    value, missing = model_mod._harmonic_rhythm(segments, 149.8)
    assert missing is None
    assert value == pytest.approx(1.75 * 149.8 / 60.0, abs=0.01)   # ≈4.37

    none_value, none_missing = model_mod._harmonic_rhythm(segments, None)
    assert none_value is None and none_missing == "requires_bpm"
    empty_value, empty_missing = model_mod._harmonic_rhythm([], 120.0)
    assert empty_value is None and empty_missing == "no_chord_segments"


def test_c7_dna_harmonic_rhythm_field_carries_value_and_reason():
    from rea_music.dna import model as model_mod

    common = {"content_hash": "b" * 64, "meta": {"duration_sec": 4.0},
              "key": {}, "structure": None, "loudness": {}, "spectral": {},
              "vocal": None, "tier": "standard", "duration_sec": 4.0}
    chords = {"segments": [{"start_sec": 0.0, "end_sec": 2.0, "chord": "C:maj"},
                           {"start_sec": 2.0, "end_sec": 4.0, "chord": "G:maj"}]}
    tempo = {"bpm": 120.0, "confidence": 0.9, "method": "x"}
    dna = model_mod.build_music_dna(tempo=tempo, chords=chords, **common)
    field = dna["harmony"]["harmonic_rhythm_beats"]
    assert field["value"] == pytest.approx(4.0)          # 2s/和弦 @120BPM = 4 拍
    assert field["unit"] == "beats/chord"

    no_bpm = model_mod.build_music_dna(
        tempo={"bpm": None, "confidence": 0.0, "method": "x",
               "missing_reason": "no_pulse_detected"}, chords=chords, **common)
    assert no_bpm["harmony"]["harmonic_rhythm_beats"]["value"] is None
    assert no_bpm["harmony"]["harmonic_rhythm_beats"]["missing_reason"] == "requires_bpm"


# ------------------------------------------------------------------ C8 无音高

def test_c8_silence_has_no_key():
    """全零信号：不报具体调性（早前按 max() 字典序报成 B minor）。"""
    from rea_music.analysis.key import analyze_key

    result = analyze_key(np.zeros(SR * 5, dtype=np.float32), SR)
    assert result["key"] is None
    assert result["missing_reason"] == "no_pitch_content"
    assert result["confidence"] == 0.0


def test_c8_real_key_still_detected():
    from rea_music.analysis.key import analyze_key
    import soundfile as sf

    y, sr = sf.read(str(FIXTURES / "pop_c_major_120.wav"), always_2d=True)
    result = analyze_key(y.mean(axis=1), sr)
    assert result["key"] == "C major"
    assert "missing_reason" not in result


# ------------------------------------------------------------------ C9 坏 job 文件

def test_c9_corrupt_job_record_is_structured(tmp_path, monkeypatch):
    from rea_music.core import jobs as jobs_mod
    from rea_music.core.errors import REAError
    from rea_music.core.paths import jobs_dir

    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    (jobs_dir() / "broken.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(REAError) as excinfo:
        jobs_mod.get_job("broken")
    assert excinfo.value.code == "job_corrupt"

    assert jobs_mod.list_jobs() == []          # 坏项跳过，不炸整张表


# ------------------------------------------------------------------ C10 段落能量

def test_c10_energy_target_comes_from_reference_curve(tmp_path, monkeypatch):
    """energy_target 必须取自参考曲 rms 曲线，而不是硬编码标签表。"""
    from rea_music.generation.plan import create_composition_plan

    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    dna = _minimal_dna()
    dna["form"] = {"layer": "inferred", "segments": [
        {"index": 0, "start_sec": 0.0, "end_sec": 7.5,
         "label": {"value": "intro"}},
        {"index": 1, "start_sec": 7.5, "end_sec": 40.0,
         "label": {"value": "verse"}},
    ]}
    dna["production"]["rms_curve_1s"] = {"value": [
        {"time_sec": float(t), "rms": 0.01 if t < 8 else 0.5}
        for t in range(40)]}

    plan = create_composition_plan(dna_refs=[dna],
                                    objective="inherit_style_new_song")
    intro, verse = plan["sections"][0], plan["sections"][1]
    assert intro["label"] == "intro" and verse["label"] == "verse"
    assert intro["energy_target"] == pytest.approx(0.02, abs=0.01)
    assert verse["energy_target"] == pytest.approx(1.0, abs=0.01)
    assert "time_range" not in intro                 # 内部字段不外泄


# ------------------------------------------------------------------ C11 临时目录

def test_c11_chordotomy_tempdir_cleaned_on_failure(tmp_path, monkeypatch):
    """CLI 失败时自建临时目录必须被清理（早前直接 raise 会残留）。"""
    import types

    from rea_music.adapters import chordotomy
    from rea_music.core.errors import CapabilityUnavailable

    work = tmp_path / "chord_work"
    work.mkdir()
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF0000")
    monkeypatch.setattr(chordotomy, "find_cli", lambda: "chordotomy")
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(work))
    monkeypatch.setattr(
        chordotomy.subprocess, "run",
        lambda *a, **kw: types.SimpleNamespace(returncode=1, stdout="",
                                               stderr="boom"))

    with pytest.raises(CapabilityUnavailable):
        chordotomy.analyze(str(audio))
    assert not work.exists(), "失败路径残留了临时目录"


def test_c11_chordotomy_tempdir_cleaned_even_with_extra_files(tmp_path,
                                                              monkeypatch):
    """CLI 在目录里多写了文件：清理用 rmtree，不因"目录非空"抛 OSError。"""
    import types

    from rea_music.adapters import chordotomy

    work = tmp_path / "chord_ok"
    work.mkdir()
    audio = tmp_path / "song2.wav"
    audio.write_bytes(b"RIFF0000")
    payload = {"schema_version": 11,
               "segments": [{"start_time": 0.0, "end_time": 1.0,
                             "chord": "C:maj"}],
               "global_key": {"label": "C major"},
               "generator": {"version": "0.3.1", "engine": {"name": "dsp"}},
               "beats": [0.0, 0.5]}
    out_path = work / f"{audio.stem}.chords.json"

    def _fake_run(cmd, **kwargs):
        out_path.write_text(json.dumps(payload), encoding="utf-8")
        (work / "cli.log").write_text("noise", encoding="utf-8")  # 额外产物
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(chordotomy, "find_cli", lambda: "chordotomy")
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(work))
    monkeypatch.setattr(chordotomy.subprocess, "run", _fake_run)

    result = chordotomy.analyze(str(audio))
    assert result["engine"] == chordotomy.ENGINE
    assert result["segments"][0]["chord"] == "C:maj"
    assert not work.exists()


# ------------------------------------------------------------------ C12 缓存键

def test_c12_dna_cache_key_tracks_config(monkeypatch):
    """改配置必须换缓存键（否则改了 key_window 还拿旧 DNA）。"""
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.core import cache as cache_mod
    from rea_music.core.config import load_config

    base = pipeline_mod.dna_cache_params("standard")
    changed_cfg = load_config()
    changed_cfg["analysis"]["key_window_sec"] = 999.0
    changed_cfg["analysis"]["chords_engine"] = "template"
    monkeypatch.setattr(pipeline_mod, "load_config", lambda: changed_cfg)
    changed = pipeline_mod.dna_cache_params("standard")

    assert base["key_window_sec"] != changed["key_window_sec"]
    assert base["chords_engine"] != changed["chords_engine"]
    assert "engines" in base and isinstance(base["engines"], dict)
    assert (cache_mod.cache_key("h" * 64, "dna", base)
            != cache_mod.cache_key("h" * 64, "dna", changed))


# ------------------------------------------------------------------ C13 内存

def test_c13_true_peak_is_chunked_and_keeps_boundary_peak(tmp_path):
    """分块 4x 过采样：块接缝处的峰值不能漏（含 0.1s 重叠）。"""
    from rea_music.analysis.loudness import _true_peak

    sr = 8000
    total = int(61.0 * sr)
    y = np.zeros((total, 1), dtype=np.float32)
    boundary = int(30.0 * sr)                      # 正好落在第一块末尾
    y[boundary] = 0.9
    peak_db = _true_peak(y, sr)
    assert peak_db == pytest.approx(20 * np.log10(0.9), abs=1.0)


def test_c13_model_cache_is_bounded(monkeypatch):
    """模型缓存必须有 LRU 上限（长驻进程不能无限累积 GB 级模型）。"""
    from rea_music.adapters import demucs as demucs_mod

    demucs_pretrained = pytest.importorskip("demucs.pretrained")

    class _FakeModel:
        pass

    demucs_mod._MODEL_CACHE.clear()
    monkeypatch.setattr(demucs_pretrained, "get_model", lambda name: _FakeModel())
    for name in ("a", "b", "c"):
        demucs_mod._load_model(name)

    assert len(demucs_mod._MODEL_CACHE) == demucs_mod._MODEL_CACHE_MAX
    assert "a" not in demucs_mod._MODEL_CACHE       # 最旧的被淘汰
    demucs_mod._MODEL_CACHE.clear()


# ------------------------------------------------------------------ C14 码率

def test_c14_bit_rate_is_none_not_zero(tmp_path):
    """soundfile 路径不报码率：必须是 None（未测），不能是 0.0。"""
    from rea_music.analysis.inspect import _soundfile_probe, inspect_audio

    path = _tone(tmp_path / "tone16.wav", seconds=1.0)
    probe = _soundfile_probe(path)
    assert probe is not None and probe["format"]["bit_rate"] is None
    meta = inspect_audio(str(path))
    assert meta["bit_rate_kbps"] is None


# ------------------------------------------------------------------ C17 ACE 路由文档

def test_c17_routes_doc_defaults_to_workspace_and_degrades(tmp_path, monkeypatch):
    """默认写 workspace；写盘失败只降级返回 None，不抛 OSError。"""
    from rea_music.core.paths import workspace_root
    from rea_music.generation import acestep

    routes = [{"method": "GET", "path": "/health", "operation_id": "health",
               "summary": "", "params": [], "json_body": False}]
    written = acestep._write_routes_doc(routes, "http://127.0.0.1:8001")
    assert written is not None
    assert str(written).startswith(str(workspace_root())), written
    assert not str(written).startswith(str(ROOT / "docs"))

    # 目录当文件写 → OSError → 返回 None（不影响 probe 结论）
    assert acestep._write_routes_doc(routes, "http://x",
                                     doc_path=str(tmp_path)) is None


# ------------------------------------------------------------------ C21/C22 测试隔离

def test_c21_fixture_bootstrap_available():
    """conftest 的 session 自举保证 fixtures 在任何单文件运行下都存在。"""
    assert (FIXTURES / "pop_c_major_120.wav").is_file()


def test_c22_workspace_is_isolated_from_repo():
    """测试工作区不得落在仓库 data/ 内。"""
    from rea_music.core import jobs as jobs_mod
    from rea_music.core import paths as paths_mod

    workspace = str(paths_mod.workspace_root())
    assert not workspace.startswith(str(ROOT / "data")), workspace
    job = jobs_mod.create_job("isolation", {})
    record = paths_mod.jobs_dir() / f"{job['job_id']}.json"
    assert record.is_file()
    assert not str(record).startswith(str(ROOT / "data"))


# ------------------------------------------------------------------ C23 路径解析

def test_c23_get_analysis_accepts_local_path(tmp_path, monkeypatch):
    """get_analysis 传长路径时不能再被当成哈希而静默返回 None。"""
    from rea_music.analysis import pipeline as pipeline_mod

    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "home"))
    deep_dir = (tmp_path / "a_very_long_directory_name_for_cache_key"
                / "another_long_subdirectory_name")
    deep_dir.mkdir(parents=True, exist_ok=True)
    path = _tone(deep_dir / "song.wav", seconds=2.0)
    _analyze(path)

    dna = pipeline_mod.get_analysis(str(path), "fast")
    assert dna is not None
    assert dna["song"]["content_hash"]
    # 哈希前缀仍然可用
    assert pipeline_mod.get_analysis(dna["song"]["content_hash"][:12], "fast")


# ------------------------------------------------------------------ C24 比较缺维度

def test_c24_compare_reports_missing_dimensions():
    """两首无数据曲目比较：不能只回一个空 dimensions 冒充成功。"""
    from rea_music.comparison.compare import compare_tracks

    empty_a = _minimal_dna()
    empty_b = _minimal_dna()
    empty_a["song"]["song_id"] = "sha256:a"
    empty_b["song"]["song_id"] = "sha256:b"
    for dna in (empty_a, empty_b):
        dna["rhythm"]["bpm"] = {"value": None}
        dna["tonality"]["key"] = {"value": None}
        dna["production"]["rms_curve_1s"] = {"value": []}
        dna["production"]["integrated_lufs"] = {"value": None}
        dna["production"]["spectral_centroid_hz"] = {"value": None}
        dna["form"]["segments"] = []

    result = compare_tracks(empty_a, empty_b)
    assert result["dimensions"] == {}
    assert result["comparable"] is False
    assert set(result["missing_dimensions"]) == {
        "tempo", "key", "energy_curve", "timbre", "loudness", "structure"}


def test_c25_dns_failure_is_not_reported_as_policy_block(monkeypatch):
    """DNS 解析失败与策略拦截必须分开：前者交给下载层报 backend_unavailable。

    回归 R3：HostUnresolvable 分流此前无测试覆盖。
    """
    from rea_music.core import net_guard
    from rea_music.core.source import _assert_url_allowed

    def boom(host, port):  # noqa: ANN001
        import socket
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(net_guard, "_resolve_addresses", boom)
    # 入口不应把"没网/域名拼错"当成地址违规抛出
    _assert_url_allowed("http://definitely-not-resolvable.example/song.mp3")
    # 但直接调用底层的契约是抛 HostUnresolvable（ValidationError 子类）
    import pytest
    with pytest.raises(net_guard.HostUnresolvable) as ei:
        net_guard.assert_public_url("http://definitely-not-resolvable.example/x")
    assert ei.value.code == "host_unresolvable"
