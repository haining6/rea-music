"""工作区目录布局。模型缓存、临时音频、分轨、工件、任务分目录存储。"""

from __future__ import annotations

import os
from pathlib import Path

# 源码树布局（editable / 开发）与已安装布局（wheel）要分开取默认工作区：
# - 源码树：<repo>/data（gitignored，文档与脚本默认指向这里）
# - 已安装：用户可写的数据目录（site-packages 通常不可写，绝不能落在 venv 里）
_PACKAGE_PREFIX = Path(__file__).resolve().parents[3]
_IN_SOURCE_TREE = (_PACKAGE_PREFIX / "pyproject.toml").is_file()


def user_data_home() -> Path:
    """平台惯例的用户数据目录（Windows: %LOCALAPPDATA%/REA-Music）。

    macOS / Linux 走 XDG 风格 ``~/.local/share/rea-music``。
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "REA-Music"
    return Path.home() / ".local" / "share" / "rea-music"


def default_home() -> Path:
    """未经 REA_MUSIC_HOME 指定时的默认工作区。"""
    if _IN_SOURCE_TREE and os.access(_PACKAGE_PREFIX, os.W_OK):
        return _PACKAGE_PREFIX / "data"
    return user_data_home()


DEFAULT_HOME = default_home()


def workspace_root() -> Path:
    """REA_MUSIC_HOME 环境变量最高优先，其次源码树 <repo>/data 或用户数据目录。"""
    env = os.environ.get("REA_MUSIC_HOME")
    return Path(env).expanduser().resolve() if env else default_home()


def _dir(name: str) -> Path:
    p = workspace_root() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    return _dir("cache")


def jobs_dir() -> Path:
    return _dir("jobs")


def artifacts_dir() -> Path:
    return _dir("artifacts")


def downloads_dir() -> Path:
    """远端输入落盘目录（D22）。只在用户主动提供 URL 时写入，不入库。"""
    return _dir("downloads")


def stems_dir() -> Path:
    return _dir("stems")


def experiments_dir() -> Path:
    return _dir("experiments")


def content_dir(kind: str, content_hash: str) -> Path:
    """按内容哈希组织工件目录：kind='audio'|'dna'|'plan' 等。"""
    p = cache_dir() / kind / content_hash
    p.mkdir(parents=True, exist_ok=True)
    return p
