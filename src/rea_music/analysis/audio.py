"""音频解码与标准化。

主路径 soundfile（wav/flac/ogg/mp3/opus，无子进程依赖）；其余格式（m4a/aac/
mp4/webm/wv/ac3… 以及从视频页抽出的音轨）回退到 ffmpeg。非核心扩展名不直接走
ffmpeg：先用 soundfile 实测内容（无扩展名/冷门后缀但实为 wav·flac 的文件在无
FFmpeg 环境也能分析）。解码产物按内容哈希缓存，重复分析不重复解码。
远端输入先经 core.source 归一化成本机文件（D22）。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import numpy as np

from rea_music.core import source as source_mod
from rea_music.core.errors import CapabilityUnavailable, InvalidAudio
from rea_music.core.hash import file_sha256
from rea_music.core.paths import content_dir

ANALYSIS_SR = 44100  # 与解码产物同采样率：onset/节拍分析对窗长敏感，
                     # 统一 44.1k 保证单测与管线行为一致（22050 会抹平拍级自相关）
TARGET_SR = 44100
SOUNDFILE_EXTS = {".wav", ".flac", ".ogg", ".oga", ".mp3", ".opus",
                  ".aif", ".aiff"}
_SF_COUNT_MAX = 2 ** 63 - 1         # libsndfile SF_COUNT_MAX = 长度未知（流式容器）
_HINT_FFMPEG_DECODE = (
    "安装 FFmpeg 并加入 PATH（Windows: winget install Gyan.FFmpeg；"
    "macOS: brew install ffmpeg；Linux: sudo apt install ffmpeg），"
    "或把文件转成 wav/flac/ogg/mp3/opus/aiff 后再分析。")

_LOGGER = logging.getLogger(__name__)


def ensure_wav(source: str | Path, content_hash: str | None = None) -> dict:
    """返回 {hash, stereo_wav, mono_wav, decoder}，必要时解码。

    source 可以是本地路径或 http(s) 链接（后者先下载到 data/downloads/）。
    """
    resolved = source_mod.resolve_source(str(source))
    source = Path(resolved["local_path"])
    if not source.exists():
        from rea_music.core.errors import FileNotFound
        raise FileNotFound(f"文件不存在: {source}")
    chash = content_hash or file_sha256(source)
    out_dir = content_dir("audio", chash)
    stereo = out_dir / "stereo.wav"
    mono = out_dir / "mono.wav"
    if not (stereo.exists() and mono.exists()):
        if source.suffix.lower() in SOUNDFILE_EXTS and _soundfile_readable(source):
            decoder = _decode_soundfile(source, stereo, mono)
        else:
            decoder = _decode_non_core(source, stereo, mono)
    else:
        decoder = "cache"
    return {"hash": chash, "stereo_wav": str(stereo), "mono_wav": str(mono),
            "decoder": decoder}


def _decode_non_core(source: Path, stereo: Path, mono: Path) -> str:
    """soundfile 读不了时的解码路由：先按内容实测，再回退 ffmpeg。

    覆盖两类输入：
    - 无扩展名 / 冷门后缀但实为 wav·flac 的文件（没有 FFmpeg 也能分析）；
    - 头部缺总样本数的流式容器（`ffmpeg -f flac -` 之类）：libsndfile 的每一次
      ``read()`` 都会把位置 seek 回去，而这类流的 ``psf_fseek()`` 会失败，
      只能交给 ffmpeg 解。

    Args:
        source: 本机待解码文件。
        stereo: 立体声标准化输出路径。
        mono: 单声道标准化输出路径。

    Returns:
        实际使用的解码器标识："soundfile" 或 "ffmpeg"。
    """
    soundfile_error: str | None = None
    if _soundfile_readable(source):
        try:
            return _decode_soundfile(source, stereo, mono)
        except Exception as exc:  # noqa: BLE001 —— 探测能读但解码失败：回退 ffmpeg
            # 不静默吞掉：原始失败原因要带进最终错误详情，否则用户只会看到
            # "需要装 FFmpeg"，而真实原因是文件头部损坏。
            soundfile_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.debug("soundfile 解码失败，回退 ffmpeg: %s", source,
                          exc_info=True)
    try:
        return _decode_ffmpeg(source, stereo, mono)
    except CapabilityUnavailable as exc:
        # 让 Agent 能区分"流式容器长度未知"与"m4a 需要 FFmpeg"（reason 透传到 job error）
        if _unknown_length(source):
            exc.details = {**(exc.details or {}), "reason": "unknown_length"}
        if soundfile_error:
            exc.details = {**(exc.details or {}),
                           "soundfile_error": soundfile_error}
        raise
    except InvalidAudio as exc:
        if soundfile_error:
            exc.details = {**(exc.details or {}),
                           "soundfile_error": soundfile_error}
        raise


def _unknown_length(source: Path) -> bool:
    """头部是否没有总样本数（流式容器，libsndfile 报 SF_COUNT_MAX）。"""
    try:
        import soundfile as sf
        return int(getattr(sf.info(str(source)), "frames", 0) or 0) >= _SF_COUNT_MAX
    except Exception:  # noqa: BLE001 —— 读不了就不算"长度未知"这一类
        return False


def _soundfile_readable(source: Path) -> bool:
    """内容实测：soundfile 能读且长度可定位（不看扩展名，读不了返回 False）。

    与 ``analysis.inspect._soundfile_probe`` 同一判定口径（libsndfile 嗅探文件头），
    此处只需布尔值，故不跨模块复用私有函数。``frames == SF_COUNT_MAX`` 表示流式
    容器没写总样本数——这种文件用 soundfile 读会撞 psf_fseek 失败，必须走 ffmpeg。
    """
    try:
        import soundfile as sf
        info = sf.info(str(source))
        return int(getattr(info, "frames", 0) or 0) < _SF_COUNT_MAX
    except Exception:  # noqa: BLE001 —— 读不了就走 ffmpeg 回退
        return False


def _decode_soundfile(source: Path, stereo: Path, mono: Path) -> str:
    """soundfile 直读解码（长度已知的文件；未知长度的流由 ffmpeg 兜底）。"""
    import librosa
    import soundfile as sf

    info = sf.info(str(source))
    if int(getattr(info, "frames", 0) or 0) >= _SF_COUNT_MAX:
        # 防御：正常路由不会把这类文件送到这里（ensure_wav 已改走 ffmpeg）
        raise CapabilityUnavailable(
            f"该文件头部未写总样本数（流式容器），内置解码库无法定位末尾："
            f"{source.name}",
            hint=_HINT_FFMPEG_DECODE)
    y, sr = sf.read(str(source), always_2d=True)  # (samples, channels)
    if not np.isfinite(y).all():
        y = np.nan_to_num(y)
    if sr != TARGET_SR:
        y = librosa.resample(y.T, orig_sr=sr, target_sr=TARGET_SR, axis=-1).T
    if y.shape[1] == 1:
        y = np.repeat(y, 2, axis=1)
    elif y.shape[1] > 2:
        y = y[:, :2]
    sf.write(str(stereo), y.astype(np.float32), TARGET_SR, subtype="PCM_16")
    sf.write(str(mono), y.mean(axis=1).astype(np.float32), TARGET_SR,
             subtype="PCM_16")
    return "soundfile"


def _decode_ffmpeg(source: Path, stereo: Path, mono: Path) -> str:
    """m4a/aac/mp4/webm/wv/ac3 等容器的回退路径，也负责从视频容器抽音轨。"""
    common = ["-hide_banner", "-loglevel", "error", "-y",
              "-i", str(source), "-ar", str(TARGET_SR)]
    try:
        subprocess.run(["ffmpeg", *common, "-ac", "2", str(stereo)],
                       check=True, capture_output=True, timeout=600)
        subprocess.run(["ffmpeg", *common, "-ac", "1", str(mono)],
                       check=True, capture_output=True, timeout=600)
    except FileNotFoundError:
        raise CapabilityUnavailable(
            f"本机没有 ffmpeg，无法解码 {source.name}（该容器需要 FFmpeg）",
            hint=_HINT_FFMPEG_DECODE,
            details={"requires": "ffmpeg", "suffix": source.suffix.lower()}) from None
    except (subprocess.CalledProcessError, OSError) as exc:
        raise InvalidAudio(
            f"ffmpeg 解码失败（{source.name}）: {exc}",
            hint="该容器需 ffmpeg 且需写权限；沙箱或受限环境下请先转成 "
                 "wav/flac/mp3 再分析。") from None
    return "ffmpeg"


def load_mono(mono_wav: str | Path, sr: int = ANALYSIS_SR):
    import librosa  # 延迟导入：librosa 首次加载编译 numba 内核较慢
    y, _ = librosa.load(str(mono_wav), sr=sr, mono=True)
    return y, sr
