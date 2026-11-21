"""inspect_audio：解析输入源、验证音频、提取基础元信息与内容指纹。

输入面（D22）：本地任意常见格式 + http(s) 直链媒体 + 视频页面链接，
统一先经 core.source.resolve_source 归一化为本机文件再探测。

探测顺序（独立运行红线）：核心格式（wav/flac/ogg/oga/mp3/opus/aif/aiff）先由
soundfile（libsndfile）直读，**不需要任何外部可执行文件**；只有 soundfile 读不了
的容器才尝试 ffprobe（FFmpeg 未装时如实降级为 capability_unavailable，不谎报格式非法）。
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

from rea_music.core import source as source_mod
from rea_music.core.config import load_config
from rea_music.core.errors import (CapabilityUnavailable, FileNotFound,
                                   InvalidAudio)
from rea_music.core.hash import file_sha256

# 常见音频/容器扩展名。不在表内不等于拒绝：交给实测探测判定（见 inspect_audio）。
AUDIO_EXT = {
    # 原有
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma",
    ".aiff", ".aif", ".mp4", ".webm",
    # D22 增补：无损/移动/广播/多声道容器
    ".wv", ".tta", ".amr", ".au", ".snd", ".ac3", ".dts", ".mka", ".m4b",
    ".3gp", ".ra", ".mp2",
    # 核心格式的别名后缀（soundfile 直读，与 SOUNDFILE_EXT 对齐）
    ".oga",
}

# soundfile（libsndfile）可直读的扩展名：零外部依赖的 8 种核心格式。
# 与 analysis.audio.SOUNDFILE_EXTS 保持一致（此处独立定义，避免 analysis 内部循环导入）。
SOUNDFILE_EXT = {".wav", ".flac", ".ogg", ".oga", ".mp3", ".opus",
                 ".aif", ".aiff"}

# 除核心格式外，白名单里剩下的容器都需要 FFmpeg 才能解析。
_FFMPEG_ONLY_EXT = AUDIO_EXT - SOUNDFILE_EXT

# PCM/FLOAT 子类型的位深：soundfile 的 subtype 就带这个信息，如实转出。
_BIT_DEPTH = {
    "PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32,
    "FLOAT": 32, "DOUBLE": 64,
}

# libsndfile 的 SF_COUNT_MAX：流式写入的容器（如 `ffmpeg -f flac -`）头部没有总样本数，
# 此时 frames 报这个值、duration 报天文数字。必须按块扫到 EOF 数真实帧数，否则会
# 误判"时长超上限"。
_SF_COUNT_MAX = 2 ** 63 - 1
_MEASURE_BLOCK_FRAMES = 1 << 18        # 262144 帧/块：扫描时内存恒定

_HINT_FFMPEG = (
    "安装 FFmpeg 并加入 PATH（Windows: winget install Gyan.FFmpeg；"
    "macOS: brew install ffmpeg；Linux: sudo apt install ffmpeg），"
    "或用任意播放器/转换工具把文件转成 wav/flac/ogg/mp3/opus/aiff —— "
    "这几种核心格式不需要 FFmpeg。")


class _FFProbeMissing(InvalidAudio):
    """ffprobe 可执行文件不在 PATH 上（内部信号：缺的是能力，不是格式非法）。

    继承 InvalidAudio 以保持历史调用方的兼容（它们都按 invalid_audio 处理）；
    inspect_audio 会先捕获它，升级为 capability_unavailable 并给出安装指引。
    """


def ffprobe(path: str | Path) -> dict:
    """调用 ffprobe 读取容器与流信息。

    Raises:
        _FFProbeMissing: 本机没有 ffprobe 可执行文件（能力缺失）。
        InvalidAudio: ffprobe 存在但无法解析该文件（内容非法）。
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise _FFProbeMissing(
            "本机没有 ffprobe（FFmpeg 未安装或不在 PATH）",
            hint=_HINT_FFMPEG) from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise InvalidAudio(
            f"ffprobe 调用失败: {exc}",
            hint="检查 FFmpeg 安装；或先转成 wav/mp3/flac。") from None
    if out.returncode != 0:
        raise InvalidAudio(f"ffprobe 无法读取: {path}",
                           hint="确认文件是可解码的音频/视频容器。")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        raise InvalidAudio(f"ffprobe 输出无法解析: {path}",
                           hint="文件可能损坏；尝试用播放器确认。") from None


