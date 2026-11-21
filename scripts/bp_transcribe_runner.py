"""basic-pitch 转写 runner —— 在 engines/bp-venv（py3.10）内独立运行。

本脚本不得 import 任何 rea_music 模块（保持主 venv 零污染）。
契约 JSON：
    {"ok": true, "audio": "...", "model_version": "icassp_2022_nmp_onnx",
     "note_count": N, "elapsed_sec": t,
     "notes": [{"pitch_midi": int, "start_sec": float,
                "end_sec": float, "amplitude": float}, ...]}
MIDI 同步落盘 --midi-out。失败时写 {"ok": false, "error": "..."} 并退出码 1。

用法（由 src/rea_music/adapters/basicpitch.py 子进程调用）：
    python bp_transcribe_runner.py <audio> --json-out X.json --midi-out Y.mid
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="basic-pitch ICASSP 2022 旋律转写（隔离 venv 内运行）")
    parser.add_argument("audio", help="输入音频路径（wav/mp3/flac 等）")
    parser.add_argument("--json-out", required=True, help="契约 JSON 输出路径")
    parser.add_argument("--midi-out", required=True, help="MIDI 输出路径")
    parser.add_argument("--onset-threshold", type=float, default=0.5,
                        help="音符起始阈值（basic-pitch 默认 0.5）")
    parser.add_argument("--frame-threshold", type=float, default=0.3,
                        help="帧概率阈值（basic-pitch 默认 0.3）")
    parser.add_argument("--minimum-frequency", type=float, default=None,
                        help="最低音符频率 Hz（可选）")
    parser.add_argument("--maximum-frequency", type=float, default=None,
                        help="最高音符频率 Hz（可选）")
    args = parser.parse_args()

    audio = Path(args.audio)
    if not audio.exists():
        _fail(args.json_out, f"audio_not_found: {audio}", 2)
        return 1

    try:
        import warnings
        warnings.filterwarnings("ignore")
        from basic_pitch import ICASSP_2022_MODEL_PATH
        from basic_pitch.inference import predict

        t0 = time.time()
        _model_output, midi_data, note_events = predict(
            str(audio), ICASSP_2022_MODEL_PATH,
            onset_threshold=args.onset_threshold,
            frame_threshold=args.frame_threshold,
            minimum_frequency=args.minimum_frequency,
            maximum_frequency=args.maximum_frequency)
        elapsed = time.time() - t0

        midi_out = Path(args.midi_out)
        midi_out.parent.mkdir(parents=True, exist_ok=True)
        midi_data.write(str(midi_out))

        notes = []
        for event in note_events:
            # 契约：事件元组 (start_sec, end_sec, pitch_midi, amplitude, pitch_bend)
            start_sec, end_sec, pitch_midi = (float(event[0]), float(event[1]),
                                              int(event[2]))
            amplitude = float(event[3]) if len(event) > 3 else 0.0
            notes.append({"pitch_midi": pitch_midi,
                          "start_sec": round(start_sec, 4),
                          "end_sec": round(end_sec, 4),
                          "amplitude": round(amplitude, 4)})

        payload = {
            "ok": True,
            "audio": str(audio),
            "model_version": "icassp_2022_nmp_onnx",
            "note_count": len(notes),
            "elapsed_sec": round(elapsed, 1),
            "midi_path": str(midi_out),
            "notes": notes,
        }
        _write_json(args.json_out, payload)
        print(f"transcribed {len(notes)} notes -> {args.midi_out}")
        return 0
    except Exception as exc:  # noqa: BLE001 —— 任何失败都进结构化契约
        traceback.print_exc(file=sys.stderr)
        _fail(args.json_out, f"{type(exc).__name__}: {exc}", 1)
        return 1


def _write_json(path: str, payload: dict) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _fail(json_out: str, error: str, code: int) -> None:
    try:
        _write_json(json_out, {"ok": False, "error": error, "exit_code": code})
    except OSError:
        pass  # JSON 落盘也失败时仅靠 stderr + 退出码


if __name__ == "__main__":
    sys.exit(main())
