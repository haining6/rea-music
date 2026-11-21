---
name: rea-music-dna
description: 读取与解释 Music DNA 时使用。教 Agent 理解 DNA 分层结构（客观测量/推断/解释）、字段溯源（method/provenance/confidence）、多引擎冲突的处理原则，以及把 DNA 转述为音乐语言的方法。
---

# Music DNA 阅读规范

## 数据分层（先看 layer 字段）

- `objective_audio_features`：仪器测量（LUFS、RMS、频谱、拍点）。可以直接引用。
- `inferred`：算法推断（和弦、段落、调性、人声存在性）。必须带置信度表述。
- `interpretive`：解释层（风格标签）。只能作参考，禁止当客观事实。

## 高价值字段速查

| 想知道什么 | 去哪里读 |
|---|---|
| 速度与律动 | `rhythm.bpm` + `tempo_candidates` + `meter_beats` |
| 情绪起伏 | `production.rms_curve_1s`（能量曲线）+ `form.segments` 的段落 rms |
| 结构 | `form.segments`（每段 label/start/end/label_confidence） |
| 和声 | `harmony.chord_segments` + `progressions`（高频 4 和弦循环） |
| 调性 | `tonality.key` + `key_regions`（转调轨迹） |
| 制作混音 | `production.integrated_lufs / lra_lu / true_peak_dbtp / spectral_centroid_hz` |
| 有没有人唱 | `vocals.presence_band` + `vocals.presence_estimate`；先看 `presence_source`：`separated_stem`（conf 0.7，Deep 档）才有判别力；`mid_side_heuristic`（≤0.45）只说明"中频居中能量多"，纯钢琴/弦乐同样会高 |
| 频段构成 | `instrumentation.band_energy_ratios` |

## 冲突处理

多引擎给出矛盾结果时（未来 deep 档常见）：
1. 不要静默选边。保留两份原始输出。
2. 双方置信度都 >0.7 且矛盾 → 报告用户矛盾点，请用户听音裁决（15 秒 A/B 试听即可解决大部分调性/下拍分歧）。
3. 一方明显低置信度 → 采信高置信度，但在叙述中标注另一方差异。

## 转述给用户的语言

把 DNA 翻译成音乐语言而不是数据语言。例如：
- ✅ "这首歌约 96 BPM、A 小调，主歌→副歌有明显能量抬升（RMS 0.12→0.19），高频段明亮度中等，推断为中等速度抒情曲，副歌叠了更密的鼓组"
- ❌ "bpm=96, key=A minor, rms=0.12→0.19, centroid=2100Hz"

段落叙述用时间轴顺序："0:08 进主歌、0:40 副歌、能量抬升 58%"。
