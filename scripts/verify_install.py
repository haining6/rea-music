"""端到端安装自检：构建 wheel → 装进全新 venv → 在仓库外跑真实分析。

用途：验证「别人 pip 安装后即可用」这条发布契约（D1 回归防线）。
用法：python scripts/verify_install.py
前提：本机有 Python 3.12；安装依赖需要网络。
可选环境变量：
  VERIFY_PIP_INDEX  自定义包索引（不设则用 pip 默认源，不硬编码任何厂商镜像）
优先使用 uv（本项目环境通常无 pip），没有 uv 时回退 pip。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "fixtures" / "pop_c_major_120.wav"
# 公开仓库不硬编码厂商镜像：默认走 pip 默认源，需要镜像时用环境变量覆盖
MIRROR = os.environ.get("VERIFY_PIP_INDEX") or None
UV = shutil.which("uv")


def run(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    print("  $", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)


def build_wheel(dist: Path) -> Path | None:
    if UV:
        r = run([UV, "build", "--wheel", "--out-dir", str(dist)], cwd=REPO)
    else:
        r = run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                 "-w", str(dist), str(REPO)], cwd=REPO)
    if r.returncode != 0:
        print("  build stderr:", (r.stderr or r.stdout)[-500:])
        return None
    return next(iter(dist.glob("rea_music-*.whl")), None)


def make_venv(venv: Path) -> Path | None:
    if UV:
        r = run([UV, "venv", "--python", "3.12", str(venv)], cwd=venv.parent)
    else:
        r = run([sys.executable, "-m", "venv", str(venv)], cwd=venv.parent)
    if r.returncode != 0:
        print("  venv stderr:", (r.stderr or r.stdout)[-500:])
        return None
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install_wheel(py: Path, wheel: Path) -> bool:
    # MIRROR 为 None 时不传索引参数，让 pip/uv 用各自的默认源
    index_args = ["--index-url", MIRROR] if MIRROR else []
    cmd = ([UV, "pip", "install", "-p", str(py), *index_args, str(wheel)]
           if UV else
           [str(py), "-m", "pip", "install", "--quiet", *index_args, str(wheel)])
    r = run(cmd, cwd=wheel.parent)
    if r.returncode != 0:
        print("  install stderr:", (r.stderr or r.stdout)[-600:])
        return False
    return True


def main() -> int:
    if not FIXTURE.exists():
        print("FAIL: 缺少 fixtures/pop_c_major_120.wav（先跑 python fixtures/make_fixtures.py）")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="rea-verify-"))
    outside = tmp / "outside"
    outside.mkdir()
    print(f"临时目录: {tmp}")

    print("[1/6] 构建 wheel ...")
    wheel = build_wheel(tmp / "dist")
    if wheel is None:
        print("FAIL: 构建失败")
        return 1
    print("  wheel:", wheel.name, "| 大小:", wheel.stat().st_size)

    print("[2/6] 创建全新 venv ...")
    py = make_venv(tmp / "venv")
    if py is None:
        print("FAIL: venv 创建失败")
        return 1

    print("[3/6] 安装 wheel（含依赖，走镜像）...")
    if not install_wheel(py, wheel):
        print("FAIL: 安装失败")
        return 1

    print("[4/6] 仓库外运行 capabilities ...")
    cap = run([str(py), "-m", "rea_music.cli.main", "capabilities"], cwd=outside)
    if cap.returncode != 0:
        print("FAIL: capabilities 失败")
        print((cap.stderr or cap.stdout)[-800:])
        return 1
    data = json.loads(cap.stdout[cap.stdout.index("{"):])
    if not data.get("ok"):
        print("FAIL: capabilities 返回 ok=false")
        return 1
    print("  ok:", data.get("version"),
          "| ffmpeg:", data.get("input", {}).get("ffmpeg", {}).get("available"))

    print("[5/6] 仓库外分析真实音频（fast）...")
    env = dict(os.environ, REA_MUSIC_HOME=str(tmp / "home"))
    ana = run([str(py), "-m", "rea_music.cli.main", "analyze",
               str(FIXTURE), "--tier", "fast"], cwd=outside, env=env)
    if ana.returncode != 0:
        print("FAIL: 分析失败（这正是 D1 类问题：schema/资源未进包）")
        print((ana.stderr or ana.stdout)[-800:])
        return 1
    out = json.loads(ana.stdout[ana.stdout.index("{"):])
    dna_path = out.get("dna_path") or (out.get("job") or {}).get("dna_path")
    if not out.get("ok") or not dna_path or not Path(dna_path).exists():
        print("FAIL: 未产出 DNA")
        print(json.dumps(out, ensure_ascii=False)[:600])
        return 1
    bpm = (json.load(open(dna_path, encoding="utf-8"))["rhythm"]["bpm"] or {}).get("value")
    print("  DNA:", dna_path, "| bpm:", bpm)

    print("[6/6] 清理 ...")
    shutil.rmtree(tmp, ignore_errors=True)
    print("PASS: wheel 安装后可在仓库外独立分析（无需 ffmpeg 也能跑核心格式）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
