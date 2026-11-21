# OPEN_SOURCE_REUSE.md — 开源技术尽调与决策矩阵

> 本文回答"**为什么选/不选**"。**许可声明的正式出口是 `THIRD_PARTY_NOTICES.md`**
> （直接依赖 + 关键传递依赖的逐包 SPDX 表）。本文覆盖的是候选项目尽调，不含全部运行时依赖。

尽调时间：2026-11-20。方法：逐仓库实际抓取 README/安装文档核实，不凭 README 单独下结论，以下"决定"均结合本机环境实测（Python 3.12 / Windows / 8GB 显存，实测空闲显存后再决定 GPU 路径 / 可直连 PyPI 与 GitHub）。

## 决策矩阵

| 项目 | 网址 | 功能 | 许可证 | 维护状态 | GPU需求 | 复用方式 | 实测/验证结果 | 最终决定 |
|---|---|---|---|---|---|---|---|---|
| libretta | github.com/GoddessAwe/libretta | Demucs分轨+Basic Pitch MIDI+librosa特征+和弦+幽灵频率，输出结构化JSON | MIT | 活跃（2026） | 可选（CPU可用） | 候选Adapter | README核验：pip可装，Windows对basic-pitch有专门提示；依赖面与本引擎高度重叠 | **参考，暂不复用**。其扁平特征无段落级归属与置信度溯源，REA-Music 需自研 DNA 分层；分轨/转写能力按独立Adapter（demucs、basic-pitch）拆开接入，避免一次性拖入 torch+TF |
| songbrain | github.com/songbrain-ai/songbrain | 托管API：BPM/调性/段落/逐词歌词/最佳时刻，远程MCP | SDK MIT（API另受条款约束） | 活跃（2026） | 无（云端） | 远程Adapter（能力探测） | MCP地址 api.songbrain.ai/v1/mcp 已核实；免费额度5首/月 | **可选远程交叉验证Adapter**，无API key时返回 capability_unavailable。不上传用户音频前必须显式授权 |
| SongScope | github.com/imnaiteek/SongScope | FastAPI+自研librosa引擎：5种tempo估计、HMM和弦、调性融合、novelty结构、LUFS、Demucs分轨 | **LICENSE 文件 404，无许可证** | 活跃 | 可选 | 仅架构参考 | 官方声明确认 essentia 无 Windows wheel；其合成素材回归测试方法被本项目采纳 | **只借鉴思想，禁止复制代码**。其验证过的算法路线（tuning-aware CQT chroma、octave-aware tempo、novelty结构、置信度分层）用已发表的标准算法独立实现 |
| all-in-one (mir-aidj) | github.com/mir-aidj/all-in-one | BPM/beats/downbeats/段落边界/段落标签（intro/verse/chorus）一体模型 | MIT（论文 WASPAA 2023） | 中等 | 需GPU（4090实测10曲73s；CPU慢）+ **Windows需源码编译NATTEN** + madmom需git安装 | Adapter候选 | **NATTEN 无 Windows wheel，pip 实测失败（2026-11-20）；用户批准安装后限时尝试同样受阻** | **不可安装，维持降级记录**。结构分析保持 agglomerative+启发式并明确低置信度；NATTEN 官方仅发 Linux wheel，Windows 需 MSVC+CUDA Toolkit 源码编译链，超出可接受风险，后续以 all-in-one-infer 或 GPU 环境再评估 |
| all-in-one-infer (openmirlab) | github.com/openmirlab/all-in-one-infer | 上一行的推理优化版 | 需核验（Apache倾向） | 活跃 | 同上 | 同上 | 未单独实测 | 同上，与 all-in-one 二选一，R2 统一验证 |
| demucs | github.com/facebookresearch/demucs | SOTA分轨（drums/bass/vocals/other） | MIT | **官方停止维护**（作者离Meta，fork仅修关键bug） | GPU默认约7GB；CPU 1.5x曲长 | subprocess Adapter | 仓库声明已核实；当前GPU空闲28MiB不可用 | **Adapter 接入但默认不安装**（torch下载量大，需用户批准）。separate_stems 未安装时返回 capability_unavailable + 安装指引 |
| basic-pitch | github.com/spotify/basic-pitch | 轻量复调旋律转写（audio→MIDI） | Apache-2.0 | 中等（2025仍有提交） | 无 | Adapter | **主venv py3.12 不可用（官方仅 ≤3.11）；已建 engines/bp-venv（本机 Astral 3.10 + onnxruntime + setuptools<81 pin，2026-11-20 实测 import+模型在位）** | **已采用（隔离venv subprocess Adapter）**。坑：resampy 需 pkg_resources，setuptools ≥81 已移除该模块，必须 pin <81 |
| librosa | github.com/librosa/librosa | Python音频分析基座 | ISC | 活跃 | 无 | **核心依赖** | venv 安装实测通过 | **采用**。R1 全部分析的地基 |
| essentia | github.com/MTG/essentia | C++音乐特征库 | AGPL-3.0 | 活跃 | 部分 | — | **无Windows wheel**（SongScope同样确认）；AGPL对分发不友好 | **不采用**（本地个人使用不受AGPL影响但Windows装不上，双重否决） |
| madmom | github.com/CPJKU/madmom | DBN节拍/下拍追踪 | BSD-2 + 专有数据条款 | 缓慢 | CPU | — | Windows需源码编译，py3.13未验证 | **不采用**，下拍用自研beat+重音启发式并标低置信度 |
| MOSS-Music | github.com/OpenMOSS/MOSS-Music | 8B音乐理解模型（caption/歌词ASR/结构/和弦推理） | Apache-2.0 | 活跃（2026-05发布） | **8B需vLLM/SGLang，远超8GB** | 远程API候选 | 硬件实测否决 | **capability_unavailable 登记**，未来经用户授权以远程API形式接入Deep档 |
| ACE-Step-1.5 | github.com/ace-step/ACE-Step-1.5 | 本地音乐生成（2B/4B DiT，turbo/sft，参考音频/repaint/cover） | **待核**（仓库未在前页明示 LICENSE） | 活跃（2026-04 XL发布） | ≤6GB可跑（2B turbo+INT8+offload）；官方py3.11–3.12 | REST Adapter（acestep-api :8001） | 官方表：6-8GB档=2B turbo+0.6B LM | **实验性对接，许可待核**。模型下载≥数GB，未获批准不装。Windows有portable包 |
| YuE2 Studio | github.com/timoncool/YuE2-Studio | 原生Windows歌曲生成studio（Rust/Tauri+yue2.cpp），**内置MCP server 127.0.0.1:8791**，支持style+lyrics+乐谱编辑+cover+6分轨 | **待核**（Rust 仓库，未在前页明示 LICENSE） | 非常活跃（2026-11仍在提交） | ≥6GB VRAM 离线生成 | **实验性生成后端**（用户已装+已配WorkBuddy） | 本机已有该应用与WorkBuddy MCP配置（disabled），studio窗口未开时端口不通 | **实验性对接，许可待核**。REA-Music 只写薄 Adapter 探测 8791 并转发，studio不可用时降级为Suno提示词包输出。**不作为稳定承诺** |
| chordotomy | github.com/zeikar/chordotomy | 和弦识别（beat同步+转调+罗马数字+借用和弦），DSP引擎无torch，附agent skill | MIT | 活跃 | 无（DSP档）/ CPU（model档） | **和弦 Adapter** | DSP档 `pip install chordotomy` 体积小；精度表已公开（Tiny AAM majmin 0.803） | **R2 尝试接入主venv**（py3.13实测为准），失败则退回自研模板匹配 |
| MCP python-sdk | github.com/modelcontextprotocol/python-sdk | MCP官方SDK | MIT | 活跃 | 无 | **核心依赖** | venv实测安装成功（随 mcp 包） | **采用**（FastMCP stdio） |
| agentskills | github.com/agentskills/agentskills | Agent Skills开放规范 | Apache-2.0（代码）/CC-BY-4.0（文档） | 活跃（LF Projects托管） | 无 | **Skill格式标准** | 规范已核实：SKILL.md+渐进式加载+scripts/references目录 | **采用**。六个skill全部按此规范编写 |
| Prefect / pytest | github.com/PrefectHQ/prefect / pytest-dev | 工作流编排/测试 | Apache-2.0/MIT | 活跃 | 无 | pytest采用；Prefect不采用 | — | **pytest 采用**；R1用轻量持久化Job文件系统，不上重型编排器（理由：单机单用户场景，Prefect server成本大于收益，DECISIONS.md #4） |

## 引用版本锚定

- 尽调基准日：2026-11-20，各仓库以当日 main 分支 README 为准。
- demucs 停止维护声明：仓库首页 "this repository is not maintained anymore"（Nov 16, 2023 最后文档提交）。
- chordotomy 精度数字取自其 docs/evaluation（Tiny AAM/GuitarSet，mir_eval 加权）。
- YuE2 Studio MCP 端口与工具入口（studio_status）取自其 README "For AI agents" 段。

## 持续维护规则

新引擎评估一律走 docs/decisions 流程：版本→许可证→兼容性→隔离安装→基准对比→回归测试→记录依据→验收后启用。
