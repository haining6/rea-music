"""输入面测试（D22）：本地任意格式 / http(s) 直链媒体 / 视频网页链接。

全部离线运行：
- 网络一律 monkeypatch（不发起真实请求，也不依赖真实 CDN 行为）；
- 外部子进程（yt-dlp / ffmpeg / ffprobe）一律打桩（沙箱内 ffmpeg 无法写文件）；
- 下载目录经 REA_MUSIC_HOME 指向 tmp_path，不污染真实 data/。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from urllib.error import HTTPError

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # rea_music 包根在 sys.path

from rea_music.adapters import registry  # noqa: E402
from rea_music.analysis import inspect as inspect_mod  # noqa: E402
from rea_music.core import source as source_mod  # noqa: E402
from rea_music.core.errors import (CapabilityUnavailable,  # noqa: E402
                                   FileNotFound, InvalidAudio, ValidationError)
from rea_music.mcp import server as srv  # noqa: E402

_YTDLP_HINT = "pip install yt-dlp（或 pip install 'rea-music[fetch]'）后重试"


# ---------------------------------------------------------------- helpers

def _tone(tmp_path: Path, name: str = "tone.wav", seconds: float = 1.0) -> Path:
    """用 soundfile 造真音频（不依赖 ffmpeg 写文件）。"""
    import soundfile as sf
    sr = 44100
    t = np.linspace(0.0, seconds, int(sr * seconds), endpoint=False)
    y = (0.1 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    path = tmp_path / name
    sf.write(str(path), y, sr)
    return path


def _probe(audio: bool = True) -> dict:
    """ffprobe 典型输出（wav 容器 + 立体声音频流）。"""
    streams = [{"codec_type": "audio", "codec_name": "pcm_s16le",
                "sample_rate": "44100", "channels": 2,
                "bits_per_sample": 16}]
    if not audio:
        streams = [{"codec_type": "video", "codec_name": "h264"}]
    return {"streams": streams,
            "format": {"format_name": "wav", "duration": "12.5",
                       "bit_rate": "1411200"}}


class _FakeResponse:
    """最小 urllib 响应替身：够用即可，但行为与真实响应一致（read(n) 递减）。"""

    def __init__(self, body: bytes, headers: dict | None = None,
                 status: int = 200):
        self._body = body
        self._pos = 0
        self.headers = headers or {}
        self.status = status
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        if self._pos >= len(self._body):
            return b""
        end = len(self._body) if amount is None or amount < 0 \
            else min(len(self._body), self._pos + amount)
        chunk = self._body[self._pos:end]
        self._pos = end
        return chunk

    def close(self) -> None:
        self.closed = True


def _urlopen_returning(body: bytes, headers: dict | None = None):
    def _fake(request, timeout=None):  # noqa: ARG001 —— 签名与 urlopen 对齐
        return _FakeResponse(body, headers)
    return _fake


def _isolate(monkeypatch, tmp_path: Path) -> Path:
    """把工作区（含 data/downloads/）重定向到 tmp_path。"""
    home = (tmp_path / "home").resolve()
    monkeypatch.setenv("REA_MUSIC_HOME", str(home))
    return home


# ---------------------------------------------------------------- 本地路径

def test_resolve_local_file(tmp_path):
    wav = _tone(tmp_path)
    out = source_mod.resolve_source(str(wav))
    assert out["kind"] == "local"
    assert out["tool"] == "soundfile"          # wav 走 soundfile 主路径
    assert Path(out["local_path"]) == wav.resolve()
    assert out["origin_url"] is None
    assert out["bytes"] == wav.stat().st_size


def test_resolve_local_container_uses_ffmpeg_tool(tmp_path):
    m4a = tmp_path / "fake.m4a"
    m4a.write_bytes(b"\x00" * 32)
    assert source_mod.resolve_source(str(m4a))["tool"] == "ffmpeg"


def test_resolve_local_missing(tmp_path):
    with pytest.raises(FileNotFound):
        source_mod.resolve_source(str(tmp_path / "no_such.wav"))


def test_resolve_rejects_directory(tmp_path):
    with pytest.raises(ValidationError):
        source_mod.resolve_source(str(tmp_path))


def test_resolve_rejects_empty_ref():
    with pytest.raises(ValidationError):
        source_mod.resolve_source("   ")


@pytest.mark.parametrize("ref", ["ftp://example.com/a.mp3",
                                 "file:///tmp/a.mp3",
                                 "rtmp://example.com/live"])
def test_resolve_rejects_bad_scheme(ref):
    with pytest.raises(ValidationError) as excinfo:
        source_mod.resolve_source(ref)
    assert excinfo.value.code == "validation_error"
    assert "http" in (excinfo.value.hint or "")


def test_is_remote_does_not_misread_windows_paths():
    assert source_mod.is_remote("https://cdn.example.com/a.mp3") is True
    assert source_mod.is_remote(r"C:\music\a.mp3") is False
    assert source_mod.is_remote("/tmp/a.mp3") is False


# ---------------------------------------------------------------- 直链下载

def test_resolve_http_media_download(tmp_path, monkeypatch):
    home = _isolate(monkeypatch, tmp_path)
    body = b"ID3" + b"\x00" * 2048
    monkeypatch.setattr(source_mod, "urlopen", _urlopen_returning(
        body, {"Content-Type": "audio/mpeg; charset=binary",
               "Content-Length": str(len(body))}))

    out = source_mod.resolve_source("https://cdn.example.com/a/song.mp3?v=2")

    assert out["kind"] == "http_media"
    assert out["tool"] == "urllib"
    assert out["origin_url"].startswith("https://cdn.example.com/a/song.mp3")
    local = Path(out["local_path"])
    assert local.exists() and local.name == "song.mp3"
    assert local.read_bytes() == body
    assert out["sha256"] == hashlib.sha256(body).hexdigest()
    assert str(home / "downloads") in str(local.parent)   # 只落本机工作区
    assert out["bytes"] == len(body)


def test_resolve_http_reuses_cached_download(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    body = b"ID3" + b"\x00" * 1024
    calls: list[int] = []

    def _fake(request, timeout=None):  # noqa: ARG001
        calls.append(1)
        return _FakeResponse(body, {"Content-Type": "audio/mpeg"})

    monkeypatch.setattr(source_mod, "urlopen", _fake)
    url = "https://cdn.example.com/a/cached.mp3"
    first = source_mod.resolve_source(url)
    second = source_mod.resolve_source(url)
    assert len(calls) == 1                    # 第二次命中本地缓存
    assert second["local_path"] == first["local_path"]
    assert second.get("cached") is True


def test_resolve_http_direct_video_link_is_media(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    body = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 256
    monkeypatch.setattr(source_mod, "urlopen", _urlopen_returning(
        body, {"Content-Type": "video/mp4"}))
    out = source_mod.resolve_source("https://cdn.example.com/clip.mp4")
    assert out["kind"] == "http_media"        # 直链视频：本机 ffmpeg 抽音轨
    assert Path(out["local_path"]).suffix == ".mp4"


def test_resolve_http_rejects_oversize_by_header(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(source_mod, "urlopen", _urlopen_returning(
        b"ID3", {"Content-Type": "audio/mpeg",
                 "Content-Length": str(source_mod.MAX_DOWNLOAD_BYTES + 1)}))
    with pytest.raises(InvalidAudio) as excinfo:
        source_mod.resolve_source("https://cdn.example.com/huge.mp3")
    assert excinfo.value.code == "invalid_audio"
    assert "512MB" in excinfo.value.message


def test_resolve_http_rejects_oversize_while_streaming(tmp_path, monkeypatch):
    home = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(source_mod, "MAX_DOWNLOAD_BYTES", 1024)
    body = b"ID3" + b"\x00" * 8000            # 无 Content-Length，靠流式累计拦截
    monkeypatch.setattr(source_mod, "urlopen", _urlopen_returning(
        body, {"Content-Type": "audio/mpeg"}))
    with pytest.raises(InvalidAudio):
        source_mod.resolve_source("https://cdn.example.com/huge2.mp3")
    leftovers = list((home / "downloads").rglob("*")) if \
        (home / "downloads").exists() else []
    assert not [p for p in leftovers if p.is_file()]   # 半截文件必须清理


def test_resolve_http_404_maps_to_file_not_found(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)

    def _fake(request, timeout=None):  # noqa: ARG001
        raise HTTPError(getattr(request, "full_url", "u"), 404, "Not Found",
                        {}, None)

    monkeypatch.setattr(source_mod, "urlopen", _fake)
    with pytest.raises(FileNotFound) as excinfo:
        source_mod.resolve_source("https://cdn.example.com/gone.mp3")
    assert excinfo.value.details.get("http_status") == 404


def test_resolve_http_non_audio_payload_rejected(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(source_mod, "urlopen", _urlopen_returning(
        b"PK\x03\x04 binary zip", {"Content-Type": "application/zip"}))
    with pytest.raises(InvalidAudio) as excinfo:
        source_mod.resolve_source("https://cdn.example.com/pack.bin")
    assert "支持格式" in (excinfo.value.hint or "")


# ---------------------------------------------------------------- 视频页 / yt-dlp

def test_resolve_page_without_ytdlp_degrades(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(source_mod, "urlopen", _urlopen_returning(
        b"<!DOCTYPE html><html><body>video</body></html>",
        {"Content-Type": "text/html; charset=utf-8"}))
    monkeypatch.setattr(source_mod, "yt_dlp_status", lambda: {
        "available": False, "path": None, "mode": None, "launcher": [],
        "version": None, "hint": _YTDLP_HINT})

    with pytest.raises(CapabilityUnavailable) as excinfo:
        source_mod.resolve_source("https://video.example.com/watch?v=abc")
    assert excinfo.value.code == "capability_unavailable"
    assert "pip install yt-dlp" in excinfo.value.hint


def test_resolve_page_with_ytdlp_extracts_audio(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(source_mod, "urlopen", _urlopen_returning(
        b"<html><head><title>page</title></head></html>",
        {"Content-Type": "text/html"}))
    monkeypatch.setattr(source_mod, "yt_dlp_status", lambda: {
        "available": True, "path": "/fake/yt-dlp", "mode": "binary",
        "launcher": ["/fake/yt-dlp"], "version": "2025.1", "hint": _YTDLP_HINT})

    def _fake_run(command, **kwargs):  # noqa: ARG001
        template = Path(command[command.index("-o") + 1])
        target = Path(str(template).replace("%(id)s", "vid9")
                      .replace("%(ext)s", "m4a"))
        target.write_bytes(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 128)

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    class _FakeSubprocess:
        run = staticmethod(_fake_run)

    monkeypatch.setattr(source_mod, "subprocess", _FakeSubprocess)

    out = source_mod.resolve_source("https://video.example.com/watch?v=abc")
    assert out["kind"] == "page"
    assert out["tool"] == "yt_dlp"
    assert Path(out["local_path"]).suffix == ".m4a"
    assert out["bytes"] > 0


def test_ytdlp_status_shape():
    status = source_mod.yt_dlp_status()
    assert isinstance(status["available"], bool)
    assert "hint" in status and "pip install" in status["hint"]


# ---------------------------------------------------------------- 格式白名单与探测

def test_new_audio_extensions_whitelisted(tmp_path, monkeypatch):
    monkeypatch.setattr(inspect_mod, "ffprobe", lambda path: _probe())
    for name in ("a.wv", "b.tta", "c.amr", "d.au", "e.ac3", "f.mka", "g.3gp",
                 "h.m4b", "i.mp2", "j.ra", "k.dts", "l.snd"):
        path = tmp_path / name
        path.write_bytes(b"\x00" * 64)
        meta = inspect_mod.inspect_audio(str(path))
        assert meta["ok"] is True, name
        assert meta["duration_sec"] == 12.5


def test_unknown_extension_probed_and_accepted(tmp_path, monkeypatch):
    path = tmp_path / "mystery.zzz"
    path.write_bytes(b"\x00" * 128)
    monkeypatch.setattr(inspect_mod, "ffprobe", lambda p: _probe())
    meta = inspect_mod.inspect_audio(str(path))
    assert meta["ok"] is True
    assert meta["codec"] == "pcm_s16le"
    assert meta["source"]["kind"] == "local"


def test_unknown_extension_rejected_when_unprobeable(tmp_path, monkeypatch):
    path = tmp_path / "junk.zzz"
    path.write_bytes(b"junk")

    def _boom(p):  # noqa: ARG001
        raise InvalidAudio("ffprobe 无法读取")

    monkeypatch.setattr(inspect_mod, "ffprobe", _boom)
    with pytest.raises(InvalidAudio) as excinfo:
        inspect_mod.inspect_audio(str(path))
    assert ".mp3" in (excinfo.value.hint or "")   # 错误信息必须列出支持清单


def test_unknown_extension_non_audio_container(tmp_path, monkeypatch):
    path = tmp_path / "silent.zzz"
    path.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(inspect_mod, "ffprobe", lambda p: _probe(audio=False))
    with pytest.raises(InvalidAudio):
        inspect_mod.inspect_audio(str(path))


def test_existing_wav_still_ok(tmp_path):
    """回归：原有格式路径不变（真实 ffprobe）。"""
    wav = _tone(tmp_path, "regression.wav")
    meta = inspect_mod.inspect_audio(str(wav))
    assert meta["ok"] is True
    assert meta["sample_rate_hz"] == 44100
    assert len(meta["content_hash"]) == 64
    assert meta["source"]["kind"] == "local"


# ---------------------------------------------------------------- MCP 接线

def test_mcp_inspect_accepts_url(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    wav_bytes = _tone(tmp_path, "source_for_url.wav").read_bytes()
    monkeypatch.setattr(source_mod, "urlopen", _urlopen_returning(
        wav_bytes, {"Content-Type": "audio/wav"}))
    result = srv.inspect_audio("https://cdn.example.com/x/remote.wav")
    assert result["ok"] is True
    assert result["source"]["kind"] == "http_media"
    assert result["source"]["resolver"] == "urllib"
    assert Path(result["path"]).exists()
    assert result["sample_rate_hz"] == 44100


def test_mcp_inspect_url_error_is_structured(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)

    def _fake(request, timeout=None):  # noqa: ARG001
        raise HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(source_mod, "urlopen", _fake)
    result = srv.inspect_audio("https://cdn.example.com/gone.mp3")
    assert result["ok"] is False
    assert result["error"]["code"] == "file_not_found"


def test_mcp_analyze_track_accepts_url(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    wav = _tone(tmp_path, "remote.wav")
    real_resolve = source_mod.resolve_source

    def _fake_resolve(ref, *, redownload=False):
        if str(ref).startswith("http"):
            return {"local_path": str(wav.resolve()), "kind": "http_media",
                    "origin_url": str(ref), "tool": "urllib",
                    "bytes": wav.stat().st_size, "sha256": None}
        return real_resolve(ref, redownload=redownload)

    monkeypatch.setattr(source_mod, "resolve_source", _fake_resolve)
    submitted: dict = {}

    def _fake_submit(func, job, *args, **kwargs):  # noqa: ARG001
        submitted["args"] = args
        return job

    monkeypatch.setattr(srv.jobs_mod, "submit", _fake_submit)

    result = srv.analyze_track("https://cdn.example.com/song.mp3", tier="fast")
    assert result["ok"] is True
    assert result["source"]["kind"] == "http_media"
    assert submitted["args"][0] == str(wav.resolve())   # Job 用的是本地落盘路径
    assert len(result["content_hash"]) == 64


def test_capabilities_reports_input_section(monkeypatch):
    """capabilities 必须带 input 段；引擎探测打桩以保持本测试秒级完成。"""
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

    block = registry.capabilities()["input"]
    assert block["remote_http"]["available"] is True
    assert block["remote_http"]["max_mb"] == 512
    assert block["remote_http"]["schemes"] == ["http", "https"]
    assert isinstance(block["yt_dlp"]["available"], bool)
    assert "pip install" in block["yt_dlp"]["hint"]
    assert ".mp3" in block["accepted_formats"]
    assert ".wv" in block["accepted_formats"]
    assert ".mka" in block["accepted_formats"]
    assert "ffprobe" in block["local_files"]["note"]
