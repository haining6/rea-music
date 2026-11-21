---
name: rea-music-compose
description: 从参考音乐特征生成原创歌曲方案时使用。覆盖 DNA→CompositionPlan 的四种模式（继承风格、多曲融合、维度修改、歌词驱动）、原创性红线、以及如何向用户确认创作方向。
---

# 原创创作方案生成

## 工具链

`rea_music_create_composition_plan` → `rea_music_render_generation_request` → `rea_music_generate_music`

## 四种模式与参数

| 用户意图 | objective | 关键参数 |
|---|---|---|
| "参考这首歌写一首新的" | `inherit_style_new_song` | dna_refs=[单曲] |
| "把 A 的节奏和 B 的和声感结合" | `blend_styles` | dna_refs=[A,B] + dimension_map 指定每维来源 |
| "保持编曲，改成更有动力" | `modify_dimension` | target_modifications={"bpm": 132, "key": ...} |
| "我有歌词，帮我配曲" | `lyrics_driven` | user_lyrics=用户原创文本 |

## dimension_map 可用维度

`tempo / key / structure / harmony / production / vocal`。索引对应 dna_refs 顺序。

## 原创性红线（不可协商）

- 计划只继承**统计特征**（BPM、调性、段落时长比例、能量曲线、频段分布）。
- 生成端歌词模板行必须整体替换为原创内容，禁止填入参考曲歌词。
- 禁止要求生成器"cover/复刻/原曲旋律"。
- 向用户交付时明确说明：方案继承的是风格规律，产出必须是新作品。

## 与用户确认的顺序（高价值问题，一次问清）

1. 目标时长与用途（demo/发行/背景乐）→ 决定 duration_scale。
2. 语言与人声性别偏好 → 进 render 参数。
3. 想保留参考曲的什么、想改什么 → 落到 dimension_map / target_modifications。
4. 无明确答案就先出默认方案，用户反馈后 revise。

## 渲染后端选择

- `render_generation_request(plan, "suno")` → 得到 style_prompt + 段落标签歌词模板，交给用户在 Suno 操作。
- `backend="yue2"` → 结构化请求（YuE2 在线时）。
- `generate_music(plan, backend="auto")` 自动探测 YuE2 Studio；离线时降级返回 Suno 包。**降级不是失败**，把提示词包完整交给用户即可。
