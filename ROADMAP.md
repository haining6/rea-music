# ROADMAP.md — 阶段路线与完成标准

当前发行版 **0.1.0** 覆盖 R1–R5 全部范围。R6/R7 状态见下方。
验收证据统一记在 `CHANGELOG.md`、`benchmarks/`、`examples/`、`DECISIONS.md` 四处
（过程管理文档属内部资料，不随公开仓库发布，故此处不再引用）。

## R0 现状审计与开源尽调 ✅
工作区审计、17 项开源尽调、架构定稿、环境搭建。
证据：`OPEN_SOURCE_REUSE.md`、`DECISIONS.md`。

## R1 最小 Agent 原生闭环 ✅
Core + 三档分析 + Music DNA v1 + 14 个 MCP 工具 + CLI + 6 Skills + 合成曲 ground truth 测试 + 端到端演示。
证据：`pytest` 全绿（数字见 CHANGELOG）、`examples/demo_e2e_transcript.json`、
`rea-music capabilities` 如实输出。

## R2 分析能力完善 ✅
chordotomy 和弦升级（py3.12 实测）、demucs 分轨（CPU 路径实测）、basic-pitch 隔离 venv、
all-in-one GPU 结构模型（NATTEN 无 Windows wheel，已裁定维持降级，见 DECISIONS D15）。
证据：`benchmarks/PERFORMANCE.md` R2 增量表、`benchmarks/chords_compare.json`。

## R3 智能创作规划深化 ✅
情绪曲线段落级编排策略、歌词驱动细分、多曲融合冲突消解（CompositionPlan 四种目标）。

## R4 生成系统集成 ✅
YuE2 Studio 实机生成打通（真实 60s 歌曲生成并再分析，见 `examples/yue2_live_record.json`）；
ACE-Step REST 后端接入（38 路由运行时发现，见 `examples/ace_live_record.json`）。
⚠️ 两后端的**许可证仍待补核**，当前定位为实验性对接，不作为稳定承诺（见 `OPEN_SOURCE_REUSE.md`）。

## R5 评测与迭代 ✅
OptimizationHistory 跨会话复用、感知类指标成对比较、迭代停止条件硬化
（默认 3 轮、连续 2 轮无改善即停）。

## R6 跨 Agent 兼容实测 🚧
WorkBuddy 注册路径已验证（脚本 + 配置形态），**Trust 授权后的一次交互式 tools/call 仍待实测**；
DSH patch 模板与 Codex `config.toml` 模板已给出，同样待实测。模板本身不算完成。

## R7 技术债 🔄
- ✅ `pip install -e .` 与 wheel 双路径可安装化（schema 移入包内，0.1.0 已修）
- ✅ 错误路径结构化覆盖（`code / hint / details` 三件套）
- ✅ 依赖诚实性：删除零引用依赖；`mcp` 下限对齐代码实际使用的 SDK 符号
- ✅ 合规出口：`THIRD_PARTY_NOTICES.md` + `MANIFEST.in`
- ✅ 链接目标地址护栏（`core/net_guard.py` 已实现，**待接入 `core/source.py` 下载主流程**）
- 🚧 CI 覆盖缺口：`tests/test_engines_r2.py`（含远程上传授权断言）尚未纳入 CI；macOS 未进矩阵
- 🚧 无可复现的发布流水线：`python -m build` + `twine check` 未进 CI，本地 `dist/` 可能过期

每阶段交付物必须可运行、可复现。空目录、TODO 注释、未验证的配置文件不算完成。
