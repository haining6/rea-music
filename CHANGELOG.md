# Changelog

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

一轮安全 / 合规 / 配置 / 文档审计后的修复，不含功能性新增。

### Fixed

- **MCP SDK 版本下限错配（会导致装完即不可用）**：`pyproject.toml` 原写 `mcp>=1.2`，
  但 `mcp/server.py` 直接 `from mcp.server import MCPServer`——该符号在 SDK 2.0 才由
  `FastMCP` 更名而来。装有 1.x 的环境 `pip install rea-music` 不会升级，两个
  console_scripts 全部 ImportError。现改为 `mcp>=2.3,<3`（下界对齐已验证的符号，
  上界防 3.x 再次更名），并加 `tests/test_pyproject_contract.py` 守护。
- **HTTP 服务可被误绑到公网**：`rea-music serve` 的 `--host` 先前无任何校验，
  而无鉴权的 HTTP 通道等价于全部本地 CLI 能力。现在非回环地址必须额外显式传
  `--allow-external` 才放行，`ARCHITECTURE.md` 原则 7「HTTP 只听回环」由代码保证。
- **配置解析失败静默**：`core/config.py` 的 `except ...: pass` 改为
  `warnings.warn` + 记录 `config_error`，并由 `capabilities` 输出 `config_source` /
  `config_error`。配置模板从 `config.example.yaml` 改为 `config.example.json`
  （加载器只读 JSON），键集合与 `_DEFAULTS` 对齐，补齐
  `remote.songbrain_api_key` / `remote.songbrain_upload_authorized` / `input.allow_private_networks`。
- **依赖声明与实际不符**：删除零引用的 `pydantic`（核心依赖）与 `rich`（dev extra）。
- **注册脚本硬编码 Windows venv 路径**：`scripts/register_workbuddy_mcp.py` 改用
  `sys.executable`，macOS/Linux 可直接运行；仅源码树才注入 `PYTHONPATH`。
- **UA 与文档里的占位仓库地址**：`CHANGELOG` 的版本链接从 `your-account` 改为真实仓库。

### Added

- `THIRD_PARTY_NOTICES.md`：直接依赖 + 关键传递依赖的逐包 SPDX 声明，含 `soxr`
  （LGPL-2.1-or-later）专条与分发边界说明；同时进 `license-files` 与 sdist。
- `MANIFEST.in`：sdist 带上 LICENSE / THIRD_PARTY_NOTICES / SECURITY / CHANGELOG /
  `schemas/*.json` / `tests/`，下游可离线自证。
- `src/rea_music/py.typed`（PEP 561）：与 `Typing :: Typed` classifier 对齐。
- `src/rea_music/core/net_guard.py`：链接目标地址护栏（deny-by-default，
  云元数据与链路本地永远拒绝，私网需 `input.allow_private_networks` 显式开启）。
  已接入 `source.py::resolve_source`，异常契约显式区分三种情形：
  禁止段 → `validation_error`（`details.blocked` 写明命中类别）、主机名解析失败 →
  `host_unresolvable`、scheme/主机名/端口非法 → `validation_error`。
  仍存的两条已知局限（DNS rebinding、重定向未逐跳校验）见 `SECURITY.md`。

### Changed

- **URL 输入的网络安全默认值收紧（用户可感知）**：`resolve_source` 现在默认拒绝
  私网与回环地址（`10/8`、`172.16/12`、`192.168/16`、`127/8` 等），指向本机或局域网
  HTTP 文件服务（本地媒体服务器、NAS、DLNA）需显式设置
  `input.allow_private_networks: true`；云元数据（`169.254.169.254`）与 CGNAT 段
  无论开关如何一律拒绝。动机：MCP 场景下链接可能被提示注入，默认值取安全一侧。
  被拒时返回 `validation_error`，`details.blocked` 写明命中网段类别。
- **畸形链接的错误码变了（Agent 可感知）**：缺主机名（如 `http://`）、端口越界、
  scheme 非 http/https 现在在入口即返回 `validation_error`；此前这类输入会被护栏
  静默放行到下载层，由 urllib 抛出难以归因的底层异常。主机名解析失败仍单独返回
  `host_unresolvable`，不会被误报成"地址非法"。
