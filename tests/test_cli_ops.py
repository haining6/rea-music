"""CLI 运维命令测试（T2）：`cache stats/clean` 与 `analyze --batch`。

全部离线：``REA_MUSIC_HOME`` 指向 tmp_path、ffprobe 打桩、音频用 soundfile 现场合成，
不访问网络，也不写仓库自带的 data/。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))  # rea_music 包根在 sys.path

from rea_music.cli.main import cli  # noqa: E402
from rea_music.core.errors import ValidationError  # noqa: E402

_PROBE = {
    "format": {"format_name": "wav", "duration": "2.5",
               "bit_rate": "705600", "size": "110250"},
    "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le",
                 "sample_rate": "22050", "channels": 1,
                 "bits_per_sample": 16}],
}


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """隔离工作区：所有缓存/Job/下载都落在 tmp_path/data。"""
    home = tmp_path / "data"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("REA_MUSIC_HOME", str(home))
    return home


def _invoke(*args: str):
    return CliRunner().invoke(cli, list(args))


def _run_json(*args: str) -> dict:
    result = _invoke(*args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _tail_json(output: str) -> dict:
    """clean 会逐条打印删除项，真实 JSON 从第一行 `{` 开始。"""
    lines = output.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "{"), None)
    assert start is not None, f"输出里没有 JSON：{output}"
    return json.loads("\n".join(lines[start:]))


def _stub_ffprobe(monkeypatch) -> None:
    """inspect 的 ffprobe 打桩：测试不依赖本机 FFmpeg。"""
    from rea_music.analysis import inspect as inspect_mod
    monkeypatch.setattr(inspect_mod, "ffprobe",
                        lambda path: json.loads(json.dumps(_PROBE)))


def _write_wav(path: Path, freq: float = 440.0, dur: float = 2.5,
               sr: int = 22050) -> Path:
    """合成一段纯正弦 wav（无版权问题，不依赖 ffmpeg 写文件）。"""
    import soundfile as sf
    t = np.linspace(0.0, dur, int(sr * dur), endpoint=False)
    y = (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    sf.write(str(path), y, sr)
    return path


def _age(path: Path, days: int) -> None:
    """把文件 mtime 调到 N 天前（不依赖 sleep）。"""
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


# ---------- cache stats ----------

def test_cache_stats_reports_dirs_and_total(workspace):
    from rea_music.cli.cache import human_bytes

    audio = workspace / "cache" / "audio" / "abc"
    audio.mkdir(parents=True)
    (audio / "stereo.wav").write_bytes(b"x" * 2048)
    downloads = workspace / "downloads" / "k"
    downloads.mkdir(parents=True)
    (downloads / "a.mp3").write_bytes(b"y" * 1024)
    (workspace / "jobs").mkdir(parents=True, exist_ok=True)
    (workspace / "jobs" / "job1.json").write_text("{}", encoding="utf-8")

    data = _run_json("cache", "stats")

    assert data["ok"] is True
    dirs = {entry["name"]: entry for entry in data["dirs"]}
    assert dirs["audio"]["files"] == 1
    assert dirs["audio"]["bytes"] == 2048
    assert dirs["audio"]["human"].endswith("KB")
    assert dirs["downloads"]["files"] == 1
    assert dirs["jobs"]["files"] == 1
    assert data["total"]["files"] >= 3
    assert data["total"]["bytes"] >= 2048 + 1024
    subtotal = {entry["name"]: entry for entry in data["subtotals"]}
    assert subtotal["cache"]["files"] == 1          # 只有 cache/audio 下的文件
    assert human_bytes(1024) == "1.0 KB"
    assert human_bytes(0) == "0 B"


# ---------- cache clean ----------

def test_cache_clean_dry_run_does_not_delete(workspace):
    old = workspace / "downloads" / "k" / "old.mp3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"z" * 4096)
    _age(old, 60)
    fresh = workspace / "downloads" / "k" / "fresh.mp3"
    fresh.write_bytes(b"z" * 128)

    result = _invoke("cache", "clean", "--what", "downloads",
                     "--older-than-days", "30")
    assert result.exit_code == 0, result.output
    data = _tail_json(result.output)

    assert data["dry_run"] is True
    assert data["candidates"] == 1
    assert data["reclaim_bytes"] == 4096
    assert data["reclaim_human"] == "4.0 KB"
    assert data["deleted"] == []
    assert "would delete" in result.output
    assert old.exists() and fresh.exists()      # dry-run 一个字节都不动


def test_cache_clean_yes_deletes_only_expired(workspace):
    old = workspace / "downloads" / "k" / "old.mp3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"z" * 4096)
    _age(old, 60)
    fresh = workspace / "downloads" / "k" / "fresh.mp3"
    fresh.write_bytes(b"z" * 128)

    result = _invoke("cache", "clean", "--what", "downloads",
                     "--older-than-days", "30", "--yes")
    assert result.exit_code == 0, result.output
    data = _tail_json(result.output)

    assert data["dry_run"] is False
    assert data["deleted"] and data["deleted"][0].endswith("old.mp3")
    assert data["failed"] == []
    assert not old.exists()
    assert fresh.exists()                       # 未超期文件必须保留
    assert "deleted" in result.output


def test_cache_clean_rejects_path_outside_whitelist(workspace):
    keep = workspace / "downloads" / "keep.mp3"
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b"k")

    for bad in ("../src", "/etc", "cache/../../..", "C:\\Windows"):
        result = _invoke("cache", "clean", "--what", bad, "--yes")
        assert result.exit_code != 0, f"{bad} 竟然被接受: {result.output}"
    assert keep.exists()
    assert (workspace / "downloads").exists()


def test_cache_clean_rejects_protected_subtree(monkeypatch, workspace):
    from rea_music.cli import cache as cache_cli

    protected = workspace / "src" / "rea_music"
    protected.mkdir(parents=True)
    victim = protected / "main.py"
    victim.write_text("print('x')", encoding="utf-8")
    monkeypatch.setitem(cache_cli.WHITELIST, "evil", lambda: protected)
    with pytest.raises(ValidationError):
        cache_cli.resolve_target("evil")
    assert victim.exists()

    monkeypatch.setitem(cache_cli.WHITELIST, "outside", lambda: ROOT)
    with pytest.raises(ValidationError):
        cache_cli.resolve_target("outside")
    with pytest.raises(ValidationError):
        cache_cli.resolve_target("no_such_target")


# ---------- analyze --batch ----------

def test_analyze_batch_summary_and_failure_isolation(workspace, tmp_path,
                                                    monkeypatch):
    _stub_ffprobe(monkeypatch)
    songs = tmp_path / "songs"
    songs.mkdir()
    _write_wav(songs / "a_440.wav", 440.0)
    _write_wav(songs / "b_220.wav", 220.0)
    (songs / "broken.wav").write_bytes(b"not-a-wav")   # 单文件失败不中断
    (songs / "cover.jpg").write_bytes(b"x")            # 非音频扩展名：跳过

    data = _run_json("analyze", "--batch", str(songs), "--tier", "fast")

    assert data["mode"] == "batch"
    assert data["tier"] == "fast"
    assert data["matched"] == 3          # wav 三个，jpg 不计
    assert data["total"] == 3
    assert data["succeeded"] == 2
    assert data["failed"] == 1
    assert data["ok"] is False           # 有失败项则汇总非全绿
    assert data["elapsed_sec"] >= 0

    ok_items = [item for item in data["results"] if item["ok"]]
    assert len(ok_items) == 2
    for item in ok_items:
        assert Path(item["dna_path"]).is_file()
        assert "bpm" in item and "key" in item
        assert item["elapsed_sec"] >= 0

    failed = [item for item in data["results"] if not item["ok"]]
    assert failed[0]["file"].endswith("broken.wav")
    assert failed[0]["error"]["code"]            # 结构化错误码必填


def test_analyze_batch_limit(workspace, tmp_path, monkeypatch):
    _stub_ffprobe(monkeypatch)
    songs = tmp_path / "songs_limit"
    songs.mkdir()
    for index in range(3):
        _write_wav(songs / f"{index}.wav", 220.0 + index * 55)

    data = _run_json("analyze", "--batch", str(songs), "--tier", "fast",
                     "--limit", "2")

    assert data["matched"] == 3          # 截断前命中数
    assert data["total"] == 2            # 实际处理数
    assert len(data["results"]) == 2
    assert data["succeeded"] == 2


def test_analyze_single_file_still_works(workspace, tmp_path, monkeypatch):
    """回归：位置参数单文件用法与输出契约不变。"""
    _stub_ffprobe(monkeypatch)
    song = _write_wav(tmp_path / "single.wav", 330.0)

    data = _run_json("analyze", str(song), "--tier", "fast")

    assert data["ok"] is True
    assert Path(data["dna_path"]).is_file()
    assert data["source"]["kind"] == "local"
    assert "rhythm" in data["dna"] and "tonality" in data["dna"]
    assert data["dna"]["song"]["analysis_tier"] == "fast"


def test_analyze_argument_conflicts(workspace, tmp_path):
    song = _write_wav(tmp_path / "conflict.wav", 330.0)

    assert _invoke("analyze").exit_code == 2                     # 两者都缺
    assert _invoke("analyze", str(song), "--batch",
                   str(tmp_path)).exit_code == 2                 # 互斥
    assert _invoke("analyze", str(song), "--limit", "1").exit_code == 2
    assert _invoke("analyze", "--batch", str(tmp_path),
                   "--limit", "0").exit_code == 2


def test_analyze_batch_rejects_empty_dir(workspace, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _invoke("analyze", "--batch", str(empty))
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "validation_error"


def test_clean_explicit_dry_run_flag(tmp_path, monkeypatch):
    """显式 --dry-run 与默认行为一致；与 --yes 互斥（CI/脚本依赖该契约）。"""
    from click.testing import CliRunner
    from rea_music.cli.main import cli
    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path))
    runner = CliRunner()
    ok = runner.invoke(cli, ["cache", "clean", "--what", "downloads",
                             "--older-than-days", "7", "--dry-run"])
    assert ok.exit_code == 0, ok.output
    import json
    out = json.loads(ok.output[ok.output.index("{"):])
    assert out["dry_run"] is True and out["deleted"] == []

    bad = runner.invoke(cli, ["cache", "clean", "--dry-run", "--yes"])
    assert bad.exit_code == 2
