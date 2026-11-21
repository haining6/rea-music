"""chordotomy 和弦引擎 Adapter（R2-C）。

实测 CLI（chordotomy 0.3.1）输出 schema 11：
    {schema_version: 11, generator{name,version,engine{name,version}},
     source{path,duration,url}, global_key{label,source,candidates},
     key_regions[{start_beat,end_beat,label}], beats[float],
     segments[{start_beat,end_beat,start_time,end_time,chord,candidates,
               bass,inversion,numeral,role,function,target,edited}]}
与设计草稿假设一致，仅 global key 位置在 global_key.label（非顶层 key），
normalize 层按实测适配。chord 标签 "N" 表示无和弦段，如实保留。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from rea_music.core.errors import CapabilityUnavailable

CLI_TIMEOUT_SEC = 900
ENGINE = "chordotomy_dsp"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_VERSION_CACHE: str | None = None


def find_cli() -> str | None:
    """定位 chordotomy CLI：PATH 优先，其次主 venv Scripts/bin 目录。

    pytest 以 .venv/Scripts/python.exe 直启时 PATH 可能不含 venv Scripts，
    显式回退定位保证引擎探测不受启动方式影响。
    """
    exe = shutil.which("chordotomy")
    if exe:
        return exe
    for candidate in (_REPO_ROOT / ".venv" / "Scripts" / "chordotomy.exe",
                      _REPO_ROOT / ".venv" / "bin" / "chordotomy"):
        if candidate.exists():
            return str(candidate)
    return None


def probe() -> dict:
    """实测 chordotomy CLI 在位且可执行（--version 探活）。"""
    exe = find_cli()
    if not exe:
        return {"available": False, "reason": "chordotomy_cli_not_found",
                "details": {"hint": "uv pip install chordotomy（MIT，无 torch 依赖）"}}
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=60)
        version = (out.stdout or "").strip().splitlines()[-1].strip() \
            if (out.stdout or "").strip() else ""
        if out.returncode != 0:
            return {"available": False, "reason": "chordotomy_cli_error",
                    "details": {"exe": exe,
                                "stderr_tail": (out.stderr or "")[-500:]}}
        return {"available": True, "reason": None,
                "details": {"exe": exe, "version": version}}
    except (subprocess.SubprocessError, OSError) as exc:
        return {"available": False, "reason": "chordotomy_probe_failed",
                "details": {"exe": exe, "error": str(exc)}}


def cli_version() -> str:
    global _CLI_VERSION_CACHE
    if _CLI_VERSION_CACHE is None:
        status = probe()
        _CLI_VERSION_CACHE = status["details"].get("version") or "unknown"
    return _CLI_VERSION_CACHE


def analyze(audio_path: str | Path, *, out_dir: str | Path | None = None,
            key_hint: str | None = None) -> dict:
    """运行 chordotomy analyze 并 normalize 到 REA 和弦契约。

    Args:
        audio_path: 输入音频（wav/mp3）。
        out_dir: CLI JSON 中间文件目录；None 则用临时目录（分析后删除）。
        key_hint: 形如 "C:maj" 的调性提示，覆盖其内部估计（可选）。

    Returns:
        {engine, method, model_version, confidence, segments, progression,
         key_from_engine, key_candidates, beats, elapsed_sec}

    Raises:
        CapabilityUnavailable: CLI 缺失或执行失败。
    """
    exe = find_cli()
    if not exe:
        raise CapabilityUnavailable(
            "chordotomy CLI 未安装", details=probe()["details"],
            hint="uv pip install chordotomy 可启用主和弦引擎；"
                 "模板后备引擎仍可用")
    audio_path = Path(audio_path)
    if not audio_path.exists():
        from rea_music.core.errors import FileNotFound
        raise FileNotFound(f"chordotomy 输入不存在: {audio_path}")

    import tempfile
    tmp_ctx = out_dir is None
    workdir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(
        prefix="rea_chordotomy_"))
    workdir.mkdir(parents=True, exist_ok=True)
    out_path = workdir / f"{audio_path.stem}.chords.json"

    cmd = [exe, "analyze", str(audio_path), "-o", str(out_path), "--force"]
    if key_hint:
        cmd += ["--key", key_hint]
    t0 = time.time()
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=CLI_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            raise CapabilityUnavailable(
                f"chordotomy 分析超时（>{CLI_TIMEOUT_SEC}s）",
                details={"audio": str(audio_path)},
            ) from None
        except OSError as exc:
            raise CapabilityUnavailable(
                f"chordotomy 子进程启动失败: {exc}",
                details={"cmd": cmd[:2]},
            ) from None
        elapsed = time.time() - t0

        if proc.returncode != 0 or not out_path.exists():
            raise CapabilityUnavailable(
                "chordotomy 分析失败",
                details={"returncode": proc.returncode,
                         "stderr_tail": (proc.stderr or "")[-500:],
                         "audio": str(audio_path)},
                hint="模板后备引擎仍可用（analysis.chords_engine=template）")
        try:
            raw = json.loads(out_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raise CapabilityUnavailable(
                "chordotomy 输出 JSON 解析失败",
                details={"json_path": str(out_path)},
            ) from None
    finally:
        # 自建临时目录在成功与失败路径上都要清干净；rmtree(ignore_errors=True)
        # 避免"目录非空/句柄占用"抛出的 OSError 顶替正常返回值。
        if tmp_ctx:
            shutil.rmtree(workdir, ignore_errors=True)

    return _normalize(raw, elapsed)


def _normalize(raw: dict, elapsed_sec: float) -> dict:
    """schema 11 → REA 和弦契约（与模板引擎输出同构，字段可互换）。"""
    segments = []
    for s in raw.get("segments", []):
        seg = {
            "start_sec": round(float(s.get("start_time", 0.0)), 2),
            "end_sec": round(float(s.get("end_time", 0.0)), 2),
            "chord": str(s.get("chord", "N")),
        }
        # engine 未提供每段置信度；0.7 为既有契约默认值，candidates 保留供 Agent 判读
        seg["confidence"] = 0.7 if seg["chord"] != "N" else 0.3
        for k in ("bass", "inversion", "numeral", "role", "function"):
            if s.get(k) is not None:
                seg[k] = s[k]
        if s.get("candidates"):
            seg["candidates"] = list(s["candidates"][:3])
        segments.append(seg)

    gk = raw.get("global_key") or {}
    generator = raw.get("generator") or {}
    version = str(generator.get("version") or cli_version())
    engine_info = (generator.get("engine") or {}).get("name") or "dsp"
    conf = round(
        sum(s["confidence"] for s in segments) / len(segments), 2
    ) if segments else 0.0

    return {
        "engine": ENGINE,
        "method": f"chordotomy_{version}_{engine_info}_beat_aligned",
        "model_version": f"chordotomy {version} ({engine_info})",
        "confidence": conf,
        "segments": segments,
        "progression": _progression([s["chord"] for s in segments]),
        "key_from_engine": gk.get("label"),
        "key_candidates": list(gk.get("candidates", []) or []),
        "beats": [round(float(b), 3) for b in raw.get("beats", [])][:2048],
        "schema_version": raw.get("schema_version"),
        "elapsed_sec": round(elapsed_sec, 1),
    }


def _progression(labels: list[str]) -> list[dict]:
    """最常见的 4 级和弦循环（n-gram 统计，与 chords.py 契约一致）。"""
    from collections import Counter
    n = 4
    grams = [tuple(labels[i:i + n]) for i in range(len(labels) - n + 1)
             if "N" not in labels[i:i + n]]
    if not grams:
        return []
    top = Counter(grams).most_common(3)
    return [{"progression": list(g), "count": c} for g, c in top if c >= 2]
