# DECISIONS.md — 关键决策记录

## D1 (2026-11-20) R1 分析路线：自研 librosa 管线而非直接套用 libretta

理由：REA-Music 的核心资产是带置信度与溯源的**段落级** Music DNA，libretta 输出整曲扁平特征，两者数据模型不同；libretta 的增量价值（demucs、basic-pitch）在 py3.13/GPU占用环境下当下不可用，拖入安装只会引入失败面。自研管线遵循 SongScope 已公开验证的标准算法路线（tuning-aware CQT chroma、octave-aware tempo、novelty 结构），但代码独立实现（SongScope 无 LICENSE，法律上不可复制）。

## D2 (2026-11-20) 分轨（demucs）：Adapter 就位，默认不安装

facebookresearch/demucs 已声明停止维护；GPU 默认参数需 ~7GB 而实测空闲仅 28MiB；CPU 版可用但 torch 下载量大。按总指令"禁止默认下载大量未经批准的大模型"，安装动作升级为 QUESTIONS.md 中待用户批准项。`separate_stems` 工具先返回 capability_unavailable + 安装指引。

## D3 (2026-11-20) 生成后端：YuE2 Studio 优先，Suno 走提示词包

用户已装 YuE2 Studio 且 WorkBuddy 已有其 MCP 配置（127.0.0.1:8791）。REA-Music 做薄探测与转发，studio 窗口未开时降级输出 Suno 风格提示词包（结构化提示词 + 段落标签歌词模板），用户在 Suno 完成创作后可把成品导入本管线评测。不伪造 Suno API。

## D4 (2026-11-20) Job 系统：文件持久化 + 线程池，不引入 Prefect

