# 5 分钟上手

目标：从零到一个真实的 Music DNA 文件，再生成一份创作方案。全程离线（除非你给链接）。

## 1. 安装（约 2 分钟）

需要 **Python 3.12**。wav / flac / ogg / mp3 / opus / aiff 这 6 种核心格式零外部依赖，装完即用；**FFmpeg 是可选依赖**，只有 m4a / aac / mp4 / webm 与视频页链接才需要它。

```bash
git clone <repo> && cd REA-Music

# 建虚拟环境（任选其一）
python -m venv .venv && . .venv/bin/activate          # Linux / macOS
python -m venv .venv && .venv\Scripts\activate         # Windows

# 安装（核心依赖：librosa 栈 + click/mcp，见 THIRD_PARTY_NOTICES.md）
pip install -e .
# 想一次装齐开发/测试依赖：
pip install -e ".[dev]"
```

可选能力（不装也能跑，只是对应功能如实降级为 `capability_unavailable`）：

```bash
pip install -e ".[fetch]"    # yt-dlp：视频页/网页链接抽音频（D22）
pip install -e ".[stems]"    # torch + demucs：deep 档分轨（体积大，按需）
pip install -e ".[chords]"   # chordotomy：更强和弦识别
```

验证安装（应输出 `"ok": true` 与引擎清单）：

```bash
rea-music --version
rea-music capabilities
```

运行时数据默认写到仓库下的 `data/`（已被 `.gitignore` 排除）。想换个位置：

```bash
export REA_MUSIC_HOME=/path/to/my-data        # Windows: set REA_MUSIC_HOME=D:\my-data
```

## 2. 三条命令（约 2 分钟）

```bash
# ① 先确认输入能被识别（本地文件 / http(s) 直链 / 视频页链接都可以）
rea-music inspect song.mp3

# ② 分析：fast 最快，standard 出和弦与段落（推荐），deep 加分轨与旋律
rea-music analyze song.mp3 --tier standard
#    → 打印 compact DNA，并给出 dna_path（完整 DNA 的 JSON 文件路径）
#    → 同一文件重复分析直接命中缓存；要重算加 --force

# ③ 用 DNA 生成原创创作方案（不抄旋律、不抄歌词，只继承风格画像）
rea-music plan <上一步的 dna_path> \
  --objective inherit_style_new_song \
  --request "80 年代 City Pop，女声，副歌要更亮"
```

批量分析一个目录（单文件失败不中断，输出汇总表）：

```bash
rea-music analyze --batch ./albums/citypop --tier fast --limit 20
```

想清理磁盘（默认只预览，`--yes` 才真删）：

```bash
rea-music cache stats
rea-music cache clean --what downloads --older-than-days 30          # dry-run
rea-music cache clean --what downloads --older-than-days 30 --yes    # 真删
```

## 3. 在 WorkBuddy 里怎么用（约 1 分钟）

REA-Music 对宿主暴露的是 **MCP Server**，宿主 Agent 负责对话与决策，本工具负责能力与执行。

```bash
# 把 MCP 条目合并进 WorkBuddy 全局配置（自动备份原文件）
python scripts/register_workbuddy_mcp.py
```

写入的是 stdio 启动方式（宿主自己拉起进程，不占端口）：

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
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

然后**重启 WorkBuddy 并在 MCP 面板点 Trust 授权**（不点授权则该条目不会加载）。
在对话里直接说"分析这首歌并给我一份创作方案"，Agent 会调用
`inspect_audio → analyze_track → create_composition_plan`。

> 上面的路径是 Windows 写法；macOS/Linux 把 `Scripts/python.exe` 换成 `bin/python`。
> 脚本会自动填入仓库真实绝对路径，示例仅用于说明结构。

其他宿主（DSH / Codex）配置方式见 `docs/integrations.md`。需要 HTTP 模式时：
`rea-music serve --transport http`（默认只听 `127.0.0.1:8912`，无鉴权）。
把 `--host` 改成非回环地址会被直接拒绝，确需暴露必须再加 `--allow-external`。

## 4. 常见报错与处理

| 报错 | 原因 | 处理 |
|---|---|---|
| `capability_unavailable`（`requires: ffmpeg`） | 传了 m4a/aac/mp4/webm，本机没装 FFmpeg | 装 FFmpeg 后重开终端；或先转成 wav/flac/ogg/mp3/opus/aiff（核心格式零依赖） |
| `file_not_found` | 路径拼错、文件被移动 | 用绝对路径重试；远程链接检查是否 404 |
| `invalid_audio`（链接场景） | 直链不是媒体、需登录/DRM | 换公开直链；或先下载到本机再传本地路径 |
| `capability_unavailable`（`yt_dlp`） | 给了视频页链接但没装 yt-dlp | `pip install -e ".[fetch]"` |
| `capability_unavailable`（分轨/旋律） | 没装 demucs / basic-pitch | deep 档会降级并写 `missing_reason`；需要就 `pip install -e ".[stems]"` |
| `backend_unavailable` | 网络不可达或远端 5xx | 稍后重试；或改本地文件 |
| `时长 xxx 超过上限` | 超过 `analysis.max_duration_sec`（默认 900s） | 先裁剪，或调高 `data/config.json` 中该值 |
| `validation_error`（scheme） | 传了 `file://` 等非 http/https | 本地文件直接传路径，不要加 `file://` |
| 分析很慢 | deep 档要跑分轨与转写 | 先用 `--tier fast` 试通链路，再升档 |
| 磁盘吃紧 | 解码 wav、分轨、下载都在 `data/` | `rea-music cache stats` 看占用，再 `cache clean` 预览清理 |
| Job 失败但 CLI 只给了 job_id | 需要看详细阶段与错误 | `rea-music jobs --job-id <id>` |

**诚实性提醒**：`confidence ≤ 0.55` 的字段是启发式估计（如段落功能标签、下拍），
转述时请说"估计/推测"；`null + missing_reason` 表示本机缺引擎，不要当成数据缺失去补。

## 下一步

- 架构与分层：`ARCHITECTURE.md`
- 设计取舍与降级理由：`DECISIONS.md`
- 输入面与下载边界：`README.md`「输入类型」+ `SECURITY.md`
- 宿主集成细节：`docs/integrations.md`
