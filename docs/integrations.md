# 宿主集成：WorkBuddy / DSH / Codex / YuE2 Studio / ACE-Step

三条通道全部指向同一个 MCP Server 实现。集成成功的唯一标准是真实 tools/call 返回，配置文件存在不算集成成功。

## YuE2 Studio（本机实测）

- 安装位置：`<YuE2_INSTALL_DIR>`（用户既有便携版 4.0.0，35GB 数据与模型齐备，含 37 个 LoRA）
- 启动方式：**用户上下文拉起**（一键启动.cmd 或直接开窗；官方 `python tools/yue2.py start` 亦可）。MCP 端口 `127.0.0.1:8791`，开窗即在线，10 分钟空闲自退
- 沙箱约束（2026-11-20 实测四法验证）：agent 沙箱内拉起的 GUI 进程约 60 秒内被回收，**agent 不负责拉起 studio，只负责驱动**。studio 由用户桌面上下文运行时，agent 纯 HTTP 调用全链畅通
- 联调：`PYTHONPATH=src .venv/Scripts/python.exe scripts/yue2_live_test.py`（等端口→握手→song_create→studio_wait→song_export 落盘 `data/artifacts/yue2_live/`→记录 `examples/yue2_live_record.json`）
- 首选工作流：REA-Music 生成方案 → `generate_music(backend="yue2")` 自动驱动；离线时降级 Suno 提示词包

## ACE-Step 1.5（可选后端）

- 位置：`engines/ACE-Step-1.5`（独立 venv，不进 REA 主依赖）
- 模型：HF 镜像/ModelScope 自动回退下载入 `checkpoints/`；8GB 显存档 = 2B turbo + 0.6B LM
- 服务：`.venv/Scripts/acestep-api.exe`（127.0.0.1:8001），路由经 `/openapi.json` 运行时发现，REA 不硬编码
- 定位：能力探测型可选后端；未启动时 capabilities 如实报 offline

## WorkBuddy（已验证注册路径，待用户点 Trust 完成最后一步）

WorkBuddy 全局 MCP 配置：`%USERPROFILE%/.workbuddy/mcp.json`。注册脚本已备好：

```bash
.venv/Scripts/python.exe scripts/register_workbuddy_mcp.py   # 自动备份原文件并合并新条目
```

写入的条目（与其它 Connector 条目的形态一致）：

```json
{
  "mcpServers": {
    "rea-music": {
      "command": "<REPO_ROOT>/.venv/Scripts/python.exe",
      "args": ["-B", "-m", "rea_music.mcp.server"],
      "cwd": "<REPO_ROOT>",
      "env": {
        "PYTHONPATH": "<REPO_ROOT>/src",
        "REA_MUSIC_HOME": "<REPO_ROOT>/data",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

注册后：打开连接器管理页 → 自定义条目 → 对 rea-music 点 **Trust** → 在会话里应能发现 14 个工具。验证命令（会话内）：调用 `rea_music_capabilities`。

注意：server 以 stdio 方式由 WorkBuddy 拉起，无需常驻进程。

## DSH (DeepSeek Harness)

参照 DSH 社区常用的 dsh-mcp.patch.yml 模式，REA-Music 提供同结构 patch 模板：

```yaml
# 保存为 dsh-mcp.patch.yml（随 DSH 的 patch 机制加载）
mcpServers:
  rea-music:
    command: "<REPO_ROOT>/.venv/Scripts/python.exe"
    args: ["-B", "-m", "rea_music.mcp.server"]
    cwd: "<REPO_ROOT>"
    env:
      PYTHONPATH: "<REPO_ROOT>/src"
      REA_MUSIC_HOME: "<REPO_ROOT>/data"
      PYTHONUTF8: "1"