def _soundfile_probe(path: str | Path) -> dict | None:
    """用 soundfile 读容器信息，产出与 ffprobe 同形的 dict。

    soundfile 读不了（格式不支持/文件损坏/无 libsndfile）时返回 None，不抛异常，
    由调用方决定下一步探测或降级。

    Args:
        path: 待探测的本机文件路径。

    Returns:
        {"format": {format_name, duration, bit_rate}, "streams": [...]}；
        bit_rate 恒为 None（soundfile 不报码率，绝不用 0 冒充）；读不了返回 None。
    """
    try:
        import soundfile as sf
        info = sf.info(str(path))
    except Exception:  # noqa: BLE001 —— 任何"读不了"都算未知格式，交下一路
        return None

    samplerate = int(getattr(info, "samplerate", 0) or 0)
    frames = int(getattr(info, "frames", 0) or 0)
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    if not math.isfinite(duration):              # 非有限值一律当"不可信"处理
        duration = frames / samplerate if samplerate > 0 and \
            0 < frames < _SF_COUNT_MAX else 0.0
    duration_unknown = False
    if frames >= _SF_COUNT_MAX:
        # 头部未写总样本数：先按块扫；libsndfile 读不动这类流时退到 ffmpeg 解码测长
        measured = _measured_frames(path)
        if measured is not None:
            frames = measured
        else:
            duration = _measured_duration_via_ffmpeg(path) or 0.0
            frames = int(round(duration * samplerate)) if samplerate > 0 else 0
            duration_unknown = frames <= 0
        duration = frames / samplerate if samplerate > 0 else 0.0
    elif duration <= 0 and samplerate > 0 and frames > 0:
        duration = frames / samplerate           # 少数容器不报 duration
    subtype = str(getattr(info, "subtype", "") or "").strip().upper()
    stream = {
        "codec_type": "audio",
        "codec_name": subtype.lower() or None,
        "sample_rate": str(samplerate),
        "channels": int(getattr(info, "channels", 0) or 0),
    }
    if subtype in _BIT_DEPTH:                    # 压缩编码没有固定位深，如实留空
        stream["bits_per_sample"] = _BIT_DEPTH[subtype]
    return {
        "format": {
            "format_name": str(getattr(info, "format", "") or "").strip().lower()
                           or None,
            "duration": str(duration),
            # soundfile 不报码率：留 None 表示"未测"，绝不用 0 冒充
            "bit_rate": None,
        },
        "streams": [stream],
        # 流式容器测不出长度：调用方必须如实降级，绝不能拿 2e14s 去撞时长上限
        "duration_unknown": duration_unknown,
    }


def _measured_frames(path: str | Path) -> int | None:
    """按块读到 EOF，数出真实帧数（内存恒定，只给未知长度的容器用）。

    注意：这类文件的 ``tell()`` 会触发 libsndfile ``psf_fseek()`` 失败，所以用
    ``read(out=缓冲)`` 绕开 soundfile 的长度检查，逐块推进。

    Args:
        path: 本机待测文件。

    Returns:
        真实帧数；读不了返回 None。
    """
    try:
        import numpy as np
        import soundfile as sf
        with sf.SoundFile(str(path)) as handle:
            buffer = np.empty((_MEASURE_BLOCK_FRAMES, max(handle.channels, 1)),
                              dtype="float32")
            total = 0
            while True:
                block = handle.read(out=buffer)
                frames = int(block.shape[0])
                if frames == 0:
                    break
                total += frames
        return total
    except Exception:  # noqa: BLE001 —— 读不了就按未知长度处理
        return None


