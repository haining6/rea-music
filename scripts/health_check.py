"""集成健康检查。逐项实测，全部通过才算 READY。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))  # rea_music 包根在 sys.path

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    import traceback
    try:
        detail = fn()
        CHECKS.append((name, True, detail or "ok"))
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc().strip().splitlines()
        CHECKS.append((name, False, f"{exc} | last: {tb[-1] if len(tb)>1 else ''}"))


check("python_version", lambda: f"{sys.version_info.major}.{sys.version_info.minor}")
check("librosa", lambda: __import__("librosa").__version__)
check("soundfile", lambda: __import__("soundfile").__version__)
check("pyloudnorm", lambda: __import__("importlib.metadata", fromlist=["version"]).version("pyloudnorm"))
check("mcp_sdk", lambda: __import__("mcp").__name__)
check("scipy", lambda: __import__("scipy").__version__)
check("sklearn", lambda: __import__("sklearn").__version__)


def _ffmpeg():
    out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    assert out.returncode == 0, "ffmpeg 不可用"
    return out.stdout.split()[2]


check("ffmpeg", _ffmpeg)


def _schema():
    p = REPO / "schemas" / "music_dna_v1.schema.json"
    json.loads(p.read_text(encoding="utf-8"))
    return "music_dna_v1"


check("schema", _schema)


def _mcp_server():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "rea_mcp_server", REPO / "src" / "rea_music" / "mcp" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rea_mcp_server"] = mod
    spec.loader.exec_module(mod)
    tools = [t for t in dir(mod) if t in (
        "capabilities", "inspect_audio", "analyze_track")]
    assert tools, "server 工具缺失"
    return f"{len(tools)} tools importable"


check("mcp_server_tools", _mcp_server)


def _yue2_probe():
    from rea_music.generation.backends import yue2_probe
    return f"available={yue2_probe()['available']}"


check("yue2_studio_probe", _yue2_probe)

print(f"{'检查项':<22} {'结果':<6} 详情")
print("-" * 60)
passed = True
for name, ok, detail in CHECKS:
    flag = "PASS" if ok else "FAIL"
    passed = passed and ok
    print(f"{name:<22} {flag:<6} {detail}")
print("-" * 60)
print("READY" if passed else "NOT READY —— 修复 FAIL 项后重试")
sys.exit(0 if passed else 1)
