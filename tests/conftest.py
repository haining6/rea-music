"""pytest 公共夹具（C21/C22）。

两条硬约束：
1. ``fixtures/*.wav`` 被 .gitignore 排除，干净克隆里没有音频。自举只在 conftest
   里实现一次（session 级 autouse），不再由各测试模块各写一遍 —— 早前
   test_standalone.py 完全没有自举，单独运行必然因缺 fixture 失败。
2. 默认把 ``REA_MUSIC_HOME`` 指向 session 级临时目录：任何用例都不许把缓存、
   Job 记录、分轨产物写进仓库 ``data/``（早前 9 个模块里 5 个没有隔离）。

需要真实工件的用例用环境变量显式 opt-out：
``REA_MUSIC_TEST_HOME=<dir>``（例如指向仓库 data/ 以复用既有分轨缓存）。
单个用例仍可用 ``monkeypatch.setenv("REA_MUSIC_HOME", ...)`` 临时改写。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "fixtures"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(scope="session", autouse=True)
def build_fixtures() -> None:
    """干净克隆里现场生成 fixtures/*.wav（整个会话只跑一次）。"""
    if (FIXTURES / "pop_c_major_120.wav").exists():
        return
    if str(FIXTURES) not in sys.path:
        sys.path.insert(0, str(FIXTURES))
    from make_fixtures import build_all

    build_all(FIXTURES)


@pytest.fixture(scope="session", autouse=True)
def isolated_workspace(tmp_path_factory):
    """把工作区重定向到 session 临时目录，避免测试写脏仓库 data/。

    Yields:
        本次会话使用的工作区根目录。
    """
    override = os.environ.get("REA_MUSIC_TEST_HOME")
    home = (Path(override).expanduser() if override
            else tmp_path_factory.mktemp("rea_home"))
    home.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("REA_MUSIC_HOME")
    os.environ["REA_MUSIC_HOME"] = str(home)
    try:
        yield home
    finally:
        if previous is None:
            os.environ.pop("REA_MUSIC_HOME", None)
        else:
            os.environ["REA_MUSIC_HOME"] = previous
