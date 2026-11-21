# ARCHITECTURE.md — Agent-Native 总架构

## 分层

```
Agent / Harness (WorkBuddy / DSH / Codex / 任意 MCP 客户端)
        │
        ├── Agent Skills (skills/rea-music-*: SKILL.md 渐进式加载，教 Agent 怎么编排)
        │
        ▼
MCP Server (src/rea_music/mcp, stdio 优先, 127.0.0.1 HTTP 可选)
        │  与 CLI (src/rea_music/cli) 共用同一 Core，零业务逻辑复制
        ▼
REA-Music Core (src/rea_music/core)
        │  统一 JSON 合同 + Job 系统 + 内容寻址缓存
        ├── analysis/  librosa 管线 (Fast/Standard/Deep 三档) + 适配器
        ├── dna/       Music DNA v1 (versioned schema + provenance)
        ├── comparison/ 多曲比较与融合
        ├── generation/ DNA→CompositionPlan→后端请求 (plan.py, backends.py)
        ├── evaluation/ 生成作品再分析→偏差评价→修订建议
        └── adapters/  引擎注册表：demucs / chordotomy / songbrain / yue2 / acestep
```

## 硬性架构原则

1. **Core 宿主无关**：core/ 不 import 任何 WorkBuddy/Codex 专有模块；不依赖特定 LLM；不依赖特定上下文格式。
2. **一核多口**：MCP、CLI、Python API 调用同一 `core` 实现。上层只做参数转换与展示。
3. **能力诚实**：每个可推断字段携带 value/unit/confidence/method/model_version/provenance，测不到就 null + missing_reason。引擎不可用时返回结构化 `capability_unavailable`，绝不静默伪造。
4. **Job 化长任务**：MCP 调用立即返回 job_id 持久化到磁盘（jobs/*.json），断线不丢任务；内容哈希 + 档位 + 参数构成缓存键，重算必须显式 force。
5. **引擎可插拔**：分析引擎与生成后端全部走 Adapter 注册表，能力发现（capabilities 工具）如实反映安装与显存状态。新增引擎不改 DNA schema 与 Skill 主体。
6. **GPU 尊重现实**：默认 CPU 路线；GPU 适配器先实测空闲显存（nvidia-smi）再决定，不足即降级并记录。
7. **HTTP 只听回环**：任何 HTTP 服务默认 127.0.0.1。
8. **音频不出本机**：除非用户显式授权（songbrain 等远程 Adapter 默认 disabled）。

## 三档分析

| 档位 | 内容 | 当前状态（0.1.0） |
|---|---|---|
| Fast | 时长/格式/指纹/BPM/拍点/调性/响度LUFS/频谱概要/RMS能量曲线 | 已实现（CPU 秒级） |
| Standard | Fast + 曲式结构段落 + 和弦进行 + 段落级DNA + 声部启发式 | 已实现（结构/和弦） |
| Deep | Standard + 分轨(demucs) + 旋律转写 + 人声精细 + 多引擎交叉验证 | 已实现，分轨/转写依赖可选引擎安装，未装时如实降级为 `capability_unavailable` |

## 数据流（第一条真实工作链）

```
音频文件
→ inspect_audio (soundfile 直读核心格式 / ffprobe 兜底其他容器 + sha256 + 格式验证)
→ analyze_track(job) → pipeline:
    decode→tempo→key→chords→structure→loudness→spectral→vocal_heuristic
→ MusicDNA v1 JSON (分段落, 带溯源)
→ create_composition_plan → CompositionPlan (通用创作方案)
→ render_generation_request → suno_prompt_package / yue2_request
→ [yue2 可用则 generate_music, 否则交给用户在 Suno/YuE2 创作]
→ 产出音频 → evaluate_generation (再分析+对照+修订建议)
```

## 目录

```
src/rea_music/{core,analysis,dna,comparison,generation,evaluation,adapters,mcp,cli}
schemas/music_dna_v1.schema.json
skills/{rea-music-analyze,dna,compose,compare,evaluate,orchestrate}/SKILL.md
fixtures/ (合成曲+ground truth)  tests/  examples/  scripts/  docs/
```

存储分目录（`REA_MUSIC_HOME`，源码树下即仓库内 `data/`，已 gitignore）：

```
data/{cache,jobs,artifacts,downloads,stems,experiments,tmp}
```

- `cache/`：按 `内容哈希 + 档位 + 参数摘要` 寻址的分析结果（`cache/<kind>/<hash>/`）
- `jobs/`：持久化 Job 记录（`jobs/*.json`），断线不丢任务
- `artifacts/`：生成音频、Suno 提示词包等交付物
- `downloads/`：远端链接输入的落盘目录（D22，仅在用户主动给链接时写入）
- `stems/`：分轨输出
- `experiments/`：评测与迭代实验记录
- `tmp/`：解码与转码临时文件

安装为 wheel（非源码树）时默认工作区改为用户数据目录：
Windows `%LOCALAPPDATA%/REA-Music`，其他平台 `~/.local/share/rea-music`。