```

状态：模板就绪，DSH 侧实测列入 R6-2（需要 DSH 运行环境配合）。

## Codex

Codex 读取 `~/.codex/config.toml` 的 `[mcp_servers]` 段：

```toml
[mcp_servers.rea-music]
command = "<REPO_ROOT>/.venv/Scripts/python.exe"
args = ["-B", "-m", "rea_music.mcp.server"]
cwd = "<REPO_ROOT>"
env = { "PYTHONPATH" = "<REPO_ROOT>/src", REA_MUSIC_HOME = "<REPO_ROOT>/data", PYTHONUTF8 = "1" }
```

状态：模板就绪，Codex 侧实测列入 R6-3。

## 健康检查

```bash
PYTHONPATH=src .venv/Scripts/python.exe scripts/health_check.py
```

检查项：venv 依赖导入、ffmpeg/ffprobe 可达、schemas 存在、MCP server 模块可加载、（可选）8791 YuE2 探测。

### YuE2 与 ACE-Step 显存互斥（8GB 卡实测）

两张引擎同时占卡会互相拖垮：ACE-Step 常驻约 5GB，YuE2 引擎初始化需要完整显存余量。实测 19:18–19:21 期间 ACE 在线时 YuE2 启动即静默退出（引擎初始化失败）。REA 侧建议：联调用 YuE2 时先停 ACE（`.venv/Scripts/acestep-api.exe` 关闭），反之亦然。GPU 全空（0/7956 MiB）时 YuE2 v4.0.0 生成 60s 曲约 57 秒。

### YuE2 闪退修复（D21，2026-11-20）

根因：`tools/yue2.py` 每次启动前 `taskkill /IM YuE2-Studio.exe`，重复双击一键启动=后一次杀前一次（无崩溃日志的静默退出）；并发 wrapper 会互相杀实例。
修复：`一键启动.cmd` 顶端加端口守卫（8791 已监听即退出，零杀戮）；新增 `启动YuE2-常驻窗口.cmd`（可见窗口 + 24h 空闲豁免）。日常推荐后者。
启动后验证：`curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8791/mcp` 返回 405 即在线。

## 网络安全边界（输入面，D22）

REA-Music 会按用户显式提供的 URL 发起本机出站请求。边界如下（实现见 `core/source.py`）：

- **谁发起**：只由本机进程（CLI/MCP/Python API）拉取，用户必须显式传 URL；不做主动爬取、不跟随页面里的二次链接
- **协议**：仅 `http` / `https`。`file://`、`ftp://`、`rtmp://` 等一律 `validation_error`，避免通过 scheme 读本机任意文件
- **大小与超时**：单次下载硬上限 512MB（先看 `Content-Length`，流式累计再兜一层）；请求超时 120s、流式读取放宽到 300s（1MB 分块，内存恒定）；yt-dlp 抽取总超时 900s。超限即 `invalid_audio` 并给出裁剪建议
- **落盘原子性**：先写 `<name>.part`，成功后 `os.replace` 原子改名；任何失败都清理半截文件，避免下次被当缓存复用
- **内容判定**：`Content-Type` 为 audio/video 视为媒体；`application/octet-stream` 时按扩展名或文件头魔数（ID3/fLaC/OggS/RIFF/ftyp）判定；`text/html`、`.m3u8`、`.mpd` 自动改走 yt-dlp
- **SSRF 说明（2026-11-21 收紧）**：本能力是"用户主动指定目标的本机 HTTP 拉取"，但入口已改为 **deny-by-default**——`core/net_guard.py` 在 `resolve_source()` 处校验目标地址：云元数据与链路本地/保留段**永远拒绝**，私网与回环默认拒绝。内网媒体服务器（NAS、DLNA、本机 HTTP 服务）仍是正当需求，但需要显式设置 `input.allow_private_networks: true` 才放行。动机：MCP 场景下 URL 由 Agent 提供、可能被提示注入，"用户主动"这个前提未必成立，所以默认值取安全一侧。调用侧仍应确认链接来源可信——本服务只监听本机，不对外提供下载代理
- **落盘位置**：`data/downloads/<sha256(url)[:12]>/`，同一链接重复调用直接复用（含 `_source.json` 侧车记录 kind/tool/sha256），不重复下载
- **不外发**：下载内容不参与任何远程调用。唯一例外是既有的 songbrain 交叉验证（deep 档、需用户显式授权上传，默认关闭），与本节输入面无关
- **版权**：内容版权归原权利人；REA-Music 只用其做本机技术特征分析，不生成、不传播副本
