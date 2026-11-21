---
name: rea-music-compare
description: 比较两首或多首歌、识别同风格差异、制定多曲融合策略时使用。教 Agent 用 compare_tracks 的分维结果做结构化对比叙述，用 blend_music_dna 做特征级融合。
---

# 音乐比较与融合

## 工具链

`rea_music_compare_tracks(ref_a, ref_b)` / `rea_music_blend_music_dna(refs, dimension_map)`

ref 可以是：音频路径、content_hash 前缀、song_id，或已分析的 DNA JSON。

## 比较报告结构

compare_tracks 返回六维独立结果，**没有总分**。向用户汇报时按维度组织：

1. 节奏：ratio 与 verdict（same/double_time/half_time/different）。
2. 调性：五度圈距离 fifths_distance（0 同调，≤2 近关系，其余远关系）。
3. 能量曲线：correlation >0.7 = 动态轨迹相似。
4. 音色：centroid 的八度距离，>0.3 差异显著。
5. 响度：delta_lu，>2 LU 即母带目标不同。
6. 曲式：段落标签序列相似度 + 段数差。

典型话术："两首同为 4/4 慢速抒情，但 A 的副歌能量抬升是 B 的 1.8 倍，频谱 B 明显更暗，编曲策略不同但曲式骨架接近。"

## 融合策略制定

blend_music_dna 的 dimension_map 决定每个维度"听谁的"。制定策略时想清楚物理含义：
- tempo 取快曲 → 全曲律动变快，原慢曲段落时长需重排。
- harmony 取 A、structure 取 B → 和声节奏与段落长度可能不匹配，提示用户。
- production 取响度目标 ≠ 自动达成，生成后要经 evaluate 校验。

## 融合禁令

只允许统计特征层融合。任何用户要求"把 A 的副歌旋律直接用进 B"都要拒绝并解释旋律版权风险，改为提取 A 副歌的音域/密度/能量特征后让生成端原创。
