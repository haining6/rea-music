"""把 rea-music 条目合并进 WorkBuddy 全局 MCP 配置。

- 先备份原 mcp.json 到 mcp.json.bak-<时间戳>
- 不覆盖任何既有条目，只新增/更新 rea-music
- 幂等：重复运行安全
- 配置路径优先级：环境变量 WORKBUDDY_MCP_CONFIG > ~/.workbuddy/mcp.json

公开仓库红线：本脚本随 MIT 仓库一起发布，**禁止**写入任何个人绝对路径——
包括但不限于 ``C:/Users/<用户名>/...``、``D:/<个人目录>/...``、``/home/<用户名>/...``。
所有路径一律通过 ``Path.home()`` / ``__file__`` 运行时推导，或由环境变量覆盖。
提交前请自查：``git diff | grep -nE 'C:/Users|D:/[A-Za-z]|/home/[a-z]'`` 应无输出。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MCP_JSON = Path(os.environ.get("WORKBUDDY_MCP_CONFIG")
                or (Path.home() / ".workbuddy" / "mcp.json"))

_ENTRY_ENV: dict[str, str] = {
    "REA_MUSIC_HOME": str(REPO / "data"),
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}
# 源码树（editable）才需要 PYTHONPATH；非源码安装时让它指向不存在的目录会干扰 import
if (REPO / "src").is_dir():
    _ENTRY_ENV["PYTHONPATH"] = str(REPO / "src")

ENTRY = {
    # 用当前解释器：脚本本来就跑在目标 venv 里，硬编码 .venv/Scripts/python.exe
    # 既假设了 Windows 布局，也假设了调用方式（python -m / 直接执行）
    "command": sys.executable,
    "args": ["-B", "-m", "rea_music.mcp.server"],
    "cwd": str(REPO),
    "env": _ENTRY_ENV,
}


def main() -> None:
    if not MCP_JSON.exists():
        raise SystemExit(f"未找到 WorkBuddy 配置: {MCP_JSON}")
    backup = MCP_JSON.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(MCP_JSON, backup)
    config = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    servers = config.setdefault("mcpServers", {})
    servers["rea-music"] = ENTRY
    MCP_JSON.write_text(json.dumps(config, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"已注册 rea-music 条目。原配置备份: {backup}")
    print(f"解释器: {sys.executable}")
    print("下一步（需手动）：WorkBuddy 连接器管理页 → 自定义条目 → 对 rea-music 点 Trust。")


if __name__ == "__main__":
    main()
