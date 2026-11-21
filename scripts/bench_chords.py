"""R2-C 双引擎和弦准确率对比基准。

chordotomy（主引擎候选）vs template_v2（既有引擎）在三首 ground truth
fixtures 上对比：真值和弦覆盖（根音+性质精确匹配）与真值内时长占比。
结果落盘 benchmarks/chords_compare.json，验收判据 chordotomy ≥ template。

用法：
    PYTHONPATH=. .venv/Scripts/python.exe scripts/bench_chords.py
    （或 .venv/Scripts/python.exe scripts/bench_chords.py，脚本自行补 sys.path）
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许无 PYTHONPATH 直接运行
    sys.path.insert(0, str(ROOT))

from rea_music.adapters import chordotomy  # noqa: E402
from rea_music.analysis import audio as audio_mod  # noqa: E402
from rea_music.analysis import chords as chords_mod  # noqa: E402
from rea_music.analysis import tempo as tempo_mod  # noqa: E402
from rea_music.core.hash import file_sha256  # noqa: E402

FIXTURES = ROOT / "fixtures"
OUT_PATH = ROOT / "benchmarks" / "chords_compare.json"
FIXTURE_FILES = ["pop_c_major_120.wav", "ballad_a_minor_96.wav",
                 "rock_g_major_150.wav"]


def _normalize_label(label: str) -> str | None:
    """根音+性质标准化为 (root, quality)；无法解析返回 None。"""
    label = (label or "").strip()
    if not label or label == "N":
        return None
    roots = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    sharp_map = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
    parts = label.split(":") if ":" in label else [label]
    root_raw = parts[0].strip()
    quality_raw = parts[1].strip().lower() if len(parts) > 1 else "maj"
    root = sharp_map.get(root_raw, root_raw)
    if root not in roots:
        return None
    if quality_raw in ("maj", "major", ""):
        quality = "maj"
    elif quality_raw in ("min", "m", "minor"):
        quality = "min"
    else:
        quality = quality_raw  # dim/aug/sus/7 系如实保留
    return f"{root}:{quality}"


def _fixture_metrics(engine_name: str, segments: list[dict],
                     truth_chords: list[str]) -> dict:
    truth_set = {c for c in (_normalize_label(c) for c in truth_chords)
                 if c}
    detected_set: set[str] = set()
    total_sec = 0.0
    in_truth_sec = 0.0
    for seg in segments:
        dur = max(seg.get("end_sec", 0.0) - seg.get("start_sec", 0.0), 0.0)
        total_sec += dur
        norm = _normalize_label(seg.get("chord", ""))
        if norm is None:
            continue
        detected_set.add(norm)
        if norm in truth_set:
            in_truth_sec += dur
    coverage = round(len(detected_set & truth_set) / len(truth_set), 3) \
        if truth_set else 0.0
    time_share = round(in_truth_sec / total_sec, 3) if total_sec else 0.0
    return {
        "engine": engine_name,
        "detected_chords": sorted(detected_set),
        "truth_coverage": coverage,
        "truth_chords_detected": len(detected_set & truth_set),
        "truth_chords_total": len(truth_set),
        "in_truth_time_share": time_share,
        "false_chords": sorted(detected_set - truth_set),
    }


def run() -> dict:
    chord_status = chordotomy.probe()
    template_only = not chord_status["available"]

    results: dict = {
        "ok": True,
        "benchmark": "chords_compare_r2c",
        "created_at": datetime.now(timezone.utc).astimezone()
                             .isoformat(timespec="seconds"),
        "chordotomy_version": chord_status["details"].get("version"),
        "fixtures": [],
        "engines": {
            "chordotomy": {"method_hint": "chordotomy CLI beat-aligned"},
            "template": {"method_hint": "hpss_chroma_beat_synced_two_stage"},
        },
    }
    per_engine_cov: dict[str, list[float]] = {"chordotomy": [],
                                              "template": []}
    per_engine_time: dict[str, list[float]] = {"chordotomy": [],
                                               "template": []}

    for name in FIXTURE_FILES:
        path = FIXTURES / name
        truth = json.loads(path.with_suffix(".truth.json")
                          .read_text(encoding="utf-8"))
        chash = file_sha256(path)
        wav = audio_mod.ensure_wav(path, chash)
        y, sr = audio_mod.load_mono(wav["mono_wav"])
        duration = len(y) / sr
        beats = tempo_mod.analyze_tempo(y, sr).get("beats_time", [])

        fixture_res = {"fixture": name, "duration_sec": round(duration, 1),
                       "truth_chords": truth["chords"], "engines": {}}

        # 模板引擎（既有 R1 路线，显式指定 template 防受 auto 配置影响）
        t0 = time.time()
        tmpl = chords_mod.analyze_chords(
            y, sr, beats, duration, external_engine_path=wav["mono_wav"],
            engine="template")
        tmpl_elapsed = round(time.time() - t0, 1)
        tmpl_metrics = _fixture_metrics(tmpl.get("engine", "template"),
                                       tmpl.get("segments", []),
                                       truth["chords"])
        tmpl_metrics["elapsed_sec"] = tmpl_elapsed
        tmpl_metrics["method"] = tmpl.get("method")
        fixture_res["engines"]["template"] = tmpl_metrics
        per_engine_cov["template"].append(tmpl_metrics["truth_coverage"])
        per_engine_time["template"].append(tmpl_metrics["in_truth_time_share"])

        # chordotomy 主引擎
        if template_only:
            fixture_res["engines"]["chordotomy"] = {
                "error": "chordotomy_unavailable",
                "reason": chord_status.get("reason")}
        else:
            t0 = time.time()
            try:
                ext = chordotomy.analyze(path)
                ext_elapsed = round(time.time() - t0, 1)
                ext_metrics = _fixture_metrics(ext.get("engine", "chordotomy"),
                                              ext.get("segments", []),
                                              truth["chords"])
                ext_metrics["elapsed_sec"] = ext_elapsed
                ext_metrics["method"] = ext.get("method")
                fixture_res["engines"]["chordotomy"] = ext_metrics
                per_engine_cov["chordotomy"].append(
                    ext_metrics["truth_coverage"])
                per_engine_time["chordotomy"].append(
                    ext_metrics["in_truth_time_share"])
            except Exception as exc:  # noqa: BLE001
                fixture_res["engines"]["chordotomy"] = {
                    "error": type(exc).__name__, "message": str(exc)}
        results["fixtures"].append(fixture_res)

    def _mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 3) if values else None

    results["summary"] = {
        "mean_truth_coverage": {
            k: _mean(v) for k, v in per_engine_cov.items()},
        "mean_in_truth_time_share": {
            k: _mean(v) for k, v in per_engine_time.items()},
    }
    cov_chord = results["summary"]["mean_truth_coverage"]["chordotomy"]
    cov_tmpl = results["summary"]["mean_truth_coverage"]["template"]
    if cov_chord is None:
        results["verdict"] = "chordotomy_unavailable_no_comparison"
        results["acceptance"] = "NOT_MET_engine_missing"
    else:
        met = cov_chord >= (cov_tmpl if cov_tmpl is not None else 0.0)
        results["verdict"] = ("chordotomy >= template" if met
                              else "chordotomy < template")
        results["acceptance"] = "MET" if met else "NOT_MET"
    return results


def main() -> int:
    report = run()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    summary = report.get("summary", {}).get("mean_truth_coverage", {})
    print(f"chords_compare -> {OUT_PATH}")
    print(f"  mean truth coverage: chordotomy={summary.get('chordotomy')} "
          f"template={summary.get('template')}")
    print(f"  verdict: {report.get('verdict')} "
          f"(acceptance={report.get('acceptance')})")
    return 0 if report.get("acceptance") == "MET" else 1


if __name__ == "__main__":
    sys.exit(main())
