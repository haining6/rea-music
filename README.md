# REA-Music

给音乐做"参数体检"，并据此生成新曲。

你丢进去一首歌（本地文件、直链、或视频页链接），它会告诉你：多快、什么调、哪几段、用了什么和弦、多响、有没有人声——每项都带可信度，测不到就明说。这些参数打包成 Music DNA（JSON），可以直接拿去对比、混搭，或作为生成新歌的依据。

> 第一次用？看 [新手入门指南](docs/GETTING-STARTED.md)，照做即可跑通。

## 能做什么

| 功能 | 一句话 | 输出 |
|---|---|---|
| 分析 | 把音频拆成可读参数 | Music DNA（段落级 + 置信度 + 溯源） |
| 对比 | 两首歌逐项比差距 | 差异表（速度/调性/响度/结构） |
| 生成 | 按参考曲风格做新歌 | 音频文件 + 生成配置 |
| 评测 | 检成品与目标差多少 | 逐维偏差 + 下一版修改建议 |

示例（standard 档约 30 秒）：

```
BPM 92.3 (置信度 0.79) · C major · 6 段（前奏/主歌/副歌/桥段/副歌/尾奏）
和弦 F–C–Am–G · 响度 -16.5 LUFS · 人声：likely
```

## 5 分钟上手

需要 Python 3.12。**wav / flac / ogg / mp3 / opus / aiff 开箱即用，不需要安装 FFmpeg 或任何外部程序**；只有 m4a / aac / mp4 / webm 与视频页链接需要 FFmpeg（可选，未装时如实报 `capability_unavailable` 并给安装方法）。

> 平台支持：Windows 与 Linux 有 CI 实测覆盖；**macOS 尚未纳入 CI**（代码按 POSIX 编写，
> 预期可用，但没在 CI 上证明过），请以上述两个平台为准。

```bash
git clone https://github.com/haining6/rea-music.git
cd rea-music
python -m venv .venv && .venv\Scripts\activate     # Windows；macOS/Linux 用 source .venv/bin/activate
pip install -e .
rea-music capabilities                             # 看到 "ok": true 即装好
rea-music analyze "D:\音乐\某首歌.mp3" --tier standard
```

三档速度：`fast` 10 秒内（速度/调性/响度）、`standard` 30 秒左右（加和弦/段落/人声，日常用）、`deep` 约 2 分钟（加真实分轨与旋律）。

批量与链接：

```bash
rea-music analyze --batch "D:\音乐" --limit 20              # 整目录，坏文件不中断
rea-music analyze "https://example.com/song.m4a"            # 直链
pip install "rea-music[fetch]"                              # 视频页需要（yt-dlp）
rea-music analyze "https://www.example.com/watch?v=xxx"     # 视频页抽音频
```

## 在 AI 助手里用

注册为工具后直接说话即可（WorkBuddy 一条命令注册）：

```bash
python scripts/register_workbuddy_mcp.py     # Windows / macOS / Linux 通用，脚本自动填当前解释器
```

- 「分析 D:\音乐\某首歌.mp3」
- 「这两首对比一下」
- 「参考这首的风格，做一首 90 秒的新歌」

## 结果怎么读

1. 每个字段都是 `value` + `confidence` + 方法溯源；**测不到 = null + 原因**，不猜。
2. confidence 低于 0.5 按"约"理解；`octave_ambiguity: true` 说明速度存在整倍歧义。
3. 引擎没装会返回 `capability_unavailable` 和安装指引，绝不伪造数据。

## 输入类型

本地任意常见格式、http(s) 直链、视频页三种输入等价：

| 输入 | 例子 | 解析方式 |
|---|---|---|
| 本地文件 | `song.mp3` `track.wv` `album.mka` | 核心格式由内置 soundfile 直读（零外部依赖）；其他扩展名先试 ffprobe，缺 ffprobe 时按内容实测判定 |
| 直链媒体 | `https://cdn.example.com/song.flac` | 流式下载到 `data/downloads/<url-hash>/` |
| 视频页 | `https://www.example.com/watch?v=xxx` | yt-dlp 抽音频后解码（需 `rea-music[fetch]`） |