def _measured_duration_via_ffmpeg(path: str | Path) -> float | None:
    """兜底测长：用 ffmpeg 解码成 wav 后数帧（只在头部缺总样本数时调用）。

    这类流的 ``tell()`` 会让 libsndfile ``psf_fseek()`` 失败，纯 libsndfile 无法
    顺序读完；ffmpeg 能完整解码，直接从解出来的 wav 取真实长度。
    走 stdout 管道而不是临时文件：不落盘、不依赖写权限（单声道 44.1k，
    900s 上限约 79MB，可接受）。

    Args:
        path: 本机待测文件。

    Returns:
        时长（秒）；本机没有 ffmpeg 或解码失败返回 None。
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-loglevel", "error",
             "-i", str(path), "-ar", "44100", "-ac", "1", "-f", "wav", "-"],
            capture_output=True, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        import io
        import soundfile as sf
        with sf.SoundFile(io.BytesIO(proc.stdout)) as handle:
            if handle.samplerate <= 0:
                return None
            return handle.frames / handle.samplerate
    except Exception:  # noqa: BLE001 —— 解码产物不可解析就按未知长度处理
        return None


def _ffprobe_or_none(path: str | Path) -> dict | None:
    """尝试 ffprobe；本机没有 ffprobe 时返回 None，内容非法仍抛 InvalidAudio。"""
    try:
        return ffprobe(path)
    except _FFProbeMissing:
        return None


def _capability_error(local: Path) -> CapabilityUnavailable:
    """两路探测都无解且本机没有 FFmpeg：如实报能力缺失（而非格式非法）。"""
    suffix = local.suffix.lower()
    ext = suffix.lstrip(".") or "无扩展名"
    if suffix in _FFMPEG_ONLY_EXT:
        message = (f"该容器需要 FFmpeg 才能解析：{local.name}（{ext}）；"
                   "本机未安装 FFmpeg")
    else:
        message = (f"无法解析 {local.name}（{ext}）：本机未安装 FFmpeg，"
                   "只能直读 wav/flac/ogg/mp3/opus/aiff 等核心格式；"
                   "文件可能损坏，或扩展名与实际内容不符")
    return CapabilityUnavailable(
        message, hint=_HINT_FFMPEG,
        details={"suffix": suffix or None, "requires": "ffmpeg",
                 "ffprobe_available": False,
                 "soundfile_formats": sorted(SOUNDFILE_EXT)})


def _probe_local(local: Path) -> tuple[dict, str]:
    """按扩展名选择探测顺序，返回 (probe, probed_by)。

    核心格式：soundfile 先读（零外部依赖），失败才找 ffprobe；
    其余扩展名（含无扩展名）：ffprobe 先试，缺 ffprobe 时回退 soundfile，
    以应对"冷门后缀/无后缀但实为 wav"的文件。

    Raises:
        InvalidAudio: 有可用的探测手段但内容非法。
        CapabilityUnavailable: 两路都读不了，且缺的正是 FFmpeg 能力。
    """
    suffix = local.suffix.lower()
    if suffix in SOUNDFILE_EXT:
        probe = _soundfile_probe(local)
        if probe is not None:
            return probe, "soundfile"
        probe = _ffprobe_or_none(local)          # 内容非法会抛 InvalidAudio
        if probe is not None:
            return probe, "ffprobe"
        raise _capability_error(local)

    try:
        probe = ffprobe(local)
    except _FFProbeMissing:
        probe = _soundfile_probe(local)          # 无 ffprobe：按内容实测兜底
        if probe is not None:
            return probe, "soundfile"
        raise _capability_error(local) from None
    except InvalidAudio:
        raise InvalidAudio(
            f"无法识别的音频格式: {local.name}"
            f"（扩展名 {local.suffix or '无'}）",
            hint="支持格式：" + "、".join(sorted(AUDIO_EXT))
                 + "；白名单外的扩展名会尝试 ffprobe 实测探测，也可先用 ffmpeg "
                   "转成 wav/mp3/flac。") from None
    return probe, "ffprobe"


def _bit_rate_kbps(raw) -> float | None:
    """ffprobe 的 bit_rate 是字符串；缺失/"N/A"/0 一律 None。

    soundfile 不报码率，早前用 "0" 填充会让 ``bit_rate_kbps`` 恒为 0.0，Agent
    会把它当"码率极低"而不是"没测"（与 duration_unknown 的诚实口径不一致）。
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return round(value / 1000, 1) if value > 0 else None


