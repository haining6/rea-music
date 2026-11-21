"""CompositionPlan：Music DNA → 原创创作方案。

四种目标：分析后再创作 / 多曲融合 / 指定维度修改 / 歌词驱动。
输出与生成模型无关的通用计划，再由 backends.py 渲染为各后端请求。
原创性硬约束：计划不含参考曲歌词文本、不含参考曲旋律音符序列。
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid

from rea_music.comparison.compare import blend_music_dna
from rea_music.core import cache as cache_mod
from rea_music.core.errors import ValidationError

SECTION_ORDER = ["intro", "verse", "pre_chorus", "chorus",
                 "verse", "chorus", "bridge", "final_chorus", "outro"]


def create_composition_plan(*, dna_refs: list[dict], objective: str,
                            user_request: str | None = None,
                            dimension_map: dict | None = None,
                            target_modifications: dict | None = None,
                            user_lyrics: str | None = None) -> dict:
    """dna_refs: 一或多份 Music DNA。objective ∈ 四种模式。"""
    if not dna_refs:
        raise ValidationError("缺少参考 Music DNA")
    objectives = ("inherit_style_new_song", "blend_styles", "modify_dimension",
                  "lyrics_driven")
    if objective not in objectives:
        raise ValidationError(f"未知 objective: {objective}", hint=f"允许: {objectives}")

    primary = dna_refs[0]
    blended = None
    if objective == "blend_styles" and len(dna_refs) > 1:
        blended = blend_music_dna(dna_refs, dimension_map or {})
    if objective == "lyrics_driven" and not user_lyrics:
        raise ValidationError("歌词驱动模式需要用户提供原创歌词",
                               hint="歌词必须为用户提供且有使用权利的文本。")

    tempo_src = blended["dimensions"]["tempo"] if blended else primary["rhythm"]["bpm"]
    key_src = blended["dimensions"]["key"] if blended else primary["tonality"]["key"]
    structure_src = (blended["dimensions"]["structure_template"] if blended
                     else primary["form"]["segments"])
    production_src = (blended["dimensions"]["production_target"] if blended
                     else primary["production"])

    bpm = _apply_mods(tempo_src, target_modifications, "bpm")
    key = _apply_mods(key_src, target_modifications, "key")

    sections = _plan_sections(structure_src, target_modifications or {},
                               primary)
    energy_curve = _energy_goals(primary, target_modifications or {})

    plan = {
        "plan_id": f"plan_{uuid.uuid4().hex[:12]}",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "schema": "composition_plan_v1",
        "objective": objective,
        "user_request": user_request,
        "sources": [d["song"]["song_id"] for d in dna_refs],
        "inherit": {
            "tempo_bpm": bpm,
            "key": key,
            "meter_beats_per_measure": _val_or(primary, ["rhythm", "meter_beats"], 4),
            "harmonic_flavor": (blended or primary)["harmony"]["progressions"].get("value")
                               if (blended or primary).get("harmony") else None,
            "instrumentation_hint": _instrumentation_hint(primary, target_modifications or {}),
            "energy_arc": energy_curve,
            "style_tags": primary.get("creative_semantics", {}).get("style_tags", {}).get("value"),
        },
        "sections": sections,
        "lyrics_guidance": _lyrics_guidance(user_lyrics, objective, primary),
        "originality_guardrails": {
            "reuse_reference_lyrics": False,
            "reuse_reference_melody_notes": False,
            "reuse_reference_audio_samples": False,
            "statistical_style_inheritance_only": True,
            "note": "风格规律通过统计特征继承；旋律与歌词必须原创产出",
        },
        "validation": {
            "sources_count": len(dna_refs),
            "sections_count": len(sections),
            "estimated_duration_sec": round(sum(s["target_duration_sec"]
                                                for s in sections), 1),
        },
    }
    cache_mod.save_cached(plan["plan_id"], "plan", plan, {"id": plan["plan_id"]})
    return plan


_NOTE_PREFIX = re.compile(r"^[A-Ga-g](?:#|b)?")
_MAX_KEY_LABEL = 24


def _coerce_bpm(raw) -> float:
    """bpm 必须是正数值：自然语言句子（如"把 BPM 调整到 92"）一律拒绝。

    早前 revise_plan 会把建议里的整句写进 plan.inherit.tempo_bpm，渲染 Suno
    时 ``round(str)`` 直接 TypeError，YuE2 则报"tempo_bpm 缺失"。
    """
    if isinstance(raw, dict):
        raw = raw.get("value")
    if isinstance(raw, bool) or raw is None:
        raise ValidationError(
            f"target_modifications.bpm 必须是数值，收到 {raw!r}",
            hint='传 92 或 "92"；自然语言修订建议需先解析成数值再写入。')
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValidationError(
            f"target_modifications.bpm 必须是数值，收到 {raw!r}",
            hint='传 92 或 "92"；自然语言修订建议需先解析成数值再写入。') from None
    if not math.isfinite(value) or value <= 0:
        raise ValidationError(
            f"target_modifications.bpm 必须是正的有限数值，收到 {raw!r}")
    return value


def _coerce_key(raw) -> str:
    """调性标签校验：允许 "C major" / "A minor" / "C#:min" 这类短标签。"""
    if isinstance(raw, dict):
        raw = raw.get("value")
    text = str(raw).strip() if raw is not None else ""
    if not text or len(text) > _MAX_KEY_LABEL or not _NOTE_PREFIX.match(text):
        raise ValidationError(
            f"target_modifications.key 不是有效调性标签: {text!r}",
            hint='传 "C major" / "A minor" 这类标签。',
            details={"received": text[:120]})
    return text


def _apply_mods(src, mods: dict | None, field: str):
    """把 target_modifications 覆盖到继承值上（bpm/key 做类型校验）。"""
    mods = mods or {}
    if field in mods:
        raw = mods[field]
        if field == "bpm":
            return _coerce_bpm(raw)
        if field == "key":
            return _coerce_key(raw)
        return raw
    return (src or {}).get("value") if isinstance(src, dict) else src


def _val_or(dna: dict, path: list[str], default):
    node = dna
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return default
    return (node or {}).get("value", default) if isinstance(node, dict) else node


_SECTION_EMOTION = {
    "intro": "calm", "verse": "reflective", "pre_chorus": "building",
    "chorus": "uplifting", "bridge": "tense", "instrumental": "neutral",
    "outro": "resolved",
}


def _plan_sections(structure_src: list, mods: dict,
                   primary: dict | None = None) -> list[dict]:
    """从参考结构提取段落节奏，生成含情绪/能量目标/编曲策略的段落方案。

    R3 深化：energy_target 由参考曲 rms 曲线按段落时间窗归一；
    strategy 溯源段级 band 能量与 chords_local 罗马数字。
    """
    if not structure_src:
        base = [{"label": "intro", "target_duration_sec": 8.0},
                {"label": "verse", "target_duration_sec": 32.0},
                {"label": "chorus", "target_duration_sec": 32.0},
                {"label": "outro", "target_duration_sec": 8.0}]
        return [_finish_section(s, primary, None) for s in base]
    total = sum(s["end_sec"] - s["start_sec"] for s in structure_src)
    sections = []
    for i, seg in enumerate(structure_src[:10]):
        label = _canonical_label(seg.get("label", {}).get("value", "verse"))
        dur = (seg["end_sec"] - seg["start_sec"]) / total * 240.0  # 归一到 ~4分钟
        local = seg if "chords_local" in seg else None
        sections.append(_finish_section(
            {"label": label, "target_duration_sec": round(dur, 1),
             # time_range 是 _energy_target 的取值依据：缺它就只能回落到硬编码表
             "time_range": [seg["start_sec"], seg["end_sec"]],
             "energy": "low" if label in ("intro", "outro") else
                       ("high" if label == "chorus" else "mid")},
            primary, local))
    if "duration_scale" in mods:
        scale = float(mods["duration_scale"])
        for s in sections:
            s["target_duration_sec"] = round(s["target_duration_sec"] * scale, 1)
    return sections


def _finish_section(section: dict, primary: dict | None,
                     local: dict | None) -> dict:
    """补齐 R3 字段：emotion 枚举 / energy_target 0-1 / strategy 溯源。"""
    label = section["label"]
    section["emotion"] = _SECTION_EMOTION.get(label, "neutral")
    section["energy_target"] = _energy_target(
        primary, section.get("time_range")) or (
        {"intro": 0.25, "verse": 0.5, "pre_chorus": 0.6, "chorus": 0.85,
         "bridge": 0.45, "instrumental": 0.55, "outro": 0.2}.get(label, 0.5))
    strategy = {}
    if primary:
        bands = primary.get("instrumentation", {}).get(
            "band_energy_ratios", {}).get("value") or {}
        if bands.get("high_2000_8000", 0) > 0.15:
            strategy["instrumentation"] = "bright-tops-and-cymbals-forward"
        if bands.get("sub_bass_lt60", 0) > 0.12:
            strategy["low_end"] = "keep-sub-bass-foundation"
        harm = (local or {}).get("chords_local") or \
            primary.get("harmony", {}).get("chord_segments", {}).get("value")
        romans = _roman_numerals(harm, primary)
        if romans:
            strategy["harmony"] = f"harmonic frame near {romans}"
        hr = primary.get("harmony", {}).get(
            "harmonic_rhythm_beats", {}).get("value")
        if hr:
            strategy["rhythm"] = f"chord change every ~{hr} beats"
    section["strategy"] = strategy or {"note": "无足够段级证据，保持参考曲平衡"}
    section.pop("time_range", None)
    return section


def _energy_target(primary: dict | None, time_range: list | None):
    """参考曲 rms 曲线在段落时间窗内的归一能量（无时间窗/无曲线返回 None）。"""
    if not primary or not time_range:
        return None
    curve = primary.get("production", {}).get(
        "rms_curve_1s", {}).get("value") or []
    try:
        t0, t1 = float(time_range[0]), float(time_range[1])
    except (TypeError, ValueError, IndexError):
        return None
    vals = [p["rms"] for p in curve
            if isinstance(p, dict) and t0 <= p.get("time_sec", -1) <= t1]
    if not vals or not curve:
        return None
    vmax = max(p.get("rms", 0) for p in curve if isinstance(p, dict)) or 1.0
    return round(float(sum(vals) / len(vals)) / vmax, 2)


_ROMAN = {"maj": ["I", "II", "III", "IV", "V", "VI", "VII"],
          "min": ["i", "ii", "iii", "iv", "v", "vi", "vii"]}


def _roman_numerals(chords: list | dict | None, primary: dict | None) -> str | None:
    """把段内和弦映射到参考调性的级数（maj/min 大小写约定）。"""
    if not chords or not primary:
        return None
    key = primary.get("tonality", {}).get("key", {}).get("value") or ""
    try:
        tonic_name, mode = key.split()
        base = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        tonic = base.index(tonic_name)
    except (ValueError, IndexError):
        return None
    out = []
    for c in chords[:6]:
        label = c.get("chord") if isinstance(c, dict) else c
        if not label or ":" not in label:
            continue
        root, suffix = label.split(":", 1)
        try:
            degree = (base.index(root) - tonic) % 12
        except ValueError:
            continue
        is_minor = "min" in suffix or (
            suffix.startswith("m") and not suffix.startswith("maj"))
        table = _ROMAN["min" if mode == "minor" else "maj"]
        names = {0: 0, 2: 1, 4: 2, 5: 3, 7: 4, 9: 5, 11: 6}
        idx = names.get(degree)
        if idx is None:
            continue
        roman = table[idx]
        if is_minor and roman.isupper():
            roman = roman.lower()
        elif not is_minor and roman.islower():
            roman = roman.upper()
        out.append(roman)
    return "-".join(out) if out else None


def _canonical_label(raw: str) -> str:
    mapping = {"pre-chorus": "pre_chorus", "prechorus": "pre_chorus",
               "final_chorus": "chorus", "inst": "instrumental",
               "section_a": "verse", "section_b": "verse",
               "section_c": "chorus", "section_d": "bridge",
               "section_e": "bridge", "section_f": "bridge",
               "section_g": "bridge", "section_h": "outro"}
    out = mapping.get(raw, raw)
    return out if out in SECTION_ORDER + ["instrumental"] else "verse"


def _energy_goals(primary: dict, mods: dict) -> list[dict]:
    curve = primary.get("production", {}).get("rms_curve_1s", {}).get("value") or []
    if not curve:
        return []
    # 取 9 个锚点归一化能量曲线
    n = len(curve)
    if n < 9:
        return []
    idx = [int(i * (n - 1) / 8) for i in range(9)]
    vals = [curve[i]["rms"] for i in idx]
    vmax = max(vals) or 1.0
    return [{"progress": round(i / 8, 2), "relative_energy": round(v / vmax, 3)}
            for i, v in zip(range(9), vals)]


def _instrumentation_hint(primary: dict, mods: dict) -> str:
    bands = primary.get("instrumentation", {}).get("band_energy_ratios", {}).get("value") or {}
    hp = primary.get("production", {}).get("harmonic_percussive_ratio", {}).get("value")
    hints = []
    if hp is not None and hp < 0.45:
        hints.append("rhythm-section-forward")
    elif hp is not None:
        hints.append("harmonic-texture-forward")
    if bands.get("air_gt8000", 0) > 0.05:
        hints.append("bright-cymbals-or-synth-air")
    if bands.get("sub_bass_lt60", 0) > 0.15:
        hints.append("prominent-808-or-sub-bass")
    if mods.get("instrumentation"):
        hints.insert(0, mods["instrumentation"])
    return ", ".join(hints) or "balanced"


def _lyrics_guidance(user_lyrics: str | None, objective: str,
                     primary: dict) -> dict:
    if objective == "lyrics_driven" and user_lyrics:
        lines = [ln.strip() for ln in user_lyrics.splitlines() if ln.strip()]
        tagged = _parse_lyric_tags(lines)
        return {
            "mode": "user_provided",
            "line_count": len(lines),
            "language_detect": "zh-dominant" if _cjk_ratio_text(
                user_lyrics) > 0.3 else "latin-script",
            "syllable_profile": {
                "method": "cjk_char_as_syllable+latin_vowel_group",
                "total_syllables": _count_syllables(user_lyrics),
                "per_line_mean": round(_count_syllables(user_lyrics) /
                                       max(len(lines), 1), 1)},
            "sectioning_suggestion": tagged or _suggest_sections_from_lines(lines),
            "density_note": "每秒约 2-3 个演唱字词；行音节数与目标 BPM 拍数对齐",
            "rights_note": "歌词由用户提供，权利归用户；系统不复制参考曲歌词",
        }
    segs = primary.get("form", {}).get("segments", [])
    chorus_n = max(2, sum(1 for s in segs
                         if s.get("label", {}).get("value") == "chorus"))
    return {
        "mode": "generate_new",
        "language_hint": "由用户指定",
        "structure_lines_hint": {"verse": "4-6 行/段", "chorus": "4 行",
                                 "chorus_repeat": chorus_n},
        "rhyme_hint": "参考曲押韵结构未提取（需歌词权利）；建议 AABB 或 ABAB",
        "theme": "由用户目标决定，禁止复用参考曲歌词意象堆砌",
    }


_TAG_LINE = {"[intro]", "[verse]", "[verse 1]", "[verse 2]", "[verse 3]",
             "[pre-chorus]", "[prechorus]", "[chorus]", "[bridge]",
             "[outro]", "[instrumental]", "[hook]", "[end]"}


def _parse_lyric_tags(lines: list[str]) -> list[dict] | None:
    """识别用户歌词里已有的段落标签行，按标签切分。"""
    idx = [i for i, ln in enumerate(lines) if ln.lower() in _TAG_LINE]
    if len(idx) < 2:
        return None
    out = []
    bounds = idx + [len(lines)]
    for k in range(len(idx)):
        tag = lines[idx[k]].strip("[]").lower().replace(" ", "_")
        label = _canonical_label(tag if tag != "hook" else "chorus")
        body = lines[idx[k] + 1: bounds[k + 1]]
        body = [b for b in body if b.lower() not in _TAG_LINE]
        if body:
            out.append({"label": label,
                        "lines": body,
                        "syllables_per_line": round(
                            _count_syllables(" ".join(body)) / len(body), 1)})
    return out or None


def _count_syllables(text: str) -> int:
    """音节估计：CJK 每字 1 音节；拉丁文按元音组计。标签行 [xxx] 不计。
    方法为启发式估计（method 已在 lyrics_guidance 标注），非精算。"""
    import re
    content = "\n".join(ln for ln in text.splitlines()
                        if ln.strip() and not ln.strip().startswith("["))
    total = 0
    for ch in content:
        if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff":
            total += 1
    latin = re.sub(r"[^a-zA-Z]+", " ", content)
    for word in latin.split():
        groups = re.findall(r"[aeiouy]+", word.lower())
        total += max(len(groups), 1)
    return total


def _cjk_ratio_text(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / max(len(text), 1)


def _suggest_sections_from_lines(lines: list[str]) -> list[dict]:
    """按行数推断段落结构建议。"""
    out = []
    i = 0
    order = ["intro", "verse", "pre_chorus", "chorus"]
    n = len(lines)
    while i < n:
        take = min(4, n - i)
        label = order[min(len(out), 3)]
        out.append({"label": label, "lines": lines[i:i + take]})
        i += take
    return out
