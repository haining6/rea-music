"""引擎能力注册表：如实反映安装状态、显存与可用性。

capabilities 的单一事实来源。新增引擎只改这里与对应 Adapter，不动 DNA schema。
"""

from __future__ import annotations

import shutil
import subprocess


def _gpu_free_mib() -> int | None:
    """实测空闲显存（MiB），失败返回 None。不信任名义容量。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        values = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        return values[0] if values else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def engine_fingerprint() -> dict:
    """影响分析结果的引擎可用性指纹（只做存在性检查，不加载模型/权重）。

    DNA 缓存键必须带上它：装好 demucs / basic-pitch 之后，同样的输入应该重算出
    deep 档结果，而不是命中"当时没有分轨引擎"的旧工件。

    Returns:
        {"demucs": bool, "basic_pitch": bool, "chordotomy": bool}
    """
    from importlib.util import find_spec

    def _has(module: str) -> bool:
        try:
            return find_spec(module) is not None
        except (ImportError, ValueError):
            return False

    from rea_music.adapters import basicpitch as bp_mod
    from rea_music.adapters import chordotomy as chordotomy_mod

    try:
        bp_available = bool(bp_mod.probe()["available"])   # 仅文件存在性检查
    except Exception:  # noqa: BLE001 —— 探测失败按不可用计（保守）
        bp_available = False
    try:
        chord_available = chordotomy_mod.find_cli() is not None
    except Exception:  # noqa: BLE001
        chord_available = False
    return {
        "demucs": _has("demucs") and _has("torch"),
        "basic_pitch": bp_available,
        "chordotomy": chord_available,
    }


def separation_status() -> dict:
    """Demucs 分轨能力（R2 起实测：torch/demucs 导入 + htdemucs 权重加载）。"""
    from rea_music.adapters import demucs as demucs_mod
    probe = demucs_mod.probe()
    gpu_free = probe["details"].get("gpu_free_mib")
    available = probe["available"]
    return {
        "engine": "demucs_htdemucs",
        "available": available,
        "runtime": probe["details"].get("runtime"),
        "model_version": demucs_mod.MODEL_VERSION,
        "sources": probe["details"].get("sources"),
        "weights_note": "htdemucs 权重首跑自动下载入 torch hub 缓存（约 300MB）",
        "gpu_free_mib": gpu_free,
        "unavailable_reason": probe.get("reason"),
        "install_hint": ("demucs 已安装" if available else
                         "未安装或权重缺失。安装需下载 torch+demucs（>1.5GB），"
                         "须用户批准：uv pip install torch --index-url "
                         "https://download.pytorch.org/whl/cpu 且 uv pip install "
                         "demucs；GPU 版需显存 ≥3GB 空闲"),
        "cpu_speed_note": "CPU 分轨耗时约 1.5x 曲长",
    }


def chord_engine_status() -> dict:
    """和弦引擎：chordotomy 主引擎（R2 起），template_v2 恒可用后备。"""
    from rea_music.adapters import chordotomy as chordotomy_mod
    probe = chordotomy_mod.probe()
    return {
        "engine": "chordotomy_dsp" if probe["available"] else "internal_template_v2",
        "available": True,
        "primary": {
            "engine": "chordotomy_dsp",
            "available": probe["available"],
            "version": probe["details"].get("version"),
            "unavailable_reason": probe.get("reason"),
        },
        "fallback": {
            "engine": "internal_template_v2_two_stage",
            "available": True,
        },
        "chordotomy_available": probe["available"],
        "config_note": "analysis.chords_engine: auto|chordotomy|template（默认 auto）",
        "install_hint": "uv pip install chordotomy 可启用和弦主引擎（无 torch 依赖）",
    }


def transcription_status() -> dict:
    """旋律转写：basic-pitch 隔离 venv 主引擎（R2 起），librosa pyin 后备。"""
    from rea_music.adapters import basicpitch as bp_mod
    probe = bp_mod.probe()
    return {
        "engine": "basic_pitch_icassp_2022" if probe["available"] else "librosa_pyin",
        "available": True,
        "primary": {
            "engine": "basic_pitch_icassp_2022",
            "available": probe["available"],
            "isolation": "engines/bp-venv (py3.10 + onnxruntime)",
            "unavailable_reason": probe.get("reason"),
        },
        "fallback": {
            "engine": "librosa_pyin",
            "available": True,
            "confidence_cap": 0.3,
        },
        "basic_pitch_available": probe["available"],
        "note": "basic-pitch 官方仅支持 Python ≤3.11，经 engines/bp-venv 子进程隔离调用",
    }


def generation_status(cfg: dict | None = None) -> dict:
    from rea_music.generation.backends import yue2_probe
    from rea_music.generation import acestep
    probe = yue2_probe((cfg or {}).get("generation", {}))
    return {
        "backends": {
            "yue2_studio_mcp": probe,
            "suno_prompt_package": {
                "available": True,
                "mode": "manual_worksflow",
                "note": "输出结构化提示词包，用户在 Suno 创作后导入成品评测",
            },
            "ace_step": acestep.probe(),
        }
    }


def remote_status(cfg: dict) -> dict:
    """songbrain 远程交叉验证：key + 用户显式上传授权双条件（原则 8）。"""
    from rea_music.adapters import songbrain as songbrain_mod
    probe = songbrain_mod.probe(cfg)
    out = {
        "songbrain_api": {
            "available": probe["available"],
            "note": "远程交叉验证引擎；启用前必须获得用户对上传音频的显式授权",
        }
    }
    if not probe["available"]:
        out["songbrain_api"]["missing_reason"] = probe["reason"]
        out["songbrain_api"]["api_key_present"] = \
            probe["details"].get("api_key_present")
        out["songbrain_api"]["upload_authorized"] = \
            probe["details"].get("upload_authorized")
    return out


def ffmpeg_status() -> dict:
    """FFmpeg 外部可执行文件探测：核心格式不需要它，只有部分容器需要。

    判定口径与 analysis.inspect 一致：ffprobe 决定"能否探测容器"，ffmpeg 决定
    "能否解码非 soundfile 格式"，两者分别如实报告——只有 ffmpeg 没有 ffprobe
    是真实存在的安装形态（如精简构建），hint 必须说清缺的是哪一个。
    """
    ffprobe_path = shutil.which("ffprobe")
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_ready = ffprobe_path is not None
    decode_ready = ffmpeg_path is not None
    available = ffprobe_ready          # 语义不变：容器探测能力由 ffprobe 决定
    if ffprobe_ready and decode_ready:
        hint = "FFmpeg 就绪（ffprobe 可探测容器、ffmpeg 可解码）"
    elif decode_ready:
        hint = ("已找到 ffmpeg，但缺少 ffprobe（部分容器元信息读取会降级）；"
                "装完整 FFmpeg 并确保 ffprobe 也在 PATH 上即可")
    elif ffprobe_ready:
        hint = ("已找到 ffprobe，但缺少 ffmpeg（m4a/aac/mp4/webm 与视频页无法解码）；"
                "装完整 FFmpeg 并确保 ffmpeg 也在 PATH 上即可")
    else:
        hint = ("未安装 FFmpeg（不在 PATH）。core 格式不受影响；需要 m4a/aac/"
                "mp4/webm 或视频页时用 winget install Gyan.FFmpeg（Windows）/"
                "brew install ffmpeg（macOS）/ sudo apt install ffmpeg（Linux）")
    return {
        "available": available,
        "ffprobe_ready": ffprobe_ready,
        "decode_ready": decode_ready,
        "ffprobe_path": ffprobe_path,
        "ffmpeg_path": ffmpeg_path,
        "needed_for": ["m4a/aac/mp4/webm", "视频页链接",
                       "非 soundfile 可解码格式"],
        "note": "wav/flac/ogg/mp3/opus/aiff 由内置 soundfile 直读，不需要 FFmpeg",
        "hint": hint,
    }


def input_status() -> dict:
    """输入类型支持（D22）：本地任意格式 / http(s) 直链 / 视频页（可选 yt-dlp）。"""
    from rea_music.analysis.inspect import SOUNDFILE_EXT
    from rea_music.core import source as source_mod
    ytdlp = source_mod.yt_dlp_status()
    return {
        "local_files": {
            "available": True,
            "note": "8 种核心格式由 soundfile 直读（零外部依赖）；白名单外的扩展名"
                    "先试 ffprobe、缺 ffprobe 时回退 soundfile，能解码即放行",
        },
        "ffmpeg": ffmpeg_status(),
        "remote_http": {
            "available": True,
            "schemes": ["http", "https"],
            "max_mb": int(source_mod.MAX_DOWNLOAD_BYTES // (1024 * 1024)),
            "timeout_sec": source_mod.DOWNLOAD_TIMEOUT_SEC,
            "read_timeout_sec": source_mod.READ_TIMEOUT_SEC,
            "chunk_kb": 1024,
            "note": "标准库 urllib 流式下载到 data/downloads/（仅本机自用，"
                    "不二次外发）；仅接受 http/https；先写 .part 再原子改名",
        },
        "yt_dlp": {
            "available": ytdlp["available"],
            "path": ytdlp["path"],
            "mode": ytdlp["mode"],
            "version": ytdlp["version"],
            "hint": ytdlp["hint"],
            "note": "视频/网页链接抽 bestaudio 后按常规解码分析；"
                    "未安装时如实降级为 capability_unavailable",
        },
        "accepted_formats": source_mod.accepted_formats(),
        "accepted_formats_note": "、".join(sorted(SOUNDFILE_EXT)) +
                                 " 这 8 种核心格式由 soundfile 直读，零外部依赖；"
                                 "其余格式需要 FFmpeg",
        "privacy": "远端输入仅落本机 data/downloads/，不做任何二次外发；"
                   "下载内容版权由使用者自行确认",
    }


def capabilities() -> dict:
    cfg = None
    try:
        from rea_music.core.config import load_config
        cfg = load_config()
    except Exception:  # noqa: BLE001
        cfg = {}
    return {
        "ok": True,
        "product": "REA-Music",
        "version": None or _version(),
        "input": input_status(),
        "tiers": {
            "fast": "时长/BPM/拍点/调性/响度/频谱概要（CPU 秒级）",
            "standard": "fast + 和弦/曲式结构/人声启发式/段落级DNA",
            "deep": "standard + 分轨与旋律（依赖引擎安装，未装时如实降级）",
        },
        "analysis_engines": {
            "tempo": {"engine": "librosa_dp+autocorr", "available": True},
            "key": {"engine": "ks+temperley_fusion", "available": True},
            "chords": chord_engine_status(),
            "structure": {
                "engine": "agglomerative+heuristic_v1",
                "available": True,
                "note": "all-in-one 结构模型经实测不可用：NATTEN 无 Windows wheel，"
                        "源码编译链超限已降级（DECISIONS.md D15）；"
                        "结构字段维持 Ward 聚类并如实标注 method",
            },
            "separation": separation_status(),
            "transcription": transcription_status(),
            "loudness": {"engine": "pyloudnorm_bs1770", "available": True},
        },
        "generation": generation_status(cfg),
        "remote": remote_status(cfg or {}),
        "output_formats": ["json"],
        "max_duration_sec": (cfg or {}).get("analysis", {}).get("max_duration_sec", 900),
        "gpu": {"free_mib": _gpu_free_mib(),
                "policy": "GPU 适配器先实测空闲显存，不足自动降级 CPU 或报 capability_unavailable"},
    }


def _version() -> str:
    from rea_music import __version__
    return __version__
