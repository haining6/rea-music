"""songbrain 远程交叉验证 Adapter（R2-D）。

包装 songbrain SDK 0.4.0（REST，DNA 模式）。原则 8：音频不出本机——
只有当 config remote.songbrain_api_key（或 SONGBRAIN_API_KEY 环境变量）
与 remote.songbrain_upload_authorized（用户显式授权上传）同时满足才可用；
任一缺失即 unavailable + missing_reason，直调抛 capability_unavailable，
Deep 档继续跑不报错。

交叉验证范围：key / bpm（songbrain DNA 模式无和弦段输出，如实记入说明）。
"""

from __future__ import annotations

import re
import time

from rea_music.core.errors import CapabilityUnavailable

SDK_METHOD = "songbrain_rest_dna_mode"
MISSING_KEY = "songbrain_api_key_missing"
MISSING_AUTH = "songbrain_upload_not_authorized"


def probe(cfg: dict | None = None) -> dict:
    """实测 key 与授权状态（纯配置检查，不发网络请求）。

    Returns:
        {available, reason, details}；reason 为 missing_reason 语义。
    """
    if cfg is None:
        from rea_music.core.config import load_config
        cfg = load_config()
    import os
    remote = cfg.get("remote", {}) or {}
    api_key = remote.get("songbrain_api_key") or \
        os.environ.get("SONGBRAIN_API_KEY")
    authorized = bool(remote.get("songbrain_upload_authorized", False))
    details = {
        "sdk_version": _sdk_version(),
        "upload_authorized": authorized,
        "api_key_present": bool(api_key),
        "note": "远程交叉验证引擎；启用前必须获得用户对上传音频的显式授权",
    }
    if not api_key:
        return {"available": False, "reason": MISSING_KEY, "details": details}
    if not authorized:
        return {"available": False, "reason": MISSING_AUTH, "details": details}
    return {"available": True, "reason": None, "details": details}


def _sdk_version() -> str:
    try:
        import songbrain
        return f"songbrain_sdk_{getattr(songbrain, '__version__', '0.4.0')}"
    except Exception:  # noqa: BLE001 —— SDK 缺失也走 unavailable 路径
        return "songbrain_sdk_not_importable"


_KEY_RE = re.compile(
    r"^(?P<tonic>[A-Ga-g](?:#|b)?)[:\s]*(?P<mode>major|min|minor|maj|minor)?$",
    re.IGNORECASE)


def normalize_key(raw: str | None) -> tuple[str, str] | None:
    """宽松解析 key 标签 → ("C", "major"|"minor")；无法解析返回 None。

    兼容 "C:maj"、"C major"、"Cmaj"、"c minor"、"C" 等写法。
    """
    if not raw or not isinstance(raw, str):
        return None
    m = _KEY_RE.match(raw.strip())
    if not m:
        return None
    tonic = m.group("tonic")[0].upper() + m.group("tonic")[1:]
    mode_raw = (m.group("mode") or "").lower()
    if mode_raw in ("maj", "major", ""):
        return tonic, "major"
    if mode_raw in ("min", "minor"):
        return tonic, "minor"
    return None


def cross_validate(audio_path: str, local_dna: dict,
                   cfg: dict | None = None) -> dict:
    """上传音频做远程分析，返回与本地 DNA 的 key/bpm 交叉验证记录。

    Raises:
        CapabilityUnavailable: key/授权缺失，或 SDK/网络失败（如实降级语义）。
    """
    status = probe(cfg)
    if not status["available"]:
        raise CapabilityUnavailable(
            "songbrain 远程交叉验证不可用",
            details={"missing_reason": status["reason"],
                     **status["details"]},
            hint="在 config remote.songbrain_api_key 填入 key 并设置 "
                 "songbrain_upload_authorized=true（须用户显式授权上传）")

    import os
    if cfg is None:
        from rea_music.core.config import load_config
        cfg = load_config()
    api_key = (cfg.get("remote", {}) or {}).get("songbrain_api_key") or \
        os.environ.get("SONGBRAIN_API_KEY")

    t0 = time.time()
    try:
        from songbrain import Songbrain
        with Songbrain(api_key=api_key) as sb:
            song = sb.analyze(file=audio_path, mode="dna", wait=True,
                              timeout=300.0)
    except Exception as exc:  # noqa: BLE001 —— 远程任何失败都结构化降级
        raise CapabilityUnavailable(
            f"songbrain 分析失败: {type(exc).__name__}",
            details={"error": str(exc),
                     "audio": str(audio_path)},
            hint="远程服务/配额问题不阻塞本地分析；可稍后重试") from exc
    elapsed = time.time() - t0

    remote_dna = song.get("song_dna") or {}
    remote_key_raw = remote_dna.get("key")
    remote_bpm = remote_dna.get("tempo_bpm")

    local_key = (local_dna.get("tonality", {}).get("key", {}) or {}).get("value")
    local_bpm = (local_dna.get("rhythm", {}).get("bpm", {}) or {}).get("value")

    key_cmp = _compare_keys(local_key, remote_key_raw)
    bpm_cmp = _compare_bpm(local_bpm, remote_bpm)

    return {
        "engine": "songbrain",
        "method": SDK_METHOD,
        "model_version": _sdk_version(),
        "remote_song_id": song.get("id"),
        "elapsed_sec": round(elapsed, 1),
        "comparisons": {
            "key": key_cmp,
            "bpm": bpm_cmp,
            "chords": {
                "skipped": True,
                "note": "songbrain DNA 模式无和弦段输出，和弦维度保留本地引擎结果",
            },
        },
        "note": "远程结果与本地引擎差异如实记录，不覆盖本地测量值",
    }


def _compare_keys(local_key: str | None,
                  remote_key_raw: str | None) -> dict:
    out = {"local": local_key, "remote": remote_key_raw}
    l_norm = normalize_key(local_key)
    r_norm = normalize_key(remote_key_raw)
    if l_norm is None or r_norm is None:
        out["match"] = None
        out["note"] = "无法解析其中一侧 key 标签，保留原始值供人工判读"
        return out
    out["local_normalized"] = f"{l_norm[0]} {l_norm[1]}"
    out["remote_normalized"] = f"{r_norm[0]} {r_norm[1]}"
    out["match"] = (l_norm[0] == r_norm[0] and l_norm[1] == r_norm[1])
    return out


def _compare_bpm(local_bpm: float | None,
                 remote_bpm: float | None) -> dict:
    out = {"local": local_bpm, "remote": remote_bpm}
    if local_bpm is None or remote_bpm is None:
        out["match"] = None
        out["note"] = "其中一侧 BPM 缺失，不做结论"
        return out
    delta = float(remote_bpm) - float(local_bpm)
    out["delta_bpm"] = round(delta, 2)
    out["delta_percent"] = round(abs(delta) / float(local_bpm) * 100.0, 2)
    out["match"] = out["delta_percent"] <= 3.0
    return out