- **零外部依赖**：wav/flac/ogg/mp3/opus/aiff 由内置 soundfile（libsndfile）直读，`pip install` 完就能分析，不装 FFmpeg 也行
- 其余扩展名（m4a/aac/mp4/webm/wma/wv/tta/amr/au/snd/ac3/dts/mka/m4b/3gp/ra/mp2…，共 30 余种）**不直接拒绝**：装了 FFmpeg 就交给 ffprobe 实测，能解码即放行；没装 FFmpeg 则返回 `capability_unavailable` 并告诉你怎么装
- 上限：单次 ≤512MB，请求 120s / 流式读取 300s；仅 http/https；先写 `.part` 再原子改名
- 隐私：内容只落本机 `data/downloads/`（gitignored），不二次外发；仅在你主动给链接时联网
- **版权**：下载内容仅供本机自用分析，请自行确认使用权；本项目不上传、不传播、不分发

## 接入通道（同一 core）

| 通道 | 入口 |
|---|---|
| MCP Server | `python -m rea_music.mcp.server`（stdio 默认）；`rea-music serve --transport http`（默认只听 127.0.0.1） |
| CLI | `python -m rea_music.cli.main {capabilities,cache,inspect,analyze,compare,plan,render,evaluate,jobs,serve}` |
| Python API | `pip install -e .` 后 `import rea_music` |
| Agent Skills | `skills/*/SKILL.md`（按 agentskills 开放规范） |

## MCP 工具（14 个）

`rea_music_capabilities / inspect_audio / analyze_track / get_analysis / get_job_status / separate_stems / extract_music_dna / compare_tracks / blend_music_dna / create_composition_plan / render_generation_request / generate_music / evaluate_generation / revise_composition_plan`

工具名用下划线形式（MCP 规范建议），与总指令中 `rea_music.analyze_track` 等点号名一一对应。

## 工作链

```
参考曲 → inspect → analyze(job) → Music DNA v1（段落级+置信度+溯源）
→ compare/blend（多曲） → CompositionPlan → Suno 提示词包 / YuE2 请求
→ 生成 → evaluate（逐维偏差+原创性风险）→ revise → 再生成
```

## 诚实性契约

- 每个可推断字段带 value/confidence/method/provenance；测不到 = null + missing_reason。
- 引擎未装 = `capability_unavailable` + 安装指引，绝不伪造数据。
- 启发式降级（下拍、段落功能标签）置信度封顶 0.55，Skill 教 Agent 如实转述。
- GPU 先实测空闲显存再决定，不信任名义容量。

## 文档索引

- [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) 新手入门（安装 / 第一条命令 / 结果怎么读 / 常见问题 / 名词小抄）
- docs/QUICKSTART.md 5 分钟上手（命令速查）
- AGENTS.md 协作最小公约
- ARCHITECTURE.md 分层架构与硬性原则
- OPEN_SOURCE_REUSE.md 开源尽调矩阵（17 项，含许可证与取舍理由）
- THIRD_PARTY_NOTICES.md 第三方组件许可声明（含 soxr LGPL 专条）
- DECISIONS.md 设计决策记录（含能力降级的裁定依据）
- ROADMAP.md 阶段路线
- SECURITY.md 安全模型与边界
- CHANGELOG.md 版本变更记录
- docs/integrations.md WorkBuddy / DSH / Codex / YuE2 / ACE-Step 集成
- schemas/music_dna_v1.schema.json 数据合同（公开契约；包内 `src/rea_music/schemas/` 同内容副本随 wheel 分发）
- skills/ 六个 Agent Skills（SKILL.md）

## 许可证

MIT License，见 [LICENSE](LICENSE)。第三方依赖与可选引擎的许可声明见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)（含 soxr 的 LGPL-2.1 专条）；
开源选型与取舍理由见 [OPEN_SOURCE_REUSE.md](OPEN_SOURCE_REUSE.md)。YuE2 Studio、ACE-Step
等外部服务不在本发行物内，其许可与使用条款由使用者自行确认。
