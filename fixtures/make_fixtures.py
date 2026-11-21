"""合成测试曲生成器：已知 ground truth 的许可安全素材。

三首曲子全部由正弦波/噪声合成，无任何版权问题：
- pop_c_major_120.wav: 4/4, C major, 120 BPM, I-V-vi-IV, intro(8s)+verse(32s)+chorus(32s)+outro(8s)
- waltz_g_major_150.wav: 3/4 感觉, G major, 150 BPM 简化
- ballad_a_minor_96.wav: 4/4, A minor, 96 BPM, 低响度长音
每曲输出 *.truth.json，供算法回归测试。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
FIXTURES_DIR = Path(__file__).resolve().parent


def _tone(freq: float, dur: float, amp: float = 0.3, sr: int = SR) -> np.ndarray:
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    env = np.minimum(1, np.minimum(t * 50, (dur - t) * 50))  # 短包络防爆音
    return amp * env * np.sin(2 * np.pi * freq * t)


def _noise_burst(dur: float, amp: float = 0.5, sr: int = SR) -> np.ndarray:
    n = int(sr * dur)
    env = np.exp(-np.linspace(0, 8, n))
    return amp * env * np.random.default_rng(7).uniform(-1, 1, n)


def _place(buf: np.ndarray, sig: np.ndarray, at_sec: float, sr: int = SR) -> None:
    start = int(at_sec * sr)
    end = min(start + len(sig), len(buf))
    if start < len(buf):
        buf[start:end] += sig[: end - start]


NOTE_FREQS = {}
_A4 = 440.0
for i in range(-45, 51):
    NOTE_FREQS[i] = _A4 * 2 ** (i / 12.0)  # MIDI 69 为 A4


def midi_freq(midi: int) -> float:
    return NOTE_FREQS[midi - 69]


CHORDS_C_MAJOR = {
    "C:maj": [48, 52, 55, 60], "G:maj": [43, 50, 55, 59],
    "A:min": [45, 52, 57, 60], "F:maj": [41, 48, 53, 57],
}
CHORDS_A_MINOR = {
    "A:min": [45, 52, 57, 60], "F:maj": [41, 48, 53, 57],
    "C:maj": [48, 52, 55, 60], "G:maj": [43, 50, 55, 59],
}
CHORDS_G_MAJOR = {
    "G:maj": [43, 50, 55, 59], "D:maj": [38, 50, 54, 57],
    "E:min": [40, 52, 55, 59], "C:maj": [48, 52, 55, 60],
}


def _synth_song(bpm: float, chords: dict[str, list[int]], progression: list[str],
                structure: list[tuple[str, float]], bass_root_offset: int = -24,
                kick_pattern: tuple[int, ...] = (1, 3),
                snare_pattern: tuple[int, ...] = (2, 4)) -> tuple[np.ndarray, list[dict]]:
    """按结构段落合成：pad 和弦 + bass + kick/snare + 简单旋律。"""
    total = sum(d for _, d in structure)
    buf = np.zeros(int(SR * total) + SR, dtype=np.float64)
    beat = 60.0 / bpm
    t_cursor = 0.0
    sections_truth = []
    melody_base = 72  # C5

    for label, dur in structure:
        sections_truth.append({"label": label, "start_sec": round(t_cursor, 2),
                               "end_sec": round(t_cursor + dur, 2)})
        bars = int(dur / (beat * 4))
        is_loud = label in ("chorus", "final_chorus")
        pad_amp = 0.26 if is_loud else 0.15
        if label == "intro":
            pad_amp = 0.09  # intro 明显更安静，与主歌能量可分
        drums_on = label not in ("intro", "outro")
        for bar in range(bars):
            bar_t = t_cursor + bar * beat * 4
            if label == "intro":
                # intro 只用主和弦，和声上与主歌可分
                chord_name = progression[0]
            else:
                chord_name = progression[(bar + int(t_cursor / (beat * 4))) % len(progression)]
            chord = chords[chord_name]
            for note in chord:
                _place(buf, _tone(midi_freq(note), beat * 4 * 0.95,
                                  pad_amp / 2.0), bar_t)
            # bass: 每拍根音
            for b in range(4):
                _place(buf, _tone(midi_freq(chord[0] + bass_root_offset + 12),
                                  beat * 0.8, 0.18), bar_t + b * beat)
            # drums
            if drums_on:
                for b in kick_pattern:
                    _place(buf, _tone(55, 0.12, 0.38 if is_loud else 0.30),
                           bar_t + (b - 1) * beat)
                for b in snare_pattern:
                    _place(buf, _noise_burst(0.08, 0.24 if is_loud else 0.18),
                           bar_t + (b - 1) * beat)
                if is_loud:
                    for b in range(8):
                        _place(buf, _noise_burst(0.02, 0.05),
                               bar_t + b * beat / 2)
            # 旋律（副歌用，只取和弦音，保证严格在调内）
            if is_loud and bar % 2 == 0:
                mel = [chord[3] + 12, chord[2] + 12, chord[1] + 12,
                       chord[3] + 12]
                for i, m in enumerate(mel):
                    _place(buf, _tone(midi_freq(m), beat * 0.9, 0.20),
                           bar_t + i * beat)
        t_cursor += dur

    buf = buf[: int(SR * total)]
    peak = np.max(np.abs(buf)) or 1.0
    buf = buf / peak * 0.891  # ≈ -1 dBFS
    return buf, sections_truth


def _stereoize(mono: np.ndarray) -> np.ndarray:
    """加轻微立体声宽度（供 vocal mid/side 启发式测试）。"""
    delayed = np.roll(mono, int(SR * 0.0005))
    left = mono * 0.9 + delayed * 0.1
    right = mono * 0.9 - delayed * 0.1
    return np.stack([left, right], axis=1)


def build_all(out_dir: Path = FIXTURES_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []

    # 曲1: Pop C major 120
    mono, sections = _synth_song(
        bpm=120.0, chords=CHORDS_C_MAJOR,
        progression=["C:maj", "G:maj", "A:min", "F:maj"],
        structure=[("intro", 8.0), ("verse", 32.0), ("chorus", 32.0), ("outro", 8.0)])
    results.append(_save(out_dir / "pop_c_major_120.wav", mono, sections,
                         {"bpm": 120.0, "key": "C major", "meter": 4,
                          "chords": ["C:maj", "G:maj", "A:min", "F:maj"]}))

    # 曲2: 民谣感 A minor 96（弱鼓、低响度）
    mono2, sections2 = _synth_song(
        bpm=96.0, chords=CHORDS_A_MINOR,
        progression=["A:min", "F:maj", "C:maj", "G:maj"],
        structure=[("intro", 8.0), ("verse", 48.0), ("chorus", 24.0), ("outro", 8.0)])
    mono2 *= 0.5
    results.append(_save(out_dir / "ballad_a_minor_96.wav", mono2, sections2,
                         {"bpm": 96.0, "key": "A minor", "meter": 4,
                          "chords": ["A:min", "F:maj", "C:maj", "G:maj"]}))

    # 曲3: 快速 G major 150
    mono3, sections3 = _synth_song(
        bpm=150.0, chords=CHORDS_G_MAJOR,
        progression=["G:maj", "D:maj", "E:min", "C:maj"],
        structure=[("intro", 8.0), ("verse", 24.0), ("chorus", 32.0), ("outro", 8.0)])
    results.append(_save(out_dir / "rock_g_major_150.wav", mono3, sections3,
                         {"bpm": 150.0, "key": "G major", "meter": 4,
                          "chords": ["G:maj", "D:maj", "E:min", "C:maj"]}))
    return results


def _save(path: Path, mono: np.ndarray, sections: list[dict], truth: dict) -> Path:
    sf.write(str(path), _stereoize(mono.astype(np.float32)), SR, subtype="PCM_16")
    payload = {"file": path.name, "sections": sections, **truth,
               "duration_sec": round(len(mono) / SR, 1)}
    path.with_suffix(".truth.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    for p in build_all():
        print("fixture:", p)