def inspect_audio(path: str | Path, *, compute_hash: bool = True) -> dict:
    """解析输入源并返回元信息。

    path 可以是本地任意常见格式、http(s) 直链媒体或视频页面链接。
    核心格式不依赖外部程序；未知扩展名也不再直接拒绝：能实测解码即放行。

    Args:
        path: 本地路径或 http(s) 链接。
        compute_hash: True 时计算内容 sha256（DNA 缓存与溯源键）。

    Returns:
        元信息 dict，含 ``probed_by``（soundfile / ffprobe）用于溯源。

        时长语义：通常为实测秒数；若输入是头部无总样本数的流式容器且本机测不出
        长度，``duration_sec`` 记 **0.0** 并由 ``duration_unknown: true`` 标明
        "这是未知、不是 0 秒"（DNA schema 要求 number，故不用 null；下游做算术
        的地方不会因 None 崩）。这种文件在没有 FFmpeg 时解码会如实报
        ``capability_unavailable``（details.reason=unknown_length）。

    Raises:
        FileNotFound: 本地文件不存在。
        InvalidAudio: 文件内容不是可解析的音频。
        CapabilityUnavailable: 该容器需要 FFmpeg 但本机未安装。
    """
    resolved = source_mod.resolve_source(str(path))
    local = Path(resolved["local_path"])
    if not local.exists():          # 下载目录被外部清理时的兜底
        raise FileNotFound(f"文件不存在: {local}")

    probe, probed_by = _probe_local(local)

    streams = probe.get("streams", [])
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio_streams:
        raise InvalidAudio("容器内没有音频流",
                           hint="检查文件是否为纯视频或损坏的音频。")
    s = audio_streams[0]
    fmt = probe.get("format", {})
    duration = float(fmt.get("duration", 0) or 0)
    # 头部没有总样本数（`ffmpeg -f flac -` 之类流式写入的产物）且测不出长度：
    # 如实标 unknown 并跳过时长上限判定，绝不拿 2e14s 去撞上限。
    duration_unknown = bool(probe.get("duration_unknown"))
    max_dur = load_config()["analysis"]["max_duration_sec"]
    if not duration_unknown and duration > max_dur:
        raise InvalidAudio(
            f"时长 {duration:.0f}s 超过上限 {max_dur}s",
            hint="超长音频请分段分析，或调高 config.analysis.max_duration_sec。")
    return {
        "ok": True,
        "path": str(local),
        "file_name": local.name,
        "file_size_bytes": local.stat().st_size,
        "content_hash": file_sha256(local) if compute_hash else None,
        "container": fmt.get("format_name"),
        "codec": s.get("codec_name"),
        "duration_sec": duration,
        "sample_rate_hz": int(s.get("sample_rate", 0) or 0),
        "channels": int(s.get("channels", 0) or 0),
        "bit_rate_kbps": _bit_rate_kbps(fmt.get("bit_rate")),
        "bit_depth": s.get("bits_per_sample") or s.get("bits_per_raw_sample"),
        "has_video_stream": any(x.get("codec_type") == "video" for x in streams),
        # 长度测不出来（流式容器的头部无总样本数）：duration_sec 记 0，语义由本字段说明
        "duration_unknown": duration_unknown,
        # 探测手段溯源：告诉 Agent 这个结论来自内置解码库还是外部 FFmpeg
        "probed_by": probed_by,
        # 来源溯源：Agent 需要知道这是本地文件还是拉取下来的远端内容
        "source": {
            "kind": resolved["kind"],
            "origin_url": resolved["origin_url"],
            "resolver": resolved["tool"],
            "download_bytes": resolved.get("bytes"),
        },
    }