单机单用户场景，Prefect server 的运维成本大于收益。Job 记录 = jobs/*.json（状态/进度/工件/重试/错误），满足断点恢复与幂等（内容哈希缓存键）要求。DAG 需求在 R1 用显式阶段顺序表达，R7 再评估是否升级。

## D5 (2026-11-20) essentia / madmom 不采用

essentia 无 Windows wheel 且 AGPL；madmom Windows 需源码编译且 py3.13 未验证。两者功能由 librosa + 自研启发式覆盖，置信度如实调低。

## D6 (2026-11-20) 下拍与段落功能标签标为低置信度启发式

R1 无 GPU 结构模型可用，downbeat 用拍点+重音模式启发式，intro/chorus 等功能标签用能量+重复度+位置启发式。DNA 中 method 字段如实标注 `heuristic_v1`，confidence ≤0.55，Skill 明确教 Agent 在 Deep 档接入更强引擎后覆盖。

## D7 (2026-11-20) Python 版本：主 venv 3.12，重依赖引擎走隔离 venv

本机全局 3.13/3.14 均无 scikit-learn 兼容版（pip 实测 1.11.3 封顶且要求 <3.13），librosa 硬依赖 sklearn。用 uv 拉 Astral CPython 3.12 建主 venv（实测 sklearn 1.9.1 可装）。basic-pitch（≤3.11）与 ACE-Step（3.11–3.12）未来用独立 venv 隔离安装，通过 subprocess Adapter 接入。

## D8 (2026-11-20) MCP 工具名采用下划线（rea_music_analyze_track）

MCP 规范建议工具名只用字母数字下划线连字符。总指令的 `rea_music.analyze_track` 点号名按 `rea_music_analyze_track` 注册，README 明示对应关系。mcp SDK 2.x 的 FastMCP 已更名 MCPServer，按新 API 实现（同类工程同版本先例验证）。

## D9 (2026-11-20) 解码主路径 soundfile，ffmpeg 降为容器回退

沙箱实测 ffmpeg 子进程被禁止写任何新文件（Permission denied，写 fixtures/data/tmp 均拒），而 python 进程写入正常。解码改 soundfile 直读（wav/flac/ogg/mp3/opus），m4a/aac 走 ffmpeg 回退并在拒绝时给出可操作错误。副产品：无子进程依赖，分析更快。

## D10 (2026-11-20) 分析采样率统一 44100

实测 22050Hz 下 onset 包络对拍级自相关被更长分析窗抹平（0.409 vs 44.1k 的 0.585），倍频仲裁把 120BPM 误判为 60。统一 44100 后单测与管线行为一致，所有 ground truth 测试通过。代价是约 2 倍 CPU 耗时，R7 评估自适应采样率。

## D11 (2026-11-20) 夹具（fixtures）两次修正记录

夹具本身曾经有错：(1) 旋律用了"和弦顶音减 2 半音"，在 C 大调内制造 Bb，音频实际已偏离声明的 ground truth，调性引擎判 F major 是正确行为；(2) 混音失衡，底鼓 0.55 vs 和弦 pad 每音 0.065，鼓组泄漏主导 chroma。修正为调内和弦音旋律 + 真实混音平衡后全链通过。教训入账：ground truth 素材必须先经音乐性复核，不能把引擎的正确行为当成 bug 修。

## D12 (2026-11-20) 节拍层级仲裁：自相关 + "最强峰 0.5 倍阈值内取最快"

librosa DP 基准拍落在小节级周期（60.1）时，用包络滞后自相关在 {1/3,1/2,1,3/2,2,3} 倍候选中仲裁：最强周期 0.5 倍阈值内取最快候选（MIR 文献标准倍频修正），DP 基准并列时优先。tempo_candidates 全量保留在 DNA 中供 Agent 判读歧义。

## D13 (2026-11-20) demucs 走 CPU torch（2.15.0+cpu）

用户批准 GPU 版空间，但 PyPI/腾讯镜像的 Windows torch wheel 为 CPU 构建，CUDA 版需走 download.pytorch.org（本机网络实测慢且不稳定）。取舍：先用镜像快速落位 CPU torch 让分轨链路全通（CPU 分轨约 1.5x 曲长），GPU 版留作后续可选升级（换装 cu12x wheel 即可，Adapter 的显存探测逻辑已就位不受影响）。capabilities 如实报 runtime=cpu。

## D14 (2026-11-20) YuE2 Studio 采用官方便携版自主拉起

本机注册表与 AppData 实测确认 YuE2 Studio 当前未安装（WorkBuddy 里的 yue2-studio MCP 条目为历史残留）。经用户批准 R4-3 联调，下载 v4.1.1 官方 portable zip（350MB，SHA256 锚定 312f26d5…）至 engines/，以 --headless 拉起（其文档声明的无人值守模式，10 分钟空闲自动退出，可用 YUE_STUDIO_IDLE_SHUTDOWN_S 调整）。不改系统注册表、不装驱动，全程可逆可删。

## D15 (2026-11-20) NATTEN 实测定论：all-in-one Windows 不可装

用户批准后限时实测：PyPI 的 natten 无 Windows wheel，导入失败。Windows 路径需 MSVC + CUDA Toolkit 源码编译链，风险与时间成本超出可接受范围。结构分析维持 Ward 聚类 + 启发式标签（置信度如实 ≤0.55），R2-E 按预定的"失败即降级记录"路径验收。后续若 all-in-one-infer 出 Windows 支持或改用 Linux 环境，再走 Evolution 流程评估。

## D16 (2026-11-20) basic-pitch 隔离环境选 py3.10 + setuptools<81 pin

本机已有 Astral CPython 3.10.22（零下载成本）。Windows 上 basic-pitch 默认走 onnxruntime（不引 TF）。坑实测两连：uv venv 无 setuptools → resampy 的 pkg_resources 缺失；而 setuptools ≥81 已移除 pkg_resources，必须 pin <81（装 80.10.2 解决）。该坑已写入 OPEN_SOURCE_REUSE.md 矩阵，防止后续重装复踩。

## D17 (2026-11-20) 独立运行红线：核心零重依赖，引擎全部可选化

用户明确指示"不希望依赖过多，希望 REA 可以独立运行"。落地为硬约束：
- REA 核心依赖仅 librosa 栈（librosa/soundfile/scipy/sklearn/pyloudnorm）+ click/pydantic/mcp；全部功能在无 torch、无 bp-venv、无 chordotomy、无远程引擎时可用（能力探测如实降级）。
- torch+demucs、songbrain、chordotomy 归入 pyproject optional extras（stems/remote/chords-premium），干净安装默认不带。
- ACE-Step 保留为 engines/ 下可选后端（用户 2026-11-20 弹窗确认），永不进入核心依赖。
- YuE2 Studio 为用户桌面应用（<YuE2_INSTALL_DIR>），agent 只经 127.0.0.1:8791 驱动，不拉起不修改其数据。

## D18 (2026-11-20) 沙箱 GUI 进程约束与联调模式

agent 沙箱内任何方式拉起的 GUI 进程（YuE2 Studio 实测：直接调用/后台挂起/任务计划程序/官方启动器四法）约 60 秒内被系统回收，studio 自身日志无错误。结论：studio 一律由用户上下文拉起，agent 走纯 HTTP 驱动。scripts/yue2_live_test.py 固化该联调模式，可复跑可交付。

## D19 (2026-11-20) YuE2 Studio v4.0.0 实测契约修正

18:30 现场联调发现三处契约差异（脚本已按实测修正）：song_create 返回字段是 `id`（非 job_id）；studio_wait 以 `done` 布尔值为完成判据；v4.0.0 **无 song_export**（v4.1.1 才加入），导出须走 `library_song_files` 取磁盘路径再复制。另：studio 10 分钟空闲自退，但运行中的 job 视为在用不会中途退出，「自动退出」发生在无任务且无调用的空窗期。该次联调已实际提交生成任务（job 9a0eaf22e7e85830，yue-server 排队成功）。

## D20 (2026-11-20) "YuE2 闪退"根因裁定与修复

用户报「总是闪退」，三项证据链裁定（Windows 事件日志零崩溃记录，安装完好）：
1. **显存互斥（主因，agent 造成）**：19:18–19:21 期间 agent 遗留的 ACE-Step API 服务占 4.96GB 显存，YuE2 引擎初始化需要完整余量，初始化失败即静默退出。已停止 ACE（GPU 0/7956MiB），并在 docs/integrations.md 写入两引擎显存互斥规则。
2. **感知型闪退**：一键启动.cmd 以 hidden 模式拉起 studio（设计如此，无窗口），叠加 10 分钟空闲自保护，容易被当成闪退。需要看见窗口时直接双击 YuE2-Studio.exe（窗口模式）。
3. **双实例互斥**：19:18:31/39 间隔 8 秒连启两次，单实例互斥使第二个静默退出。启动一次即可。
修复后实测：60.24s 中文抒情曲 57 秒生成成功，闭环分析全部通过。

## D21 (2026-11-20) YuE2 闪退二次根因：启动器 taskkill 竞态，已用幂等守卫修复

二次排查定位到决定性证据：`tools/yue2.py::_stop_existing()` 在**每次**启动前执行 `taskkill /IM YuE2-Studio.exe`，且 `fallback_visible_window` 分支还会再次调用。双击两次 `一键启动.cmd` 时，第二次调用把第一次启动的 studio 静默杀掉（taskkill 不产生任何崩溃事件，与"无 WER 记录 + 无声消失"完全吻合）；两个 wrapper 并发时更会互相杀对方实例，最终零实例存活。观测到的 19:18:31/39、20:19:40/20:20:16 两对时间戳即双次点击竞态。
修复（均实测验证）：
1. `一键启动.cmd` 顶部加幂等守卫：端口 8791 已监听时直接退出，绝不 taskkill。原文件备份 `一键启动.cmd.bak-20261120`。
2. 新启动器 `启动YuE2-常驻窗口.cmd`：可见窗口 + `YUE_STUDIO_IDLE_SHUTDOWN_S=86400`（24 小时不空闲自退）+ 同一守卫。用户日常推荐用它。
3. 两个 .cmd 全 ASCII 消息（cmd 解析器不支持 UTF-8 批次文件里的中文 echo，实测会拆行报错）。
验收：假监听下两启动器均正确退出零杀戮零解析错误；窗口模式拉起后 MCP 8791 在线（probe 405 + 进程存活）。显存占用提示（10.2GB 需求 vs 7.7GB 可用）属该机型的长期状态，studio 走 card_memory_short 慢路径可正常生成，不是崩溃原因。

## D22 (2026-11-20) 输入面扩展：不必须是 MP3，也可以给链接（含视频）

用户原话：「解析的时候，不必须是MP3」+「也可以提供链接，包括视频等等」。裁定：把输入从"本地音频文件"提升为**任意常见媒体来源**，但不新增核心依赖、不降低诚实性。

落地：
1. 新增 `core/source.py::resolve_source`，把三种输入归一化成一份本机文件：`local` / `http_media` / `page`。下游 inspect/解码/分析零改动即可复用。
2. 格式白名单从 12 项扩到 24 项（wv/tta/amr/au/snd/ac3/dts/mka/m4b/3gp/ra/mp2）。**白名单不再是否决点**：表外扩展名交给 ffprobe 实测，能解码即放行，探测失败才 `invalid_audio` 并列出清单——白名单从"守门员"降级为"快路径提示"。
3. 直链走标准库 urllib 流式下载（无新增依赖）；视频页走可选 yt-dlp（`pyproject` extras `fetch`，版本下限 `>=2025.1`（参考工程 requirements 同款；2024.1 已被 pip 安全策略判 unsafe）），`shutil.which` + `find_spec` 双探测，缺失时 `capability_unavailable` + 安装指引。沙箱实测本机 yt-dlp 以 python_module 模式可用（2026.10.26），故 capabilities 如实报 available。
4. 边界硬化：仅 http/https（其他 scheme → `validation_error`，挡住 `file://` 读任意本地文件）；512MB 硬上限（header + 流式双检）；请求超时 120s、流式读取 300s、1MB 分块、先写 `.part` 再 `os.replace` 原子改名（下载约定与参考工程语义一致，实现独立、无跨项目 import）。半截下载文件必须清理，否则下次会被当缓存复用——这是本设计中唯一有状态风险的环节，用 `.part` + `finally unlink` 处理。
5. 错误码沿用既有 8 码，不新增：404→`file_not_found`，非媒体/需登录→`invalid_audio`，bad scheme→`validation_error`，yt-dlp 缺失→`capability_unavailable`，网络不可达/5xx→`backend_unavailable`。
6. 隐私与版权：下载只落 `data/downloads/`（gitignored），不二次外发；SSRF 不设内网黑名单（内网媒体服务器是正当需求），改为在 docs/integrations.md 显式写明风险归属由调用侧承担。

> **本条已部分被推翻（2026-11-21）**：「不做内网黑名单」在 MCP 场景下不成立——URL 由 Agent 提供、可能被提示注入，"用户主动"这个前提不再可靠。现行做法是：云元数据/链路本地/保留段永远拒绝，私网与回环改由 `input.allow_private_networks` 显式放行（deny-by-default），实现见 `src/rea_music/core/net_guard.py`，说明见 `SECURITY.md` §2。决策记录保留原文以存证，不回溯改写。

取舍理由：用户要的是"少一步手工转码/下载"。用"白名单 + ffprobe 兜底"覆盖任意本地格式，用"urllib 优先、yt-dlp 兜底"覆盖链接，两者都能在零新增核心依赖下完成，符合 D17 独立运行红线。代价是链接输入引入网络不确定性——用硬上限 + 结构化错误如实暴露，而不是假装成功。

## D23 (2026-11-21) BPM 仲裁 = 谐波族聚合 × 对数正态先验；人声存在度改用分轨真值

**证据**：真实生成曲（yue2 01a1f8da，真值 92BPM）曾被读成 184.6（候选：184.6 ac=0.247 vs 92.3 ac=0.224，仅差 10%）。实测纯先验加权不足以压住 2 倍自相关差距（rock 夹具 149.8 的 raw ac 只有 74.9 的 51%，174 合成曲边际仅 4.5%），故仲裁公式定为 `score = support(b) × prior(b)`，support 为 sub-harmonic summation（ac(b)+ac(b/2)/2+ac(b/3)/3，只聚合候选集内谐波），prior 为对数正态（中心 110BPM、σ=0.45）。歧义 <8% 时 confidence 封顶 0.5 且 `octave_ambiguity` 外化（已透传入 DNA: rhythm.bpm.octave_ambiguity，schema 可选布尔）。实测：yue2 曲 184.6→92.3 conf 0.79；三夹具不变；174 快歌不被压。
**人声**：整混音启发式（0.85 vs 0.90）判别力不足（ACE 纯钢琴被误判 likely）。deep 档现在用 demucs vocals 轨 RMS 占比作真值口径重算 presence（presence_source=separated_stem，conf 0.7，原启发式值保留 heuristics_presence，覆盖率增益 2.5 为固定单调映射）。实测判别力 9.3 倍：ACE 曲 0.0385→absent(0.10)，yue2 曲 0.3583→likely(0.90)。标准档加 presence_band 三档标签，不改数值公式。
**测试债修复**（QA 独立发现）：test_engines_r2 与 test_plan_loop_r3r5 存在克隆级缺陷（fixtures 导入缺 sys.path、跨测试文件依赖生成顺序、硬编码 .venv 解释器），已修；CI 仅排除 test_engines_r2.py（真实引擎验收依赖本地 engines/ ~18GB，不入库），不削弱任何断言。
