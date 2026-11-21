"""demucs 分轨 Adapter（R2-A）。

沙箱约束：ffmpeg 子进程禁止写文件，demucs 自带的 `demucs.separate` CLI 不可用。
按设计 A1 绕开其内部解码/写盘：
    soundfile 读 ensure_wav 立体声产物 → torch 张量 → demucs python API
    （demucs.apply.apply_model，设计草稿中的 model.apply 与 torch nn.Module.apply
    冲突，实测以 apply_model 为准）→ soundfile 写 workspace/stems/<hash>/<model>/
    四轨 + manifest（各轨 rms、耗时秒）。

首次运行时 htdemucs 权重自动下载入 torch hub 缓存（约 300MB，一次性行为），
capabilities 中如实注明。
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from rea_music.core.errors import CapabilityUnavailable
from rea_music.core.paths import stems_dir

MODEL_NAME = "htdemucs"
MODEL_VERSION = "htdemucs"
# 分轨产物目录里的 manifest 文件名（缓存命中键）
_MANIFEST = "manifest.json"
# GPU 门槛：空闲显存 ≥3GB 才启用 GPU 路径（与 registry 策略一致）
_GPU_FREE_MIB_MIN = 3000
# 静音轨判定阈值：低于该 rms 的轨不参与旋律转写选轨
SILENT_STEM_RMS = 5e-4
# 模型常驻上限：htdemucs 单模型约 1GB 内存/显存，长驻进程不能无限累积
_MODEL_CACHE_MAX = 2

_MODEL_CACHE: "OrderedDict[str, object]" = OrderedDict()


def probe() -> dict:
    """实测检查：torch/demucs 可导入、模型可加载、CPU/GPU 运行时。

    返回 {available, reason, details}；只做检查，不做业务。
    """
    details: dict = {}
    try:
        import torch  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": "torch_not_importable",
                "details": {"error": str(exc)}}
    try:
        import demucs  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": "demucs_not_importable",
                "details": {"error": str(exc)}}
    try:
        t0 = time.time()
        model = _load_model(MODEL_NAME)
        details["model_load_sec"] = round(time.time() - t0, 1)
        details["sources"] = list(model.sources)
        details["samplerate"] = int(model.samplerate)
        details["audio_channels"] = int(model.audio_channels)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": "model_weights_unavailable",
                "details": {"error": str(exc),
                            "hint": "htdemucs 权重首跑自动下载入 torch hub 缓存"
                                    "（约 300MB），需联网且获得用户批准"}}
    # GPU 门控复用 registry 的实测逻辑（惰性导入避免环）
    from rea_music.adapters.registry import _gpu_free_mib
    gpu_free = _gpu_free_mib()
    runtime = "gpu" if (gpu_free is not None and gpu_free >= _GPU_FREE_MIB_MIN
                        and _cuda_usable()) else "cpu"
    details["runtime"] = runtime
    details["gpu_free_mib"] = gpu_free
    details["torch_version"] = _torch_version()
    return {"available": True, "reason": None, "details": details}


def _cuda_usable() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


def _torch_version() -> str:
    try:
        import torch
        return str(torch.__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _load_model(name: str):
    """加载并缓存 demucs 模型（LRU 上限 _MODEL_CACHE_MAX，避免长驻进程累积）。"""
    cached = _MODEL_CACHE.get(name)
    if cached is not None:
        _MODEL_CACHE.move_to_end(name)
        return cached
    from demucs.pretrained import get_model
    model = get_model(name)
    _MODEL_CACHE[name] = model
    while len(_MODEL_CACHE) > _MODEL_CACHE_MAX:
        _evicted_name, evicted = _MODEL_CACHE.popitem(last=False)
        del evicted
        _release_cuda_cache()
    return model


def _release_cuda_cache() -> None:
    """释放未被引用的 CUDA 缓存（CPU 环境静默跳过）。"""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 —— 释放失败不影响业务结果
        return


def stems_out_dir(content_hash: str, model: str = MODEL_NAME) -> Path:
    return stems_dir() / content_hash / model


def separate(stereo_wav: str | Path, content_hash: str,
             model: str = MODEL_NAME, force: bool = False) -> dict:
    """四轨分轨（drums/bass/other/vocals），产物持久化并带 manifest。

    Args:
        stereo_wav: ensure_wav 产出的 44.1kHz 立体声 wav 路径。
        content_hash: 内容哈希，决定输出目录（幂等缓存键）。
        force: True 时忽略已有 manifest 重算。

    Returns:
        {model, method, model_version, runtime, elapsed_sec, duration_sec,
         stems_dir, manifest_path, stems: {name: {"path", "rms"}}}

    Raises:
        CapabilityUnavailable: 引擎缺失/权重不可用。
        InvalidAudio: 输入无法读取。
    """
    status = probe()
    if not status["available"]:
        raise CapabilityUnavailable(
            "demucs 分轨引擎不可用",
            details=status["details"],
            hint="检查 torch/demucs 安装与 htdemucs 权重缓存（首跑自动下载）")

    import soundfile as sf

    out_dir = stems_out_dir(content_hash, model)
    manifest_path = out_dir / _MANIFEST
    if manifest_path.exists() and not force:
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass  # manifest 损坏则重算

    # dtype=float32：soundfile 默认 float64，900s 立体声会多占约 300MB
    y, sr = sf.read(str(stereo_wav), always_2d=True, dtype="float32")
    if y.ndim != 2 or y.shape[1] < 2:
        from rea_music.core.errors import InvalidAudio
        raise InvalidAudio(f"分轨输入需要立体声 wav: {stereo_wav}")
    if sr != status["details"]["samplerate"]:
        # ensure_wav 统一 44.1k，此处防御性重采样
        import librosa
        y = librosa.resample(y.T, orig_sr=sr,
                             target_sr=status["details"]["samplerate"]).T
        y = np.ascontiguousarray(y, dtype=np.float32)

    import torch
    from demucs.apply import apply_model

    m = _load_model(model)
    device = torch.device(status["details"]["runtime"])
    mix = torch.from_numpy(np.ascontiguousarray(y.T, dtype=np.float32)) \
        .unsqueeze(0)  # (1, ch, samples)

    estimates = None
    t0 = time.time()
    try:
        with torch.no_grad():
            estimates = apply_model(m, mix, device=device, shifts=1, split=True,
                                    overlap=0.25, progress=False)
        elapsed = time.time() - t0

        out_dir.mkdir(parents=True, exist_ok=True)
        stems: dict[str, dict] = {}
        names = list(m.sources)
        for k, name in enumerate(names):
            track = estimates[0, k].T.cpu().numpy().astype(np.float32)
            track = np.clip(track, -1.0, 1.0 - 1e-6)  # PCM_16 落盘防削波
            path = out_dir / f"{name}.wav"
            sf.write(str(path), track, int(m.samplerate), subtype="PCM_16")
            rms = float(np.sqrt(np.mean(track.astype(np.float64) ** 2)))
            stems[name] = {"path": str(path), "rms": round(rms, 6)}
    finally:
        # 显式释放中间张量：否则长驻 MCP 进程反复跑 deep 档时内存/显存单调上涨
        # （模型本身由 _MODEL_CACHE 的 LRU 上限管理）。
        estimates = None
        mix = None
        _release_cuda_cache()

    manifest = {
        "ok": True,
        "model": model,
        "method": f"demucs_{model}_python_api",
        "model_version": f"{model} (demucs {_demucs_version()})",
        "runtime": status["details"]["runtime"],
        "elapsed_sec": round(elapsed, 1),
        "duration_sec": round(len(y) / int(m.samplerate), 1),
        "content_hash": content_hash,
        "stems_dir": str(out_dir),
        "manifest_path": str(manifest_path),
        "stems": stems,
        "cpu_speed_note": "CPU 分轨耗时约 1.5x 曲长",
        "created_at": datetime.now(timezone.utc).astimezone()
                           .isoformat(timespec="seconds"),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    return manifest


def _demucs_version() -> str:
    try:
        import demucs
        return str(getattr(demucs, "__version__", "unknown"))
    except Exception:  # noqa: BLE001
        return "unknown"


def pick_melody_stem(manifest: dict) -> tuple[str, str] | None:
    """从分轨 manifest 里挑旋律转写用轨：vocals 优先，静音则回退 other。

    Returns:
        (stem_name, stem_path) 或 None（无有效内容轨）。
    """
    stems = manifest.get("stems") or {}
    for name in ("vocals", "other"):
        info = stems.get(name)
        if info and info.get("rms", 0.0) > SILENT_STEM_RMS:
            return name, info["path"]
    return None


def separate_stems_job(job_id: str, audio_path: str, content_hash: str,
                       model: str = MODEL_NAME, force: bool = False) -> str:
    """Job 入口（MCP 后台/同步共用）：返回 manifest 路径字符串。"""
    from rea_music.core import jobs as jobs_mod
    jobs_mod.set_stage(job_id, "decode_audio", 0.1)
    from rea_music.analysis import audio as audio_mod
    wav = audio_mod.ensure_wav(audio_path, content_hash)
    jobs_mod.set_stage(job_id, "separate", 0.25)
    manifest = separate(wav["stereo_wav"], content_hash, model=model,
                        force=force)
    jobs_mod.set_stage(job_id, "write_manifest", 0.95)
    return str(manifest["manifest_path"])