- `.gitignore` 补齐本地配置与密钥（`config.json` / `config.yaml` / `.env*` / `*.local`）
  及开发期缓存（`.mypy_cache/`、`.ruff_cache/`、`.coverage`、`htmlcov/`、`.DS_Store`）。
- 文档对齐实测：`ARCHITECTURE.md` 存储目录改为 `data/{cache,jobs,artifacts,downloads,stems,experiments,tmp}`；
  `ROADMAP.md` 去掉指向内部文档的失效引用并按实际状态勾选；`AGENTS.md` 移除机器相关镜像配置；
  `benchmarks/PERFORMANCE.md` 去硬件型号与失效 TODO；`README.md` 的 `--http` 更正为
  `rea-music serve --transport http`。

## [0.1.0] - 2026-11-20

首个可运行版本：本地/链接输入 → Music DNA → 创作方案 → 渲染请求 → 闭环评测全链打通，
离线测试套件在 Windows 实机全绿（发布后测试规模仍在增长，当前数字见
`pytest --collect-only`；本条目不再维护一个会过期的固定值）。

### Added

- **无 FFmpeg 环境下的完整分析链路**：核心格式（wav/flac/ogg/mp3/opus/aiff）由内置
  soundfile（libsndfile）直读，纯 `pip install` 后不安装任何外部可执行文件即可完成
  inspect → analyze → Music DNA 产出；核心格式的探测不产生任何子进程。
- **Music DNA v1**：段落级 DNA（每段频谱摘要、局部和弦、RMS），每个可推断字段带
  `value / confidence / method / provenance`，测不到时为 `null + missing_reason`；
  配套 `schemas/music_dna_v1.schema.json` 结构校验（包内同内容副本随 wheel/sdist 分发）。
- **三档分析管线**：`fast`（节奏/调性/响度/频谱）、`standard`（+和弦/结构/人声）、
  `deep`（+分轨/旋律转写/交叉验证）；同一文件同档位命中内容哈希缓存。
- **输入面扩展（D22）**：本地任意常见格式（白名单 24 项，表外扩展名由 ffprobe 实测
  放行）、http(s) 直链媒体（标准库 urllib 流式下载）、视频/网页链接（可选 yt-dlp）。
  仅接受 http/https；512MB 与超时上限；`.part` + 原子改名，半截文件不复用。
- **内容寻址缓存 + 持久化 Job**：键 = 内容哈希 + 档位 + 参数摘要；Job 以
  `data/jobs/*.json` 落盘，记录状态/阶段/进度/错误/工件，MCP 断线不丢任务。
- **MCP Server**：14 个工具（capabilities / inspect / analyze / get_analysis /
  get_job_status / separate_stems / extract_music_dna / compare / blend / plan /
  render / generate / evaluate / revise），stdio 默认，streamable HTTP 仅回环。
- **CLI**：`capabilities / inspect / analyze / compare / plan / render / evaluate /
  jobs / serve`；本版新增
  - `analyze --batch <目录>`：递归批量分析，单文件失败不中断，输出每文件
    `{file, ok, dna_path|error, bpm, key, elapsed_sec}` 汇总表，`--limit N` 可限量；
  - `cache stats` / `cache clean`：逐目录文件数/占用统计；按白名单 key 清理，
    默认 dry-run，`--yes` 才删除，拒绝越界路径与代码目录。
- **创作与评测**：CompositionPlan（继承风格 / 融合 / 定向改造 / 歌词驱动）、Suno 提示词包、
  YuE2 与 ACE-Step 渲染请求、生成后逐维偏差评测与修订循环（默认最多 3 轮）。
- **可选引擎适配器与能力探测**：demucs 分轨、basic-pitch 旋律转写（含 librosa pyin 后备）、
  chordotomy 和弦、SongBrain 远程交叉验证、yt-dlp 链接抽取；缺失时
  `capability_unavailable` + 安装指引，不伪造能力。
