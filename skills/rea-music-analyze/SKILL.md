---
name: rea-music-analyze
description: 分析参考歌曲时使用。指导 Agent 选择分析档位（fast/standard/deep）、编排工具调用顺序、解读置信度与 missing_reason、识别低质量结果并决定是否升级重分析。涉及"分析这首歌""拆解音乐结构""提取音乐DNA"等任务先读本 skill。
---

# REA-Music 分析流程

## 你将使用的工具

`rea_music_capabilities` → `rea_music_inspect_audio` → `rea_music_analyze_track` → `rea_music_get_job_status` → `rea_music_get_analysis`

## 标准编排

1. **先查能力**。调 `rea_music_capabilities`，确认分轨引擎、生成后端、显存的真实状态。不要假设任何引擎存在。
2. **检查文件**。`rea_music_inspect_audio` 验证格式、时长、声道，拿 content_hash。文件损坏此时就能发现。
3. **选档位**。按任务目的选，不是无脑 deep：
   - 用户只要"这歌什么风格、多少 BPM" → `fast`
   - 要"结构、和弦、段落分析、创作参考" → `standard`
   - 要"精细人声、旋律转写、多引擎交叉验证" → `deep`（先确认 capabilities 里对应引擎 available，否则如实告知用户 deep 与 standard 的差异）
4. **提交 Job**。`rea_music_analyze_track(path, tier, wait=false)`，记下 job_id。3 分钟以上曲目建议异步。
5. **轮询**。`rea_music_get_job_status` 每次间隔建议 ≥5 秒；看到 `stage` 移动说明活着，不要中断重启。
6. **取结果**。`rea_music_get_analysis(ref, detail="compact")`。给 Agent 自己看 compact；要给用户看完整 JSON 或做逐拍分析时才取 full。

## 置信度纪律（硬规则）

- DNA 每个字段带 `confidence`。**≤0.55 的字段是启发式降级**（下拍、段落功能标签等），向用户表述时必须加"估计/推测"，禁止说成确定结论。
- 字段为 `null` 且带 `missing_reason` 时，如实说"该项需要分轨/转写引擎，当前未安装"，不要编造数值。
- `rhythm.tempo_candidates` 显示倍频歧义时，报告 BPM 要附上候选区间。
- 和弦引擎若显示 `chordotomy_dsp`，精度可信度高于 `template_v1`；后者误差明显，重大结论（编曲扒谱级）需提醒用户。

## 升级与重分析

- 同一文件 + 同档位会命中缓存。用户说"重新分析"且文件未变 → 传 `force=true`。
- fast 结果不够用 → 直接用同 hash 升档 `standard`，不用重新 inspect。
- 分析结果与用户听感明显矛盾（如 BPM 说 120 用户听是 60）→ 检查 `tempo_candidates`，向用户解释倍频歧义，可以强制改用半/双倍值重新规划，无需重跑全管线。

## 失败处理

- `capability_unavailable` → 读 `hint`，安装类操作需用户批准，绝不自行下载大体积依赖。
- Job `failed` → 看 `job.error.code`；`invalid_audio` 先修文件（ffmpeg 转码），`internal_error` 重试一次，再失败如实报告。
- 用户音频不可解码 → 建议先转 `wav`（16/44.1k）再分析。
