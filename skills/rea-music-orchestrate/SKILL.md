---
name: rea-music-orchestrate
description: 组合 REA-Music 全部能力的总编排。任务涉及完整工作链（分析→DNA→方案→生成→评测→迭代）或需要决定深度/成本/顺序时先读本 skill，再按需深入各专项 skill。
---

# REA-Music 总编排

## 全链路标准动线

```
capabilities → inspect → analyze_track(standard) → get_analysis
→ create_composition_plan → render_generation_request / generate_music
→ [用户生成] → evaluate_generation → revise_composition_plan → 再生成
```

## 动线决策表

| 任务 | 档位 | 并行性 | 停止点 |
|---|---|---|---|
| 单曲风格解读 | fast | 无 | DNA 讲解完即停 |
| 单曲深度拆解 | standard→deep | 分轨与响度分析可并行 | 段落+和弦+制作全维度讲完 |
| 单曲→原创新歌 | standard | 分析与用户需求确认可并行 | 用户确认方案后转生成 |
| 多曲融合 | standard ×N | **N 首 analyze_track 可并行提交** | 融合策略需用户确认 |
| 生成-评测-迭代 | standard | 每轮串行 | 3 轮上限或用户满意 |

## 并行与依赖

- 可并行：多曲分析、inspect 与用户提问、分轨后各 stem 分析。
- 必须串行：plan 依赖 DNA 完成；evaluate 依赖生成文件存在；revise 依赖 evaluate。
- 高成本步骤（deep 分轨、全曲转写）只有用户明确要求或任务必需时才跑。

## 用户授权红线（必须停下来问）

1. 安装任何大体积引擎（demucs/torch、ACE-Step 模型）。
2. 上传音频到任何远程服务（songbrain 等）。
3. 发布或分享生成作品。
4. 连续迭代超过 3 轮继续烧生成额度。

## 弱模型兼容路径

模型能力弱时不必勉强推理，直接按结构化流程走：把 compact DNA 的字段逐段念给用户、按 decision 表选工具、按评测建议机械执行修订。强模型则应做风格解读、多曲关系分析、创作策略的主动建议。两条路径产出结构一致，差别只在解释深度。

## 资源纪律

- 分析前看 capabilities 的 gpu.free_mib，接近 0 时全部走 CPU 档。
- 同文件重复分析必须命中缓存；未命中先查是不是 tier 或版本变了。
- 任何 job 卡在 running 超过曲长 3 倍时间 → 查 get_job_status 的 stage，无移动则报告用户，不无限等待。