- **6 个 Agent Skills**（agentskills 规范）与 WorkBuddy / DSH / Codex 集成脚本。
- **CI**：GitHub Actions，`ubuntu-latest` + `windows-latest`，Python 3.12，
  安装 FFmpeg、生成合成夹具后跑 `pytest tests/ -q --ignore=tests/test_engines_r2.py`
  （该文件依赖可选引擎，CI 环境不装）；独立 job 做
  `pip install -e .` + `rea-music capabilities` 冒烟。macOS 暂未纳入矩阵。
- **发布配套**：`SECURITY.md`（威胁模型、回环 MCP、SSRF 边界、上传授权、无遥测）、
  `docs/QUICKSTART.md`（5 分钟上手）。

### Changed

- **inspect 探测顺序改为 soundfile 优先、ffprobe 兜底**：核心格式用内置解码库直读，
  其余容器才试 ffprobe；无 FFmpeg 时 m4a/aac/mp4/webm 如实报
  `capability_unavailable` + 安装指引（不再误报 `invalid_audio`）。inspect 输出新增
  `probed_by`（`soundfile` / `ffprobe`）标明结论来源；`capabilities().input.ffmpeg`
  报告 FFmpeg 可用状态与 `needed_for`。
- 非核心扩展名解码前先按内容实测 soundfile 能否直读：无扩展名 / 冷门后缀但实为
  wav·flac 的文件在无 FFmpeg 环境也能分析，读不了才回退 ffmpeg（装了 FFmpeg 时行为不劣化）。
- **独立运行红线（D17）**：核心依赖收敛到 librosa 栈 + click/mcp；
  torch+demucs、chordotomy、songbrain、yt-dlp 全部转为 optional extras，
  干净安装默认不带，功能按能力探测降级。
- 解码主路径改为 soundfile 直读，ffmpeg 仅作容器回退（D9）；分析采样率统一 44.1kHz（D10）。
- 节拍判定加入包络自相关 + 倍频仲裁（D12），`tempo_candidates` 全量保留供判读；
  下拍与段落功能标签标注为低置信度启发式（D6，conf ≤ 0.55）。
- CLI `analyze` 的位置参数改为可选（与 `--batch` 共存），单文件用法与输出结构不变。

### Fixed

- **非 editable 安装不可用（wheel/sdist 缺 DNA schema）**：schema 移入包内
  `src/rea_music/schemas/` 并由 `importlib.resources` 读取（`package-data` 随包分发），
  仓库根 `schemas/` 保留同内容公开副本；`pip install <wheel>` 后任意目录都能 analyze。
  默认工作区在非源码安装时改为用户数据目录（Windows `%LOCALAPPDATA%\REA-Music`，
  其他 `~/.local/share/rea-music`），不再落到 site-packages 旁边。
- **流式写入的 flac 被误报「时长 209146758205324s 超过上限」**：libsndfile 对头部无
  总样本数的流报 `SF_COUNT_MAX`。现在先按内容测长（ffmpeg 管道解码兜底），测不出时
  如实标 `duration_unknown: true` 并跳过时长上限判定；解码交给 ffmpeg，缺 FFmpeg 时
  报 `capability_unavailable`（不再出现 `array is too big`）。
- 干净 venv（只装 pytest，无 torch/demucs）跑测试时，demucs 两个用例 skip 而非 fail。
- `capabilities.input.ffmpeg` 的 hint 按 ffprobe / ffmpeg 组合分态给准（只有 ffmpeg 时
  不再谎称"未安装"，并新增 `ffprobe_ready`）；`analyze` 失败时 job error 保留
  `code / hint / details`，Agent 可区分"长度未知"与"m4a 需要 FFmpeg"。
- 测试夹具的音乐性错误（调外音、混音失衡）修正后，BPM/调性 ground truth 全部通过（D11）。
- YuE2 Studio 闪退根因修复：启动器 `taskkill` 竞态以幂等守卫消除（D20 / D21）。
- 下载半截文件不再被当作缓存复用（`.part` + 原子改名 + 失败清理）。
- 远端错误映射归位：404→`file_not_found`、非媒体/需登录→`invalid_audio`、
  网络不可达/5xx→`backend_unavailable`、非法 scheme→`validation_error`。

[0.1.0]: https://github.com/haining6/rea-music/releases/tag/v0.1.0
