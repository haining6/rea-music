---
name: rea-music-evaluate
description: 评估 AI 生成音乐的质量、目标符合度与原创性风险时使用。教 Agent 用 evaluate_generation 的逐维结果做验收判断、解读原创性风险、并用 revise_composition_plan 完成下一轮迭代。
---

# 生成作品评测与迭代

## 工具链

`rea_music_evaluate_generation(generated_path, target_ref)` → `rea_music_revise_composition_plan(plan, evaluation)`

## 逐维验收（不做单一总分）

评测返回各维独立 satisfied 判定 + revision_suggestions：

| 维度 | satisfied 判据 | 不达标时常见原因 |
|---|---|---|
| 节奏符合度 | ratio ∈ (0.97, 1.03) | 生成器忽略 BPM 提示词，需在风格描述里强化 |
| 调性符合度 | 五度圈距离 0 | 大多数生成器不吃调性约束，向用户说明这是已知边界 |
| 能量曲线 | correlation > 0.6 | 段落结构与目标差异大 |
| 响度目标 | delta < 2 LU | 后期母带问题，或生成器输出电平习惯 |
| 原创性风险 | chord_8gram_overlap | 高重叠时必须提醒人工复核 |

## 技术质量检查

`technical_quality.issues` 会列出：响度过低、真峰值超限、动态过窄。技术问题是硬伤，优先于风格问题修复。

## 原创性风险分级

- `low`：正常风格继承范围。
- `medium`：和弦走向高度相似，旋律大概率无碍，提示用户自查。
- `high_similarity_warning`：必须停下。提醒用户对比两曲听感，确认无实质性相似后再继续迭代。机器指标不能替代人工判定，这一条要明说。

## 迭代闭环

1. evaluate → 2. revise（自动生成修订版 plan）→ 3. 用户再生成 → 4. 再 evaluate。
- 每轮都留下 evaluation_id 与 plan revision_history，构成 OptimizationHistory。
- **迭代上限默认 3 轮**。连续两轮无改善就停下来向用户复盘，不要无限循环烧额度。
- 主观维度 `pending_human_review` 永远需要用户试听确认，Agent 不能替用户判定"好听"。
