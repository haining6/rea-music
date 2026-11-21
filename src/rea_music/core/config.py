"""配置加载。``config.json`` 缺省时全部用默认值，允许零配置启动。

配置文件位置：``REA_MUSIC_HOME/config.json``（JSON 格式，非 YAML）。
模板见仓库根 ``config.example.json``。

解析失败**不再静默吞掉**：写入 ``config_error`` 并由 ``capabilities`` 暴露，
否则用户把格式写错时会看到"默认值生效"这种无法解释的行为。
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

from rea_music.core.paths import workspace_root

_DEFAULTS: dict = {
    "analysis": {
        "default_tier": "standard",
        "max_duration_sec": 900,
        "structure_min_segment_sec": 8.0,
        "key_window_sec": 24.0,
    },
    "generation": {
        "yue2_mcp_url": "http://127.0.0.1:8791/mcp",
        "suno_prompt_package": True,
        # YuE2 真实生成链（R4-A）：studio_wait 轮询上限与导出格式
        "yue2_wait_timeout_sec": 600,
        "yue2_export_format": "wav",
        # ACE-Step REST 探测地址（R4-B）：路由以运行时 /openapi.json 发现为准
        "acestep_base_url": "http://127.0.0.1:8001",
        # 评测迭代硬化（R5/A8）：默认上限 3 轮；连续 2 轮 satisfied 维数无改善即停
        "optimization": {
            "max_iterations": 3,
            "satisfied_repeat_stop": 2,
        },
    },
    "jobs": {
        "max_workers": 2,
        "max_retries": 2,
    },
    "remote": {
        "songbrain_enabled": False,
        # 远程交叉验证双开关（SECURITY.md §3）：key 与上传授权必须同时存在，
        # 缺任一条即 capability_unavailable，不会静默外发音频。
        "songbrain_api_key": None,
        "songbrain_upload_authorized": False,
    },
    "input": {
        # SSRF 边界：默认只放行公网地址；NAS/DLNA 用例需显式置 True。
        # 云元数据与链路本地地址不受此开关影响，永远拒绝（见 core/net_guard.py）。
        "allow_private_networks": False,
    },
}

#: 最近一次 load_config() 的来源与错误，供 capabilities / 诊断使用
_LAST_STATUS: dict = {
    "config_source": "builtin_defaults",
    "config_error": None,
}

#: 同一个文件路径只告警一次，避免每次 capabilities 调用都刷警告
_WARNED: set[str] = set()


def config_file() -> Path:
    """配置文件路径（不一定存在）。"""
    return workspace_root() / "config.json"


def load_config() -> dict:
    """合并默认值与 ``REA_MUSIC_HOME/config.json``（若存在且可解析）。

    Returns:
        完整配置字典。用户文件里的同名字典段做浅合并，非字典值直接覆盖。

    Note:
        解析失败时**不抛异常**（保持"零配置也能跑"的既有契约），但会
        ``warnings.warn`` 一次并把错误记录进 :func:`config_status`。
    """
    cfg = json.loads(json.dumps(_DEFAULTS))  # deep copy，避免调用方改到 _DEFAULTS
    path = config_file()
    source = "builtin_defaults"
    error: dict | None = None

    if Path(path).exists():
        try:
            raw = Path(path).read_text(encoding="utf-8")
            user = json.loads(raw)
            if not isinstance(user, dict):
                raise ValueError(f"顶层必须是 JSON 对象，实际是 {type(user).__name__}")
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            # 键名不进日志：key 可能就在文件里，避免错误信息把密钥带出去
            error = {
                "path": str(path),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "effect": "已回落到内置默认值，你的配置未生效",
            }
            msg = (f"配置文件解析失败，已回落默认值: {path} "
                   f"({type(exc).__name__}: {exc})。"
                   f"注意本文件必须是 JSON 格式（模板见 config.example.json）。")
            if str(path) not in _WARNED:
                _WARNED.add(str(path))
                warnings.warn(msg, RuntimeWarning, stacklevel=2)
        else:
            source = str(path)
            for section, values in user.items():
                if isinstance(values, dict) and isinstance(cfg.get(section), dict):
                    cfg[section].update(values)
                else:
                    cfg[section] = values

    env_tier = os.environ.get("REA_MUSIC_TIER")
    if env_tier in ("fast", "standard", "deep"):
        cfg["analysis"]["default_tier"] = env_tier

    _LAST_STATUS["config_source"] = source
    _LAST_STATUS["config_error"] = error
    return cfg


def config_status() -> dict:
    """返回最近一次配置加载的来源与错误。

    Returns:
        ``{"config_source": "builtin_defaults" | "<绝对路径>",
        "config_error": None | {"path", "error_type", "error", "effect"}}``

    Note:
        需先调用过 :func:`load_config`，否则返回初始状态（默认值）。
        ``capabilities`` 会把它拼进输出，Agent 因此能判断"我的配置到底生效了没有"。
    """
    return dict(_LAST_STATUS)
