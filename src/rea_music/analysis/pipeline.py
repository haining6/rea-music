"""分析管线编排：Fast / Standard / Deep 三档，阶段进度回写 Job，DNA 落缓存。

同一文件 + 同档位 + 同参数 → 直接命中缓存；force=True 才重算。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from rea_music import __version__
from rea_music.analysis import audio as audio_mod
from rea_music.analysis import chords as chords_mod
from rea_music.analysis import inspect as inspect_mod
from rea_music.analysis import key as key_mod
from rea_music.analysis import loudness as loudness_mod
from rea_music.analysis import spectral as spectral_mod
from rea_music.analysis import structure as structure_mod
from rea_music.analysis import tempo as tempo_mod
from rea_music.analysis import vocal as vocal_mod
from rea_music.core import cache as cache_mod
from rea_music.core import jobs as jobs_mod
from rea_music.core.config import load_config
from rea_music.core.errors import InvalidAudio, REAError
from rea_music.dna import model as dna_model
from rea_music.dna import validate as dna_validate

TIERS = ("fast", "standard", "deep")
_STAGES = {
    "fast": [("inspect", 0.1), ("decode", 0.2), ("tempo", 0.4),
             ("key", 0.6), ("loudness", 0.8), ("spectral", 0.95), ("assemble", 1.0)],
    "standard": [("inspect", 0.05), ("decode", 0.1), ("tempo", 0.25),
                 ("key", 0.4), ("chords", 0.55), ("structure", 0.75),
                 ("vocal", 0.85), ("loudness", 0.92), ("spectral", 0.97),
                 ("assemble", 1.0)],
    "deep": [("inspect", 0.02), ("decode", 0.05), ("tempo", 0.1),
             ("key", 0.15), ("chords", 0.2), ("structure", 0.26),
             ("vocal", 0.3), ("stems", 0.36), ("melody", 0.44),
             ("loudness", 0.5), ("spectral", 0.56), ("assemble", 0.62),
             ("cross_validation", 0.75), ("save", 1.0)],
}
# melody 转写缓存键版本（契约变更时递增使旧缓存失效）
_BP_CACHE_V = "r2"
_DNA_CACHE_V = "r8"


def dna_cache_params(tier: str, cfg: dict | None = None) -> dict:
    """DNA 缓存键参数：档位 + 版本 + 影响结果的配置 + 引擎可用性指纹。

    只带 {tier, version} 会导致"改了 key_window_sec / 新装了 demucs 之后仍命中
    旧 DNA"，产出的工件与当前配置不符。这里把真正影响数值的开关都纳入键内；
    无关配置（如 max_workers、提示词开关）不纳入，避免无谓失效。
    """
    config = cfg or load_config()
    analysis = config.get("analysis", {}) or {}
    from rea_music.adapters import registry
    return {
        "tier": tier,
        "version": __version__,
        "cache_v": _DNA_CACHE_V,
        "key_window_sec": analysis.get("key_window_sec"),
        "structure_min_segment_sec": analysis.get("structure_min_segment_sec"),
        "chords_engine": analysis.get("chords_engine", "auto"),
        "engines": registry.engine_fingerprint(),
    }


def analyze_track_job(job_id: str, audio_path: str, tier: str = "standard",
                      force: bool = False) -> str:
    """Job 入口：返回 DNA 工件路径字符串。供 MCP 后台 / CLI 同步共用。"""
    if tier not in TIERS:
        raise InvalidAudio(f"未知档位 {tier}", hint=f"允许: {TIERS}")
    cfg = load_config()

    def _stage(name: str):
        progress = dict(_STAGES[tier]).get(name, 0.5)
        jobs_mod.set_stage(job_id, name, progress)

    _stage("inspect")
    meta = inspect_mod.inspect_audio(audio_path)
    chash = meta["content_hash"]
    params = dna_cache_params(tier, cfg)

    if not force:
        cached = cache_mod.load_cached(chash, "dna", params)
        if cached:
            return str(cache_mod.cached_path(chash, "dna", params))

    _stage("decode")
    wav = audio_mod.ensure_wav(audio_path, chash)
    y, sr = audio_mod.load_mono(wav["mono_wav"])
    duration = meta["duration_sec"]

    _stage("tempo")
    tempo = tempo_mod.analyze_tempo(y, sr)

    _stage("key")
    key = key_mod.analyze_key(y, sr,
                              window_sec=cfg["analysis"]["key_window_sec"])

    chords, structure, vocal = None, None, None
    if tier in ("standard", "deep"):
        _stage("chords")
        chords = chords_mod.analyze_chords(y, sr, tempo.get("beats_time", []),
                                           duration,
                                           external_engine_path=wav["mono_wav"])
        _stage("structure")
        structure = structure_mod.analyze_structure(
            y, sr, tempo.get("beats_time", []),
            min_segment_sec=cfg["analysis"]["structure_min_segment_sec"])
        _stage("vocal")
        vocal = vocal_mod.analyze_vocal_presence(wav["stereo_wav"])

    stems_manifest = None
    stems_missing_reason: str | None = None
    melody: dict | None = None
    if tier == "deep":
        _stage("stems")
        from rea_music.adapters import demucs as demucs_mod
        from rea_music.adapters import registry
        cap = registry.separation_status()
        if cap["available"]:
            try:
                stems_manifest = demucs_mod.separate(wav["stereo_wav"], chash)
            except REAError as exc:
                stems_missing_reason = f"separation_failed: {exc.message}"
        else:
            stems_missing_reason = "separation_engine_unavailable"
        if stems_manifest:
            # Deep 档真值口径：分轨产物含 vocals 轨时，用该轨 RMS 占比重算
            # presence（覆盖启发式值，原值留 heuristics_presence 备复核）
            vocal = vocal_mod.apply_separated_stem_presence(
                vocal, stems_manifest.get("stems") or {})
        elif vocal:
            vocal = {**vocal, "degraded": True,
                     "degraded_reason": stems_missing_reason}

        _stage("melody")
        if stems_manifest:
            melody = _transcribe_melody(stems_manifest, chash)
            if melody and melody.get("source_stem") == "vocals" and vocal:
                fm = vocal.get("fine_metrics") or {}
                fm["vocal_range_semitones"] = {
                    "value": melody.get("vocal_range_semitones"),
                    "method": melody.get("method")}
                fm["melody_notes"] = {"value": melody.get("note_count", 0),
                                      "method": melody.get("method")}
                vocal = {**vocal, "fine_metrics": fm}

    _stage("loudness")
    loudness = loudness_mod.analyze_loudness(wav["stereo_wav"])
    _stage("spectral")
    spectral = spectral_mod.analyze_spectral(y, sr)

    _stage("assemble")
    segments_extra = _segments_extra(y, sr, structure, chords)
    dna = dna_model.build_music_dna(
        content_hash=chash, meta=meta, tempo=tempo, key=key, chords=chords,
        structure=structure, loudness=loudness, spectral=spectral, vocal=vocal,
        tier=tier, duration_sec=duration, segments_extra=segments_extra,
        source_path=str(audio_path))

    # Deep 档后置填充：只覆盖既有 null 字段或新增可选字段（R2-A/B/D）
    if tier == "deep":
        if stems_manifest:
            dna["instrumentation"]["stems"] = dna_model.fv(
                stems_manifest["stems"], method="demucs_htdemucs",
                model_version=stems_manifest.get("model_version"),
                confidence=0.8, provenance="measured")
            dna["instrumentation"]["stems"]["elapsed_sec"] = \
                stems_manifest.get("elapsed_sec")
            dna["instrumentation"]["stems"]["stems_dir"] = \
                stems_manifest.get("stems_dir")
        elif stems_missing_reason:
            dna["instrumentation"]["stems"]["missing_reason"] = \
                stems_missing_reason
        if melody:
            if melody.get("notes") is not None:
                dna["melody"]["notes"] = dna_model.fv(
                    melody["notes"], confidence=melody.get("confidence", 0.8),
                    method=melody.get("method"),
                    model_version=melody.get("model_version"),
                    provenance="inferred")
                dna["melody"]["notes"]["note_count"] = melody.get("note_count")
                dna["melody"]["notes"]["elapsed_sec"] = melody.get("elapsed_sec")
                if melody.get("source_stem"):
                    dna["melody"]["notes"]["source_stem"] = \
                        melody["source_stem"]
            else:
                dna["melody"]["notes"] = dna_model.fv(
                    None, missing_reason=melody.get(
                        "missing_reason", "transcription_failed"),
                    method=melody.get("method"), provenance="unavailable")
            if melody.get("vocal_range_semitones") is not None:
                dna["melody"]["vocal_range_semitones"] = dna_model.fv(
                    melody["vocal_range_semitones"], unit="semitones",
                    confidence=melody.get("confidence", 0.8),
                    method=melody.get("method"),
                    model_version=melody.get("model_version"),
                    provenance="inferred")
                if melody.get("source_stem") != "vocals":
                    dna["melody"]["vocal_range_semitones"]["note"] = (
                        f"值来自 {melody.get('source_stem')} 轨的转写音域"
                        "（无人声内容的器乐曲），非人声音域")
            if melody.get("midi_path"):
                dna["melody"]["midi_path"] = dna_model.fv(
                    melody["midi_path"], confidence=melody.get("confidence", 0.8),
                    method=melody.get("method"), provenance="derived")

        _stage("cross_validation")
        from rea_music.adapters import songbrain as songbrain_mod
        sb_probe = songbrain_mod.probe()
        if sb_probe["available"]:
            duration_ok = 30.0 <= float(duration) <= 600.0
            if not duration_ok:
                dna["provenance_summary"]["cross_validation"] = {
                    "engine": "songbrain", "skipped": True,
                    "note": f"时长 {duration:.0f}s 不在 songbrain 支持区间 "
                            f"(30s-10min)，跳过上传",
                }
            else:
                try:
                    dna["provenance_summary"]["cross_validation"] = \
                        songbrain_mod.cross_validate(str(audio_path), dna)
                except REAError as exc:
                    # 远程失败不阻塞本地分析，如实记录
                    dna["provenance_summary"]["cross_validation"] = {
                        "engine": "songbrain", "ok": False,
                        "error_code": exc.code, "error": exc.message,
                        "note": "远程交叉验证失败已降级，本地结果不受影响",
                    }

    dna_validate.validate_or_raise(dna)

    _stage("save")
    path = cache_mod.save_cached(chash, "dna", dna, params)
    return str(path)


def _transcribe_melody(stems_manifest: dict, content_hash: str) -> dict | None:
    """Deep 档旋律转写（R2-B）：basic-pitch 主引擎，pyin 后备。

    Returns:
        {notes, note_count, vocal_range_semitones, midi_path, method,
         model_version, source_stem, elapsed_sec[, missing_reason]} 或
        None（无可用内容轨）。
    """
    from rea_music.adapters import basicpitch as bp_mod
    from rea_music.adapters import demucs as demucs_mod
    from rea_music.core import cache as cache_mod
    from rea_music.core.hash import file_sha256
    from rea_music.core.paths import artifacts_dir

    pick = demucs_mod.pick_melody_stem(stems_manifest)
    if not pick:
        return None
    stem_name, stem_path = pick

    bp_status = bp_mod.probe()
    if not bp_status["available"]:
        return _pyin_fallback(stem_path, stem_name)

    stem_hash = file_sha256(stem_path)
    cached = cache_mod.load_cached(stem_hash, "bp_transcription",
                                   {"v": _BP_CACHE_V})
    if cached:
        payload = dict(cached)
        payload["cached"] = True
    else:
        art = artifacts_dir() / content_hash
        json_out = art / f"melody_{stem_name}.notes.json"
        midi_out = art / f"melody_{stem_name}.mid"
        payload = bp_mod.transcribe(stem_path, json_out=json_out,
                                    midi_out=midi_out)
        payload["source_stem"] = stem_name
        cache_mod.save_cached(stem_hash, "bp_transcription", payload,
                              {"v": _BP_CACHE_V})

    notes = payload.get("notes") or []
    method = payload.get("method") or "basic_pitch_icassp_2022"
    if not notes:
        return {"notes": None, "note_count": 0, "vocal_range_semitones": None,
                "midi_path": payload.get("midi_path"), "method": method,
                "model_version": payload.get("model_version"),
                "source_stem": stem_name,
                "elapsed_sec": payload.get("elapsed_sec"),
                "missing_reason": "no_lead_notes_detected_in_stem"}

    pitches = [int(n["pitch_midi"]) for n in notes]
    out = {
        "notes": notes,
        "note_count": len(notes),
        "vocal_range_semitones": max(pitches) - min(pitches),
        "midi_path": payload.get("midi_path"),
        "method": method,
        "model_version": payload.get("model_version"),
        "source_stem": stem_name,
        "elapsed_sec": payload.get("elapsed_sec"),
        # 纯合成 fixtures 的 lead 内容多落 other 轨：如实降置信度
        "confidence": 0.8 if stem_name == "vocals" else 0.6,
    }
    return out


def _pyin_fallback(stem_path: str, stem_name: str) -> dict:
    """basic-pitch 不可用时的 librosa pyin 粗 f0 后备（置信度封顶 0.3）。"""
    import numpy as np
    import soundfile as sf

    method = "librosa_pyin_fallback"
    y, sr = sf.read(stem_path, always_2d=True)
    mono = y.mean(axis=1)
    rms = float(np.sqrt(np.mean(mono ** 2)))
    if rms < 5e-4:
        return {"notes": None, "note_count": 0,
                "vocal_range_semitones": None, "midi_path": None,
                "method": method, "model_version": "librosa_pyin",
                "source_stem": stem_name, "confidence": 0.3,
                "missing_reason": "stem_silent_pyin_skipped"}

    import librosa
    if sr > 22050:  # f0 粗估计 22.05k 足够，省一半耗时
        mono = librosa.resample(mono, orig_sr=sr, target_sr=22050)
        sr = 22050
    f0, voiced, conf = librosa.pyin(mono, sr=sr, fmin=65.0, fmax=1046.0,
                                    hop_length=512)
    voiced_flag = np.asarray(voiced, dtype=bool)
    voiced_frac = float(np.mean(voiced_flag)) if voiced_flag.size else 0.0
    conf_arr = np.asarray(conf, dtype=float)
    with np.errstate(invalid="ignore"):
        mean_conf = float(np.nanmean(conf_arr)) if conf_arr.size else 0.0
    if not np.isfinite(mean_conf):
        mean_conf = 0.0
    if voiced_frac < 0.05 or mean_conf <= 0.3:
        return {"notes": None, "note_count": 0,
                "vocal_range_semitones": None, "midi_path": None,
                "method": method, "model_version": "librosa_pyin",
                "source_stem": stem_name, "confidence": 0.3,
                "missing_reason": "pyin_low_confidence"}

    # 连续有声段 → 粗音符（中位 f0 → MIDI 四舍五入）
    midi = librosa.hz_to_midi(np.where(voiced_flag, f0, np.nan))
    fps = sr / 512
    notes = []
    i = 0
    while i < len(midi):
        if np.isfinite(midi[i]):
            j = i
            while j + 1 < len(midi) and np.isfinite(midi[j + 1]):
                j += 1
            span = midi[i:j + 1]
            med = float(np.nanmedian(span))
            if j - i + 1 >= int(fps * 0.08):  # 至少 80ms 才成音
                notes.append({"pitch_midi": int(round(med)),
                              "start_sec": round(i / fps, 3),
                              "end_sec": round((j + 1) / fps, 3),
                              "amplitude": round(min(rms * 4.0, 1.0), 3)})
            i = j + 1
        else:
            i += 1
    if not notes:
        return {"notes": None, "note_count": 0,
                "vocal_range_semitones": None, "midi_path": None,
                "method": method, "model_version": "librosa_pyin",
                "source_stem": stem_name, "confidence": 0.3,
                "missing_reason": "pyin_no_stable_voiced_runs"}
    pitches = [n["pitch_midi"] for n in notes]
    return {"notes": notes, "note_count": len(notes),
            "vocal_range_semitones": max(pitches) - min(pitches),
            "midi_path": None, "method": method,
            "model_version": "librosa_pyin", "source_stem": stem_name,
            "elapsed_sec": None, "confidence": 0.3}


def _segments_extra(y: np.ndarray, sr: int, structure: dict | None,
                    chords: dict | None) -> list[dict]:
    """段落级补充特征：每段的频谱摘要、局部和弦。"""
    if not structure or not structure.get("segments"):
        return []
    out = []
    for seg in structure["segments"]:
        extra = spectral_mod.segment_band_summary(
            y, sr, seg["start_sec"], seg["end_sec"])
        if chords and chords.get("segments"):
            local = [c for c in chords["segments"]
                     if c["end_sec"] > seg["start_sec"]
                     and c["start_sec"] < seg["end_sec"]]
            extra["chords_local"] = [
                {"chord": c["chord"],
                 "start_sec": max(c["start_sec"], seg["start_sec"]),
                 "end_sec": min(c["end_sec"], seg["end_sec"])}
                for c in local[:8]]
        extra.setdefault("rms", seg.get("rms"))
        out.append(extra)
    return out


def get_analysis(content_hash_or_path: str, tier: str = "standard") -> dict | None:
    """按内容哈希（前缀或完整）取缓存 DNA；也接受本机音频路径。

    解析顺序：存在的文件路径 → 完整哈希（≥32 位）→ 哈希前缀。
    早前实现只按长度分流，长度超过 32 的路径会被当成哈希而静默返回 None。
    """
    params = dna_cache_params(tier)
    ref = str(content_hash_or_path)
    if ref.startswith("sha256:"):
        ref = ref[len("sha256:"):]
    try:
        path = Path(ref)
        if path.is_file():
            from rea_music.core.hash import file_sha256
            return cache_mod.load_cached(file_sha256(path), "dna", params)
    except OSError:
        pass                                  # 非法路径字符：按哈希继续尝试
    if len(ref) >= 32:
        return cache_mod.load_cached(ref, "dna", params)
    # 短哈希前缀匹配
    from rea_music.core.paths import cache_dir
    for d in (cache_dir() / "dna").glob(f"{ref}*"):
        data = cache_mod.load_cached(d.name, "dna", params)
        if data:
            return data
    return None
