"""basic-pitch 旋律转写 Adapter（R2-B）。

主 venv 为 Python 3.12，basic-pitch 官方仅支持 ≤3.11，故按设计 A2 走
engines/bp-venv（py3.10 + onnxruntime）子进程隔离调用，主 venv 零污染：

    engines/bp-venv/Scripts/python.exe scripts/bp_transcribe_runner.py
        <audio> --json-out <contract.json> --midi-out <song.mid>

runner 产出契约 JSON {notes:[pitch_midi,start_sec,end_sec,amplitude]} +
MIDI 文件；本 Adapter 负责 probe（venv 解释器 + ICASSP nmp.onnx 在位实测）、
子进程调度、stderr 捕获与结构化错误。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from rea_music.core.errors import CapabilityUnavailable

_REPO_ROOT = Path(__file__).resolve().parents[3]
BP_VENV_DIR = _REPO_ROOT / "engines" / "bp-venv"
BP_PYTHON = BP_VENV_DIR / "Scripts" / "python.exe"
RUNNER = _REPO_ROOT / "scripts" / "bp_transcribe_runner.py"
MODEL_REL = Path("Lib") / "site-packages" / "basic_pitch" / "saved_models" \
    / "icassp_2022" / "nmp.onnx"
SUBPROCESS_TIMEOUT_SEC = 600
METHOD = "basic_pitch_icassp_2022"


def _model_path() -> Path:
    return BP_VENV_DIR / MODEL_REL


def probe() -> dict:
    """实测检查 bp-venv 解释器与 ICASSP_2022 模型文件在位。"""
    details: dict = {"venv_python": str(BP_PYTHON),
                     "runner": str(RUNNER),
                     "model_path": str(_model_path())}
    if not BP_PYTHON.exists():
        return {"available": False, "reason": "bp_venv_python_missing",
                "details": {**details,
                            "hint": "按 engines/ 下的隔离 venv 方案重建 "
                                    "engines/bp-venv（py3.10 + basic-pitch + "
                                    "onnxruntime + setuptools<81）"}}
    if not _model_path().exists():
        return {"available": False, "reason": "icassp_2022_model_missing",
                "details": {**details,
                            "hint": "nmp.onnx 模型文件缺失；重装 basic-pitch "
                                    "或恢复 saved_models/icassp_2022 目录"}}
    if not RUNNER.exists():
        return {"available": False, "reason": "runner_script_missing",
                "details": {**details,
                            "hint": "scripts/bp_transcribe_runner.py 缺失"}}
    return {"available": True, "reason": None, "details": details}


def transcribe(audio_path: str | Path, *, json_out: str | Path,
               midi_out: str | Path) -> dict:
    """在 bp-venv 里转写音频 → 契约 JSON + MIDI。

    Args:
        audio_path: 待转写音频（通常为 vocals 轨 wav）。
        json_out: 契约 JSON 输出路径（由调用方管理缓存与命名）。
        midi_out: MIDI 落盘路径。

    Returns:
        {ok, method, model_version, notes, note_count, elapsed_sec, midi_path}

    Raises:
        CapabilityUnavailable: venv/模型缺失或子进程失败（stderr 进 details）。
    """
    status = probe()
    if not status["available"]:
        raise CapabilityUnavailable(
            "basic-pitch 隔离环境不可用", details=status["details"],
            hint=status["details"].get("hint") or
                 "恢复 engines/bp-venv 后重试；pYIN 后备路径可用")
    audio_path = Path(audio_path)
    if not audio_path.exists():
        from rea_music.core.errors import FileNotFound
        raise FileNotFound(f"转写输入不存在: {audio_path}")
    json_out = Path(json_out)
    midi_out = Path(midi_out)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    midi_out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [str(BP_PYTHON), str(RUNNER), str(audio_path),
           "--json-out", str(json_out), "--midi-out", str(midi_out)]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC, cwd=str(_REPO_ROOT))
    except subprocess.TimeoutExpired:
        raise CapabilityUnavailable(
            f"basic-pitch 转写超时（>{SUBPROCESS_TIMEOUT_SEC}s）",
            details={"audio": str(audio_path),
                     "hint": "CPU 推理慢属预期（约 0.3x 曲长），超长输入请分轨分段"},
        ) from None
    except OSError as exc:
        raise CapabilityUnavailable(
            f"无法启动 bp-venv 子进程: {exc}",
            details={"cmd": cmd[:2], "stderr": str(exc)},
        ) from None
    elapsed = time.time() - t0

    stderr_tail = (proc.stderr or "")[-2000:]
    if proc.returncode != 0 or not json_out.exists():
        raise CapabilityUnavailable(
            "basic-pitch 转写子进程失败",
            details={"returncode": proc.returncode,
                     "stderr_tail": stderr_tail,
                     "audio": str(audio_path)},
            hint="查看 stderr_tail 定位 venv 内依赖问题；pYIN 后备路径可用")
    try:
        payload = json.loads(json_out.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raise CapabilityUnavailable(
            "basic-pitch 输出契约 JSON 损坏",
            details={"json_path": str(json_out), "stderr_tail": stderr_tail},
        ) from None
    if not payload.get("ok"):
        raise CapabilityUnavailable(
            "basic-pitch 转写失败: " + str(payload.get("error")),
            details={"stderr_tail": stderr_tail,
                     "audio": str(audio_path)},
            hint="pYIN 后备路径可用")
    payload["elapsed_sec"] = round(elapsed, 1)
    payload["method"] = METHOD
    payload["model_version"] = str(payload.get("model_version") or
                                   "icassp_2022_nmp_onnx")
    return payload
