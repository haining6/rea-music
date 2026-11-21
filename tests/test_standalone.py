"""独立运行回归：干净 pip 环境（PATH 上没有 ffmpeg / ffprobe）也能跑通核心格式。

背景（用户硬性要求「其他人部署后就能用，不需要其他依赖」）：纯
``pip install -e .`` 的 venv 里没有任何外部可执行文件。历史实现对所有输入
无条件调用 ffprobe，导致连标准 wav 都被误报 ``invalid_audio``。

本文件把"没有 FFmpeg"当作硬约束，验证：
1. wav/flac/ogg 等核心格式的 inspect → decode → analyze → DNA 全链路可用；
2. 核心格式探测**不产生任何子进程**（零外部依赖的直接证据）；
3. 仅 FFmpeg 可解的容器（m4a/aac/mp4/webm）如实降级为
   ``capability_unavailable`` + 安装提示，而不是谎报格式非法；
4. capabilities 如实报告 FFmpeg 的可用状态。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # rea_music 包根在 sys.path

from rea_music.adapters import registry  # noqa: E402
from rea_music.analysis import audio as audio_mod  # noqa: E402
from rea_music.analysis import inspect as inspect_mod  # noqa: E402
from rea_music.core import source as source_mod  # noqa: E402
from rea_music.core.errors import (CapabilityUnavailable, InvalidAudio,  # noqa: E402
                                   REAError)
from rea_music.dna import validate as dna_validate_mod  # noqa: E402

FIXTURE = ROOT / "fixtures" / "pop_c_major_120.wav"
TRUTH = json.loads(
    (ROOT / "fixtures" / "pop_c_major_120.truth.json").read_text(encoding="utf-8"))
_FFMPEG_BIN_PREFIXES = ("ffmpeg", "ffprobe")


# ---------------------------------------------------------------- helpers

@pytest.fixture
def no_ffmpeg(monkeypatch):
    """模拟干净 pip 环境：任何 ffmpeg / ffprobe 子进程都当作"不存在"。"""
    real_run = subprocess.run

    def _run(cmd, *args, **kwargs):
        exe = str(cmd[0]) if isinstance(cmd, (list, tuple)) and cmd else str(cmd)
        if Path(exe).name.lower().startswith(_FFMPEG_BIN_PREFIXES):
            raise FileNotFoundError(2, "No such file or directory", exe)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)
    return monkeypatch


def _home(monkeypatch, tmp_path: Path) -> Path:
    """把工作区（缓存 / 任务 / DNA）重定向到 tmp_path。"""
    home = (tmp_path / "home").resolve()
    monkeypatch.setenv("REA_MUSIC_HOME", str(home))
    return home


def _tone(path: Path, sf_format: str, seconds: float = 1.0) -> Path:
    """用 soundfile 造真音频（不依赖 ffmpeg 写文件）。"""
    import soundfile as sf
    sr = 44100
    t = np.linspace(0.0, seconds, int(sr * seconds), endpoint=False)
    y = (0.1 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    sf.write(str(path), y, sr, format=sf_format)
    return path


def _ffprobe_like() -> dict:
    """ffprobe 典型输出（wav 容器 + 立体声音频流），用于替代真二进制的分支。"""
    return {"streams": [{"codec_type": "audio", "codec_name": "pcm_s16le",
                         "sample_rate": "44100", "channels": 2,
                         "bits_per_sample": 16}],
            "format": {"format_name": "wav", "duration": "12.5",
                       "bit_rate": "1411200"}}


def _stub_engine_probes(monkeypatch) -> None:
    """引擎探测打桩：capabilities 测试只关心输入段，保持秒级完成。"""
    monkeypatch.setattr(registry, "separation_status",
                        lambda: {"available": False, "engine": "demucs",
                                 "install_hint": "t"})
    monkeypatch.setattr(registry, "chord_engine_status",
                        lambda: {"available": True})
    monkeypatch.setattr(registry, "transcription_status",
                        lambda: {"available": True})
    monkeypatch.setattr(registry, "generation_status",
                        lambda cfg=None: {"backends": {}})
    monkeypatch.setattr(registry, "remote_status", lambda cfg: {})


# ---------------------------------------------------------------- inspect

def test_inspect_wav_without_ffmpeg(no_ffmpeg):
    """核心回归：PATH 上没有 ffprobe 时，标准 wav 仍能被完整识别。"""
    meta = inspect_mod.inspect_audio(str(FIXTURE))

    assert meta["ok"] is True
    assert meta["probed_by"] == "soundfile"           # 溯源：内置解码库出的结论
    assert meta["duration_sec"] == pytest.approx(TRUTH["duration_sec"], abs=0.5)
    assert meta["sample_rate_hz"] == 44100
    assert meta["channels"] == 2
    assert meta["container"] == "wav"
    assert str(meta["codec"]).startswith("pcm")
    assert meta["bit_depth"] == 16
    assert meta["content_hash"] and len(meta["content_hash"]) == 64
    assert meta["source"]["kind"] == "local"


def test_core_format_never_spawns_any_process(monkeypatch):
    """零外部依赖的直接证据：核心格式探测不许产生任何子进程。"""
    calls: list = []

    def _spy(cmd, *args, **kwargs):  # noqa: ARG001
        calls.append(cmd)
        raise AssertionError(f"核心格式不应调用外部程序: {cmd}")

    monkeypatch.setattr(subprocess, "run", _spy)
    meta = inspect_mod.inspect_audio(str(FIXTURE))

    assert meta["probed_by"] == "soundfile"
    assert calls == []


@pytest.mark.parametrize(("suffix", "sf_format"), [
    ("wav", "WAV"), ("flac", "FLAC"), ("ogg", "OGG"), ("oga", "OGG"),
    ("aiff", "AIFF"),
])
def test_core_formats_probe_without_ffmpeg(tmp_path, no_ffmpeg, suffix,
                                           sf_format):
    """wav/flac/ogg/aiff 全部由 soundfile 直读，不依赖外部程序。"""
    path = _tone(tmp_path / f"tone.{suffix}", sf_format)
    meta = inspect_mod.inspect_audio(str(path))

    assert meta["probed_by"] == "soundfile"
    assert meta["sample_rate_hz"] == 44100
    assert meta["channels"] == 1
    assert meta["duration_sec"] == pytest.approx(1.0, abs=0.1)


def test_unknown_extension_wav_falls_back_to_soundfile(tmp_path, no_ffmpeg):
    """无扩展名 / 冷门后缀但实为 wav：缺 ffprobe 时按内容实测兜底。"""
    path = _tone(tmp_path / "no_suffix_audio", "WAV")
    meta = inspect_mod.inspect_audio(str(path))

    assert meta["probed_by"] == "soundfile"
    assert meta["sample_rate_hz"] == 44100
    assert meta["container"] == "wav"


def test_ffmpeg_only_container_without_ffmpeg_is_capability_unavailable(
        tmp_path, no_ffmpeg):
    """m4a 这类只有 FFmpeg 能解的容器：如实降级，不谎报 invalid_audio。"""
    path = tmp_path / "fake.m4a"
    path.write_bytes(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 64)

    with pytest.raises(CapabilityUnavailable) as excinfo:
        inspect_mod.inspect_audio(str(path))

    err = excinfo.value
    assert err.code == "capability_unavailable"        # 不是 invalid_audio
    assert "FFmpeg" in err.message
    assert "FFmpeg" in (err.hint or "")
    assert "install" in (err.hint or "").lower()
    assert err.details["requires"] == "ffmpeg"
    assert err.details["suffix"] == ".m4a"


def test_container_with_ffprobe_present_stays_invalid_audio(tmp_path,
                                                            monkeypatch):
    """有 ffprobe 但内容非法：维持 invalid_audio（不因缺能力而掩盖真错误）。"""
    path = tmp_path / "broken.m4a"
    path.write_bytes(b"\x00" * 32)

    def _boom(p):  # noqa: ARG001 —— 模拟 ffprobe 存在但读不了该文件
        raise InvalidAudio("ffprobe 无法读取")

    monkeypatch.setattr(inspect_mod, "ffprobe", _boom)
    with pytest.raises(InvalidAudio) as excinfo:
        inspect_mod.inspect_audio(str(path))
    assert excinfo.value.code == "invalid_audio"


def test_ffprobe_branch_marks_probed_by(tmp_path, monkeypatch):
    """非核心扩展名走 ffprobe 时，溯源字段要写 ffprobe。"""
    path = tmp_path / "cold.wv"
    path.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(inspect_mod, "ffprobe", lambda p: _ffprobe_like())

    meta = inspect_mod.inspect_audio(str(path))
    assert meta["probed_by"] == "ffprobe"
    assert meta["duration_sec"] == 12.5


# ---------------------------------------------------------------- decode / analyze

@pytest.mark.parametrize(("suffix", "sf_format"), [
    ("wav", "WAV"), ("flac", "FLAC"), ("ogg", "OGG"),
])
def test_decode_core_formats_without_ffmpeg(tmp_path, monkeypatch, no_ffmpeg,
                                            suffix, sf_format):
    """解码主路径（soundfile）不依赖 ffmpeg：flac/ogg 也能出标准化 wav。

    no_ffmpeg 夹具只提供副作用（让 ffmpeg 子进程一律 FileNotFoundError）。
    """
    _home(monkeypatch, tmp_path)
    path = _tone(tmp_path / f"decode.{suffix}", sf_format)

    out = audio_mod.ensure_wav(str(path))

    assert out["decoder"] == "soundfile"
    assert Path(out["stereo_wav"]).exists()
    assert Path(out["mono_wav"]).exists()


def test_analyze_fast_without_ffmpeg(tmp_path, monkeypatch, no_ffmpeg):
    """整条链路：inspect → Job → pipeline(fast) → DNA，全程无外部程序。"""
    from rea_music.cli.main import _analyze_one

    _home(monkeypatch, tmp_path)
    out = _analyze_one(str(FIXTURE), "fast", False)

    dna_path = Path(out["dna_path"])
    assert dna_path.exists()
    assert dna_validate_mod.validate(out["dna"]) == []      # 100% 过 schema
    assert out["dna"]["song"]["content_hash"] == out["content_hash"]
    assert out["dna"]["song"]["container"] == "wav"         # 溯源来自 soundfile
    assert out["dna"]["rhythm"]["bpm"]["value"] == pytest.approx(
        TRUTH["bpm"], abs=3.0)
    assert out["dna"]["tonality"]["key"]["value"] == TRUTH["key"]


def test_analyze_extensionless_wav_without_ffmpeg(tmp_path, monkeypatch,
                                                 no_ffmpeg):
    """无扩展名但实为 wav：inspect 与 decode 都要走内容兜底，无 FFmpeg 也出 DNA。"""
    from rea_music.cli.main import _analyze_one

    _home(monkeypatch, tmp_path)
    path = tmp_path / "track_noext"
    path.write_bytes(FIXTURE.read_bytes())      # 真实媒体内容，只是没有后缀

    out = _analyze_one(str(path), "fast", False)

    assert Path(out["dna_path"]).exists()
    assert dna_validate_mod.validate(out["dna"]) == []
    assert out["dna"]["song"]["container"] == "wav"
    assert out["dna"]["rhythm"]["bpm"]["value"] == pytest.approx(
        TRUTH["bpm"], abs=3.0)
    assert out["dna"]["tonality"]["key"]["value"] == TRUTH["key"]


def test_unknown_length_container_reports_measured_duration(tmp_path,
                                                            monkeypatch):
    """流式容器的头部没有总样本数：frames=SF_COUNT_MAX、duration 天文数字。

    必须按块扫出真实长度，而不是把 2.09e14s 丢进"时长超上限"校验。
    """
    import soundfile as sf

    path = _tone(tmp_path / "streamed.flac", "FLAC", seconds=2.0)

    class _UnknownLengthInfo:
        frames = 2 ** 63 - 1          # libsndfile SF_COUNT_MAX
        duration = 209146758205323.72
        samplerate = 44100
        channels = 1
        format = "FLAC"
        subtype = "PCM_16"

    monkeypatch.setattr(sf, "info", lambda p: _UnknownLengthInfo())

    meta = inspect_mod.inspect_audio(str(path))

    assert meta["duration_sec"] == pytest.approx(2.0, abs=0.1)
    assert meta["sample_rate_hz"] == 44100


_FFMPEG_BIN = shutil.which("ffmpeg")


@pytest.mark.skipif(_FFMPEG_BIN is None,
                    reason="需要本机 ffmpeg 生成无总样本数的 flac 流")
def test_streamed_flac_pipe_inspect_and_decode(tmp_path, monkeypatch):
    """QA 实测缺陷回归：`ffmpeg -f flac -` 产物（头部无总样本数）。

    修复前：inspect 报 `时长 209146758205324s 超过上限 900s`，
    ensure_wav 抛裸 `ValueError: array is too big`（用户看到 internal_error）。
    修复后：有 FFmpeg 时解码测出真实 20s 并正常解码；没有 FFmpeg 时如实报
    capability_unavailable（绝不拿 2e14s 去撞时长上限）。
    """
    import soundfile as sf

    home = _home(monkeypatch, tmp_path)
    proc = subprocess.run(
        [_FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
         "-i", str(FIXTURE), "-t", "20", "-f", "flac", "-"],
        capture_output=True, check=True)
    path = tmp_path / "streamed.flac"
    path.write_bytes(proc.stdout)
    assert path.stat().st_size > 0

    # 1) 有 FFmpeg：inspect 用 ffmpeg 解码测出真实时长（管道，不落盘）
    meta = inspect_mod.inspect_audio(str(path))
    assert meta["duration_sec"] == pytest.approx(20.0, abs=0.3)
    assert meta["probed_by"] == "soundfile"

    # libsndfile 定位不了这种流的末尾 → 解码必须改走 ffmpeg（沙箱里 ffmpeg 不能写
    # 文件，故只验证路由决策，不真跑 ffmpeg 写盘）
    assert audio_mod._soundfile_readable(path) is False
    routed: dict = {}

    def _fake_ffmpeg(src, stereo, mono):  # noqa: ARG001
        routed["src"] = src
        sf.write(str(stereo), np.zeros((16, 2), dtype="float32"), 44100)
        sf.write(str(mono), np.zeros((16, 1), dtype="float32"), 44100)
        return "ffmpeg"

    real_decode_ffmpeg = audio_mod._decode_ffmpeg
    monkeypatch.setattr(audio_mod, "_decode_ffmpeg", _fake_ffmpeg)
    assert audio_mod.ensure_wav(str(path))["decoder"] == "ffmpeg"
    assert Path(routed["src"]).name == "streamed.flac"
    monkeypatch.setattr(audio_mod, "_decode_ffmpeg", real_decode_ffmpeg)

    # 2) 无 FFmpeg：时长如实标 unknown（不报"超长"），分析阶段诚实报能力缺失
    def _no_binary(cmd, *args, **kwargs):  # noqa: ARG001
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(subprocess, "run", _no_binary)
    shutil.rmtree(home / "cache" / "audio", ignore_errors=True)   # 丢掉上一步的缓存

    meta_no_ffmpeg = inspect_mod.inspect_audio(str(path))
    assert meta_no_ffmpeg["ok"] is True
    assert meta_no_ffmpeg["duration_unknown"] is True
    assert meta_no_ffmpeg["duration_sec"] == 0.0     # 不编造天文数字
    assert meta_no_ffmpeg["codec"] == "pcm_16"

    with pytest.raises(CapabilityUnavailable):
        audio_mod.ensure_wav(str(path))
    from rea_music.cli.main import _analyze_one
    with pytest.raises(REAError) as excinfo:
        _analyze_one(str(path), "fast", False)       # 走 CLI 同款链路
    assert excinfo.value.code == "capability_unavailable"
    # 底层语义必须透传到 error.details，Agent 才能区分"长度未知"与"m4a 需 FFmpeg"
    assert excinfo.value.details.get("reason") == "unknown_length"
    assert excinfo.value.details.get("requires") == "ffmpeg"
    assert excinfo.value.details.get("job_id")


def test_job_error_keeps_details_and_hint(tmp_path, monkeypatch):
    """Job 落盘的错误必须保留 code/hint/details（下游只拿 message 会丢语义）。"""
    from rea_music.core import jobs as jobs_mod

    _home(monkeypatch, tmp_path)

    def _boom(job_id, *args, **kwargs):  # noqa: ARG001
        raise CapabilityUnavailable("解码失败：需要 FFmpeg", hint="装 FFmpeg",
                                    details={"reason": "unknown_length",
                                             "requires": "ffmpeg"})

    job = jobs_mod.create_job("analyze_track", {"audio_path": "x"})
    done = jobs_mod.run_inline(_boom, job)
    assert done["status"] == "failed"
    error = done["error"]
    assert error["code"] == "capability_unavailable"
    assert error["hint"] == "装 FFmpeg"
    assert error["details"]["reason"] == "unknown_length"
    assert error["details"]["requires"] == "ffmpeg"


# ---------------------------------------------------------------- 打包一致性

def test_schema_public_copy_matches_package_resource():
    """仓库根 schemas/ 是对外公开契约的副本，必须与包内资源逐字节一致。

    权威副本在 ``src/rea_music/schemas/``（随 wheel/sdist 分发）；这里防两份漂移。
    """
    package_copy = (ROOT / "src" / "rea_music" / "schemas"
                    / "music_dna_v1.schema.json")
    public_copy = ROOT / "schemas" / "music_dna_v1.schema.json"

    assert package_copy.is_file(), "包内 schema 缺失（打包必失败）"
    assert public_copy.is_file(), "公开契约副本缺失"
    assert package_copy.read_bytes() == public_copy.read_bytes()


def test_load_schema_reads_package_resource():
    """schema 必须从包内资源读，不能依赖仓库根目录（wheel 安装也要能用）。"""
    schema = dna_validate_mod.load_schema()
    assert schema.get("title") == "Music DNA v1"
    assert dna_validate_mod.schema_path().is_file()


# ---------------------------------------------------------------- capabilities

def test_capabilities_reports_ffmpeg_absent(monkeypatch):
    """干净环境：capabilities 明说 FFmpeg 不可用，并说清核心格式不受影响。"""
    monkeypatch.setattr(registry.shutil, "which", lambda name: None)
    monkeypatch.setattr(source_mod, "yt_dlp_status", lambda: {
        "available": False, "path": None, "mode": None, "launcher": [],
        "version": None, "hint": "pip install yt-dlp（或 pip install "
                                 "'rea-music[fetch]'）后重试"})
    _stub_engine_probes(monkeypatch)

    block = registry.capabilities()["input"]

    assert block["ffmpeg"]["available"] is False
    assert block["ffmpeg"]["decode_ready"] is False
    assert block["ffmpeg"]["ffprobe_path"] is None
    assert set(block["ffmpeg"]["needed_for"]) == {
        "m4a/aac/mp4/webm", "视频页链接", "非 soundfile 可解码格式"}
    assert "FFmpeg" in block["ffmpeg"]["hint"]
    assert "soundfile" in block["accepted_formats_note"]
    for ext in (".wav", ".flac", ".ogg", ".oga", ".mp3", ".opus", ".aiff"):
        assert ext in block["accepted_formats_note"]
        assert ext in block["accepted_formats"]      # 说明与清单不许自相矛盾


def test_capabilities_reports_ffmpeg_present(monkeypatch):
    """装了 FFmpeg：如实报告可用与路径。"""
    paths = {"ffprobe": "/fake/bin/ffprobe", "ffmpeg": "/fake/bin/ffmpeg"}
    monkeypatch.setattr(registry.shutil, "which", lambda name: paths.get(name))
    monkeypatch.setattr(source_mod, "yt_dlp_status", lambda: {
        "available": False, "path": None, "mode": None, "launcher": [],
        "version": None, "hint": "pip install yt-dlp（或 pip install "
                                 "'rea-music[fetch]'）后重试"})
    _stub_engine_probes(monkeypatch)

    block = registry.capabilities()["input"]

    assert block["ffmpeg"]["available"] is True
    assert block["ffmpeg"]["ffprobe_ready"] is True
    assert block["ffmpeg"]["decode_ready"] is True
    assert block["ffmpeg"]["ffprobe_path"] == "/fake/bin/ffprobe"
    assert "就绪" in block["ffmpeg"]["hint"]
    assert block["local_files"]["note"].find("ffprobe") >= 0


def test_capabilities_reports_ffmpeg_without_ffprobe(monkeypatch):
    """只有 ffmpeg、没有 ffprobe（精简构建）：hint 必须点明缺的是 ffprobe。

    此前这里会谎称"未安装 FFmpeg"，与 decode_ready=true 自相矛盾（QA 实测发现）。
    """
    paths = {"ffmpeg": "/fake/bin/ffmpeg"}
    monkeypatch.setattr(registry.shutil, "which", lambda name: paths.get(name))
    monkeypatch.setattr(source_mod, "yt_dlp_status", lambda: {
        "available": False, "path": None, "mode": None, "launcher": [],
        "version": None, "hint": "pip install yt-dlp（或 pip install "
                                 "'rea-music[fetch]'）后重试"})
    _stub_engine_probes(monkeypatch)

    block = registry.capabilities()["input"]["ffmpeg"]

    assert block["available"] is False          # 语义不变：容器探测靠 ffprobe
    assert block["ffprobe_ready"] is False
    assert block["decode_ready"] is True
    assert block["ffmpeg_path"] == "/fake/bin/ffmpeg"
    assert "ffprobe" in block["hint"]
    assert "未安装 FFmpeg" not in block["hint"]   # 不许自相矛盾
